#!/usr/bin/env python3
"""
Ö1 Themen-Monitor & RSS-Generator
==================================
Holt das Sendeprogramm der ORF Audio-API ab, filtert nach den in config.yaml
definierten Sendungen und generiert:
1. feed.xml   - Reiner Text/Artikel-RSS-Feed für jeden RSS-Reader (NetNewsWire, Feedly, Reeder etc.)
2. index.html - Übersichtliche, ansprechende Webseite mit Zeitraum-Wahl (3, 7, 14, 30 Tage),
                Volltextsuche und Sendungsfiltern.

Benötigt nur die Python 3 Standardbibliothek (kein pip install nötig).
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
from html import escape, unescape
from pathlib import Path

# ---------------------------------------------------------------------------
# Config Parser
# ---------------------------------------------------------------------------

def parse_yaml_config(path):
    """Simple YAML parser without external dependencies."""
    config = {
        "feed": {
            "title": "Ö1 Themen-Übersicht",
            "description": "Tägliche Themen ausgewählter Ö1-Sendungen",
            "link": "https://pruescher-ux.github.io/oe1/",
            "language": "de-AT",
        },
        "days_back": 30,
        "output_feed": "feed.xml",
        "output_html": "index.html",
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
            elif key in ("output_feed", "output_file"):
                config["output_feed"] = val
                current_section = None
                continue
            elif key == "output_html":
                config["output_html"] = val
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

REQUEST_DELAY = 0.15  # seconds between API requests


def api_get(url):
    """Fetch JSON from ORF Audio API."""
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
    """Fetch detailed metadata for a single broadcast."""
    time.sleep(REQUEST_DELAY)
    url = "{}/broadcast/{}".format(API_BASE, broadcast_id)
    data = api_get(url)
    if data and "payload" in data:
        return data["payload"]
    return None


# ---------------------------------------------------------------------------
# Content & Topic Extraction
# ---------------------------------------------------------------------------

def clean_html(text):
    """Strip HTML tags and unescape entities."""
    if not text:
        return ""
    clean = re.sub(r"<[^>]+>", " ", text)
    return unescape(re.sub(r"\s+", " ", clean)).strip()


def parse_topics_and_meta(subtitle, description):
    """Extract individual topics, subtopics, and contributors."""
    raw_sub = subtitle or ""
    clean_sub = clean_html(raw_sub)

    topics = []
    meta_info = []

    if not clean_sub:
        return topics, meta_info

    if "+++" in clean_sub:
        parts = [p.strip() for p in clean_sub.split("+++") if p.strip()]
        for p in parts:
            topics.append(p)
    elif re.search(r"<br\s*/?>", raw_sub, re.IGNORECASE):
        parts = [clean_html(p) for p in re.split(r"<br\s*/?>", raw_sub, flags=re.IGNORECASE)]
        for p in parts:
            if not p:
                continue
            if re.match(r"^(Moderation|Gestaltung|Gäste|Mit):", p, re.IGNORECASE):
                meta_info.append(p)
            else:
                topics.append(p)
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


def build_item_content(program_title, episode_title, subtitle, detail_desc):
    """Creates a clear title, structured topic list, and rich text."""
    topics, meta_info = parse_topics_and_meta(subtitle, detail_desc)
    clean_desc = clean_html(detail_desc)
    clean_sub = clean_html(subtitle)

    pt = (program_title or "").strip()
    et = (episode_title or "").strip()
    is_generic = (not et) or (pt and et.lower() == pt.lower())

    if is_generic:
        if len(topics) > 1:
            short_topics = []
            for t in topics:
                t_short = re.split(r"Gestaltung:|Mit:|Gäste:|Moderation:", t, flags=re.IGNORECASE)[0].strip().rstrip(".")
                if t_short:
                    short_topics.append(t_short)
            display_title = "{}: {}".format(pt or et, " | ".join(short_topics))
        elif topics:
            display_title = "{}: {}".format(pt or et, topics[0])
        else:
            display_title = pt or et
    else:
        if topics and len(topics) == 1:
            subtopic = re.split(r"Gäste:|Mit:|Gestaltung:|Moderation:", topics[0], flags=re.IGNORECASE)[0].strip().rstrip(".")
            if subtopic and subtopic.lower() not in et.lower() and len(subtopic) > 3 and len(subtopic) < 75:
                display_title = "{}: {} – {}".format(pt, et, subtopic) if pt else "{} – {}".format(et, subtopic)
            else:
                display_title = "{}: {}".format(pt, et) if pt else et
        else:
            display_title = "{}: {}".format(pt, et) if pt else et

    return display_title, topics, meta_info, clean_desc or clean_sub


def matches_show(broadcast, show_patterns):
    """Match broadcast against user's configured show list."""
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
# RSS 2.0 Feed Generator (News / Article Feed)
# ---------------------------------------------------------------------------

def generate_rss_xml(items, feed_config):
    """Builds clean, standard-compliant RSS 2.0 text/news feed."""
    lines = []
    lines.append('<?xml version="1.0" encoding="utf-8"?>')
    lines.append('<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" xmlns:atom="http://www.w3.org/2005/Atom">')
    lines.append('  <channel>')
    lines.append('    <title>{}</title>'.format(escape(feed_config["title"])))
    lines.append('    <description>{}</description>'.format(escape(feed_config["description"])))
    lines.append('    <link>{}</link>'.format(escape(feed_config["link"])))
    lines.append('    <language>{}</language>'.format(escape(feed_config["language"])))
    lines.append('    <lastBuildDate>{}</lastBuildDate>'.format(format_datetime(datetime.now(timezone.utc))))
    lines.append('    <generator>oe1-themen-monitor</generator>')

    for item in items:
        lines.append('    <item>')
        lines.append('      <title>{}</title>'.format(escape(item["title"])))
        if item.get("link"):
            lines.append('      <link>{}</link>'.format(escape(item["link"])))
        lines.append('      <guid isPermaLink="false">{}</guid>'.format(escape(item["guid"])))
        if item.get("pub_date"):
            lines.append('      <pubDate>{}</pubDate>'.format(item["pub_date"]))

        desc_html = []
        if item.get("topics"):
            if len(item["topics"]) > 1:
                desc_html.append("<p><strong>📌 Themen dieser Sendung:</strong></p><ul>")
                for t in item["topics"]:
                    desc_html.append("  <li>{}</li>".format(escape(t)))
                desc_html.append("</ul>")
            else:
                desc_html.append("<p><strong>📌 Thema:</strong> {}</p>".format(escape(item["topics"][0])))

        if item.get("meta_info"):
            for m in item["meta_info"]:
                desc_html.append("<p><strong>👥 </strong>{}</p>".format(escape(m)))

        if item.get("description"):
            desc_html.append("<p>{}</p>".format(escape(item["description"])))

        if item.get("link"):
            desc_html.append('<p><a href="{}">🔗 Zur Sendung auf oe1.ORF.at</a></p>'.format(escape(item["link"])))

        content_str = "\n".join(desc_html)
        lines.append('      <description><![CDATA[{}]]></description>'.format(content_str))
        lines.append('      <content:encoded><![CDATA[{}]]></content:encoded>'.format(content_str))
        lines.append('    </item>')

    lines.append('  </channel>')
    lines.append('</rss>')
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# HTML Web Magazine Generator (mit Zeitraum-Auswahl & Volltextsuche)
# ---------------------------------------------------------------------------

GERMAN_DAYS = ["Montag", "Dienstag", "Mittwoch", "Donnerstag", "Freitag", "Samstag", "Sonntag"]
GERMAN_MONTHS = ["", "Jänner", "Februar", "März", "April", "Mai", "Juni", "Juli", "August", "September", "Oktober", "November", "Dezember"]

def format_german_date(dt):
    """Format datetime as German string, e.g. 'Mittwoch, 04. November 2026'."""
    day_name = GERMAN_DAYS[dt.weekday()]
    month_name = GERMAN_MONTHS[dt.month]
    return "{}, {:02d}. {} {}".format(day_name, dt.day, month_name, dt.year)


def generate_web_html(items, feed_config, show_patterns):
    """Builds an interactive responsive HTML magazine with time period selector & live search."""
    days_dict = {}
    all_shows = set()

    for item in items:
        dt = item.get("pub_date_dt")
        date_key = dt.strftime("%Y-%m-%d") if dt else "Unbekannt"
        if date_key not in days_dict:
            days_dict[date_key] = {
                "date": dt,
                "days_ago": item.get("days_ago", 0),
                "items": []
            }
        days_dict[date_key]["items"].append(item)
        if item.get("show_name"):
            all_shows.add(item["show_name"])

    sorted_dates = sorted(days_dict.keys(), reverse=True)
    sorted_shows = sorted(list(all_shows))

    html = []
    html.append('<!DOCTYPE html>')
    html.append('<html lang="de">')
    html.append('<head>')
    html.append('  <meta charset="utf-8">')
    html.append('  <meta name="viewport" content="width=device-width, initial-scale=1.0">')
    html.append('  <title>Ö1 Themen-Übersicht</title>')
    html.append('  <link rel="alternate" type="application/rss+xml" title="Ö1 Themen RSS" href="feed.xml">')
    html.append('  <style>')
    html.append("""
      :root {
        --primary: #c62828;
        --primary-light: #ffcdd2;
        --primary-hover: #b71c1c;
        --bg: #f7f8fa;
        --card-bg: #ffffff;
        --text: #1f2937;
        --text-muted: #6b7280;
        --border: #e5e7eb;
      }
      * { box-sizing: border-box; }
      body {
        font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
        background-color: var(--bg);
        color: var(--text);
        margin: 0;
        padding: 0;
        line-height: 1.55;
      }
      header {
        background: #ffffff;
        border-bottom: 2px solid var(--primary);
        padding: 20px 20px 14px;
        position: sticky;
        top: 0;
        z-index: 100;
        box-shadow: 0 2px 10px rgba(0,0,0,0.05);
      }
      .header-content {
        max-width: 950px;
        margin: 0 auto;
        display: flex;
        justify-content: space-between;
        align-items: center;
        flex-wrap: wrap;
        gap: 12px;
      }
      h1 { margin: 0; font-size: 1.5rem; color: var(--primary); font-weight: 700; }
      .header-sub { font-size: 0.85rem; color: var(--text-muted); margin-top: 2px; }
      .header-actions {
        display: flex;
        align-items: center;
        gap: 10px;
      }
      .search-box {
        padding: 7px 14px;
        border: 1px solid var(--border);
        border-radius: 20px;
        font-size: 0.88rem;
        width: 220px;
        outline: none;
        transition: border 0.15s ease;
      }
      .search-box:focus { border-color: var(--primary); box-shadow: 0 0 0 2px rgba(198,40,40,0.15); }
      .rss-badge {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        background: #f57c00;
        color: #ffffff;
        padding: 7px 14px;
        border-radius: 20px;
        font-size: 0.82rem;
        font-weight: 600;
        text-decoration: none;
      }
      .rss-badge:hover { background: #e65100; }
      .controls-bar {
        max-width: 950px;
        margin: 14px auto 0;
        display: flex;
        flex-direction: column;
        gap: 10px;
      }
      .filter-group {
        display: flex;
        align-items: center;
        flex-wrap: wrap;
        gap: 6px;
      }
      .group-label {
        font-size: 0.78rem;
        font-weight: 700;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--text-muted);
        margin-right: 4px;
        min-width: 65px;
      }
      .pill {
        background: #f3f4f6;
        border: 1px solid var(--border);
        border-radius: 16px;
        padding: 4px 11px;
        font-size: 0.82rem;
        cursor: pointer;
        color: #4b5563;
        user-select: none;
        transition: all 0.15s ease;
      }
      .pill:hover { background: #e5e7eb; }
      .pill.active {
        background: var(--primary);
        color: #ffffff;
        border-color: var(--primary);
        font-weight: 600;
      }
      .time-pill.active {
        background: #2563eb;
        border-color: #2563eb;
      }
      .stats-bar {
        max-width: 950px;
        margin: 14px auto 0;
        font-size: 0.82rem;
        color: var(--text-muted);
        display: flex;
        justify-content: space-between;
      }
      main {
        max-width: 950px;
        margin: 24px auto;
        padding: 0 16px;
      }
      .day-section {
        margin-bottom: 36px;
      }
      .day-title {
        font-size: 1.25rem;
        font-weight: 700;
        margin-bottom: 14px;
        padding-bottom: 6px;
        border-bottom: 1px solid #d1d5db;
        color: #111827;
        display: flex;
        align-items: center;
        gap: 8px;
      }
      .broadcast-card {
        background: var(--card-bg);
        border: 1px solid var(--border);
        border-radius: 8px;
        padding: 16px 20px;
        margin-bottom: 14px;
        box-shadow: 0 1px 3px rgba(0,0,0,0.03);
        transition: box-shadow 0.2s ease;
      }
      .broadcast-card:hover {
        box-shadow: 0 4px 12px rgba(0,0,0,0.06);
      }
      .card-meta {
        display: flex;
        align-items: center;
        gap: 10px;
        margin-bottom: 6px;
        flex-wrap: wrap;
      }
      .show-badge {
        background: var(--primary-light);
        color: #991b1b;
        font-size: 0.78rem;
        font-weight: 700;
        padding: 2px 8px;
        border-radius: 4px;
        text-transform: uppercase;
        letter-spacing: 0.5px;
      }
      .broadcast-time {
        font-size: 0.82rem;
        color: var(--text-muted);
      }
      .broadcast-title {
        font-size: 1.15rem;
        font-weight: 700;
        margin: 4px 0 10px;
        color: #111827;
        line-height: 1.35;
      }
      .topics-list {
        background: #f9fafb;
        border-left: 3px solid var(--primary);
        padding: 10px 14px 10px 24px;
        margin: 12px 0;
        border-radius: 0 6px 6px 0;
      }
      .topics-list strong {
        display: block;
        font-size: 0.82rem;
        text-transform: uppercase;
        letter-spacing: 0.5px;
        color: var(--primary);
        margin-bottom: 6px;
        margin-left: -10px;
      }
      .topics-list ul {
        margin: 0;
        padding-left: 0;
        list-style: none;
      }
      .topics-list li {
        font-size: 0.94rem;
        margin-bottom: 6px;
        position: relative;
        padding-left: 14px;
        color: #1f2937;
      }
      .topics-list li::before {
        content: "•";
        color: var(--primary);
        font-weight: bold;
        position: absolute;
        left: 0;
      }
      .contributors {
        font-size: 0.88rem;
        color: #374151;
        margin: 8px 0;
        padding: 6px 12px;
        background: #f3f4f6;
        border-radius: 4px;
      }
      .description-text {
        font-size: 0.92rem;
        color: #4b5563;
        margin-top: 8px;
        line-height: 1.5;
      }
      .card-footer {
        margin-top: 10px;
        padding-top: 8px;
        border-top: 1px solid #f3f4f6;
        display: flex;
        justify-content: flex-end;
      }
      .oe1-link {
        font-size: 0.84rem;
        color: var(--primary);
        text-decoration: none;
        font-weight: 600;
      }
      .oe1-link:hover { text-decoration: underline; }
      .no-results {
        text-align: center;
        padding: 60px 20px;
        color: var(--text-muted);
        font-size: 1.1rem;
        display: none;
      }
      footer {
        text-align: center;
        padding: 40px 20px;
        color: var(--text-muted);
        font-size: 0.85rem;
        border-top: 1px solid var(--border);
        background: #ffffff;
      }
      footer a { color: var(--primary); text-decoration: none; }
    """)
    html.append('  </style>')
    html.append('</head>')
    html.append('<body>')

    # Header
    html.append('  <header>')
    html.append('    <div class="header-content">')
    html.append('      <div>')
    html.append('        <h1>🎙 Ö1 Themen-Monitor</h1>')
    html.append('        <div class="header-sub">Themen & Schwerpunkte ausgewählter Ö1-Sendungen</div>')
    html.append('      </div>')
    html.append('      <div class="header-actions">')
    html.append('        <input type="text" id="searchInput" class="search-box" placeholder="🔍 Thema / Begriff suchen..." oninput="applyAllFilters()">')
    html.append('        <a href="feed.xml" class="rss-badge" target="_blank" title="In RSS-Reader abonnieren">')
    html.append('          <svg width="14" height="14" viewBox="0 0 24 24" fill="currentColor"><circle cx="6.18" cy="17.82" r="2.18"/><path d="M4 4.44v2.83c7.03 0 12.73 5.7 12.73 12.73h2.83c0-8.59-6.97-15.56-15.56-15.56zm0 5.66v2.83c3.9 0 7.07 3.17 7.07 7.07h2.83c0-5.46-4.44-9.9-9.9-9.9z"/></svg>')
    html.append('          RSS-Feed')
    html.append('        </a>')
    html.append('      </div>')
    html.append('    </div>')

    # Controls Bar (Timeframe & Shows)
    html.append('    <div class="controls-bar">')

    # 1. Timeframe selection
    html.append('      <div class="filter-group">')
    html.append('        <span class="group-label">Zeitraum:</span>')
    html.append('        <button class="pill time-pill" data-days="3" onclick="setTimeframe(3, this)">Letzte 3 Tage</button>')
    html.append('        <button class="pill time-pill" data-days="7" onclick="setTimeframe(7, this)">Letzte 7 Tage</button>')
    html.append('        <button class="pill time-pill active" data-days="14" onclick="setTimeframe(14, this)">Letzte 14 Tage (2 Wochen)</button>')
    html.append('        <button class="pill time-pill" data-days="30" onclick="setTimeframe(30, this)">Voller Monat (30 Tage)</button>')
    html.append('      </div>')

    # 2. Show selection
    html.append('      <div class="filter-group">')
    html.append('        <span class="group-label">Sendung:</span>')
    html.append('        <button class="pill show-pill active" data-show="all" onclick="setShowFilter(\'all\', this)">Alle Sendungen</button>')
    for s in sorted_shows:
        count = sum(1 for it in items if it.get("show_name") == s)
        html.append('        <button class="pill show-pill" data-show="{}" onclick="setShowFilter(\'{}\', this)">{}</button>'.format(
            escape(s), escape(s), escape(s)
        ))
    html.append('      </div>')

    html.append('    </div>')

    # Stats
    html.append('    <div class="stats-bar">')
    html.append('      <span id="counterText">Lade Beiträge...</span>')
    html.append('    </div>')
    html.append('  </header>')

    # Main content
    html.append('  <main>')
    html.append('    <div id="noResults" class="no-results">Keine passenden Sendungen für diese Auswahl gefunden.</div>')

    for date_key in sorted_dates:
        day_data = days_dict[date_key]
        dt = day_data["date"]
        days_ago = day_data["days_ago"]
        date_title = format_german_date(dt) if dt else date_key

        html.append('    <section class="day-section" data-date="{}" data-days-ago="{}">'.format(date_key, days_ago))
        html.append('      <h2 class="day-title">📅 {}</h2>'.format(escape(date_title)))

        for item in day_data["items"]:
            show_name = item.get("show_name", "")
            search_corpus = " ".join([
                item.get("title", ""),
                " ".join(item.get("topics", [])),
                " ".join(item.get("meta_info", [])),
                item.get("description", "")
            ]).lower()

            html.append('      <article class="broadcast-card" data-show="{}" data-days-ago="{}" data-search="{}">'.format(
                escape(show_name), days_ago, escape(search_corpus)
            ))
            html.append('        <div class="card-meta">')
            html.append('          <span class="show-badge">{}</span>'.format(escape(show_name)))
            if item.get("time_str"):
                html.append('          <span class="broadcast-time">🕒 {}</span>'.format(escape(item["time_str"])))
            html.append('        </div>')

            html.append('        <h3 class="broadcast-title">{}</h3>'.format(escape(item["title"])))

            if item.get("topics"):
                html.append('        <div class="topics-list">')
                if len(item["topics"]) > 1:
                    html.append('          <strong>Themen dieser Sendung:</strong>')
                    html.append('          <ul>')
                    for t in item["topics"]:
                        html.append('            <li>{}</li>'.format(escape(t)))
                    html.append('          </ul>')
                else:
                    html.append('          <strong>Thema:</strong>')
                    html.append('          <div>{}</div>'.format(escape(item["topics"][0])))
                html.append('        </div>')

            if item.get("meta_info"):
                for m in item["meta_info"]:
                    html.append('        <div class="contributors">👥 {}</div>'.format(escape(m)))

            if item.get("description"):
                html.append('        <div class="description-text">{}</div>'.format(escape(item["description"])))

            if item.get("link"):
                html.append('        <div class="card-footer">')
                html.append('          <a href="{}" target="_blank" rel="noopener" class="oe1-link">Zur Sendung auf oe1.ORF.at ↗</a>'.format(escape(item["link"])))
                html.append('        </div>')

            html.append('      </article>')

        html.append('    </section>')

    html.append('  </main>')

    # Footer
    html.append('  <footer>')
    html.append('    <p>Aktualisiert: {} Uhr (UTC)</p>'.format(datetime.now(timezone.utc).strftime("%d.%m.%Y um %H:%M")))
    html.append('    <p>Datenquelle: <a href="https://oe1.orf.at" target="_blank">Österreich 1 / ORF Audio-API</a></p>')
    html.append('  </footer>')

    # Interactive Client-Side JavaScript
    html.append("""
  <script>
    let currentTimeframe = 14; // Default: 14 days
    let currentShow = 'all';

    // Restore saved timeframe if available
    try {
      const savedTf = localStorage.getItem('oe1_timeframe');
      if (savedTf) {
        currentTimeframe = parseInt(savedTf, 10);
        document.querySelectorAll('.time-pill').forEach(btn => {
          if (parseInt(btn.getAttribute('data-days'), 10) === currentTimeframe) {
            document.querySelectorAll('.time-pill').forEach(p => p.classList.remove('active'));
            btn.classList.add('active');
          }
        });
      }
    } catch(e) {}

    function setTimeframe(days, button) {
      currentTimeframe = days;
      try { localStorage.setItem('oe1_timeframe', days); } catch(e) {}
      document.querySelectorAll('.time-pill').forEach(p => p.classList.remove('active'));
      button.classList.add('active');
      applyAllFilters();
    }

    function setShowFilter(showName, button) {
      currentShow = showName;
      document.querySelectorAll('.show-pill').forEach(p => p.classList.remove('active'));
      button.classList.add('active');
      applyAllFilters();
    }

    function applyAllFilters() {
      const searchQuery = (document.getElementById('searchInput').value || '').trim().toLowerCase();
      const cards = document.querySelectorAll('.broadcast-card');
      const sections = document.querySelectorAll('.day-section');
      let visibleCount = 0;

      cards.forEach(card => {
        const daysAgo = parseInt(card.getAttribute('data-days-ago'), 10);
        const cardShow = card.getAttribute('data-show');
        const cardSearch = card.getAttribute('data-search') || '';

        const matchTime = daysAgo < currentTimeframe;
        const matchShow = (currentShow === 'all' || cardShow === currentShow);
        const matchSearch = (!searchQuery || cardSearch.includes(searchQuery));

        if (matchTime && matchShow && matchSearch) {
          card.style.display = 'block';
          visibleCount++;
        } else {
          card.style.display = 'none';
        }
      });

      // Show/hide date sections
      sections.forEach(sec => {
        const daysAgo = parseInt(sec.getAttribute('data-days-ago'), 10);
        if (daysAgo >= currentTimeframe) {
          sec.style.display = 'none';
          return;
        }
        let hasVisible = false;
        sec.querySelectorAll('.broadcast-card').forEach(c => {
          if (c.style.display !== 'none') hasVisible = true;
        });
        sec.style.display = hasVisible ? 'block' : 'none';
      });

      // Update counter
      const counter = document.getElementById('counterText');
      if (counter) {
        counter.innerText = `${visibleCount} Sendungsbeiträge (Zeitraum: ${currentTimeframe} Tage)`;
      }

      // No results message
      const noRes = document.getElementById('noResults');
      if (noRes) {
        noRes.style.display = (visibleCount === 0) ? 'block' : 'none';
      }
    }

    // Run filters on load
    applyAllFilters();
  </script>
    """)
    html.append('</body>')
    html.append('</html>')

    return "\n".join(html)


# ---------------------------------------------------------------------------
# Main Execution
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Ö1 Themen-Monitor & RSS-Generator")
    parser.add_argument("-c", "--config", default=None, help="Pfad zur config.yaml")
    parser.add_argument("-d", "--days", type=int, default=None, help="Anzahl Tage rückwirkend")
    parser.add_argument("-o", "--output-feed", default=None, help="Pfad für feed.xml")
    parser.add_argument("--output-html", default=None, help="Pfad für index.html")
    args = parser.parse_args()

    script_dir = Path(__file__).parent.resolve()
    config_path = Path(args.config) if args.config else script_dir / "config.yaml"

    if not config_path.exists():
        print("❌ Config nicht gefunden: {}".format(config_path), file=sys.stderr)
        sys.exit(1)

    config = parse_yaml_config(str(config_path))

    days_back = args.days if args.days is not None else config["days_back"]
    output_feed = args.output_feed if args.output_feed else config["output_feed"]
    output_html = args.output_html if args.output_html else config["output_html"]
    show_patterns = config["shows"]

    if not show_patterns:
        print("❌ Keine Sendungen in der Config definiert!", file=sys.stderr)
        sys.exit(1)

    if not os.path.isabs(output_feed):
        output_feed = str(script_dir / output_feed)
    if not os.path.isabs(output_html):
        output_html = str(script_dir / output_html)

    print("📰  Ö1 Themen-Monitor & RSS Generator")
    print("    Überwachte Sendungen: {}".format(len(show_patterns)))
    print("    Zeitraum:             {} Tage rückwirkend".format(days_back))
    print("    Ausgabe RSS:          {}".format(output_feed))
    print("    Ausgabe Webseite:     {}".format(output_html))
    print()

    all_items = []
    today = datetime.now(timezone(timedelta(hours=1)))  # CET

    for days_ago in range(days_back):
        day = today - timedelta(days=days_ago)
        day_str = day.strftime("%Y%m%d")
        print("📅 Lade {} (-{}d) ...".format(day.strftime('%d.%m.%Y'), days_ago), end=" ", flush=True)

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

            pub_date_str = ""
            pub_date_dt = None
            time_str = ""

            start = bc.get("niceTime") or bc.get("scheduledStart") or bc.get("start")
            end = bc.get("scheduledEnd") or bc.get("end")

            if start:
                try:
                    dt = datetime.fromisoformat(start.replace("Z", "+00:00"))
                    pub_date_str = format_datetime(dt)
                    pub_date_dt = dt
                    local_start = dt.astimezone(timezone(timedelta(hours=1)))
                    time_str = local_start.strftime("%H:%M Uhr")
                    if end:
                        dt_end = datetime.fromisoformat(end.replace("Z", "+00:00")).astimezone(timezone(timedelta(hours=1)))
                        time_str = "{} – {} Uhr".format(local_start.strftime("%H:%M"), dt_end.strftime("%H:%M"))
                except (ValueError, AttributeError):
                    pass

            link = ""
            if bc.get("link") and bc["link"].get("url"):
                link = bc["link"]["url"]

            detail_desc = raw_desc
            if bc.get("id"):
                detail = fetch_broadcast_detail(bc["id"])
                if detail:
                    if detail.get("description"):
                        detail_desc = detail["description"]
                    if not subtitle and detail.get("subtitle"):
                        subtitle = detail["subtitle"]

            display_title, topics, meta_info, clean_desc = build_item_content(
                program_title, episode_title, subtitle, detail_desc
            )

            matched += 1

            item = {
                "title": display_title,
                "show_name": program_title or episode_title,
                "topics": topics,
                "meta_info": meta_info,
                "description": clean_desc,
                "link": link,
                "guid": "oe1-{}".format(bc.get("id", "")),
                "pub_date": pub_date_str,
                "pub_date_dt": pub_date_dt,
                "time_str": time_str,
                "days_ago": days_ago,
            }
            all_items.append(item)

        print("{} Treffer (von {} Sendungen)".format(matched, len(broadcasts)))

    all_items.sort(key=lambda x: x.get("pub_date_dt") or datetime.min.replace(tzinfo=timezone.utc), reverse=True)

    print("\n✅ Gesamt: {} Sendungsbeiträge erfasst".format(len(all_items)))

    # 1. Write RSS Feed
    rss_xml = generate_rss_xml(all_items, config["feed"])
    with open(output_feed, "w", encoding="utf-8") as f:
        f.write(rss_xml)
    print("📄 RSS-Feed geschrieben: {}".format(output_feed))

    # 2. Write Web HTML
    web_html = generate_web_html(all_items, config["feed"], show_patterns)
    with open(output_html, "w", encoding="utf-8") as f:
        f.write(web_html)
    print("🌐 Webseite geschrieben:  {}".format(output_html))


if __name__ == "__main__":
    main()
