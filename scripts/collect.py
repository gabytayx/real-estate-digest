#!/usr/bin/env python3
"""
Collect vastgoed news from the configured sources, match it against the
company watchlist, tag it, and write data/articles.json.

Design notes:
- Never let one broken source kill the run. Every source is wrapped; failures
  are recorded in the per-source health table instead of raising.
- Feed discovery is layered: explicit feed URLs -> <link rel=alternate> on the
  homepage -> HTML listing scrape. A source only counts as failed if all
  three come back empty.
- Company matching uses word boundaries on accent-folded text, so "AM" does
  not match "Amsterdam" and "Dar" does not match "Daarom".
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
FETCH_BODIES = os.environ.get("FETCH_BODIES", "1") != "0"
BODY_LIMIT = int(os.environ.get("BODY_LIMIT", "80"))  # max article pages per run
TIMEOUT = 25
UA = "real-estate-digest/1.0 (+https://github.com/gabytayx/real-estate-digest)"

SESSION = requests.Session()
SESSION.headers.update({"User-Agent": UA, "Accept-Language": "nl,en;q=0.8"})


# --------------------------------------------------------------------------
# watchlist
# --------------------------------------------------------------------------

# Entries that are junk, truncated, or so short/common they generate nothing
# but false positives. Anything here is dropped from the watchlist at load.
JUNK = {
    "b", "wb", "am", "dar", "mevrouw", "meneer", "amaha", "oonbron", "oonbedrijf",
    "oonstaete", "oonstichting 'thuis", "oonzorg nederland", "onen limburg",
    "onenbreburg", "oningbedrijf velsen", "orgfederatie oldenzaal", "ork accountants",
    "orld of walas", "ovvice", "iekenfondsraad", "uidwester", "uiver vastgoed",
    "ulven invest", "urich insurance", "ublin nederland", "üblin nederland",
    "egwaard beheer", "ella nederland", "inc real estate", "ior student housing",
    "okogawa europe", "witserse maatschappij van levensverzekering en lijfrente",
    "koper volgens vastgoeddata", "verkoper volgens vastgoeddata", "next level",
    "minerva", "estea", "hilva", "him", "itec", "itek", "dvm", "myb.", "zwb",
    "steengoed", "tauro", "leyten", "signa", "suez", "cito", "dar", "crv",
}

# Short names that ARE worth keeping, matched case-sensitively to cut noise.
CASE_SENSITIVE = {"NSI", "DWS", "TPG", "VGP", "CTP", "WDP", "SEGRO", "KPN", "DSM",
                  "NIBC", "PME", "PFZW", "ABP", "COA", "NVM", "VU", "AEW", "TIAA",
                  "IBM", "RWE", "MARK", "KUDO", "BPRE", "DCD", "LRE", "VDG", "DHG",
                  "SF Group", "VB Groep", "3B Group", "O Capital", "FOUR-D"}


SUFFIXES = ("group", "groep", "holding", "holdings", "beheer", "bv", "b v",
            "nv", "n v", "investments", "vastgoed", "real estate", "capital")


def fold(s: str) -> str:
    """
    Lowercase, strip accents, and flatten punctuation so that real-world
    spellings collapse onto one form:
        'Klépierre'          -> 'klepierre'
        'A.s.r. real estate' -> 'asr real estate'
        'M.J. de Nijs'       -> 'mj de nijs'
    """
    s = unicodedata.normalize("NFKD", s)
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = s.lower()
    s = s.replace("&", " en ")
    s = re.sub(r"[.\u2019'`]", "", s)      # drop dots and apostrophes entirely
    s = re.sub(r"[-–—/,]", " ", s)          # dashes and slashes become spaces
    return re.sub(r"\s+", " ", s).strip()


def strip_suffix(folded_name: str) -> str | None:
    """
    'meerdervoort group' -> 'meerdervoort', so a bare mention still matches.
    Returns None when stripping would leave something too short to be safe.
    """
    for suf in SUFFIXES:
        if folded_name.endswith(" " + suf):
            base = folded_name[: -len(suf) - 1].strip()
            if len(base) >= 5 and base not in JUNK:
                return base
    return None


def load_companies() -> list[dict]:
    path = DATA / "companies.txt"
    raw = [ln.strip() for ln in path.read_text(encoding="utf-8").splitlines()]
    seen, out = set(), []
    for name in raw:
        if not name or name.startswith("#"):
            continue
        key = fold(name)
        if key in seen or key in JUNK:
            continue
        # Drop 1-3 char names unless explicitly whitelisted.
        if len(name) < 4 and name not in CASE_SENSITIVE:
            continue
        seen.add(key)

        # Build the match pattern. Parenthesised aliases become alternatives:
        # "Dutch City Development (DCD)" -> matches either form.
        aliases = [name]
        m = re.match(r"^(.*?)\s*\(([^)]+)\)\s*$", name)
        if m:
            aliases = [m.group(1).strip(), m.group(2).strip()]

        cs = any(a in CASE_SENSITIVE for a in aliases)
        variants = set()
        for a in aliases:
            if len(a) < 3:
                continue
            f = fold(a)
            variants.add(f)
            base = strip_suffix(f)
            if base:
                variants.add(base)

        if cs:
            # Acronyms like NSI, DWS, MARK: match the exact casing against the
            # raw text, otherwise "MARK" hits every mention of someone named Mark.
            pats = [re.compile(rf"(?<!\w){re.escape(a)}(?!\w)")
                    for a in aliases if a in CASE_SENSITIVE]
        else:
            pats = [re.compile(rf"(?<!\w){re.escape(v)}(?!\w)") for v in variants if v]
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
# categories + badges
# --------------------------------------------------------------------------

CATEGORY_RULES = {
    "Woningmarkt": ["woning", "huurwoning", "appartement", "wooncomplex", "woonzorg",
                    "huurmarkt", "corporatie", "sociale huur", "middenhuur"],
    "Kantoren": ["kantoor", "kantoorgebouw", "kantoorruimte", "office", "werkplek"],
    "Retail": ["winkel", "winkelcentrum", "retail", "supermarkt", "horeca", "flagship"],
    "Logistiek": ["logistiek", "distributiecentrum", "bedrijfsruimte", "warehouse",
                  "dc ", "opslag", "bedrijfspand"],
    "Beleggingen": ["belegger", "belegging", "fonds", "investeer", "acquisitie",
                    "portefeuille", "rendement", "yield", "kapitaal"],
    "Transacties": ["verkocht", "verkoopt", "gekocht", "koopt", "verworven", "verwerft",
                    "aangekocht", "overgenomen", "neemt over", "transactie", "deal",
                    "afgestoten", "levert op"],
    "Personalia": ["benoemd", "benoeming", "aangesteld", "treedt aan", "stapt op",
                   "vertrekt", "opvolger", "nieuwe directeur", "nieuwe ceo",
                   "nieuwe cfo", "versterkt team", "naar ", "directie"],
    "Nieuwbouw": ["nieuwbouw", "ontwikkel", "bouwt", "oplevering", "transformatie",
                  "herontwikkeling", "bouwstart", "eerste steen", "gebiedsontwikkeling"],
    "Duurzaamheid": ["duurzaam", "verduurzam", "energielabel", "esg", "co2", "warmtepomp",
                     "circulair", "paris proof", "energieneutraal"],
    "Zorgvastgoed": ["zorgvastgoed", "zorginstelling", "ziekenhuis", "verpleeghuis",
                     "kliniek", "zorgcomplex"],
    "Studentenhuisvesting": ["student", "studentenhuisvesting", "campus", "kamers"],
    "Hotels": ["hotel", "hospitality", "vakantiepark", "resort", "leisure"],
    "Beleid & Gemeente": ["gemeente", "provincie", "bestemmingsplan", "vergunning",
                          "kamerbrief", "wetsvoorstel", "minister", "beleid",
                          "omgevingswet", "tweede kamer"],
    "Marktoverzicht": ["onderzoek", "rapport", "kwartaal", "marktcijfers", "index",
                       "trend", "prognose", "opname", "leegstand", "cbs"],
}

BADGE_RULES = {
    # Values may be plain substrings or compiled regexes; both are checked
    # against the folded (lowercased, de-punctuated) text.
    "sale": ["verkocht", "verkoopt", "afgestoten", "van de hand", "desinvest",
             "doet afstand"],
    "buy": ["gekocht", "koopt", "verworven", "verwerft", "aangekocht", "overgenomen",
            "neemt over", "acquireert", "breidt portefeuille uit", "koopt terug"],
    "rolechange": [
        "benoemd", "benoeming", "aangesteld", "treedt aan", "stapt op", "vertrekt bij",
        "opvolger", "nieuwe directeur", "nieuwe ceo", "nieuwe cfo", "nieuwe coo",
        "nieuwe bestuurder", "directielid", "wordt partner", "versterkt directie",
        "start als", "begint als", "maakt de overstap", "verruilt",
        # Headline shorthand for a person moving firms: "Sander Groot naar MHM".
        # Anchored and length-capped so it cannot fire on ordinary prose.
        re.compile(r"^[a-z]+(?: [a-z]+){1,3} naar [a-z]"),
    ],
    "transaction": ["transactie", "huurovereenkomst", "verhuurd", "verhuurt", "huurt",
                    "huren", "gehuurd", "sale and leaseback", "financiering",
                    "miljoen euro", "mln euro", "koopsom", "levert op"],
    "market": ["onderzoek", "rapport", "kwartaal", "marktcijfers", "index", "prognose",
               "leegstand", "cbs", "trend", "halfjaar"],
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
    if not cats:
        cats = ["Overig"]
    return sorted(cats), badges


# --------------------------------------------------------------------------
# fetching
# --------------------------------------------------------------------------

def get(url: str) -> requests.Response | None:
    try:
        r = SESSION.get(url, timeout=TIMEOUT)
        if r.status_code == 200 and r.content:
            return r
    except requests.RequestException:
        pass
    return None


def discover_feeds(source: dict) -> list[str]:
    """Explicit feeds first, then <link rel=alternate> on the homepage."""
    found = list(source.get("feeds", []))
    r = get(source["home"])
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for link in soup.find_all("link", rel=lambda v: v and "alternate" in v):
            if "rss" in (link.get("type") or "") or "xml" in (link.get("type") or ""):
                href = link.get("href")
                if href:
                    found.append(urljoin(source["home"], href))
    # de-dupe, preserve order
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
        link = e.get("link")
        title = (e.get("title") or "").strip()
        if not link or not title:
            continue
        summary = BeautifulSoup(e.get("summary", "") or "", "html.parser").get_text(" ", strip=True)
        date = None
        for field in ("published", "updated", "created"):
            if e.get(field):
                try:
                    date = dateparser.parse(e[field])
                    break
                except (ValueError, TypeError, OverflowError):
                    continue
        items.append({"title": title, "url": link, "summary": summary, "dt": date,
                      "source": source["key"], "sourceLabel": source["label"]})
    return items


def scrape_listing(source: dict) -> list[dict]:
    """Last-resort fallback: pull article links straight off a listing page."""
    items, seen = [], set()
    pattern = source.get("link_pattern", "/")
    for page in source.get("listing", []):
        r = get(page)
        if not r:
            continue
        soup = BeautifulSoup(r.text, "html.parser")
        host = urlparse(source["home"]).netloc
        for a in soup.find_all("a", href=True):
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


BODY_CACHE: dict[str, str] = {}


def fetch_body(url: str) -> str:
    if url in BODY_CACHE:
        return BODY_CACHE[url]
    text = ""
    r = get(url)
    if r:
        soup = BeautifulSoup(r.text, "html.parser")
        for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
            tag.decompose()
        node = soup.find("article") or soup.find("main") or soup.body
        if node:
            text = node.get_text(" ", strip=True)[:20000]
    BODY_CACHE[url] = text
    time.sleep(0.6)  # be a polite guest
    return text


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    watchlist = load_companies()
    print(f"watchlist: {len(watchlist)} usable company names")

    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(days=WINDOW_DAYS)

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
                    via = f"rss:{feed_url}"
                    break
            if not got:
                got = scrape_listing(src)
                via = "html-listing" if got else "none"
        except Exception as exc:  # noqa: BLE001 - one bad source must not stop the run
            err = f"{type(exc).__name__}: {exc}"
        health.append({"source": src["key"], "label": src["label"], "items": len(got),
                       "via": via, "error": err})
        print(f"  {src['key']:<18} {len(got):>4} items  via {via} {err}")
        raw.extend(got)

    # ---- de-dupe by URL -------------------------------------------------
    by_url = {}
    for item in raw:
        by_url.setdefault(item["url"], item)
    print(f"{len(by_url)} unique candidate articles")

    bodies_fetched = 0
    articles = []
    for url, item in by_url.items():
        dt = item["dt"]
        if dt and dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        # Unknown date: keep it but treat as today, else feedless sources vanish.
        if dt is None:
            dt = previous.get(url, {}).get("date")
            dt = dateparser.parse(dt).replace(tzinfo=timezone.utc) if dt else now
        if dt < cutoff:
            continue

        text = f"{item['title']} {item['summary']}"
        folded = fold(text)
        hits = match_companies(text, folded, watchlist)

        # No hit on the teaser? The company may only appear in the body.
        if not hits and FETCH_BODIES and bodies_fetched < BODY_LIMIT:
            body = fetch_body(url)
            bodies_fetched += 1
            if body:
                text_full = f"{text} {body}"
                folded_full = fold(text_full)
                hits = match_companies(text_full, folded_full, watchlist)
                if hits:
                    folded = folded_full
                    if not item["summary"]:
                        item["summary"] = body[:400].rsplit(" ", 1)[0] + "…"

        if not hits:
            continue  # watchlist is the whole point of the digest

        cats, badges = classify(folded)
        articles.append({
            "title": item["title"],
            "summary": item["summary"] or "",
            "date": dt.date().isoformat(),
            "source": item["source"],
            "sourceLabel": item["sourceLabel"],
            "url": url,
            "companies": hits,
            "categories": cats,
            "badges": badges,
            "isNew": url not in previous,
        })

    articles.sort(key=lambda a: (a["date"], a["title"]), reverse=True)
    for i, a in enumerate(articles, 1):
        a["id"] = i

    out = {
        "generated": now.isoformat(timespec="seconds"),
        "windowDays": WINDOW_DAYS,
        "health": health,
        "articles": articles,
    }
    prev_path.write_text(json.dumps(out, ensure_ascii=False, indent=2), encoding="utf-8")

    new_count = sum(1 for a in articles if a["isNew"])
    print(f"kept {len(articles)} matched articles ({new_count} new), "
          f"{bodies_fetched} bodies fetched")

    dead = [h["label"] for h in health if h["items"] == 0]
    if dead:
        print(f"::warning::no items from: {', '.join(dead)}")
    if len(dead) == len(sources):
        print("::error::every source returned zero items - check selectors/feeds")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
