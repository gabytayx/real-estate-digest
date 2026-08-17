#!/usr/bin/env python3
"""Offline checks: matching, classification, date gating, renderer."""
import json, subprocess, sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
import collect  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent

MATCH_CASES = [
    ("Gemeente Amsterdam koopt ADM-terrein terug van Wim Beelen voor 165 mln euro",
     {"Gemeente Amsterdam"}, {"buy", "transaction"}),
    ("Rudius Vastgoed heeft een kantoorgebouw aan de Jansbuitensingel 30 in Arnhem gekocht.",
     {"Rudius Vastgoed"}, {"buy"}),
    ("PingProperties heeft winkelcentrum Passage Schiedam verkocht aan vastgoedinvesteerder Meerdervoort.",
     {"Meerdervoort Group"}, {"sale"}),
    ("A.s.r. real estate sloot een langjarige huurovereenkomst met Zeeman in Leiden.",
     {"ASR Real Estate"}, {"transaction"}),
    ("Sander Groot naar MHM Onroerend Goed", set(), {"rolechange"}),
    ("Humble Holdings huurt logistieke ruimte van Segro Netherlands.", {"Segro"}, {"transaction"}),
    # --- false positives that earlier versions produced ---
    ("De raad van bestuur vergadert dinsdag over de begroting.", set(), set()),
    ("Minister praat over de green deal en duurzame nieuwbouw.", set(), set()),
    ("Mark de Boer opent de tweede Upfront Foodstore in Rotterdam.", set(), set()),
    ("De markt in Amsterdam trekt aan, aldus de makelaar.", set(), set()),
]

EXTRA = ["Gemeente Amsterdam", "Rudius Vastgoed", "Meerdervoort Group", "ASR Real Estate",
         "Segro", "MARK", "AM", "De Raad Vastgoed", "Green Real Estate", "Leyten",
         "Minerva", "Steengoed"]


def load_wl():
    p = collect.DATA / "companies.txt"
    orig = p.read_text(encoding="utf-8")
    p.write_text(orig + "\n" + "\n".join(EXTRA), encoding="utf-8")
    try:
        return collect.load_companies(), orig
    finally:
        p.write_text(orig, encoding="utf-8")


def main() -> int:
    wl, _ = load_wl()
    names = {e["name"] for e in wl}
    fails = 0
    print("watchlist: %d entries" % len(wl))

    for want, cond, msg in [(False, "AM" in names, "2-char junk dropped"),
                            (True, "MARK" in names, "whitelisted acronym kept"),
                            (True, "Leyten" in names, "real company Leyten kept"),
                            (True, "Minerva" in names, "real company Minerva kept"),
                            (True, "Steengoed" in names, "real company Steengoed kept")]:
        ok = cond == want
        fails += 0 if ok else 1
        print("  %s watchlist: %s" % ("ok  " if ok else "FAIL", msg))

    print("\nmatching + badges")
    for text, want_co, want_b in MATCH_CASES:
        folded = collect.fold(text)
        got_co = set(collect.match_companies(text, folded, wl))
        got_b = set(collect.classify(folded)[1])
        ok = got_co == want_co and want_b.issubset(got_b)
        fails += 0 if ok else 1
        print("  %s %s" % ("ok  " if ok else "FAIL", text[:58]))
        if not ok:
            print("       companies want %s got %s" % (want_co or "{}", got_co or "{}"))
            print("       badges want superset of %s got %s" % (want_b, got_b))

    print("\ndate gating")
    now = datetime.now(timezone.utc)
    date_cases = [
        ("2025-10-14T09:00:00+02:00", False, "October 2025 article rejected"),
        (None, False, "undated article rejected (not assumed today)"),
        ((now + timedelta(days=400)).isoformat(), False, "absurd future date rejected"),
        (now.isoformat(), True, "today's article accepted"),
        ((now - timedelta(days=3)).isoformat(), False, "3 days old rejected at INGEST_DAYS=1"),
    ]
    ingest_from = now.date() - timedelta(days=collect.INGEST_DAYS - 1)
    for raw, want, msg in date_cases:
        dt = collect.parse_date(raw)
        accepted = dt is not None and dt.date() >= ingest_from
        ok = accepted == want
        fails += 0 if ok else 1
        print("  %s %s" % ("ok  " if ok else "FAIL", msg))

    print("\nrenderer")
    fixture = {"generated": now.isoformat(timespec="seconds"), "windowDays": 7,
        "ingestDays": 1,
        "health": [{"source": "vastgoedjournaal", "label": "Vastgoedjournaal",
                    "items": 38, "via": "rss", "error": ""}],
        "articles": [{"id": 1, "title": "Segro koopt logistiek pand", "summary": "x",
                      "date": now.date().isoformat(), "source": "vastgoedjournaal",
                      "sourceLabel": "Vastgoedjournaal", "url": "https://e.com/1",
                      "companies": ["Segro"], "categories": ["Logistiek"],
                      "badges": ["buy"], "isNew": True,
                      "customers": ["Segro"], "prospects": [], "keyDeal": True}]}
    (collect.DATA / "articles.json").write_text(json.dumps(fixture), encoding="utf-8")
    r = subprocess.run([sys.executable, str(ROOT / "scripts" / "render.py")],
                       capture_output=True, text=True)
    print("  " + (r.stdout.strip() or r.stderr.strip()))
    html = (ROOT / "index.html").read_text(encoding="utf-8")
    for probe, expect in [("Segro koopt logistiek pand", True), ("__ARTICLES_JSON__", False),
                          ("__HEADER_META__", False), ("__ARCHIVE_BANNER__", False),
                          ("key-deal-mark", True), ("Bestaande Klanten", True),
                          ("prefers-reduced-motion", True)]:
        ok = (probe in html) == expect
        fails += 0 if ok else 1
        print("  %s html %s %r" % ("ok  " if ok else "FAIL",
                                   "contains" if expect else "omits", probe))
    print("\nFAILURES: %d" % fails)
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
