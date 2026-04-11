#!/usr/bin/env python3
"""
process_scraped_data.py
=======================
Reads raw venue JSON files produced by scrape_broadsheet.py and
rebuilds data/combined.json (and per-year snapshots) with real
Broadsheet data.

Usage:
  python3 scripts/process_scraped_data.py

The script merges:
  • data/raw/venues_live.json          — current Broadsheet venues
  • data/raw/venues_wayback_YYYY.json  — archived venues per year

Any years without scraped data fall back to the logistic interpolation
from generate_snapshots.py (i.e. the curated estimates).
"""

import json
import os
import re
from collections import defaultdict
from datetime import datetime
from pathlib import Path

SCRIPT_DIR   = Path(__file__).parent
REPO_ROOT    = SCRIPT_DIR.parent
RAW_DIR      = REPO_ROOT / "data" / "raw"
SNAP_DIR     = REPO_ROOT / "data" / "snapshots"
COMBINED_OUT = REPO_ROOT / "data" / "combined.json"
GEOJSON_PATH = REPO_ROOT / "data" / "melbourne-suburbs.geojson"

SNAP_DIR.mkdir(parents=True, exist_ok=True)

YEARS = [2014, 2016, 2018, 2020, 2022, 2024, 2026]

# Colour palette (Tableau-inspired, rank-ordered so top suburbs are vibrant)
COLORS = [
    "#e15759", "#f28e2b", "#76b7b2", "#59a14f", "#edc948",
    "#b07aa1", "#ff9da7", "#9c755f", "#17becf", "#4e79a7",
    "#f1ce63", "#d37295", "#a0cbe8", "#86bcb6", "#8cd17d",
    "#499894", "#e6845e", "#d4a6c8", "#ffbe7d", "#c9d02c",
    "#ffa15a", "#19d3f3", "#ff6692", "#b6e880", "#ff97ff",
    "#fecb52", "#c73f0a", "#7da8a8", "#a9c574", "#72b7b2",
]


# ── Suburb name normalisation ─────────────────────────────────────────────────

SUBURB_ALIASES = {
    "cbd":             "Melbourne",
    "central melbourne": "Melbourne",
    "city":            "Melbourne",
    "city of melbourne": "Melbourne",
    "north fitzroy":   "Fitzroy North",
    "east brunswick":  "Brunswick East",
    "west brunswick":  "Brunswick West",
    "south st kilda":  "St Kilda",
    "north richmond":  "Richmond",
    "east richmond":   "Richmond",
    "west richmond":   "Richmond",
    "south collingwood": "Collingwood",
    "east collingwood":  "Collingwood",
}

def normalise_suburb(name: str) -> str:
    if not name:
        return ""
    low = name.strip().lower()
    return SUBURB_ALIASES.get(low, name.strip().title())


# ── Score calculation ─────────────────────────────────────────────────────────

def venues_to_year_scores(venues: list, target_year: int) -> dict:
    """
    Given a list of venue dicts {suburb, updated, ...}, compute a raw
    hipster score for each suburb for `target_year`.

    Scoring rules:
      - Base: every venue updated in [target_year-1 .. target_year+1]
        counts as 10 points for its suburb.
      - Recency bonus: updated exactly in target_year → ×1.2 multiplier.
      - Recency penalty: updated 2+ years before target_year → ×0.5
        (venue was notable but may have faded).
      - Venues with no updated date: counted at half weight if their
        source year matches target_year.

    Returns {suburb: raw_score}
    """
    scores = defaultdict(float)

    for v in venues:
        suburb = normalise_suburb(v.get("suburb", ""))
        if not suburb:
            continue

        updated_str = v.get("updated")
        if updated_str:
            try:
                updated_year = int(updated_str[:4])
            except (ValueError, TypeError):
                updated_year = None
        else:
            updated_year = None

        # Determine recency weight
        if updated_year is None:
            weight = 2.5
        elif updated_year == target_year:
            weight = 12.0
        elif abs(updated_year - target_year) == 1:
            weight = 8.0
        elif abs(updated_year - target_year) == 2:
            weight = 5.0
        elif abs(updated_year - target_year) <= 4:
            weight = 3.0
        else:
            weight = 1.0

        # Apply venue-type multiplier if present (from hotlist)
        weight *= v.get("type_weight", 1.0)

        scores[suburb] += weight

    return dict(scores)


def normalise(scores: dict) -> dict:
    """Distribute 100 points proportionally across suburbs (pool-based index)."""
    if not scores:
        return {}
    total = sum(scores.values()) or 1
    return {k: round(v / total * 100, 1) for k, v in scores.items()}


# Hipster weight multiplier per venue type from the hotlist
VENUE_TYPE_WEIGHTS = {
    "cafe":       1.3,
    "bar":        1.4,
    "wine bar":   1.5,
    "shop":       1.3,
    "restaurant": 1.0,
}

MONTH_MAP = {
    "january": 1, "february": 2, "march": 3, "april": 4,
    "may": 5, "june": 6, "july": 7, "august": 8,
    "september": 9, "october": 10, "november": 11, "december": 12,
}

def parse_hotlist_date(date_str: str) -> str | None:
    """Convert 'DD Month YYYY' → 'YYYY' year string."""
    if not date_str:
        return None
    parts = date_str.strip().split()
    for part in parts:
        if len(part) == 4 and part.isdigit():
            return part
    return None


def load_hotlist() -> list:
    """
    Load broadsheet_melbourne_hotlist.json and convert to standard venue format.
    Returns list of {suburb, updated, type_weight} dicts.
    """
    hotlist_path = RAW_DIR / "broadsheet_melbourne_hotlist.json"
    if not hotlist_path.exists():
        return []
    with open(hotlist_path) as f:
        raw = json.load(f)
    venues = []
    for v in raw:
        suburb = normalise_suburb(v.get("suburb", ""))
        if not suburb:
            continue
        updated = parse_hotlist_date(v.get("updatedDate", ""))
        vtype = (v.get("type") or "").strip().lower()
        type_weight = VENUE_TYPE_WEIGHTS.get(vtype, 1.0)
        venues.append({
            "suburb":      suburb,
            "updated":     updated,
            "type_weight": type_weight,
        })
    print(f"Loaded {len(venues)} venues from broadsheet_melbourne_hotlist.json")
    return venues


# ── Load raw data ─────────────────────────────────────────────────────────────

def load_raw_venues() -> dict:
    """
    Load all raw venue JSON files.
    Returns {year_or_'live': [venue, ...]}
    """
    data = {}

    # Hotlist (hand-curated current venues) — primary 2026 source
    hotlist = load_hotlist()

    # Live scraped file (may be empty if scrape was blocked)
    live_venues = []
    live_path = RAW_DIR / "venues_live.json"
    if live_path.exists():
        with open(live_path) as f:
            raw_live = json.load(f)
        if raw_live:
            live_venues = raw_live
            print(f"Loaded {len(live_venues)} live scraped venues")

    # Merge: hotlist + any live scrape results
    data["live"] = hotlist + live_venues
    if not data["live"]:
        print("No live venues found — will fall back to interpolated estimates")

    # Wayback files
    for p in sorted(RAW_DIR.glob("venues_wayback_*.json")):
        m = re.search(r"(\d{4})", p.name)
        if m:
            year = int(m.group(1))
            with open(p) as f:
                venues = json.load(f)
            data[year] = venues
            print(f"Loaded {len(venues)} Wayback venues for {year}")

    return data


def load_kimonolabs_2014() -> dict | None:
    """
    Load the real 2014 Kimonolabs snapshot if it exists and has real source data.
    Returns {suburb: pool_normalised_score} or None.
    """
    snap_2014 = SNAP_DIR / "2014.json"
    if not snap_2014.exists():
        return None
    with open(snap_2014) as f:
        d = json.load(f)
    if "Kimonolabs" not in d.get("source", ""):
        return None
    raw = {s["name"]: s["score"] for s in d.get("suburbs", [])}
    # Re-pool-normalise (original scores are max-normalised to 100)
    total = sum(raw.values()) or 1
    normalised = {k: round(v / total * 100, 1) for k, v in raw.items()}
    print(f"  Loaded real 2014 Kimonolabs data ({len(normalised)} suburbs)")
    return normalised


def load_existing_combined() -> dict | None:
    """Load existing combined.json as fallback for years without scraped data."""
    if COMBINED_OUT.exists():
        with open(COMBINED_OUT) as f:
            return json.load(f)
    return None


# ── Build per-year scores ─────────────────────────────────────────────────────

def build_scores_for_year(year: int, raw: dict, existing: dict | None,
                          kimonolabs_2014: dict | None = None) -> dict:
    """
    Return {suburb: normalised_score} for `year`, using real scraped data
    where available, falling back to existing interpolated data.
    """
    # 2014: use real Kimonolabs data if available
    if year == 2014 and kimonolabs_2014:
        print(f"  Year 2014: {len(kimonolabs_2014)} suburbs from Kimonolabs scrapes (real data)")
        return kimonolabs_2014, "kimonolabs_2014"

    venues_for_year = []

    # Live file covers 2023-2026
    if year >= 2023:
        venues_for_year.extend(raw.get("live", []))

    # Wayback file for this year
    if year in raw:
        venues_for_year.extend(raw[year])

    # Adjacent wayback year (±2 years) if nothing exact
    if not venues_for_year:
        for delta in [1, 2, 3]:
            for y in [year - delta, year + delta]:
                if y in raw and raw[y]:
                    print(f"  Year {year}: no direct data, using {y} data as proxy")
                    venues_for_year = raw[y]
                    break
            if venues_for_year:
                break

    if venues_for_year:
        raw_scores   = venues_to_year_scores(venues_for_year, year)
        norm_scores  = normalise(raw_scores)
        source       = "broadsheet_scraped"
        print(f"  Year {year}: {len(norm_scores)} suburbs from {len(venues_for_year)} venues (scraped)")
    elif existing:
        # Fall back to interpolated data
        norm_scores = {}
        for s in existing.get("suburbs", []):
            idx = existing["years"].index(year) if year in existing["years"] else None
            if idx is not None:
                norm_scores[s["name"]] = s["scores"][idx]
        source = "interpolated_fallback"
        print(f"  Year {year}: using interpolated fallback ({len(norm_scores)} suburbs)")
    else:
        norm_scores = {}
        source      = "no_data"
        print(f"  Year {year}: NO DATA")

    return norm_scores, source


# ── Write outputs ─────────────────────────────────────────────────────────────

def write_snapshots(all_scores: dict, sources: dict):
    """Write per-year snapshot JSON files."""
    source_descriptions = {
        "kimonolabs_2014":   "Broadsheet Melbourne via Kimonolabs weekly scrapes (Jul–Dec 2014)",
        "broadsheet_scraped": "Broadsheet Melbourne venue pages (scraped, suburb + Updated date)",
        "interpolated_fallback": "Logistic interpolation (no scraped data for this year)",
        "no_data": "No data available",
    }
    prev = None
    for year in YEARS:
        scores = all_scores.get(year, {})
        src    = sources.get(year, "unknown")

        suburbs_list = []
        for name, score in sorted(scores.items(), key=lambda x: -x[1]):
            trend = None
            if prev and name in prev:
                delta = score - prev[name]
                trend = "rising" if delta > 3 else ("falling" if delta < -3 else "stable")
            suburbs_list.append({"name": name, "score": score, "trend": trend})

        out = {
            "year":        year,
            "source":      source_descriptions.get(src, src),
            "methodology": (
                "10–12 pts per venue Updated in this year, 8 pts ±1 year, "
                "5 pts ±2 years, 3 pts ±4 years; normalised 0–100"
                if src == "broadsheet_scraped"
                else "Logistic interpolation; normalised 0–100"
            ),
            "suburbs":     suburbs_list,
        }
        path = SNAP_DIR / f"{year}.json"
        with open(path, "w") as f:
            json.dump(out, f, indent=2)
        print(f"  Written: {path}")
        prev = scores


def write_combined(all_scores: dict, sources: dict):
    """Write data/combined.json."""
    all_suburbs = set()
    for scores in all_scores.values():
        all_suburbs.update(scores.keys())
    all_suburbs = sorted(all_suburbs)

    suburb_entries = []
    for name in all_suburbs:
        score_list = [all_scores.get(y, {}).get(name, 0.0) for y in YEARS]
        if max(score_list) < 3:
            continue

        s14, s26 = score_list[0], score_list[-1]
        if s14 < 1:
            trend = "rising" if s26 > 5 else "stable"
        elif s26 > s14 * 1.45:
            trend = "rising"
        elif s26 < s14 * 0.82:
            trend = "falling"
        else:
            trend = "stable"

        suburb_entries.append({
            "name":   name,
            "scores": score_list,
            "trend":  trend,
            "type":   "cbd" if name == "Melbourne" else "suburb",
            "color":  "",  # assigned after sorting
        })

    suburb_entries.sort(key=lambda x: -sum(x["scores"]))
    for i, s in enumerate(suburb_entries):
        s["color"] = COLORS[i % len(COLORS)]

    # Build source summary
    scraped_years  = [y for y, s in sources.items() if s == "broadsheet_scraped"]
    fallback_years = [y for y, s in sources.items() if s == "interpolated_fallback"]

    combined = {
        "years":   YEARS,
        "suburbs": suburb_entries,
        "sources": {
            "primary":   "Broadsheet Melbourne (venue pages, suburb + Updated date)",
            "scraped":   scraped_years,
            "fallback":  fallback_years,
            "method":    (
                "Venues weighted by recency of Broadsheet 'Updated' timestamp relative "
                "to each year; per-suburb scores normalised 0–100 annually"
            ),
        },
    }

    with open(COMBINED_OUT, "w") as f:
        json.dump(combined, f, indent=2)
    print(f"\nWritten: {COMBINED_OUT} ({len(suburb_entries)} suburbs)")


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    print("Loading raw scraped venue data …")
    raw      = load_raw_venues()
    existing = load_existing_combined()

    total_raw = sum(len(v) for k, v in raw.items())
    if total_raw == 0:
        print("\n⚠️  No scraped data found in data/raw/")
        print("   Run  python3 scripts/scrape_broadsheet.py  first.")
        return

    kimonolabs_2014 = load_kimonolabs_2014()

    print(f"\nBuilding scores for years: {YEARS}")
    all_scores = {}
    sources    = {}

    for year in YEARS:
        scores, src = build_scores_for_year(year, raw, existing, kimonolabs_2014)
        all_scores[year] = scores
        sources[year]    = src

    print("\nWriting per-year snapshots …")
    write_snapshots(all_scores, sources)

    print("\nBuilding combined.json …")
    write_combined(all_scores, sources)

    # Print top suburbs table
    print("\n── Top 12 suburbs by 2026 score (real Broadsheet data) ──────────")
    with open(COMBINED_OUT) as f:
        combined = json.load(f)
    year_idx = combined["years"].index(2026)
    top = sorted(combined["suburbs"], key=lambda s: -s["scores"][year_idx])[:12]
    print(f"{'Suburb':25s}  {'2014':>6}  {'2018':>6}  {'2022':>6}  {'2026':>6}  Trend")
    print("─" * 65)
    for s in top:
        sc = s["scores"]
        idx14 = combined["years"].index(2014)
        idx18 = combined["years"].index(2018)
        idx22 = combined["years"].index(2022)
        print(f"{s['name']:25s}  {sc[idx14]:6.1f}  {sc[idx18]:6.1f}  {sc[idx22]:6.1f}  {sc[year_idx]:6.1f}  {s['trend']}")

    print("\nDone. Reload your browser to see the updated visualisation.")


if __name__ == "__main__":
    main()
