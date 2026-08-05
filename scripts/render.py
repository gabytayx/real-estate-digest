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


def dutch_date(d) -> str:
    if isinstance(d, Path):
        d = datetime.fromisoformat(d.stem)
    return f"{DAYS_NL[d.weekday()].capitalize()} {d.day} {MONTHS_NL[d.month - 1]} {d.year}"


def short_date(d: datetime) -> str:
    return f"{d.day} {MONTHS_NL[d.month - 1]} {d.year}"


def editions() -> list:
    """All archived editions on disk, newest first."""
    out = []
    for p in ARCHIVE.glob("*.html"):
        if p.name == "index.html":
            continue
        try:
            datetime.fromisoformat(p.stem)
        except ValueError:
            continue  # ignore stray files that aren't dated editions
        out.append(p)
    return sorted(out, key=lambda p: p.stem, reverse=True)


def banner(today: datetime, prev, prefix: str) -> str:
    """
    Build the archive strip. `prefix` differs between the two copies we write:
    '' inside archive/, 'archive/' for the root index.html.
    """
    parts = ['<a href="%sindex.html">Alle edities</a>' % prefix]
    parts.append('<span>&middot;</span><a href="%s%s.html">Deze editie (%s)</a>'
                 % (prefix, today.date().isoformat(), short_date(today)))
    if prev:
        parts.append('<span>&middot;</span><a href="%s%s">Vorige editie (%s)</a>'
                     % (prefix, prev.name, short_date(datetime.fromisoformat(prev.stem))))
    return "\n  ".join(parts)


def main() -> int:
    payload = json.loads((DATA / "articles.json").read_text(encoding="utf-8"))
    articles = payload["articles"]
    generated = datetime.fromisoformat(payload["generated"])
    today = generated.date()

    template = (ROOT / "templates" / "index.template.html").read_text(encoding="utf-8")

    # Previous edition = newest archived file that isn't today's. Computed
    # before we write today's copy, so a same-day re-run stays correct.
    ARCHIVE.mkdir(exist_ok=True)
    prev = next((p for p in editions() if p.stem != today.isoformat()), None)

    labels = {"propertynl": "propertynl.com"}
    sources = " &nbsp;&middot;&nbsp; ".join(
        labels.get(h["source"], "%s.nl" % h["source"]) for h in payload["health"])
    dead = [h["label"] for h in payload["health"] if h["items"] == 0]
    warning = " &nbsp;&middot;&nbsp; \u26a0\ufe0f geen items van: %s" % ", ".join(dead) if dead else ""
    meta = ("%s &nbsp;&middot;&nbsp; %d artikelen &nbsp;&middot;&nbsp; %d nieuw "
            "&nbsp;&middot;&nbsp; laatste %s dagen &nbsp;&middot;&nbsp; %s%s"
            % (dutch_date(generated), len(articles),
               sum(1 for a in articles if a["isNew"]),
               payload["windowDays"], sources, warning))

    def build(prefix: str) -> str:
        return (template
                .replace("__ARTICLES_JSON__", json.dumps(articles, ensure_ascii=False, indent=2))
                .replace("__DATE_LONG__", short_date(generated))
                .replace("__HEADER_META__", meta)
                .replace("__ARCHIVE_BANNER__", banner(generated, prev, prefix)))

    (ROOT / "index.html").write_text(build("archive/"), encoding="utf-8")
    dated = ARCHIVE / ("%s.html" % today.isoformat())
    dated.write_text(build(""), encoding="utf-8")

    # Rebuild the archive index from disk so it can never drift out of sync.
    all_editions = editions()
    rows = "\n".join('    <li><a href="%s">%s</a></li>' % (p.name, dutch_date(p))
                     for p in all_editions)
    (ARCHIVE / "index.html").write_text(
        "<!DOCTYPE html>\n<html lang=\"nl\">\n<head>\n<meta charset=\"UTF-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1.0\">\n"
        "<title>Archief \u2014 Vastgoed Nieuws Digest</title>\n<style>\n"
        "body{font-family:'Segoe UI',system-ui,sans-serif;background:#f4f6f9;color:#1f2937;"
        "max-width:720px;margin:0 auto;padding:40px 24px}\n"
        "h1{font-size:22px;color:#1a2e44;margin-bottom:4px}\n"
        "p.sub{color:#6b7280;font-size:13px;margin-bottom:24px}\n"
        "p.sub a{color:#e07b39}\n"
        "ul{list-style:none;padding:0}\n"
        "li{background:#fff;border:1px solid #dde3ec;border-radius:8px;margin-bottom:8px}\n"
        "li a{display:block;padding:12px 16px;color:#1a2e44;text-decoration:none;font-weight:600}\n"
        "li a:hover{color:#e07b39}\n</style>\n</head>\n<body>\n"
        "<h1>\U0001f3e2 Archief \u2014 Vastgoed Nieuws Digest</h1>\n"
        '<p class="sub"><a href="../index.html">\u2190 naar de actuele digest</a> '
        "&nbsp;&middot;&nbsp; %d edities</p>\n<ul>\n%s\n</ul>\n</body>\n</html>\n"
        % (len(all_editions), rows),
        encoding="utf-8")

    print("wrote index.html (%d articles), %s, archive/index.html (%d editions); "
          "previous edition: %s"
          % (len(articles), dated.relative_to(ROOT), len(all_editions),
             prev.stem if prev else "none yet"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
