#!/usr/bin/env python3
"""Render data/articles.json into index.html + archive/YYYY-MM-DD.html."""

from __future__ import annotations

import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
ARCHIVE = ROOT / "archive"

DAYS_NL = ["maandag", "dinsdag", "woensdag", "donderdag", "vrijdag", "zaterdag", "zondag"]
MONTHS_NL = ["januari", "februari", "maart", "april", "mei", "juni", "juli",
             "augustus", "september", "oktober", "november", "december"]


def dutch_date(d: datetime) -> str:
    return f"{DAYS_NL[d.weekday()].capitalize()} {d.day} {MONTHS_NL[d.month - 1]} {d.year}"


def main() -> int:
    payload = json.loads((DATA / "articles.json").read_text(encoding="utf-8"))
    articles = payload["articles"]
    generated = datetime.fromisoformat(payload["generated"])
    today = generated.date()

    template = (ROOT / "templates" / "index.template.html").read_text(encoding="utf-8")

    sources = " &nbsp;·&nbsp; ".join(h["label"].lower() + ".nl" if not h["label"].startswith("Property")
                                    else "propertynl.com" for h in payload["health"])
    dead = [h["label"] for h in payload["health"] if h["items"] == 0]
    warning = f" &nbsp;·&nbsp; ⚠️ geen items van: {', '.join(dead)}" if dead else ""
    meta = (f"{dutch_date(generated)} &nbsp;·&nbsp; {len(articles)} artikelen &nbsp;·&nbsp; "
            f"{sum(1 for a in articles if a['isNew'])} nieuw &nbsp;·&nbsp; "
            f"laatste {payload['windowDays']} dagen &nbsp;·&nbsp; {sources}{warning}")

    html = (template
            .replace("__ARTICLES_JSON__", json.dumps(articles, ensure_ascii=False, indent=2))
            .replace("__DATE_LONG__", f"{today.day} {MONTHS_NL[today.month - 1]} {today.year}")
            .replace("__HEADER_META__", meta))

    (ROOT / "index.html").write_text(html, encoding="utf-8")

    ARCHIVE.mkdir(exist_ok=True)
    dated = ARCHIVE / f"{today.isoformat()}.html"
    dated.write_text(html, encoding="utf-8")

    # Rebuild the archive index from what is actually on disk, so it can never
    # drift out of sync with the files (and re-runs on the same day are safe).
    entries = sorted((p for p in ARCHIVE.glob("*.html") if p.name != "index.html"),
                     key=lambda p: p.stem, reverse=True)
    rows = "\n".join(
        f'    <li><a href="{p.name}">{dutch_date(datetime.fromisoformat(p.stem))}</a></li>'
        for p in entries)
    (ARCHIVE / "index.html").write_text(
        "<!DOCTYPE html>\n<html lang=\"nl\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>Archief — Vastgoed Nieuws Digest</title>\n<style>\n"
        "body{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f6f9;color:#1f2937;"
        "max-width:720px;margin:0 auto;padding:40px 24px}\n"
        "h1{font-size:22px;color:#1a2e44;margin-bottom:4px}\n"
        "p.sub{color:#6b7280;font-size:13px;margin-bottom:24px}\n"
        "ul{list-style:none;padding:0}\n"
        "li{background:#fff;border:1px solid #dde3ec;border-radius:8px;margin-bottom:8px}\n"
        "li a{display:block;padding:12px 16px;color:#1a2e44;text-decoration:none;font-weight:600}\n"
        "li a:hover{color:#e07b39}\n</style>\n</head>\n<body>\n"
        "<h1>🏢 Archief — Vastgoed Nieuws Digest</h1>\n"
        f'<p class="sub"><a href="../index.html">← naar de actuele digest</a> &nbsp;·&nbsp; '
        f"{len(entries)} edities</p>\n<ul>\n{rows}\n</ul>\n</body>\n</html>\n",
        encoding="utf-8")

    print(f"wrote index.html ({len(articles)} articles), {dated.relative_to(ROOT)}, "
          f"archive/index.html ({len(entries)} editions)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
