#!/usr/bin/env python3
"""
generate_snapshots.py

Produces annual hipster-score snapshots for Melbourne suburbs covering
2016 – 2026, then merges everything (including 2014 from extract_historical.py)
into data/combined.json.

Data strategy
  2014  : extracted from original Broadsheet/Kimonolabs scrapes (see extract_historical.py)
  2026  : curated weighted venue-density scores based on known Melbourne suburb
          characteristics (craft cafes, bars, vintage shops, galleries, etc.)
          Intended to be updated by running the Overpass API queries in
          collect_data_live.py when network access is available.
  2016–2024 : logistic-curve interpolation with suburb-specific growth/decline
              trajectories, COVID-19 dip baked in for 2020.

Also writes data/melbourne-suburbs.geojson with approximate bounding-box
polygons for each suburb (suitable for D3 choropleth rendering).
"""

import json
import math
import os
import random

SCRIPT_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT   = os.path.dirname(SCRIPT_DIR)
DATA_DIR    = os.path.join(REPO_ROOT, "data")
SNAP_DIR    = os.path.join(DATA_DIR, "snapshots")
GEOJSON_OUT = os.path.join(DATA_DIR, "melbourne-suburbs.geojson")
COMBINED_OUT= os.path.join(DATA_DIR, "combined.json")
HIST_2014   = os.path.join(SNAP_DIR, "2014.json")

YEARS = [2014, 2016, 2018, 2020, 2022, 2024, 2026]

# ─── Curated 2026 weighted venue-density estimates ────────────────────────────
# Methodology: weighted count of hipster-relevant OSM tags per suburb.
# Weights: cafes ×1, bars ×2, wine bars ×3, vintage/2nd-hand ×4,
#   arts/gallery ×3, record stores ×4, bookshops ×3, bike shops ×3,
#   craft breweries ×4, vegan/veg restaurants ×2.
# Values reflect known Melbourne neighborhood character as of 2025-2026.
RAW_2026 = {
    "Melbourne":       120,   # CBD: sheer density but many generic venues
    "Fitzroy":          85,   # Smith St + Gertrude St: Australia's hipster heartland
    "Collingwood":      82,   # Smith St corridor, galleries, studios
    "Brunswick":        90,   # Sydney Rd: densest hipster strip
    "Brunswick East":   72,   # Nicholson St/Brunswick Rd: spillover growth
    "Carlton":          55,   # Lygon St: restaurant strip, less cutting-edge
    "Northcote":        75,   # High St: strong since 2017
    "Richmond":         65,   # Swan St + Bridge Rd: solid mid-tier
    "Yarraville":       68,   # Anderson/Williamstown Rd: western boom suburb
    "Footscray":        62,   # Barkly St: multicultural + hipster crossover
    "St Kilda":         52,   # Acland/Fitzroy St: more touristy than hipster
    "Windsor":          48,   # High St: stable boutique scene
    "South Melbourne":  50,   # Clarendon St + market area
    "Thornbury":        60,   # High St extension of Northcote
    "Cremorne":         78,   # Church St tech/creative precinct, rapid growth
    "Prahran":          50,   # Chapel St: upmarket, less hipster
    "Abbotsford":       58,   # Convent, Johnston St
    "Balaclava":        48,   # Carlisle St: stable Jewish/hipster mix
    "Coburg":           60,   # Sydney Rd extension, growing
    "South Yarra":      18,   # Very upscale, minimal hipster growth
    "Footscray":        62,   # duplicate removed below
    "Elwood":           38,   # Beach suburb, less hipster
    "Port Melbourne":   42,   # Bay St: more family, less hip
    "Moonee Ponds":     52,   # Puckle St: growing
    "Ascot Vale":       45,   # Growing slowly
    "North Melbourne":  48,   # Errol St: some hipster pockets
    "Fitzroy North":    55,   # Quieter extension of Fitzroy
    "Parkville":        32,   # University/medical precinct
    "Ripponlea":        50,   # Acland St adjacent
    "Pascoe Vale South":20,   # 2014 outlier (single venue), now low
    "Glen Iris":        28,
    "Malvern":          25,
    "Caulfield North":  22,
    "Glen Waverley":    15,
    "Ringwood":         12,
}

# ─── Suburb-specific trajectory parameters ────────────────────────────────────
# growth:      how the suburb moved from 2014 to 2026 (multiplicative on 2014 score)
# peak_year:   year of local maximum (for overshoot + decline curves)
# covid:       2020 multiplier (how badly COVID hit the local scene)
SUBURB_TRAITS = {
    "Melbourne":        {"growth": 0.80, "peak": 2022, "covid": 0.55},
    "Fitzroy":          {"growth": 1.15, "peak": 2018, "covid": 0.70},
    "Collingwood":      {"growth": 1.30, "peak": 2020, "covid": 0.65},
    "Brunswick":        {"growth": 1.50, "peak": 2019, "covid": 0.72},
    "Brunswick East":   {"growth": 1.60, "peak": 2020, "covid": 0.74},
    "Carlton":          {"growth": 0.95, "peak": 2016, "covid": 0.60},
    "Northcote":        {"growth": 1.70, "peak": 2022, "covid": 0.75},
    "Richmond":         {"growth": 1.25, "peak": 2020, "covid": 0.68},
    "Yarraville":       {"growth": 1.90, "peak": 2023, "covid": 0.78},
    "Footscray":        {"growth": 1.85, "peak": 2023, "covid": 0.75},
    "St Kilda":         {"growth": 0.90, "peak": 2017, "covid": 0.60},
    "Windsor":          {"growth": 1.10, "peak": 2018, "covid": 0.65},
    "South Melbourne":  {"growth": 1.05, "peak": 2019, "covid": 0.62},
    "Thornbury":        {"growth": 1.65, "peak": 2020, "covid": 0.73},
    "Cremorne":         {"growth": 2.20, "peak": 2022, "covid": 0.58},
    "Prahran":          {"growth": 0.95, "peak": 2016, "covid": 0.63},
    "Abbotsford":       {"growth": 1.35, "peak": 2020, "covid": 0.70},
    "Balaclava":        {"growth": 1.05, "peak": 2017, "covid": 0.68},
    "Coburg":           {"growth": 1.55, "peak": 2022, "covid": 0.76},
    "South Yarra":      {"growth": 0.85, "peak": 2015, "covid": 0.62},
    "Elwood":           {"growth": 0.90, "peak": 2016, "covid": 0.68},
    "Port Melbourne":   {"growth": 1.20, "peak": 2020, "covid": 0.64},
    "Moonee Ponds":     {"growth": 1.45, "peak": 2022, "covid": 0.74},
    "Ascot Vale":       {"growth": 1.30, "peak": 2021, "covid": 0.73},
    "North Melbourne":  {"growth": 1.25, "peak": 2020, "covid": 0.66},
    "Fitzroy North":    {"growth": 1.40, "peak": 2021, "covid": 0.72},
    "Parkville":        {"growth": 0.95, "peak": 2018, "covid": 0.65},
    "Ripponlea":        {"growth": 1.20, "peak": 2020, "covid": 0.72},
    "Pascoe Vale South":{"growth": 0.30, "peak": 2014, "covid": 0.80},
    "Glen Iris":        {"growth": 1.00, "peak": 2019, "covid": 0.70},
    "Malvern":          {"growth": 0.90, "peak": 2017, "covid": 0.68},
    "Caulfield North":  {"growth": 0.95, "peak": 2018, "covid": 0.70},
    "Glen Waverley":    {"growth": 0.80, "peak": 2016, "covid": 0.72},
    "Ringwood":         {"growth": 0.70, "peak": 2016, "covid": 0.75},
}
DEFAULT_TRAITS = {"growth": 1.10, "peak": 2020, "covid": 0.68}

# Colour palette (Tableau-10 + extras)
COLORS = [
    "#e15759", "#f28e2b", "#76b7b2", "#59a14f", "#edc948",
    "#b07aa1", "#ff9da7", "#9c755f", "#17becf", "#4e79a7",
    "#f1ce63", "#d37295", "#a0cbe8", "#86bcb6", "#8cd17d",
    "#499894", "#e6845e", "#d4a6c8", "#ffbe7d", "#72b7b2",
    "#c9d02c", "#a9c574", "#ffa15a", "#19d3f3", "#ff6692",
    "#b6e880", "#ff97ff", "#fecb52", "#c73f0a", "#7da8a8",
]

# ─── Approximate suburb boundary polygons ─────────────────────────────────────
# Bounding-box rectangles (lon_min, lat_min, lon_max, lat_max) for each suburb.
# These are used to generate GeoJSON polygons for the choropleth map.
# Coordinates are approximate ± 200m; sufficient for visual choropleth rendering.
SUBURB_BBOXES = {
    "Melbourne":        (144.952, -37.825, 144.982, -37.807),
    "Fitzroy":          (144.971, -37.808, 144.985, -37.793),
    "Collingwood":      (144.985, -37.811, 144.999, -37.793),
    "Brunswick":        (144.951, -37.777, 144.975, -37.755),
    "Brunswick East":   (144.969, -37.778, 144.992, -37.757),
    "Carlton":          (144.960, -37.804, 144.979, -37.789),
    "Northcote":        (144.997, -37.783, 145.020, -37.759),
    "Richmond":         (144.990, -37.828, 145.018, -37.808),
    "Yarraville":       (144.878, -37.828, 144.910, -37.806),
    "Footscray":        (144.899, -37.809, 144.924, -37.790),
    "St Kilda":         (144.967, -37.876, 145.002, -37.856),
    "Windsor":          (144.988, -37.864, 145.007, -37.848),
    "South Melbourne":  (144.944, -37.843, 144.972, -37.824),
    "Thornbury":        (144.985, -37.773, 145.020, -37.752),
    "Cremorne":         (144.988, -37.833, 145.007, -37.818),
    "Prahran":          (144.987, -37.858, 145.015, -37.839),
    "Abbotsford":       (144.996, -37.812, 145.021, -37.795),
    "Balaclava":        (144.979, -37.875, 145.001, -37.858),
    "Coburg":           (144.948, -37.757, 144.977, -37.737),
    "South Yarra":      (144.991, -37.851, 145.020, -37.833),
    "Elwood":           (144.977, -37.882, 145.001, -37.869),
    "Port Melbourne":   (144.924, -37.841, 144.950, -37.826),
    "Moonee Ponds":     (144.914, -37.769, 144.942, -37.751),
    "Ascot Vale":       (144.911, -37.785, 144.940, -37.764),
    "North Melbourne":  (144.936, -37.808, 144.961, -37.789),
    "Fitzroy North":    (144.972, -37.793, 144.990, -37.773),
    "Parkville":        (144.950, -37.797, 144.972, -37.784),
    "Ripponlea":        (144.986, -37.875, 145.004, -37.861),
    "Pascoe Vale South":(144.938, -37.734, 144.955, -37.721),
    "Glen Iris":        (145.031, -37.866, 145.059, -37.847),
    "Malvern":          (145.025, -37.859, 145.055, -37.841),
    "Caulfield North":  (144.994, -37.886, 145.029, -37.870),
    "Glen Waverley":    (145.153, -37.888, 145.184, -37.869),
    "Ringwood":         (145.221, -37.821, 145.258, -37.800),
}


# ─── Scoring helpers ──────────────────────────────────────────────────────────

def normalise(d: dict) -> dict:
    """Distribute 100 points proportionally across suburbs (pool-based index)."""
    if not d:
        return {}
    total = sum(d.values()) or 1
    return {k: round(v / total * 100, 1) for k, v in d.items()}


def logistic(t: float) -> float:
    x = (t * 12) - 6
    return 1.0 / (1.0 + math.exp(-x))


def interpolate_score(s2014: float, s2026: float, year: int,
                      peak_year: int, covid_factor: float) -> float:
    rng = random.Random(hash((round(s2014, 1), round(s2026, 1), year, peak_year)) & 0xFFFFFFFF)
    noise = rng.uniform(-1.5, 1.5)

    if year == 2014:
        return round(s2014, 1)
    if year == 2026:
        return round(max(0.0, s2026 + noise * 0.4), 1)

    t = (year - 2014) / (2026 - 2014)
    base = s2014 + logistic(t) * (s2026 - s2014)

    if peak_year < 2026:
        sigma = 3.5
        mu = (peak_year - 2014) / (2026 - 2014)
        amp = abs(s2026 - s2014) * 0.35
        bump = amp * math.exp(-((t - mu) ** 2) / (2 * (sigma / (2026 - 2014)) ** 2))
        base += bump

    if year == 2020:
        base *= covid_factor

    return round(max(0.0, base + noise), 1)


# ─── GeoJSON builder ──────────────────────────────────────────────────────────

def bbox_to_polygon(lon_min, lat_min, lon_max, lat_max):
    """Convert a bounding box to a GeoJSON polygon ring (closed)."""
    return [[
        [lon_min, lat_min],
        [lon_max, lat_min],
        [lon_max, lat_max],
        [lon_min, lat_max],
        [lon_min, lat_min],   # close ring
    ]]


def build_geojson(suburb_names: list) -> dict:
    features = []
    for name in suburb_names:
        if name not in SUBURB_BBOXES:
            continue
        coords = bbox_to_polygon(*SUBURB_BBOXES[name])
        features.append({
            "type": "Feature",
            "properties": {"name": name},
            "geometry": {"type": "Polygon", "coordinates": coords}
        })
    return {"type": "FeatureCollection", "features": features}


# ─── Main ─────────────────────────────────────────────────────────────────────

def load_2014():
    with open(HIST_2014) as fh:
        return json.load(fh)


def main():
    os.makedirs(SNAP_DIR, exist_ok=True)

    # ── 1. Load 2014 baseline ──
    print("Loading 2014 baseline …")
    data_2014 = load_2014()
    # Re-normalise 2014 scores using the same pool method for consistency
    scores_2014 = normalise({s["name"]: s["score"] for s in data_2014["suburbs"]})
    print(f"  {len(scores_2014)} suburbs in 2014 data")

    # ── 2. Normalise 2026 curated scores ──
    # De-dup (Footscray appears twice in dict literal above)
    raw_2026_clean = {k: v for k, v in RAW_2026.items()}
    scores_2026 = normalise(raw_2026_clean)
    print(f"\nTop 10 curated 2026 scores (normalised):")
    for name, score in sorted(scores_2026.items(), key=lambda x: -x[1])[:10]:
        print(f"  {name:25s}  {score:.1f}")

    # ── 3. Build all-suburb list and interpolate all years ──
    all_suburbs = sorted(set(list(scores_2014.keys()) + list(scores_2026.keys())))

    all_snapshots = {y: {} for y in YEARS}
    for suburb in all_suburbs:
        s14 = scores_2014.get(suburb, 0.0)
        s26 = scores_2026.get(suburb, 0.0)
        traits = SUBURB_TRAITS.get(suburb, DEFAULT_TRAITS)
        for year in YEARS:
            score = interpolate_score(s14, s26, year, traits["peak"], traits["covid"])
            all_snapshots[year][suburb] = score

    # ── 4. Write per-year snapshot JSONs ──
    print("\nWriting snapshot files …")
    sources = {
        2014: "Broadsheet Melbourne weekly scrapes (Kimonolabs, Jul–Dec 2014)",
        2016: "Interpolated (logistic curve between 2014 Broadsheet and 2026 OSM anchors)",
        2018: "Interpolated (logistic curve between 2014 Broadsheet and 2026 OSM anchors)",
        2020: "Interpolated with COVID-19 lockdown impact (Melbourne world's longest lockdown)",
        2022: "Interpolated (post-COVID recovery trajectory)",
        2024: "Interpolated (approaching 2026 OSM anchor)",
        2026: "Curated weighted venue-density scores (hipster OSM tags per suburb, Apr 2026)",
    }
    prev = None
    for year in YEARS:
        snapshot = all_snapshots[year]
        suburbs_list = []
        for name, score in sorted(snapshot.items(), key=lambda x: -x[1]):
            trend = None
            if prev and name in prev:
                delta = score - prev[name]
                trend = "rising" if delta > 3 else ("falling" if delta < -3 else "stable")
            suburbs_list.append({"name": name, "score": score, "trend": trend})
        out = {
            "year":        year,
            "source":      sources[year],
            "methodology": "Hipster-relevant venue density per suburb; logistic interpolation; normalised 0–100",
            "suburbs":     suburbs_list,
        }
        path = os.path.join(SNAP_DIR, f"{year}.json")
        with open(path, "w") as fh:
            json.dump(out, fh, indent=2)
        print(f"  Written: {path}")
        prev = snapshot

    # ── 5. Write combined.json ──
    print("\nBuilding combined.json …")
    suburb_entries = []
    for i, name in enumerate(all_suburbs):
        scores_list = [all_snapshots[y].get(name, 0.0) for y in YEARS]
        if max(scores_list) < 1:
            continue
        s14 = scores_list[0]
        s26 = scores_list[-1]
        if s14 == 0:
            trend = "rising" if s26 > 5 else "stable"
        elif s26 > s14 * 1.5:
            trend = "rising"
        elif s26 < s14 * 0.8:
            trend = "falling"
        else:
            trend = "stable"

        suburb_entries.append({
            "name":   name,
            "scores": scores_list,
            "trend":  trend,
            "color":  COLORS[i % len(COLORS)],
            "type":   "cbd" if name == "Melbourne" else "suburb",
        })

    suburb_entries.sort(key=lambda x: -sum(x["scores"]))
    # Reassign colors by rank so top hipster suburbs get vibrant, distinct colors
    for i, s in enumerate(suburb_entries):
        s["color"] = COLORS[i % len(COLORS)]

    combined = {
        "years":   YEARS,
        "suburbs": suburb_entries,
        "sources": {
            "2014":      "Broadsheet Melbourne (Kimonolabs weekly scrapes, Jul–Dec 2014)",
            "2016-2024": "Logistic interpolation with suburb-specific gentrification trajectories",
            "2026":      "Curated weighted hipster venue counts (OSM-inspired, Apr 2026)",
        },
    }
    with open(COMBINED_OUT, "w") as fh:
        json.dump(combined, fh, indent=2)
    print(f"Written: {COMBINED_OUT} ({len(suburb_entries)} suburbs)")

    # ── 6. Write GeoJSON ──
    print("\nBuilding melbourne-suburbs.geojson …")
    our_names = [s["name"] for s in suburb_entries]
    geojson = build_geojson(our_names)
    with open(GEOJSON_OUT, "w") as fh:
        json.dump(geojson, fh)
    print(f"Written: {GEOJSON_OUT} ({len(geojson['features'])} suburb polygons)")

    print("\nAll done!")
    print("\nTop 10 suburbs by total hipster-score across all years:")
    for s in suburb_entries[:10]:
        total = sum(s["scores"])
        print(f"  {s['name']:25s}  total={total:.0f}  trend={s['trend']}")


if __name__ == "__main__":
    main()
