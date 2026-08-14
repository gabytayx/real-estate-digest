#!/usr/bin/env python3
"""
Collect vastgoed news from the configured sources, match it against the
company watchlist, tag it, and write data/articles.json.

Dates are treated as load-bearing: an article with no verifiable publication
date is dropped, never assumed to be recent. Sidebar and "meest gelezen"
links on listing pages are exactly how a 2025 article ends up in a 2026
digest, and an undated item is far more likely to be one of those than a
genuine new story.

Two independent windows:
  INGEST_DAYS (default 1) - how fresh an article must be to be added at all.
  WINDOW_DAYS (default 7) - how long an already-collected article stays on
                            the page before it ages out.
"""

from __future__ import annotations

import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urljoin, urlparse

import feedparser
import requests
from bs4 import BeautifulSoup
from dateutil import parser as dateparser

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

WINDOW_DAYS = int(os.environ.get("WINDOW_DAYS", "7"))
INGEST_DAYS = int(os.environ.get("INGEST_DAYS", "1"))
FETCH_PAGES = os.environ.get("FETCH_PAGES", "1") != "0"
PAGE_LIMIT = int(os.environ.get("PAGE_LIMIT", "120"))
TIMEOUT = 25
UA = "real-estate-digest/1.0 (+https://github.com/gabytayx/real-estate-digest)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})


# --------------------------------------------------------------------------
# watchlist
# --------------------------------------------------------------------------

# Only entries that are genuinely unusable: truncated fragments from the
# original spreadsheet, placeholder rows, and names so short they match
# ordinary Dutch words. Real companies with odd names stay in.
JUNK = {
    "b", "wb", "am", "mevrouw", "meneer", "amaha", "oonbron", "oonbedrijf",
    "oonstaete", "oonstichting thuis", "oonzorg nederland", "onen limburg",
    "onenbreburg", "oningbedrijf velsen", "orgfederatie oldenzaal", "ork accountants",
    "orld of walas", "ovvice", "iekenfondsraad", "uidwester", "uiver vastgoed",
    "ulven invest", "urich insurance", "ublin nederland", "egwaard beheer",
    "ella nederland", "inc real estate", "ior student housing", "okogawa europe",
    "witserse maatschappij van levensverzekering en lijfrente",
    "koper volgens vastgoeddata", "verkoper volgens vastgoeddata",
    "next level", "mevrouw ", "b ",
}

# Acronyms matched on exact casing, so MARK does not fire on someone named Mark.
CASE_SENSITIVE = {"NSI", "DWS", "TPG", "VGP", "CTP", "WDP", "SEGRO", "KPN", "DSM",
                  "NIBC", "PME", "PFZW", "ABP", "COA", "NVM", "VU", "AEW", "TIAA",
                  "IBM", "RWE", "MARK", "KUDO", "BPRE", "DCD", "LRE", "VDG", "DHG",
                  "CRV", "DVM", "ZWB", "HIM", "SF Group", "VB Groep", "3B Group",
                  "O Capital", "FOUR-D", "AT Capital", "TCN", "HTM"}

# Corporate-form suffixes only. Deliberately excludes "vastgoed", "capital",
# "investments" and "real estate": stripping those turns "Green Real Estate"
# into "green" and "De Raad Vastgoed" into "de raad", which match constantly.
SUFFIXES = ("group", "groep", "holding", "holdings", "beheer", "bv", "nv")
MIN_BASE = 6  # a suffix-stripped alias must be at least this long to be used


def fold(s: str) -> str:
    """
    Lowercase, strip accents, flatten punctuation:
        'Klépierre'          -> 'klepierre'
        'A.s.r. real estate' -> 'asr real estate'
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " en ")
    s = re.sub(r"[.\u2019'`]", "", s)
    s = re.sub(r"[-\u2013\u2014/,]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def strip_suffix(folded_name: str) -> str | None:
    for suf in SUFFIXES:
        if folded_name.endswith(" " + suf):
            base = folded_name[: -len(suf) - 1].strip()
            if len(base) >= MIN_BASE and base not in JUNK:
                return base
    return None


def load_companies() -> list[dict]:
    raw = (DATA / "companies.txt").read_text(encoding="utf-8").splitlines()
    seen, out = set(), []
    for line in raw:
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        key = fold(name)
        if key in seen or key in JUNK:
            continue
        if len(name) < 4 and name not in CASE_SENSITIVE:
            continue
        seen.add(key)

        aliases = [name]
        m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", name)
        if m:
            aliases = [m.group(1).strip(), m.group(2).strip()]

        cs = any(a in CASE_SENSITIVE for a in aliases)
        if cs:
            pats = [re.compile(r"(?<!\w)%s(?!\w)" % re.escape(a))
                    for a in aliases if a in CASE_SENSITIVE]
        else:
            variants = set()
            for a in aliases:
                if len(a) < 3:
                    continue
                f = fold(a)
                variants.add(f)
                base = strip_suffix(f)
                if base:
                    variants.add(base)
            pats = [re.compile(r"(?<!\w)%s(?!\w)" % re.escape(v)) for v in variants if v]
        if pats:
            out.append({"name": name, "patterns": pats, "cs": cs})
    return out


def match_companies(text: str, folded: str, watchlist: list[dict]) -> list[str]:
    hits = []
    for entry in watchlist:
        haystack = text if entry["cs"] else folded
        if any(p.search(haystack) for p in entry["patterns"]):
            hits.append(entry["name"])
    return sorted(set(hits))


# --------------------------------------------------------------------------
# categories + badges  (values may be substrings or compiled regexes)
# --------------------------------------------------------------------------

ROLE_MOVE = re.compile(r"^[a-z]+(?: [a-z]+){1,3} naar [a-z]")

CATEGORY_RULES = {
    "Woningmarkt": ["woning", "huurwoning", "appartement", "wooncomplex", "woonzorg",
                    "huurmarkt", "corporatie", "sociale huur", "middenhuur"],
    "Kantoren": ["kantoor", "kantoorgebouw", "kantoorruimte", "office", "werkplek"],
    "Retail": ["winkel", "winkelcentrum", "retail", "supermarkt", "horeca", "flagship"],
    "Logistiek": ["logistiek", "distributiecentrum", "bedrijfsruimte", "warehouse",
                  "opslag", "bedrijfspand", re.compile(r"\bdc\b")],
    "Beleggingen": ["belegger", "belegging", "fonds", "investeer", "acquisitie",
                    "portefeuille", "rendement", "yield", "kapitaal"],
    "Transacties": ["verkocht", "verkoopt", "gekocht", "koopt", "verworven", "verwerft",
                    "aangekocht", "overgenomen", "neemt over", "transactie",
                    "afgestoten", re.compile(r"\bdeal\b")],
    "Personalia": ["benoemd", "benoeming", "aangesteld", "treedt aan", "stapt op",
                   "opvolger", "nieuwe directeur", "nieuwe ceo", "nieuwe cfo",
                   "directie", "start als", ROLE_MOVE],
    "Nieuwbouw": ["nieuwbouw", "ontwikkel", "bouwt", "oplevering", "transformatie",
                  "herontwikkeling", "bouwstart", "eerste steen", "gebiedsontwikkeling"],
    "Duurzaamheid": ["duurzaam", "verduurzam", "energielabel", "esg", "co2", "warmtepomp",
                     "circulair", "paris proof", "energieneutraal"],
    "Zorgvastgoed": ["zorgvastgoed", "zorginstelling", "ziekenhuis", "verpleeghuis",
                     "kliniek", "zorgcomplex"],
    "Studentenhuisvesting": ["studentenhuisvesting", "studentenwoning", "campus",
                             "studentenkamer"],
    "Hotels": ["hotel", "hospitality", "vakantiepark", "resort", "leisure"],
    "Beleid & Gemeente": ["gemeente", "provincie", "bestemmingsplan", "vergunning",
                          "kamerbrief", "wetsvoorstel", "minister", "omgevingswet",
                          "tweede kamer"],
    "Marktoverzicht": ["onderzoek", "rapport", "kwartaal", "marktcijfers", "prognose",
                       "leegstand", "cbs", re.compile(r"\bindex\b"),
                       re.compile(r"\btrend\b")],
}

BADGE_RULES = {
    "sale": ["verkocht", "verkoopt", "afgestoten", "van de hand", "desinvest",
             "doet afstand"],
    "buy": ["gekocht", "koopt", "verworven", "verwerft", "aangekocht", "overgenomen",
            "neemt over", "acquireert", "breidt portefeuille uit"],
    "rolechange": ["benoemd", "benoeming", "aangesteld", "treedt aan", "stapt op",
                   "vertrekt bij", "opvolger", "nieuwe directeur", "nieuwe ceo",
                   "nieuwe cfo", "nieuwe coo", "nieuwe bestuurder", "directielid",
                   "wordt partner", "versterkt directie", "start als", "begint als",
                   "maakt de overstap", "verruilt", ROLE_MOVE],
    "transaction": ["transactie", "huurovereenkomst", "verhuurd", "verhuurt", "huurt",
                    "huren", "gehuurd", "sale and leaseback", "financiering",
                    "miljoen euro", "mln euro", "koopsom", re.compile(r"\bdeal\b")],
    "market": ["onderzoek", "rapport", "kwartaal", "marktcijfers", "prognose",
               "leegstand", "cbs", "halfjaar", re.compile(r"\bindex\b")],
}


def _hits(folded: str, rules) -> bool:
    for rule in rules:
        if isinstance(rule, str):
            if rule in folded:
                return True
        elif rule.search(folded):
            return True
    return False


def classify(folded: str) -> tuple[list[str], list[str]]:
    cats = [c for c, kws in CATEGORY_RULES.items() if _hits(folded, kws)]
    badges = [b for b, kws in BADGE_RULES.items() if _hits(folded, kws)]
    return sorted(cats) or ["Overig"], badges


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def get(url: str):
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and r.content:
            return r
    except requests.RequestException:
        pass
    return None


def parse_date(value):
    """Parse a date string, returning an aware UTC datetime or None."""
    if not value:
        return None
    try:
        dt = dateparser.parse(value)
    except (ValueError, TypeError, OverflowError):
        return None
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    # A date in the future by more than a day means we parsed something that
    # was not a publication date at all.
    if dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


def discover_feeds(source: dict) -> list[str]:
    found = list(source.get("feeds", []))
    r = get(source["home"])
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            t = link.get("type") or ""
            if "rss" in t or "xml" in t:
                href = link.get("href")
                if href:
                    found.append(urljoin(source["home"], href))
    seen, out = set(), []
    for f in found:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def parse_feed(url: str, source: dict) -> list[dict]:
    r = get(url)
    if not r:
        return []
    parsed = feedparser.parse(r.content)
    items = []
    for e in parsed.entries:
        link, title = e.get("link"), (e.get("title") or "").strip()
        if not link or not title:
            continue
        summary = BeautifulSoup(e.get("summary", "") or "", "html.parser").get_text(" ", strip=True)
        dt = None
        for field in ("published", "updated", "created"):
            dt = parse_date(e.get(field))
            if dt:
                break
        items.append({"title": title, "url": link, "summary": summary, "dt": dt,
                      "source": source["key"], "sourceLabel": source["label"]})
    return items


def scrape_listing(source: dict) -> list[dict]:
    """
    Fallback for sources without a usable feed. Only looks inside the main
    content region when one exists, so "meest gelezen" and footer link blocks
    do not leak years-old articles into a daily digest.
    """
    items, seen = [], set()
    pattern = source.get("link_pattern", "/")
    for page in source.get("listing", []):
        r = get(page)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        for junk in soup.select("aside, footer, nav, .sidebar, #sidebar, "
                                "[class*=popular], [class*=gelezen], [class*=related], "
                                "[class*=gerelateerd], [class*=archief]"):
            junk.decompose()
        region = soup.find("main") or soup.find(id="content") or soup.body
        if region is None:
            continue
        host = urlparse(source["home"]).netloc
        for a in region.find_all("a", href=True):
            href = urljoin(page, a["href"])
            if urlparse(href).netloc != host or pattern not in href:
                continue
            title = a.get_text(" ", strip=True)
            if len(title) < 25 or href in seen:
                continue
            seen.add(href)
            items.append({"title": title, "url": href, "summary": "", "dt": None,
                          "source": source["key"], "sourceLabel": source["label"]})
    return items


PAGE_CACHE: dict[str, tuple[str, object]] = {}

# Where publication dates hide in practice, in order of trustworthiness.
META_KEYS = [("meta", {"property": "article:published_time"}, "content"),
             ("meta", {"itemprop": "datePublished"}, "content"),
             ("meta", {"name": "datePublished"}, "content"),
             ("meta", {"name": "publish-date"}, "content"),
             ("meta", {"name": "date"}, "content"),
             ("time", {"datetime": True}, "datetime")]


def fetch_page(url: str) -> tuple[str, object]:
    """Return (body_text, published_dt_or_None), fetching at most once per URL."""
    if url in PAGE_CACHE:
        return PAGE_CACHE[url]
    body, published = "", None
    r = get(url)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")

        for tag, attrs, attr in META_KEYS:
            el = soup.find(tag, attrs=attrs)
            if el and el.get(attr):
                published = parse_date(el.get(attr))
                if published:
                    break

        if not published:  # JSON-LD blocks
            for script in soup.find_all("script", type="application/ld+json"):
                try:
                    blob = json.loads(script.string or "")
                except (json.JSONDecodeError, TypeError):
                    continue
                for node in (blob if isinstance(blob, list) else [blob]):
                    if isinstance(node, dict):
                        published = parse_date(node.get("datePublished"))
                        if published:
                            break
                if published:
                    break

        for junk in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            junk.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        if node:
            body = node.get_text(" ", strip=True)[:20000]
        time.sleep(0.6)  # be a polite guest
    PAGE_CACHE[url] = (body, published)
    return PAGE_CACHE[url]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    watchlist = load_companies()
    now = datetime.now(timezone.utc)
    today = now.date()
    ingest_from = today - timedelta(days=INGEST_DAYS - 1)
    keep_from = today - timedelta(days=WINDOW_DAYS - 1)
    print("watchlist: %d usable names | ingest from %s | keep from %s"
          % (len(watchlist), ingest_from, keep_from))

    previous = {}
    prev_path = DATA / "articles.json"
    if prev_path.exists():
        for a in json.loads(prev_path.read_text(encoding="utf-8")).get("articles", []):
            previous[a["url"]] = a

    health, raw = [], []
    for src in sources:
        got, via, err = [], "none", ""
        try:
            for feed_url in discover_feeds(src):
                got = parse_feed(feed_url, src)
                if got:
                    via = "rss"
                    break
            if not got:
                got = scrape_listing(src)
                via = "html-listing" if got else "none"
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            err = "%s: %s" % (type(exc).__name__, exc)
        health.append({"source": src["key"], "label": src["label"], "items": len(got),
                       "via": via, "error": err})
        print("  %-18s %4d items  via %s %s" % (src["key"], len(got), via, err))
        raw.extend(got)

    by_url = {}
    for item in raw:
        by_url.setdefault(item["url"], item)

    stats = {"undated": 0, "too_old": 0, "no_match": 0, "pages": 0}
    fresh = []
    for url, item in by_url.items():
        dt = item["dt"]
        text = "%s %s" % (item["title"], item["summary"])
        folded = fold(text)
        hits = match_companies(text, folded, watchlist)

        # Fetch the page when we need the date, or when the teaser had no
        # company hit and the body might. Either way it is one request.
        if FETCH_PAGES and stats["pages"] < PAGE_LIMIT and (dt is None or not hits):
            body, published = fetch_page(url)
            stats["pages"] += 1
            if dt is None:
                dt = published
            if body and not hits:
                text_full = "%s %s" % (text, body)
                folded_full = fold(text_full)
                hits = match_companies(text_full, folded_full, watchlist)
                if hits:
                    folded = folded_full
                    if not item["summary"]:
                        item["summary"] = body[:400].rsplit(" ", 1)[0] + "\u2026"

        if dt is None:
            stats["undated"] += 1   # no verifiable date -> never assume today
            continue
        if dt.date() < ingest_from:
            stats["too_old"] += 1
            continue
        if not hits:
            stats["no_match"] += 1
            continue

        cats, badges = classify(folded)
        fresh.append({"title": item["title"], "summary": item["summary"] or "",
                      "date": dt.date().isoformat(), "source": item["source"],
                      "sourceLabel": item["sourceLabel"], "url": url,
                      "companies": hits, "categories": cats, "badges": badges,
                      "isNew": url not in previous})

    # Carry over previously collected articles that have not aged out yet.
    carried = 0
    articles = {a["url"]: a for a in fresh}
    for url, old in previous.items():
        if url in articles:
            continue
        if old.get("date", "") >= keep_from.isoformat():
            old = dict(old, isNew=False)
            articles[url] = old
            carried += 1

    out_articles = sorted(articles.values(),
                          key=lambda a: (a["date"], a["title"]), reverse=True)
    for i, a in enumerate(out_articles, 1):
        a["id"] = i

    prev_path.write_text(json.dumps({
        "generated": now.isoformat(timespec="seconds"),
        "windowDays": WINDOW_DAYS,
        "ingestDays": INGEST_DAYS,
        "health": health,
        "articles": out_articles,
    }, ensure_ascii=False, indent=2), encoding="utf-8")

    print("candidates %d -> new %d, carried over %d, total %d"
          % (len(by_url), len(fresh), carried, len(out_articles)))
    print("  dropped: %d undated, %d outside ingest window, %d no watchlist match"
          " | %d pages fetched" % (stats["undated"], stats["too_old"],
                                   stats["no_match"], stats["pages"]))

    dead = [h["label"] for h in health if h["items"] == 0]
    if dead:
        print("::warning::no items from: %s" % ", ".join(dead))
    if len(dead) == len(sources):
        print("::error::every source returned zero items")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
