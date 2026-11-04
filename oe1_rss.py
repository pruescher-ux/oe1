#!/usr/bin/env python3
"""
Ö1 Custom RSS Feed Generator
=============================
Fragt die ORF Audio-API ab, filtert nach konfigurierten Sendungen,
extrahiert konkrete Themen/Schwerpunkte und erzeugt eine valide
RSS 2.0 XML-Datei mit Podcast-Enclosures.

Verwendung:
    python3 oe1_rss.py                    # nutzt config.yaml im selben Verzeichnis
    python3 oe1_rss.py -c /pfad/config.yaml
    python3 oe1_rss.py --days 14          # überschreibt days_back aus config
    python3 oe1_rss.py -o /pfad/feed.xml  # überschreibt output_file aus config

Benötigt nur die Python-Standardbibliothek (kein pip install nötig).
"""

import argparse
import json
import os
import re
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from html import unescape
from pathlib import Path

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

def parse_yaml_config(path):
    """Parse our simple YAML config without external dependencies."""
    config = {
        "feed": {
            "title": "Mein Ö1 Feed",
            "description": "Ausgewählte Ö1 Sendungen",
            "link": "https://oe1.orf.at",
            "language": "de-AT",
        },
        "days_back": 7,
        "output_file": "oe1_custom_feed.xml",
        "shows": [],
    }

    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    current_section = None

    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue

        if not line[0].isspace() and ":" in stripped:
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")

            if key == "feed":
                current_section = "feed"
                continue
            elif key == "shows":
                current_section = "shows"
                continue
            elif key == "days_back":
                config["days_back"] = int(val)
                current_section = None
                continue
            elif key == "output_file":
                config["output_file"] = val
                current_section = None
                continue

        if current_section == "feed" and line.startswith("  "):
            key, _, val = stripped.partition(":")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key in config["feed"]:
                config["feed"][key] = val

        if current_section == "shows" and "match:" in stripped:
            match = stripped.split("match:")[-1].strip().strip('"').strip("'")
            if match:
                config["shows"].append(match)

    seen = set()
    unique = []
    for s in config["shows"]:
        lower = s.lower()
        if lower not in seen:
            seen.add(lower)
            unique.append(s)
    config["shows"] = unique

    return config


# ---------------------------------------------------------------------------
# ORF Audio API
# ---------------------------------------------------------------------------

API_BASE = "https://audioapi.orf.at/oe1/api/json/5.0"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

REQUEST_DELAY = 0.25  # seconds between API requests


def api_get(url):
    """Fetch JSON from the ORF Audio API."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())
    except Exception as e:
        print("  ⚠ API-Fehler für {}: {}".format(url, e), file=sys.stderr)
        return None


def fetch_broadcasts_for_day(date_str):
    """Fetch all broadcasts for a given day (YYYYMMDD)."""
    url = "{}/broadcasts/{}".format(API_BASE, date_str)
    data = api_get(url)
    if data and "payload" in data:
        return data["payload"]
    return []


def fetch_broadcast_detail(broadcast_id):
    """Fetch full detail (including streams) for a single broadcast."""
    time.sleep(REQUEST_DELAY)
    url = "{}/broadcast/{}".format(API_BASE, broadcast_id)
    data = api_get(url)
    if data and "payload" in data:
        return data["payload"]
    return None


# ---------------------------------------------------------------------------
# Topic Extraction & Text Processing
# ---------------------------------------------------------------------------

def clean_html(text):
    """Remove HTML tags and decode entities."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", clean)).strip()


def parse_topics_and_meta(subtitle, description):
    """
    Extract concrete topics, subtopics, and contributors from subtitle / description.
    """
    raw_sub = subtitle or ""
    clean_sub = clean_html(raw_sub)
    
    topics = []
    meta_info = []

    if not clean_sub:
        return topics, meta_info

    # 1. Shows like Europa-Journal separate items with '+++'
    if "+++" in clean_sub:
        parts = [p.strip() for p in clean_sub.split("+++") if p.strip()]
        for p in parts:
            topics.append(p)

    # 2. Shows like Radiokolleg (omnibus) or Kulturjournal separate items with <br>
    elif re.search(r"<br\s*/?>", raw_sub, re.IGNORECASE):
        parts = [clean_html(p) for p in re.split(r"<br\s*/?>", raw_sub, flags=re.IGNORECASE)]
        for p in parts:
            if not p:
                continue
            # Check if this line is a moderator or contributor
            if re.match(r"^(Moderation|Gestaltung|Gäste|Mit):", p, re.IGNORECASE):
                meta_info.append(p)
            else:
                topics.append(p)

    # 3. Single subtitle string with possible 'Gäste:' or 'Mit:'
    else:
        guest_match = re.search(r"(Gäste:|Mit:|Moderation:|Gestaltung:)(.*)", clean_sub, re.IGNORECASE)
        if guest_match:
            main_topic = clean_sub[:guest_match.start()].strip().rstrip(".")
            if main_topic:
                topics.append(main_topic)
            meta_info.append(guest_match.group(0).strip())
        else:
            topics.append(clean_sub)

    return topics, meta_info


def build_episode_title_and_notes(program_title, episode_title, subtitle, detail_desc):
    """
    Builds a clear, informative episode title listing the actual topics,
    and structured shownotes (HTML & plain text).
    """
    topics, meta_info = parse_topics_and_meta(subtitle, detail_desc)
    
    clean_sub = clean_html(subtitle)
    clean_desc = clean_html(detail_desc)

    pt = (program_title or "").strip()
    et = (episode_title or "").strip()

    # Determine if episode_title is just the generic show name (e.g. "Radiokolleg")
    is_generic = (not et) or (pt and et.lower() == pt.lower())

    # --- Title construction ---
    if is_generic:
        if len(topics) > 1:
            # Multi-topic show: create a concise list of the subtopic headlines
            short_topics = []
            for t in topics:
                # Strip out 'Gestaltung: ...' or 'Mit: ...' from the title
                t_short = re.split(r"Gestaltung:|Mit:|Gäste:|Moderation:", t, flags=re.IGNORECASE)[0].strip()
                t_short = t_short.rstrip(".")
                if t_short:
                    short_topics.append(t_short)
            display_title = "{}: {}".format(pt or et, " | ".join(short_topics))
        elif topics:
            display_title = "{}: {}".format(pt or et, topics[0])
        else:
            display_title = pt or et
    else:
        # We have a specific episode title
        # Check if subtitle adds a specific concrete subtopic
        if topics and len(topics) == 1:
            subtopic = topics[0]
            # Strip trailing period and contributors
            subtopic_clean = re.split(r"Gäste:|Mit:|Gestaltung:|Moderation:", subtopic, flags=re.IGNORECASE)[0].strip().rstrip(".")
            # If subtopic isn't already contained in episode_title and is concise
            if (subtopic_clean
                    and subtopic_clean.lower() not in et.lower()
                    and len(subtopic_clean) > 3
                    and len(subtopic_clean) < 75):
                display_title = "{}: {} – {}".format(pt, et, subtopic_clean) if pt else "{} – {}".format(et, subtopic_clean)
            else:
                display_title = "{}: {}".format(pt, et) if pt else et
        else:
            display_title = "{}: {}".format(pt, et) if pt else et

    # --- Structured Shownotes construction ---
    # HTML version
    html_parts = []
    text_parts = []

    if len(topics) > 1:
        html_parts.append("<p><strong>📌 Themen dieser Sendung:</strong></p>")
        html_parts.append("<ul>")
        text_parts.append("📌 THEMEN DIESER SENDUNG:")
        for t in topics:
            html_parts.append("  <li>{}</li>".format(escape_xml(t)))
            text_parts.append("  • {}".format(t))
        html_parts.append("</ul>")
        text_parts.append("")
    elif len(topics) == 1 and topics[0]:
        html_parts.append("<p><strong>📌 Thema:</strong> {}</p>".format(escape_xml(topics[0])))
        text_parts.append("📌 THEMA: {}".format(topics[0]))
        text_parts.append("")

    if meta_info:
        for m in meta_info:
            html_parts.append("<p><strong>👥 </strong>{}</p>".format(escape_xml(m)))
            text_parts.append("👥 {}".format(m))
        text_parts.append("")

    if html_parts:
        html_parts.append("<hr/>")

    # Add the detailed description text
    if detail_desc:
        html_parts.append(detail_desc)
        text_parts.append(clean_desc)
    elif clean_sub and len(topics) <= 1:
        html_parts.append("<p>{}</p>".format(escape_xml(clean_sub)))
        text_parts.append(clean_sub)

    final_html = "\n".join(html_parts)
    final_text = "\n".join(text_parts).strip()

    return display_title, final_text, final_html


def matches_show(broadcast, show_patterns):
    """Check if a broadcast matches any of the configured show patterns."""
    program_title = (broadcast.get("programTitle") or "").lower()
    link_text = ""
    if broadcast.get("link"):
        link_text = (broadcast["link"].get("text") or "").lower()
    title = (broadcast.get("title") or "").lower()

    for pattern in show_patterns:
        p = pattern.lower()
        if p in program_title or p in link_text or p in title:
            return True
    return False


# ---------------------------------------------------------------------------
# Stream & Media
# ---------------------------------------------------------------------------

def get_audio_url(broadcast_detail):
    """Extract progressive MP3 URL from a broadcast detail."""
    streams = broadcast_detail.get("streams", [])
    if not streams:
        return None, 0

    first = streams[0]
    urls = first.get("urls", {})
    audio_url = urls.get("progressive", "")

    if audio_url:
        audio_url = re.sub(r"\{[^}]*\}", "", audio_url)
        audio_url = audio_url.rstrip("&")

    total_duration = sum(s.get("duration", 0) for s in streams)
    return audio_url if audio_url else None, total_duration


def get_image_url(broadcast):
    """Get the best image URL from a broadcast."""
    images = broadcast.get("images", [])
    if not images:
        return None

    img = images[0]
    versions = img.get("versions", [])
    if not versions:
        return None

    for v in versions:
        if "square" in v.get("path", ""):
            return v["path"]
    return versions[0].get("path")


# ---------------------------------------------------------------------------
# RSS Generation
# ---------------------------------------------------------------------------

def escape_xml(text):
    """Escape special XML characters."""
    if not text:
        return ""
    text = text.replace("&", "&amp;")
    text = text.replace("<", "&lt;")
    text = text.replace(">", "&gt;")
    text = text.replace('"', "&quot;")
    return text


def build_rss_string(items, feed_config):
    """Build RSS 2.0 XML as a string with podcast extensions."""
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append('<rss version="2.0"')
    lines.append('  xmlns:itunes="http://www.itunes.com/dtds/podcast-1.0.dtd"')
    lines.append('  xmlns:content="http://purl.org/rss/1.0/modules/content/"')
    lines.append('  xmlns:atom="http://www.w3.org/2005/Atom">')
    lines.append('  <channel>')
    lines.append('    <title>{}</title>'.format(escape_xml(feed_config["title"])))
    lines.append('    <description>{}</description>'.format(escape_xml(feed_config["description"])))
    lines.append('    <link>{}</link>'.format(escape_xml(feed_config["link"])))
    lines.append('    <language>{}</language>'.format(escape_xml(feed_config["language"])))
    lines.append('    <generator>oe1-custom-rss</generator>')
    lines.append('    <lastBuildDate>{}</lastBuildDate>'.format(
        format_datetime(datetime.now(timezone.utc))
    ))

    items.sort(key=lambda x: x.get("pub_date_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    for item in items:
        lines.append('    <item>')
        lines.append('      <title>{}</title>'.format(escape_xml(item["title"])))

        if item.get("description"):
            lines.append('      <description>{}</description>'.format(escape_xml(item["description"])))

        if item.get("content_html"):
            lines.append('      <content:encoded><![CDATA[{}]]></content:encoded>'.format(
                item["content_html"]
            ))

        if item.get("link"):
            lines.append('      <link>{}</link>'.format(escape_xml(item["link"])))

        lines.append('      <guid isPermaLink="false">{}</guid>'.format(escape_xml(item["guid"])))

        if item.get("pub_date"):
            lines.append('      <pubDate>{}</pubDate>'.format(item["pub_date"]))

        if item.get("audio_url"):
            duration_sec = item.get("duration_ms", 0) / 1000
            est_size = str(int(duration_sec * 16000))
            lines.append('      <enclosure url="{}" type="audio/mpeg" length="{}" />'.format(
                escape_xml(item["audio_url"]), est_size
            ))

        if item.get("duration_ms"):
            duration_sec = int(item["duration_ms"] / 1000)
            hours = duration_sec // 3600
            minutes = (duration_sec % 3600) // 60
            seconds = duration_sec % 60
            lines.append('      <itunes:duration>{:02d}:{:02d}:{:02d}</itunes:duration>'.format(
                hours, minutes, seconds
            ))

        if item.get("image_url"):
            lines.append('      <itunes:image href="{}" />'.format(escape_xml(item["image_url"])))

        if item.get("show_name"):
            lines.append('      <itunes:subtitle>{}</itunes:subtitle>'.format(
                escape_xml(item["show_name"])
            ))

        lines.append('    </item>')

    lines.append('  </channel>')
    lines.append('</rss>')

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ö1 Custom RSS Feed Generator")
    parser.add_argument("-c", "--config", default=None,
                        help="Pfad zur config.yaml (Standard: config.yaml im Script-Verzeichnis)")
    parser.add_argument("-d", "--days", type=int, default=None,
                        help="Anzahl Tage rückwirkend (überschreibt Config)")
    parser.add_argument("-o", "--output", default=None,
                        help="Ausgabe-Datei (überschreibt Config)")
    parser.add_argument("-v", "--verbose", action="store_true",
                        help="Ausführliche Ausgabe")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    config_path = Path(args.config) if args.config else script_dir / "config.yaml"

    if not config_path.exists():
        print("❌ Config nicht gefunden: {}".format(config_path), file=sys.stderr)
        sys.exit(1)

    config = parse_yaml_config(str(config_path))

    days_back = args.days if args.days is not None else config["days_back"]
    output_file = args.output if args.output else config["output_file"]
    show_patterns = config["shows"]

    if not show_patterns:
        print("❌ Keine Sendungen in der Config definiert!", file=sys.stderr)
        sys.exit(1)

    if not os.path.isabs(output_file):
        output_file = str(script_dir / output_file)

    print("🎙  Ö1 Custom RSS Feed Generator")
    print("   Sendungen: {} konfiguriert".format(len(show_patterns)))
    print("   Zeitraum:  {} Tage zurück".format(days_back))
    print("   Ausgabe:   {}".format(output_file))
    print()

    all_items = []
    today = datetime.now(timezone(timedelta(hours=1)))  # CET

    for i in range(days_back):
        day = today - timedelta(days=i)
        day_str = day.strftime("%Y%m%d")
        print("📅 Lade {} ...".format(day.strftime('%d.%m.%Y')), end=" ", flush=True)

        broadcasts = fetch_broadcasts_for_day(day_str)
        matched = 0

        for bc in broadcasts:
            if not matches_show(bc, show_patterns):
                continue

            program_title = bc.get("programTitle") or ""
            if not program_title and bc.get("link"):
                program_title = bc["link"].get("text", "")

            episode_title = bc.get("title") or ""
            subtitle = bc.get("subtitle") or ""
            raw_desc = bc.get("description") or ""

            # Parse dates
            pub_date_str = ""
            pub_date_dt = None
            start = bc.get("niceTime") or bc.get("scheduledStart") or bc.get("start")
            if start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    pub_date_str = format_datetime(dt)
                    pub_date_dt = dt
                except (ValueError, AttributeError):
                    pass

            # Link & image
            link = ""
            if bc.get("link") and bc["link"].get("url"):
                link = bc["link"]["url"]

            image_url = get_image_url(bc)

            # Audio & Detail
            audio_url = None
            duration_ms = bc.get("duration", 0)
            detail_desc = raw_desc

            if bc.get("isOnDemand") and bc.get("id"):
                detail = fetch_broadcast_detail(bc["id"])
                if detail:
                    audio_url, stream_duration = get_audio_url(detail)
                    if stream_duration:
                        duration_ms = stream_duration
                    if detail.get("description"):
                        detail_desc = detail["description"]
                    if not subtitle and detail.get("subtitle"):
                        subtitle = detail["subtitle"]

            # Only include items that actually have on-demand audio
            if not audio_url:
                continue

            # Build topic-enriched title and description
            display_title, plain_desc, rich_html = build_episode_title_and_notes(
                program_title, episode_title, subtitle, detail_desc
            )

            if args.verbose:
                print("\n     🔊 {}".format(display_title), flush=True)

            matched += 1

            item = {
                "title": display_title,
                "show_name": program_title,
                "description": plain_desc,
                "content_html": rich_html,
                "link": link,
                "guid": "oe1-{}".format(bc.get('id', '')),
                "pub_date": pub_date_str,
                "pub_date_dt": pub_date_dt,
                "audio_url": audio_url,
                "duration_ms": duration_ms,
                "image_url": image_url,
            }
            all_items.append(item)

        print("{} Treffer (von {} Sendungen)".format(matched, len(broadcasts)))

    print("\n✅ {} Beiträge mit Audio gefunden".format(len(all_items)))

    if not all_items:
        print("⚠  Keine passenden Sendungen gefunden. Prüfe deine Config!", file=sys.stderr)

    # Build RSS
    rss_xml = build_rss_string(all_items, config["feed"])

    with open(output_file, "w", encoding="utf-8") as f:
        f.write(rss_xml)

    print("📄 Feed geschrieben: {}".format(output_file))

    # Stats
    shows_found = set()
    for item in all_items:
        if item.get("show_name"):
            shows_found.add(item["show_name"])

    if shows_found:
        print("\n📻 Gefundene Sendungen:")
        for s in sorted(shows_found):
            count = sum(1 for it in all_items if it.get("show_name") == s)
            print("   • {} ({}x)".format(s, count))

    # Check for configured shows with no matches
    unmatched = []
    for pattern in show_patterns:
        found = False
        for item in all_items:
            p = pattern.lower()
            if p in (item.get("show_name") or "").lower():
                found = True
                break
        if not found:
            unmatched.append(pattern)

    if unmatched:
        print("\n⚠  Keine Treffer für: {}".format(', '.join(unmatched)))
        print("   (Möglicherweise in den letzten {} Tagen nicht gesendet)".format(days_back))


if __name__ == "__main__":
    main()
