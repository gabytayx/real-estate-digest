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

# Two tiers of optional suffix, because "Meerdervoort Group" and "Invesco Real
# Estate" are routinely written without the suffix, but blindly stripping
# sector words turns "Green Real Estate" into the search term "green".
SUFFIX_CORP = ("group", "groep", "holding", "holdings", "beheer", "bv", "nv")
SUFFIX_SECTOR = ("real estate", "vastgoed", "capital", "investments", "invest",
                 "properties", "property", "development", "ontwikkeling")

# Words too generic to stand alone as a company identifier.
GENERIC = {"green", "base", "urban", "prime", "next", "first", "city", "world",
           "core", "park", "dutch", "holland", "europa", "europe", "partner",
           "partners", "impact", "global", "nieuw", "new", "open", "real",
           "estate", "property", "invest", "capital", "vastgoed", "groep",
           "group", "holding", "united", "royal", "koninklijke", "algemene",
           "nederlandse", "nederland", "amsterdam", "rotterdam", "utrecht"}
ARTICLE_PREFIXES = ("de ", "het ", "van ", "der ", "den ", "'t ")


def _usable_base(base: str, min_len: int) -> bool:
    if len(base) < min_len or base in GENERIC or base in JUNK:
        return False
    if base.startswith(ARTICLE_PREFIXES):
        return False   # "De Raad Vastgoed" -> "de raad" matches ordinary prose
    return True


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
    """'meerdervoort group' -> 'meerdervoort', so a bare mention still matches."""
    for suf in SUFFIX_CORP:
        if folded_name.endswith(" " + suf):
            base = folded_name[: -len(suf) - 1].strip()
            if _usable_base(base, 5):
                return base
    for suf in SUFFIX_SECTOR:
        if folded_name.endswith(" " + suf):
            base = folded_name[: -len(suf) - 1].strip()
            if _usable_base(base, 6):
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

        # "Emro Real Estate | Emro" lets you add a short form the press
        # actually uses, without weakening the automatic rules for everyone.
        if "|" in name:
            parts = [x.strip() for x in name.split("|") if x.strip()]
            name, aliases = parts[0], parts
        else:
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
            keys = {fold(a) for a in aliases}
            for a in aliases:
                base = strip_suffix(fold(a))
                if base:
                    keys.add(base)
            out.append({"name": name, "patterns": pats, "cs": cs, "keys": keys})
    return out


def load_customers(watchlist: list[dict]) -> tuple[set, list[str]]:
    """
    Read data/customers.txt and return (set of canonical watchlist names that
    are customers, list of names auto-added to the watchlist).

    Customers absent from companies.txt are appended to the watchlist rather
    than ignored: a customer silently going unmonitored because of a spelling
    difference is the worst possible failure here.
    """
    path = DATA / "customers.txt"
    if not path.exists():
        return set(), []
    wanted = []
    for line in path.read_text(encoding="utf-8").splitlines():
        name = line.strip()
        if name and not name.startswith("#"):
            wanted.append(name)

    customer_names, added = set(), []
    for name in wanted:
        key = fold(name)
        base = strip_suffix(key)
        hit = next((e for e in watchlist
                    if key in e["keys"] or (base and base in e["keys"])), None)
        if hit:
            customer_names.add(hit["name"])
            continue
        # Not on the watchlist yet - add it so it is actually monitored.
        pats = [re.compile(r"(?<!\w)%s(?!\w)" % re.escape(key))]
        if name in CASE_SENSITIVE:
            pats = [re.compile(r"(?<!\w)%s(?!\w)" % re.escape(name))]
        watchlist.append({"name": name, "patterns": pats,
                          "cs": name in CASE_SENSITIVE, "keys": {key}})
        customer_names.add(name)
        added.append(name)
    return customer_names, added


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
    "Transacties": ["verkocht", "verkoopt", re.compile(r"\bgekocht\b"),
                    re.compile(r"\bkoopt\b"), "verworven", "verwerft", "aangekocht",
                    "overgenomen", "neemt over", "transactie", "afgestoten",
                    re.compile(r"\bdeal\b")],
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
    # "koopt" must be word-bounded: as a bare substring it sits inside
    # "verkoopt", so every sale was also being tagged as a purchase.
    "buy": [re.compile(r"\bkoopt\b"), re.compile(r"\bgekocht\b"), "verworven",
            "verwerft", "aangekocht", "overgenomen", "neemt over", "acquireert",
            "breidt portefeuille uit", re.compile(r"\bkoopt terug\b")],
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


NL_MONTHS = {"januari": 1, "februari": 2, "maart": 3, "april": 4, "mei": 5,
             "juni": 6, "juli": 7, "augustus": 8, "september": 9, "oktober": 10,
             "november": 11, "december": 12,
             "jan": 1, "feb": 2, "mrt": 3, "apr": 4, "jun": 6, "jul": 7,
             "aug": 8, "sep": 9, "okt": 10, "nov": 11, "dec": 12}

NL_DATE_RE = re.compile(
    r"\b(\d{1,2})\s+(" + "|".join(sorted(NL_MONTHS, key=len, reverse=True)) + r")\.?\s+(\d{4})\b",
    re.IGNORECASE)


def parse_dutch_date(text: str):
    """
    Pull a Dutch-formatted date out of visible page text, e.g. '14 april 2026'.
    Needed because several of these sites publish no date metadata at all and
    render the date only as body text. Takes the earliest match in the page,
    which is where the article's own date sits - later matches tend to belong
    to related-article teasers.
    """
    m = NL_DATE_RE.search(text)
    if not m:
        return None
    day, month, year = int(m.group(1)), NL_MONTHS[m.group(2).lower()], int(m.group(3))
    try:
        dt = datetime(year, month, day, tzinfo=timezone.utc)
    except ValueError:
        return None
    if dt > datetime.now(timezone.utc) + timedelta(days=1):
        return None
    return dt


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


PAGE_CACHE: dict[str, tuple[str, object, str]] = {}

# Where publication dates hide in practice, in order of trustworthiness.
META_KEYS = [("meta", {"property": "article:published_time"}, "content"),
             ("meta", {"itemprop": "datePublished"}, "content"),
             ("meta", {"name": "datePublished"}, "content"),
             ("meta", {"name": "publish-date"}, "content"),
             ("meta", {"name": "date"}, "content"),
             ("time", {"datetime": True}, "datetime")]


def fetch_page(url: str) -> tuple[str, object, str]:
    """
    Return (article_text, published_dt_or_None, teaser).

    Only text inside the article body is returned. Breadcrumbs, byline blocks,
    view counters, tag lists and "gerelateerd" rails are stripped first: left
    in, they poison the summary, inflate the category tags, and cause company
    names that merely appear in a sidebar to match every article on the site.
    """
    if url in PAGE_CACHE:
        return PAGE_CACHE[url]
    body, published, teaser = "", None, ""
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

        # The publisher's own teaser beats anything we could cut from the body.
        for attrs in ({"property": "og:description"}, {"name": "description"}):
            el = soup.find("meta", attrs=attrs)
            if el and (el.get("content") or "").strip():
                teaser = el["content"].strip()
                break

        for junk in soup(["script", "style", "nav", "footer", "header", "aside",
                          "form", "figcaption", "noscript"]):
            junk.decompose()
        for junk in soup.select(
                "[class*=breadcrumb], [class*=byline], [class*=author], [class*=meta],"
                "[class*=share], [class*=social], [class*=tag], [class*=views],"
                "[class*=related], [class*=gerelateerd], [class*=popular],"
                "[class*=gelezen], [class*=sidebar], [class*=widget], [class*=advert],"
                "[class*=banner], [class*=newsletter], [class*=nieuwsbrief],"
                "[class*=agenda], [class*=comment], [id*=sidebar], [id*=related]"):
            junk.decompose()

        # Require a real article container. Falling back to <body> is what
        # dragged whole-page chrome into the matching text.
        node = soup.find("article") or soup.find(attrs={"class": "article-body"}) \
            or soup.find(attrs={"itemprop": "articleBody"}) or soup.find("main")
        if node:
            paras = [p.get_text(" ", strip=True) for p in node.find_all("p")]
            paras = [x for x in paras if len(x) > 40]
            body = " ".join(paras)[:20000] or node.get_text(" ", strip=True)[:20000]

        # Last resort: several of these sites ship no date metadata at all and
        # print the date as plain text near the headline.
        if not published:
            head = (node or soup).get_text(" ", strip=True)[:1200]
            published = parse_dutch_date(head)
        time.sleep(0.6)  # be a polite guest
    PAGE_CACHE[url] = (body, published, teaser)
    return PAGE_CACHE[url]


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def main() -> int:
    sources = json.loads((DATA / "sources.json").read_text(encoding="utf-8"))
    watchlist = load_companies()
    customers, auto_added = load_customers(watchlist)
    now = datetime.now(timezone.utc)
    today = now.date()
    ingest_from = today - timedelta(days=INGEST_DAYS - 1)
    keep_from = today - timedelta(days=WINDOW_DAYS - 1)
    print("watchlist: %d usable names (%d customers) | ingest from %s | keep from %s"
          % (len(watchlist), len(customers), ingest_from, keep_from))
    if auto_added:
        print("::notice::added to watchlist from customers.txt (not in companies.txt): %s"
              % ", ".join(auto_added))

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

    stats = {"undated": 0, "too_old": 0, "no_match": 0, "pages": 0,
         "stale_dropped": 0}
    fresh = []
    for url, item in by_url.items():
        dt = item["dt"]
        text = "%s %s" % (item["title"], item["summary"])
        folded = fold(text)
        hits = match_companies(text, folded, watchlist)

        # Fetch the page when we need the date, or when the teaser had no
        # company hit and the body might. Either way it is one request.
        if FETCH_PAGES and stats["pages"] < PAGE_LIMIT and (dt is None or not hits):
            body, published, teaser = fetch_page(url)
            stats["pages"] += 1
            if dt is None:
                dt = published
            if teaser and not item["summary"]:
                item["summary"] = teaser
            if body and not hits:
                text_full = "%s %s" % (text, body)
                folded_full = fold(text_full)
                hits = match_companies(text_full, folded_full, watchlist)
                if hits and not item["summary"] and body:
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

        # Classify on title + teaser only. Running the keyword rules over a
        # full article body matches almost every category and badge, which is
        # how one story ends up tagged Logistiek + Personalia + Duurzaamheid.
        cats, badges = classify(fold("%s %s" % (item["title"], item["summary"])))
        cust = [h for h in hits if h in customers]
        pros = [h for h in hits if h not in customers]
        fresh.append({"title": item["title"], "summary": item["summary"] or "",
                      "date": dt.date().isoformat(), "source": item["source"],
                      "sourceLabel": item["sourceLabel"], "url": url,
                      "companies": hits, "customers": cust, "prospects": pros,
                      "categories": cats, "badges": badges,
                      # A customer buying or selling a building is the single
                      # thing this digest exists to surface.
                      "keyDeal": bool(cust) and bool({"sale", "buy"} & set(badges)),
                      "isNew": url not in previous})

    # Carry over previously collected articles that have not aged out yet.
    carried = 0
    articles = {a["url"]: a for a in fresh}
    for url, old in previous.items():
        if url in articles:
            continue
        # Re-validate rather than trusting the stored value. A date written by
        # an older buggy version stays wrong forever otherwise, and a wrong
        # date that looks recent survives every subsequent run.
        stored = old.get("date", "")
        if not (keep_from.isoformat() <= stored <= today.isoformat()):
            stats["stale_dropped"] += 1
            continue
        old = dict(old, isNew=False)
        # Backfill fields for records written before the customer split existed.
        if "customers" not in old:
            hits = old.get("companies", [])
            old["customers"] = [h for h in hits if h in customers]
            old["prospects"] = [h for h in hits if h not in customers]
            old["keyDeal"] = (bool(old["customers"])
                              and bool({"sale", "buy"} & set(old.get("badges", []))))
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

    n_cust = sum(1 for a in out_articles if a.get("customers"))
    n_key = sum(1 for a in out_articles if a.get("keyDeal"))
    print("candidates %d -> new %d, carried over %d, total %d"
          % (len(by_url), len(fresh), carried, len(out_articles)))
    print("  %d customer articles, of which %d are buy/sell deals" % (n_cust, n_key))
    print("  dropped: %d undated, %d outside ingest window, %d no watchlist match,"
          " %d carried records with bad dates | %d pages fetched"
          % (stats["undated"], stats["too_old"], stats["no_match"],
             stats["stale_dropped"], stats["pages"]))

    if out_articles:
        from collections import Counter
        counts = Counter(c for a in out_articles for c in a["companies"])
        suspicious = [(n, k) for n, k in counts.most_common()
                      if k >= 5 and k > 0.4 * len(out_articles)]
        for name, k in suspicious:
            print("::warning::'%s' matched %d of %d articles - likely appearing in "
                  "page furniture rather than the news itself" % (name, k, len(out_articles)))

    dead = [h["label"] for h in health if h["items"] == 0]
    if dead:
        print("::warning::no items from: %s" % ", ".join(dead))
    if len(dead) == len(sources):
        print("::error::every source returned zero items")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
