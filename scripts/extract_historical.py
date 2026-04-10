#!/usr/bin/env python3
"""
extract_historical.py

Extracts the embedded rawData from hipstermeter.html (Kimonolabs scrapes,
Jul 2014 – Feb 2015) and converts it into the canonical snapshot JSON
format used by the modernised app.

Output: ../data/snapshots/2014.json
"""

import json
import re
import os
from collections import defaultdict

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT  = os.path.dirname(SCRIPT_DIR)
SRC_HTML   = os.path.join(REPO_ROOT, "hipstermeter.html")
OUT_FILE   = os.path.join(REPO_ROOT, "data", "snapshots", "2014.json")


def extract_raw_data(html_path: str) -> list:
    """Pull the rawData JS array out of the HTML and parse it as JSON."""
    with open(html_path, "r", encoding="utf-8") as fh:
        html = fh.read()

    # Locate the start of the outer "[" after "var rawData = "
    start_marker = "var rawData = ["
    start = html.index(start_marker) + len(start_marker) - 1  # points at "["

    # Walk forward counting bracket depth to find the matching "]"
    depth = 0
    end = start
    for i, ch in enumerate(html[start:], start):
        if ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
            if depth == 0:
                end = i + 1
                break

    raw_json = html[start:end]
    return json.loads(raw_json)


def process_raw_data(raw_data: list) -> dict:
    """
    Reproduce the original scoring logic:
      - Each suburb appearing in the top-9 for a week earns 10 points.
      - Multiple venues in same suburb → accumulate (e.g. 2 venues = 20 pts).
    Returns {suburb_name: total_points} across all 24 weekly snapshots.
    Also returns per-week breakdown for transparency.
    """
    suburb_totals  = defaultdict(int)
    suburb_weekly  = defaultdict(list)   # suburb → [score_week1, score_week2, …]
    weeks = []

    for version in raw_data:
        date_str = version["thisversionrun"]
        venues   = version["results"]["collection1"]
        week_scores = defaultdict(int)

        for venue in venues[:9]:
            suburb = venue["property7"]["text"].strip()
            week_scores[suburb] += 10

        weeks.append(date_str)
        seen_this_week = set()
        for suburb, pts in week_scores.items():
            suburb_totals[suburb] += pts
            suburb_weekly[suburb].append(pts)
            seen_this_week.add(suburb)

        # Fill 0 for suburbs not seen this week
        for suburb in suburb_totals:
            if suburb not in seen_this_week:
                suburb_weekly[suburb].append(0)

    return suburb_totals, suburb_weekly, weeks


def normalise(scores: dict) -> dict:
    """Normalise raw point totals to a 0–100 scale."""
    if not scores:
        return {}
    max_val = max(scores.values())
    if max_val == 0:
        return {k: 0 for k in scores}
    return {k: round(v / max_val * 100, 1) for k, v in scores.items()}


def main():
    print(f"Reading: {SRC_HTML}")
    raw_data = extract_raw_data(SRC_HTML)
    print(f"  Found {len(raw_data)} weekly snapshots")

    suburb_totals, suburb_weekly, weeks = process_raw_data(raw_data)
    print(f"  Unique suburbs: {len(suburb_totals)}")

    normalised = normalise(suburb_totals)

    suburbs_out = []
    for name, score in sorted(normalised.items(), key=lambda x: -x[1]):
        raw_pts = suburb_totals[name]
        suburbs_out.append({
            "name":       name,
            "score":      score,
            "rawPoints":  raw_pts,
            "venueCount": round(raw_pts / 10),   # approx venue appearances
            "trend":      None                   # no prior year to compare
        })

    output = {
        "year":        2014,
        "source":      "Broadsheet Melbourne via Kimonolabs weekly scrapes (Jul–Dec 2014)",
        "methodology": "10 pts per suburb appearance in weekly Broadsheet top-9; normalised 0–100",
        "weekCount":   len(weeks),
        "suburbs":     suburbs_out
    }

    os.makedirs(os.path.dirname(OUT_FILE), exist_ok=True)
    with open(OUT_FILE, "w", encoding="utf-8") as fh:
        json.dump(output, fh, indent=2)

    print(f"Written: {OUT_FILE}")
    print("\nTop 10 suburbs (2014):")
    for s in suburbs_out[:10]:
        print(f"  {s['name']:25s}  {s['score']:5.1f}  ({s['rawPoints']} pts)")


if __name__ == "__main__":
    main()
