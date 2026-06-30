# v: bsh-src-04
"""
Broadsheet — source layer.
Fetches the 15 verified RSS feeds and normalises them into a clean article list.
Each feed is tagged region / category / source_type — these tags are the DIMENSIONS
the reader-preference model learns on later.

Pure stdlib + feedparser. No API key needed (RSS is public).
"""

import feedparser
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# The 15 verified-live feeds (tested 2026-06-26). Tags drive the preference model.
FEEDS = [
    # name, url, region, category, source_type
    # English-language sources spread across continents (deliberately NOT US-centric).
    # ---- WORLD ----
    ("BBC World",        "https://feeds.bbci.co.uk/news/world/rss.xml",                      "uk",        "world",    "wire"),         # UK
    ("Al Jazeera",       "https://www.aljazeera.com/xml/rss/all.xml",                        "qatar",     "world",    "broadcaster"),  # Qatar
    ("Deutsche Welle",   "https://rss.dw.com/rdf/rss-en-all",                                "germany",   "world",    "public"),       # Germany
    ("SCMP",             "https://www.scmp.com/rss/91/feed",                                 "hongkong",  "world",    "newspaper"),    # Hong Kong
    ("CGTN China",       "https://www.cgtn.com/subscribe/rss/section/china.xml",             "china",     "world",    "broadcaster"),  # mainland China
    ("The Guardian",     "https://www.theguardian.com/world/rss",                            "uk",        "world",    "newspaper"),    # UK
    # ---- TECH ----
    ("The Register",     "https://www.theregister.com/headlines.atom",                       "uk",        "tech",     "analysis"),     # UK
    ("Rest of World",    "https://restofworld.org/feed/latest/",                             "global",    "tech",     "analysis"),     # Global South focus
    ("The Verge",        "https://www.theverge.com/rss/index.xml",                           "us",        "tech",     "product-news"), # US (one kept)
    ("Hacker News",      "https://hnrss.org/frontpage",                                      "global",    "tech",     "community"),    # Global community
    ("CGTN Sci-Tech",    "https://www.cgtn.com/subscribe/rss/section/tech-sci.xml",          "china",     "tech",     "broadcaster"),  # mainland China
    # ---- BUSINESS / MARKETS ----
    ("Nikkei Asia",      "https://asia.nikkei.com/rss/feed/nar",                             "japan",     "business", "newspaper"),    # Japan
    ("Economic Times",   "https://economictimes.indiatimes.com/rssfeedstopstories.cms",      "india",     "business", "newspaper"),    # India
    ("CoinDesk",         "https://www.coindesk.com/arc/outboundfeeds/rss/",                  "global",    "business", "crypto"),       # Crypto, global
    ("CGTN Business",    "https://www.cgtn.com/subscribe/rss/section/business.xml",          "china",     "business", "broadcaster"),  # mainland China
    # ---- SPORT ----
    ("BBC Sport",        "https://feeds.bbci.co.uk/sport/rss.xml",                           "uk",        "sport",    "wire"),         # UK
    ("ABC Sport (AU)",   "https://www.abc.net.au/news/feed/45924/rss.xml",                   "australia", "sport",    "broadcaster"),  # Australia
    # ---- SCIENCE ----
    ("ScienceDaily",     "https://www.sciencedaily.com/rss/all.xml",                         "us",        "science",  "research"),     # US
    ("Phys.org",         "https://phys.org/rss-feed/",                                       "global",    "science",  "research"),     # Global
    # ---- CULTURE / ENTERTAINMENT ----
    ("Guardian Film",    "https://www.theguardian.com/film/rss",                             "uk",        "culture",  "newspaper"),    # UK
    ("Screen Daily",     "https://www.screendaily.com/rss",                                  "uk",        "culture",  "trade"),        # UK (intl film biz)
    ("NDTV Entertainment","https://feeds.feedburner.com/ndtvmovies-latest",                  "india",     "culture",  "broadcaster"),  # India
    ("CGTN Culture",     "https://www.cgtn.com/subscribe/rss/section/culture.xml",           "china",     "culture",  "broadcaster"),  # mainland China
]


def _parse_date(entry):
    """Best-effort published datetime (UTC). Returns None if unparseable."""
    for attr in ("published", "updated", "pubDate"):
        val = entry.get(attr)
        if val:
            try:
                dt = parsedate_to_datetime(val)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.astimezone(timezone.utc)
            except Exception:
                pass
    # feedparser also exposes a parsed struct_time
    for attr in ("published_parsed", "updated_parsed"):
        st = entry.get(attr)
        if st:
            try:
                return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc)
            except Exception:
                pass
    return None


def _clean_summary(entry):
    """Short text summary, stripped of HTML noise."""
    raw = entry.get("summary", "") or ""
    # crude tag strip — good enough for synthesis input
    import re
    text = re.sub(r"<[^>]+>", " ", raw)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:400]  # cap; synthesis doesn't need full bodies


def fetch_feed(name, url, region, category, source_type, timeout=20):
    """Fetch one feed, return list of normalised article dicts (or [] on failure)."""
    try:
        d = feedparser.parse(url)
    except Exception as e:
        print(f"  [FAIL] {name}: {e}")
        return []
    if getattr(d, "bozo", 0) and not d.entries:
        print(f"  [FAIL] {name}: no entries (bozo={d.bozo})")
        return []
    out = []
    for e in d.entries:
        title = (e.get("title") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "summary": _clean_summary(e),
            "link": e.get("link", ""),
            "published": _parse_date(e),
            "source": name,
            "region": region,
            "category": category,
            "source_type": source_type,
        })
    return out


def fetch_all(max_per_feed=8):
    """Fetch all feeds. Returns (articles, report)."""
    all_articles = []
    report = []
    for (name, url, region, category, source_type) in FEEDS:
        arts = fetch_feed(name, url, region, category, source_type)
        arts = arts[:max_per_feed]
        all_articles.append((name, arts))
        report.append((name, len(arts), region, category, source_type))
    # flatten
    flat = [a for (_, arts) in all_articles for a in arts]
    return flat, report


if __name__ == "__main__":
    print("Fetching 15 feeds...\n")
    articles, report = fetch_all(max_per_feed=8)
    print(f"{'SOURCE':<18}{'#':>4}  {'REGION':<14}{'CATEGORY':<10}{'TYPE'}")
    print("-" * 70)
    ok = 0
    for (name, n, region, category, source_type) in report:
        flag = "OK " if n > 0 else "ZERO"
        if n > 0:
            ok += 1
        print(f"{name:<18}{n:>4}  {region:<14}{category:<10}{source_type}   {flag}")
    print("-" * 70)
    print(f"{ok}/15 feeds returned articles. Total: {len(articles)} articles.\n")

    # Show a few sample articles
    print("Sample articles:")
    for a in articles[:5]:
        pub = a["published"].strftime("%Y-%m-%d %H:%M") if a["published"] else "no-date"
        print(f"  [{a['source']} | {a['category']}] {a['title'][:70]}  ({pub})")
