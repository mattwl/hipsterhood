#!/usr/bin/env python3
"""
scrape_broadsheet.py
====================
Scrapes Broadsheet Melbourne venue pages for suburb + "Updated" timestamps,
then uses the Wayback Machine CDX API to gather historical data back to 2014.

Outputs:
  data/raw/venues_live.json        — current Broadsheet venues (suburb + updated date)
  data/raw/venues_wayback_YYYY.json — archived venues per year (2014-2022)

Run process_scraped_data.py after this to rebuild combined.json.

Usage:
  pip install requests beautifulsoup4 lxml
  python3 scripts/scrape_broadsheet.py

  # Live scrape only (faster, ~10-30 min):
  python3 scripts/scrape_broadsheet.py --live-only

  # Wayback only (use previously saved live data):
  python3 scripts/scrape_broadsheet.py --wayback-only

  # Resume interrupted run:
  python3 scripts/scrape_broadsheet.py --resume
"""

import argparse
import json
import os
import re
import sys
import time
import random
from collections import defaultdict
from datetime import datetime
from pathlib import Path

try:
    import requests
    from bs4 import BeautifulSoup
except ImportError:
    sys.exit("Missing dependencies. Run:  pip install requests beautifulsoup4 lxml")

# Optional: set USE_PLAYWRIGHT = True if requests keeps getting 403 from Broadsheet.
# Requires:  pip install playwright && playwright install chromium
USE_PLAYWRIGHT = False

# ── Paths ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR = Path(__file__).parent
REPO_ROOT  = SCRIPT_DIR.parent
RAW_DIR    = REPO_ROOT / "data" / "raw"
RAW_DIR.mkdir(parents=True, exist_ok=True)

LIVE_OUT       = RAW_DIR / "venues_live.json"
PROGRESS_FILE  = RAW_DIR / "_progress.json"

# ── Broadsheet directory categories to crawl ──────────────────────────────────
# Each tuple: (display name, URL path, sub-type filter or None)
BROADSHEET_CATEGORIES = [
    ("Cafes",          "food-and-drink/directory/cafe",           None),
    ("Restaurants",    "food-and-drink/directory/restaurant",     None),
    ("Bars",           "food-and-drink/directory/bar",            None),
    ("Wine bars",      "food-and-drink/directory/wine-bar",       None),
    ("Pubs",           "food-and-drink/directory/pub",            None),
    ("Shops/Fashion",  "fashion/directory/shop",                  None),
    ("Arts/Culture",   "arts-and-entertainment/directory",        None),
    ("Music venues",   "arts-and-entertainment/directory/music",  None),
]

BASE_URL = "https://www.broadsheet.com.au/melbourne"

# ── Wayback Machine config ────────────────────────────────────────────────────
CDX_API    = "http://web.archive.org/cdx/search/cdx"
WAYBACK    = "https://web.archive.org/web"

# For each target year, grab a snapshot from roughly mid-year
HISTORICAL_YEARS = {
    2014: "20140901",
    2015: "20151001",
    2016: "20161001",
    2017: "20171001",
    2018: "20181001",
    2019: "20191001",
    2020: "20201001",   # Note: during COVID lockdowns
    2021: "20211001",
    2022: "20221001",
    2023: "20231001",
}

# Max venue pages to fetch per year from Wayback (keep runtime manageable)
MAX_WAYBACK_VENUES_PER_YEAR = 300

# ── Month name → number lookup ────────────────────────────────────────────────
MONTH_NAMES = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}

# ── Known Melbourne inner suburbs (for suburb extraction/validation) ───────────
KNOWN_SUBURBS = {
    "abbotsford", "ascot vale", "balaclava", "brunswick", "brunswick east",
    "brunswick west", "carlton", "carlton north", "clifton hill", "coburg",
    "collingwood", "cremorne", "docklands", "elwood", "fitzroy",
    "fitzroy north", "footscray", "glen iris", "glen waverley", "hawthorn",
    "kensington", "malvern", "melbourne", "moonee ponds", "morningside",
    "north melbourne", "northcote", "parkville", "pascoe vale south",
    "port melbourne", "prahran", "richmond", "ringwood", "ripponlea",
    "south melbourne", "south yarra", "southbank", "st kilda", "thornbury",
    "toorak", "williamstown", "windsor", "yarraville",
}


# ── HTTP session ──────────────────────────────────────────────────────────────

def make_session():
    """
    Create a requests Session with browser-like headers.
    Automatically uses cloudscraper (better Cloudflare bypass) if installed.
    Supports BROADSHEET_COOKIE env var for manual cf_clearance injection.
    """
    # Prefer cloudscraper if available
    try:
        import cloudscraper
        s = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "darwin"})
        print("Using cloudscraper for Cloudflare bypass.")
    except ImportError:
        s = requests.Session()

    s.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept":          "text/html,application/xhtml+xml,application/xhtml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-AU,en-GB;q=0.9,en;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
        "DNT":             "1",
        "Connection":      "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Sec-Fetch-Dest":  "document",
        "Sec-Fetch-Mode":  "navigate",
        "Sec-Fetch-Site":  "none",
        "Cache-Control":   "max-age=0",
    })

    # Manual Cloudflare cookie injection: set BROADSHEET_COOKIE=cf_clearance=XXXX
    cookie_str = os.environ.get("BROADSHEET_COOKIE", "")
    if cookie_str:
        for part in cookie_str.split(";"):
            if "=" in part:
                k, v = part.strip().split("=", 1)
                s.cookies.set(k.strip(), v.strip(), domain="www.broadsheet.com.au")
        print(f"Injected cookies from BROADSHEET_COOKIE env var.")

    return s


def fetch_playwright(url):
    """Fetch via a real Chromium browser (bypasses Cloudflare JS challenges)."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return None, -1
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, wait_until="networkidle", timeout=30000)
            html = page.content()
            browser.close()
            return html, 200
    except Exception as e:
        print(f"    Playwright error: {e}")
        return None, -1


def fetch(session, url, retries=4, base_delay=3):
    """
    Fetch a URL with exponential backoff.
    Returns (response_text, status_code) or (None, error_code).
    """
    if USE_PLAYWRIGHT:
        return fetch_playwright(url)

    delay = base_delay
    for attempt in range(retries):
        try:
            r = session.get(url, timeout=20, allow_redirects=True)
            if r.status_code == 200:
                return r.text, 200
            elif r.status_code == 429:
                wait = delay * 2
                print(f"    Rate-limited (429). Waiting {wait}s …")
                time.sleep(wait)
                delay *= 2
            elif r.status_code == 403:
                print(f"    403 Forbidden — Cloudflare may be blocking.")
                print("    → If this keeps happening, see SCRAPING_GUIDE.txt §Troubleshooting")
                if attempt < retries - 1:
                    time.sleep(delay + random.uniform(1, 3))
                    delay *= 2
            else:
                return None, r.status_code
        except requests.exceptions.ConnectionError as e:
            print(f"    Connection error: {e}")
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
        except requests.exceptions.Timeout:
            print(f"    Timeout. Retrying in {delay}s …")
            if attempt < retries - 1:
                time.sleep(delay)
                delay *= 2
    return None, -1


# ── Broadsheet page parsers ───────────────────────────────────────────────────

def parse_updated_date(soup):
    """
    Extract the "Updated: DD Month YYYY" date from a Broadsheet venue page.
    Returns a datetime or None.
    """
    # Strategy 1: look for visible "Updated:" text
    for tag in soup.find_all(string=re.compile(r"Updated\s*:", re.I)):
        m = re.search(
            r"Updated\s*:?\s*(\d{1,2})\s+(\w+)\s+(\d{4})",
            str(tag), re.I
        )
        if m:
            try:
                day   = int(m.group(1))
                month = MONTH_NAMES.get(m.group(2).lower())
                year  = int(m.group(3))
                if month and 2010 <= year <= 2030:
                    return datetime(year, month, day)
            except ValueError:
                pass

    # Strategy 2: JSON-LD structured data (dateModified / datePublished)
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            for key in ("dateModified", "datePublished", "dateCreated"):
                val = data.get(key, "")
                if val:
                    # ISO 8601: 2023-04-06T10:00:00+11:00
                    m2 = re.match(r"(\d{4})-(\d{2})-(\d{2})", val)
                    if m2:
                        return datetime(int(m2.group(1)), int(m2.group(2)), int(m2.group(3)))
        except (json.JSONDecodeError, AttributeError):
            pass

    # Strategy 3: <meta> tags
    for meta in soup.find_all("meta"):
        name = (meta.get("property") or meta.get("name") or "").lower()
        if any(k in name for k in ("modified", "published", "updated")):
            content = meta.get("content", "")
            m3 = re.match(r"(\d{4})-(\d{2})-(\d{2})", content)
            if m3:
                return datetime(int(m3.group(1)), int(m3.group(2)), int(m3.group(3)))

    # Strategy 4: any element with itemprop="dateModified"
    tag = soup.find(itemprop="dateModified") or soup.find(itemprop="datePublished")
    if tag:
        val = tag.get("content") or tag.get_text()
        m4 = re.search(r"(\d{4})-(\d{2})-(\d{2})", val)
        if m4:
            return datetime(int(m4.group(1)), int(m4.group(2)), int(m4.group(3)))

    return None


def parse_suburb(soup, url=""):
    """
    Extract Melbourne suburb from a Broadsheet venue page.
    Returns a suburb string (title-cased) or None.
    """
    # Strategy 1: structured data address
    for script in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(script.string or "")
            addr = data.get("address") or {}
            if isinstance(addr, dict):
                suburb = (
                    addr.get("addressLocality")
                    or addr.get("addressRegion")
                    or ""
                ).strip()
                if suburb:
                    return suburb.title()
        except (json.JSONDecodeError, AttributeError):
            pass

    # Strategy 2: look for suburb in address block text
    addr_patterns = [
        r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)?)\s+VIC\b',
        r'\b([A-Z][a-z]+(?: [A-Z][a-z]+)?)\s+\d{4}\b',
    ]
    full_text = soup.get_text(" ", strip=True)
    for pat in addr_patterns:
        m = re.search(pat, full_text)
        if m:
            candidate = m.group(1).lower()
            if candidate in KNOWN_SUBURBS:
                return m.group(1).title()

    # Strategy 3: breadcrumb navigation often contains suburb
    for crumb in soup.find_all(["nav", "ol", "ul"], class_=re.compile(r"breadcrumb", re.I)):
        items = [a.get_text(strip=True) for a in crumb.find_all("a")]
        for item in items:
            if item.lower() in KNOWN_SUBURBS:
                return item.title()

    # Strategy 4: look for suburb-tagged elements
    for el in soup.find_all(class_=re.compile(r"suburb|location|address", re.I)):
        text = el.get_text(" ", strip=True).lower()
        for s in KNOWN_SUBURBS:
            if s in text:
                return s.title()

    # Strategy 5: check URL slug for suburb hint
    url_lower = url.lower()
    for s in sorted(KNOWN_SUBURBS, key=len, reverse=True):
        if s.replace(" ", "-") in url_lower or s.replace(" ", "_") in url_lower:
            return s.title()

    return None


def discover_venue_urls(session, category_path, base=BASE_URL, max_pages=50):
    """
    Walk paginated Broadsheet directory pages and return venue URLs.
    Broadsheet uses ?page=N or loads more via JS (we try both).
    """
    urls = set()
    print(f"  Discovering: /{category_path}")

    # Try page-based pagination
    for page in range(1, max_pages + 1):
        if page == 1:
            url = f"{base}/{category_path}"
        else:
            url = f"{base}/{category_path}?page={page}"

        html, status = fetch(session, url)
        if not html or status != 200:
            break

        soup = BeautifulSoup(html, "lxml")

        # Find venue links (typically /melbourne/.../directory/[type]/[slug])
        found_this_page = 0
        for a in soup.find_all("a", href=True):
            href = a["href"]
            # Venue pages look like: /melbourne/.../directory/[type]/[slug]
            if re.search(r"/melbourne/.*?/directory/[^/]+/[^/?#]+$", href):
                full = "https://www.broadsheet.com.au" + href if href.startswith("/") else href
                if full not in urls:
                    urls.add(full)
                    found_this_page += 1

        print(f"    Page {page}: found {found_this_page} new venue links (total: {len(urls)})")

        if found_this_page == 0:
            break

        # Polite delay between pages
        time.sleep(random.uniform(1.5, 3.0))

    return list(urls)


def scrape_venue_page(session, url):
    """
    Fetch a single venue page and return a dict with name, suburb, updated_date.
    Returns None on failure.
    """
    html, status = fetch(session, url)
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")

    # Venue name: try h1 or title
    name = None
    h1 = soup.find("h1")
    if h1:
        name = h1.get_text(strip=True)
    if not name:
        title = soup.find("title")
        if title:
            name = title.get_text(strip=True).split("|")[0].strip()

    suburb      = parse_suburb(soup, url)
    updated     = parse_updated_date(soup)
    updated_iso = updated.strftime("%Y-%m-%d") if updated else None

    if not suburb:
        return None   # Can't use this venue without a suburb

    return {
        "name":        name or url.split("/")[-1].replace("-", " ").title(),
        "suburb":      suburb,
        "updated":     updated_iso,
        "url":         url,
    }


# ── Live scrape ───────────────────────────────────────────────────────────────

def run_live_scrape(session, resume=False):
    """Scrape current Broadsheet Melbourne directory. Returns list of venue dicts."""
    # Load progress if resuming
    seen_urls  = set()
    venues     = []

    if resume and PROGRESS_FILE.exists():
        with open(PROGRESS_FILE) as f:
            prog = json.load(f)
            seen_urls = set(prog.get("seen_urls", []))
            venues    = prog.get("venues", [])
        print(f"Resuming: {len(venues)} venues already scraped, {len(seen_urls)} URLs seen.")

    # Discover venue URLs across all categories
    all_venue_urls = set()
    for display, path, _ in BROADSHEET_CATEGORIES:
        print(f"\n{'─'*60}")
        print(f"Category: {display}")
        cat_urls = discover_venue_urls(session, path)
        all_venue_urls.update(cat_urls)

    new_urls = [u for u in all_venue_urls if u not in seen_urls]
    print(f"\n{'─'*60}")
    print(f"Total venue URLs found: {len(all_venue_urls)}  ({len(new_urls)} not yet scraped)")

    # Scrape each venue page
    for i, url in enumerate(new_urls, 1):
        slug = url.split("/")[-1]
        print(f"  [{i}/{len(new_urls)}] {slug[:50]:<50}", end=" ", flush=True)

        venue = scrape_venue_page(session, url)
        seen_urls.add(url)

        if venue:
            venues.append(venue)
            print(f"  {venue['suburb']:<20}  {venue.get('updated', 'no date')}")
        else:
            print("  (skipped — no suburb found)")

        # Save progress every 50 venues
        if i % 50 == 0:
            with open(PROGRESS_FILE, "w") as f:
                json.dump({"seen_urls": list(seen_urls), "venues": venues}, f)
            print(f"  ── Progress saved ({len(venues)} venues) ──")

        # Polite delay
        time.sleep(random.uniform(1.0, 2.5))

    print(f"\nLive scrape complete: {len(venues)} venues with suburb data")

    # Save final result
    with open(LIVE_OUT, "w") as f:
        json.dump(venues, f, indent=2)
    print(f"Written: {LIVE_OUT}")

    # Clean up progress file
    if PROGRESS_FILE.exists():
        PROGRESS_FILE.unlink()

    return venues


# ── Wayback Machine scrape ────────────────────────────────────────────────────

def cdx_search(url_pattern, from_ts, to_ts, limit=500):
    """
    Query the Wayback Machine CDX API for archived Broadsheet venue pages.
    Returns list of (timestamp, original_url) tuples.
    """
    params = {
        "url":      url_pattern,
        "output":   "json",
        "fl":       "timestamp,original",
        "from":     from_ts,
        "to":       to_ts,
        "limit":    limit,
        "collapse": "urlkey",            # one snapshot per unique URL
        "filter":   "statuscode:200",
        "matchType":"prefix",
    }
    try:
        r = requests.get(CDX_API, params=params, timeout=30)
        r.raise_for_status()
        rows = r.json()
        if len(rows) < 2:
            return []
        return [(row[0], row[1]) for row in rows[1:]]  # skip header row
    except Exception as e:
        print(f"    CDX API error: {e}")
        return []


def scrape_wayback_year(session, year):
    """
    For a given year, find archived Broadsheet Melbourne venue pages
    via the CDX API and scrape suburb + date from each.
    Returns list of venue dicts.
    """
    out_file = RAW_DIR / f"venues_wayback_{year}.json"
    if out_file.exists():
        print(f"  Year {year}: already scraped ({out_file.name}), skipping.")
        with open(out_file) as f:
            return json.load(f)

    target_ts = HISTORICAL_YEARS[year]
    from_ts = str(year) + "0101"
    to_ts   = str(year) + "1231"

    print(f"\n{'─'*60}")
    print(f"Year {year} — querying Wayback CDX API …")

    # Query for all venue directory pages from this year
    pattern = "broadsheet.com.au/melbourne/*/directory/*/*"
    snapshots = cdx_search(pattern, from_ts, to_ts, limit=MAX_WAYBACK_VENUES_PER_YEAR)

    if not snapshots:
        print(f"  No CDX results for {year}.")
        return []

    print(f"  Found {len(snapshots)} archived venue pages for {year}")

    venues = []
    for i, (ts, orig_url) in enumerate(snapshots, 1):
        # Fetch the archived version
        archived_url = f"{WAYBACK}/{ts}/{orig_url}"
        slug = orig_url.split("/")[-1]
        print(f"  [{i}/{len(snapshots)}] {slug[:50]:<50}", end=" ", flush=True)

        html, status = fetch(session, archived_url, retries=3, base_delay=2)
        if not html:
            print(f"  (fetch failed, status={status})")
            continue

        soup = BeautifulSoup(html, "lxml")
        suburb  = parse_suburb(soup, orig_url)
        updated = parse_updated_date(soup)

        if not suburb:
            print("  (no suburb)")
            continue

        updated_iso = updated.strftime("%Y-%m-%d") if updated else None
        name_tag = soup.find("h1")
        name = name_tag.get_text(strip=True) if name_tag else slug.replace("-", " ").title()

        venues.append({
            "name":         name,
            "suburb":       suburb,
            "updated":      updated_iso,
            "archived_ts":  ts,
            "url":          orig_url,
        })
        print(f"  {suburb:<20}  {updated_iso or 'no date'}")

        # Polite delay — Wayback Machine asks for ≥1 req/sec
        time.sleep(random.uniform(1.0, 2.0))

    print(f"  Year {year}: {len(venues)} venues scraped")
    with open(out_file, "w") as f:
        json.dump(venues, f, indent=2)
    print(f"  Written: {out_file}")

    return venues


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="Scrape Broadsheet Melbourne venue data")
    parser.add_argument("--live-only",    action="store_true", help="Only scrape live Broadsheet (skip Wayback)")
    parser.add_argument("--wayback-only", action="store_true", help="Only scrape Wayback (skip live Broadsheet)")
    parser.add_argument("--resume",       action="store_true", help="Resume interrupted live scrape")
    parser.add_argument("--years",        nargs="+", type=int,
                        help=f"Which historical years to fetch (default: all {list(HISTORICAL_YEARS.keys())})")
    args = parser.parse_args()

    session = make_session()

    # ── Phase 1: Live Broadsheet ──
    if not args.wayback_only:
        print("\n" + "="*60)
        print("PHASE 1: Live Broadsheet Melbourne scrape")
        print("="*60)

        # Warm up the session — visit the homepage first to get cookies
        print("Warming up session (visiting homepage) …")
        html, status = fetch(session, "https://www.broadsheet.com.au/melbourne")
        if status != 200:
            print(f"\n⚠️  Could not reach Broadsheet (status {status}).")
            print("   See SCRAPING_GUIDE.txt §Troubleshooting for help.")
            if not args.live_only:
                print("   Continuing with Wayback Machine scrape …")
            else:
                sys.exit(1)
        else:
            run_live_scrape(session, resume=args.resume)

    # ── Phase 2: Wayback Machine historical data ──
    if not args.live_only:
        print("\n" + "="*60)
        print("PHASE 2: Wayback Machine historical scrape")
        print("="*60)

        years = args.years or list(HISTORICAL_YEARS.keys())
        for year in sorted(years):
            scrape_wayback_year(session, year)
            # Longer pause between years to be respectful of Wayback
            time.sleep(3)

    print("\n" + "="*60)
    print("Scraping complete!")
    print("="*60)
    print("\nNext step: run process_scraped_data.py to rebuild the visualisation data:")
    print("  python3 scripts/process_scraped_data.py")


if __name__ == "__main__":
    main()
