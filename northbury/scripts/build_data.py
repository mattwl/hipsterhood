#!/usr/bin/env python3
"""
build_data.py
=============
Builds northbury/data/sa1.geojson with:
  - Real SA1 boundaries from ABS REST API (ASGS 2021)
  - Median house sale price per SA1 (loaded from raw_listings.json, geocoded)
  - Median lot size per SA1 (from raw_listings.json land_m2 field, or Vicmap WFS)

Run:
  pip install requests pandas geopandas shapely geopy
  python3 northbury/scripts/build_data.py

House price data input:
  Place northbury/data/raw_listings.json with format:
  [{"address": "12 Smith St, Thornbury VIC 3071", "price": 1250000, "land_m2": 420}, ...]
  Fields: address (required), price (required), land_m2 (optional), property_type (optional)
"""

import json
import re
import sys
import statistics
from pathlib import Path
from collections import defaultdict

import requests

SCRIPT_DIR = Path(__file__).parent
OUT_DIR    = SCRIPT_DIR.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE   = OUT_DIR / "sa1.geojson"
RAW_FILE   = OUT_DIR / "raw_listings.json"

SA2_NAMES = {
    "206021112": "Thornbury",
    "206021499": "Northcote - East",
    "206021500": "Northcote - West",
}


# ── Step 1: SA1 boundaries from ABS REST API ──────────────────────────────────────────────

def fetch_sa1_boundaries():
    print("Fetching SA1 boundaries from ABS REST API …")

    # Thornbury/Northcote bounding box (EPSG:4326)
    BBOX = "144.975,-37.800,145.025,-37.745"

    bases = [
        "https://geo.abs.gov.au/arcgis/rest/services/ASGS2021/SA1/MapServer",
        "https://geo.abs.gov.au/arcgis/rest/services/ASGS_2021/SA1/MapServer",
    ]

    for base in bases:
        for layer in ("0", "1", "2"):
            url = f"{base}/{layer}/query"

            probe = requests.get(url, params={
                "where": "1=1",
                "geometry": BBOX,
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": 3,
            }, timeout=30)
            if probe.status_code != 200:
                continue
            probe_data = probe.json()
            features = probe_data.get("features", [])
            if not features:
                continue

            sample_props = features[0].get("properties", {})
            print(f"  Layer {base.split('/')[-1]}/{layer} has data. Fields: {list(sample_props.keys())}")

            r = requests.get(url, params={
                "where": "1=1",
                "geometry": BBOX,
                "geometryType": "esriGeometryEnvelope",
                "inSR": "4326",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*",
                "outSR": "4326",
                "f": "geojson",
                "resultRecordCount": 200,
            }, timeout=30)
            r.raise_for_status()
            geojson = r.json()

            # Filter to Thornbury / Northcote SA2s
            sa2_name_field = next((k for k in sample_props if "SA2" in k.upper() and "NAME" in k.upper()), None)
            if sa2_name_field:
                before = len(geojson.get("features", []))
                geojson["features"] = [
                    f for f in geojson["features"]
                    if any(s in str(f["properties"].get(sa2_name_field, ""))
                           for s in ("Thornbury", "Northcote"))
                ]
                after = len(geojson["features"])
                print(f"  Filtered {before} → {after} SA1s (Thornbury + Northcote only)")

            n = len(geojson.get("features", []))
            if n == 0:
                continue

            # Normalise SA1_CODE_2021 / SA2_CODE_2021 fields
            sa2_code_field = next((k for k in sample_props if "SA2" in k.upper() and "CODE" in k.upper()), None)
            if sa2_code_field:
                for f in geojson["features"]:
                    code = str(f["properties"].get(sa2_code_field, ""))
                    f["properties"]["SA2_CODE_2021"] = code
                    if not f["properties"].get("SA2_NAME_2021"):
                        f["properties"]["SA2_NAME_2021"] = SA2_NAMES.get(code, "")

            sa1_code_field = next((k for k in sample_props if "SA1" in k.upper() and "CODE" in k.upper()), None)
            if sa1_code_field and sa1_code_field != "SA1_CODE_2021":
                for f in geojson["features"]:
                    f["properties"]["SA1_CODE_2021"] = f["properties"].get(sa1_code_field, "")

            print(f"  Using {n} SA1 features")
            return geojson

    sys.exit("No SA1 features found — ABS API may be down or bbox is wrong")


# ── Step 2: Load listings from raw_listings.json ─────────────────────────────

def load_raw_listings() -> list:
    """
    Load house sale listings from northbury/data/raw_listings.json.

    Expected format (each item):
      {
        "address":       "12 Smith St, Thornbury VIC 3071",   # required
        "price":         1250000,                              # required
        "land_m2":       420,                                  # optional
        "property_type": "house"                               # optional, filters out apartments/units
      }

    To generate this file, run your local REA scraper and save results there.
    """
    if not RAW_FILE.exists():
        print(f"\n  raw_listings.json not found at {RAW_FILE}")
        print("  → Run your local REA scraper and save results to northbury/data/raw_listings.json")
        print("  → Format: [{\"address\": \"...\", \"price\": 1250000, \"land_m2\": 420}, ...]")
        print("  Continuing without price/lot data.\n")
        return []

    with open(RAW_FILE) as f:
        raw = json.load(f)

    valid = []
    skipped = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue

        # Filter property type
        prop_type = (item.get("property_type") or item.get("type") or "house").lower()
        if any(t in prop_type for t in ["apartment", "unit", "flat", "townhouse"]):
            skipped += 1
            continue

        # Validate price
        price = item.get("price", 0)
        if isinstance(price, str):
            price = int(re.sub(r"[^\d]", "", price) or "0")
        price = int(price) if price else 0
        if price < 100_000 or price > 15_000_000:
            skipped += 1
            continue

        # Validate address
        addr = str(item.get("address", "")).strip()
        if len(addr) < 5:
            skipped += 1
            continue

        land_m2 = item.get("land_m2") or item.get("land_size")
        try:
            land_m2 = float(land_m2) if land_m2 else None
        except (TypeError, ValueError):
            land_m2 = None

        valid.append({"address": addr, "price": price, "land_m2": land_m2})

    print(f"  Loaded {len(valid)} valid listings from raw_listings.json ({skipped} skipped)")
    return valid


# ── Step 3: Geocode addresses ───────────────────────────────────────────────

def geocode_listings(listings: list) -> list:
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
    except ImportError:
        print("  geopy not installed — skipping geocoding (pip install geopy)")
        return []

    geolocator = Nominatim(user_agent="northbury-map/1.0")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)
    geocoded = []
    print(f"  Geocoding {len(listings)} listings (1 req/sec) …")
    for i, listing in enumerate(listings):
        query = listing["address"]
        if "VIC" not in query.upper() and "VICTORIA" not in query.upper():
            query += ", Melbourne, VIC, Australia"
        try:
            loc = geocode(query, exactly_one=True, timeout=10)
            if loc:
                geocoded.append({**listing, "lat": loc.latitude, "lng": loc.longitude})
        except Exception:
            pass
        if (i + 1) % 20 == 0:
            print(f"    {i+1}/{len(listings)} geocoded …")

    print(f"  Geocoded {len(geocoded)}/{len(listings)} listings")
    return geocoded


# ── Step 4: Assign to SA1, extract prices + lot sizes ────────────────────────

def assign_to_sa1(listings_geocoded: list, sa1_geojson: dict) -> tuple:
    """Returns (prices_by_sa1, lot_sizes_from_listings)."""
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
    except ImportError:
        print("  geopandas not installed — skipping SA1 assignment (pip install geopandas)")
        return {}, {}

    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    # Normalise column name
    sa1_code_col = next((c for c in sa1_gdf.columns if "SA1" in c.upper() and "CODE" in c.upper()), None)
    if sa1_code_col and sa1_code_col != "sa1_code":
        sa1_gdf = sa1_gdf.rename(columns={sa1_code_col: "sa1_code"})
    elif "SA1_CODE_2021" in sa1_gdf.columns:
        sa1_gdf = sa1_gdf.rename(columns={"SA1_CODE_2021": "sa1_code"})

    pts = gpd.GeoDataFrame(
        listings_geocoded,
        geometry=[Point(l["lng"], l["lat"]) for l in listings_geocoded],
        crs="EPSG:4326",
    )
    joined = gpd.sjoin(pts, sa1_gdf[["sa1_code", "geometry"]], how="left", predicate="within")

    prices_by_sa1 = defaultdict(list)
    lots_by_sa1   = defaultdict(list)
    for _, row in joined.iterrows():
        code = str(row.get("sa1_code", ""))
        if not code or code == "nan":
            continue
        if row["price"] > 0:
            prices_by_sa1[code].append(row["price"])
        lm2 = row.get("land_m2")
        if lm2 and 50 < float(lm2) < 5000:
            lots_by_sa1[code].append(float(lm2))

    print(f"  SA1s with price data from listings: {len(prices_by_sa1)}")
    print(f"  SA1s with lot size from listings:   {len(lots_by_sa1)}")
    return dict(prices_by_sa1), dict(lots_by_sa1)


# ── Step 5: Lot sizes from Vicmap (WFS then ArcGIS REST) ──────────────────────

def fetch_vicmap_lot_sizes(sa1_geojson: dict) -> dict:
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError:
        print("  geopandas not installed — skipping Vicmap lot sizes")
        return {}

    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    minx, miny, maxx, maxy = sa1_gdf.total_bounds
    bbox_wfs   = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    bbox_esri  = f"{minx},{miny},{maxx},{maxy}"

    print("  Fetching Vicmap parcel data …")

    parcels_geojson = None

    # Victorian Government WFS candidates
    wfs_candidates = [
        ("https://opendata.maps.vic.gov.au/geoserver/ows", "PARCEL_SHP"),
        ("https://opendata.maps.vic.gov.au/geoserver/ows", "vmpropertysmp:PARCEL_MP"),
        ("https://opendata.maps.vic.gov.au/geoserver/ows", "vicmap_property:PARCEL_SHP"),
        ("https://opendata.maps.vic.gov.au/geoserver/wfs", "PARCEL_SHP"),
    ]
    for wfs_url, type_name in wfs_candidates:
        try:
            r = requests.get(wfs_url, params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeName": type_name,
                "outputFormat": "application/json",
                "count": 3000,
                "BBOX": bbox_wfs,
            }, timeout=30)
            if r.status_code == 200:
                data = r.json()
                features = data.get("features", [])
                if features:
                    print(f"  WFS {type_name}: {len(features)} parcel features")
                    parcels_geojson = data
                    break
                else:
                    print(f"  WFS {type_name}: 0 features")
            else:
                print(f"  WFS {type_name}: HTTP {r.status_code}")
        except Exception as e:
            print(f"  WFS {type_name}: error ({e})")

    # ArcGIS REST fallbacks
    if not parcels_geojson:
        arcgis_urls = [
            "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/Vicmap_Property/FeatureServer/0/query",
            "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/Vicmap_Property/FeatureServer/1/query",
            "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/Vicmap_Property/FeatureServer/2/query",
            "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/Vicmap_Property/FeatureServer/3/query",
        ]
        for url in arcgis_urls:
            try:
                r = requests.get(url, params={
                    "geometry": bbox_esri,
                    "geometryType": "esriGeometryEnvelope",
                    "spatialRel": "esriSpatialRelIntersects",
                    "inSR": "4326", "outSR": "4326",
                    "outFields": "*",
                    "returnGeometry": "true",
                    "f": "geojson",
                    "resultRecordCount": 2000,
                }, timeout=30)
                if r.status_code == 200:
                    data = r.json()
                    features = data.get("features", [])
                    if features:
                        layer = url.split("/")[-2]
                        print(f"  ArcGIS layer {layer}: {len(features)} parcel features")
                        parcels_geojson = data
                        break
                    else:
                        print(f"  ArcGIS {url.split('/')[-2]}: 0 features")
                else:
                    print(f"  ArcGIS {url.split('/')[-2]}: HTTP {r.status_code}")
            except Exception as e:
                print(f"  ArcGIS error ({e})")

    if not parcels_geojson:
        print("  No Vicmap parcel data accessible — lot sizes from Vicmap unavailable")
        return {}

    parcels_gdf  = gpd.GeoDataFrame.from_features(parcels_geojson["features"], crs="EPSG:4326")
    parcels_proj = parcels_gdf.to_crs("EPSG:7855")
    parcels_proj["area_m2"] = parcels_proj.geometry.area
    parcels_proj = parcels_proj[(parcels_proj["area_m2"] >= 50) & (parcels_proj["area_m2"] <= 5000)]

    sa1_proj = sa1_gdf.to_crs("EPSG:7855")
    sa1_code_col = next((c for c in sa1_proj.columns if "SA1" in c.upper() and "CODE" in c.upper()), "SA1_CODE_2021")
    sa1_proj = sa1_proj.rename(columns={sa1_code_col: "sa1_code"})

    joined = gpd.sjoin(
        parcels_proj[["area_m2", "geometry"]],
        sa1_proj[["sa1_code", "geometry"]],
        how="left",
        predicate="within",
    )
    lot_sizes = {}
    for sa1_code, group in joined.groupby("sa1_code"):
        lot_sizes[str(sa1_code)] = {"median_m2": round(group["area_m2"].median(), 1), "count": len(group)}

    print(f"  Computed lot sizes for {len(lot_sizes)} SA1s from Vicmap")
    return lot_sizes


# ── Step 6: Merge and write ────────────────────────────────────────────────────────

def merge_and_write(
    sa1_geojson: dict,
    prices_by_sa1: dict,
    vicmap_lot_sizes: dict,
    listing_lot_sizes: dict,
):
    median_prices = {}
    for sa1_code, prices in prices_by_sa1.items():
        if prices:
            median_prices[sa1_code] = {
                "median_price": int(statistics.median(prices)),
                "sale_count": len(prices),
            }

    # Prefer Vicmap lot sizes; fall back to listing-derived
    effective_lots = {}
    for sa1_code in set(list(vicmap_lot_sizes.keys()) + list(listing_lot_sizes.keys())):
        if sa1_code in vicmap_lot_sizes:
            effective_lots[sa1_code] = vicmap_lot_sizes[sa1_code]
        elif sa1_code in listing_lot_sizes:
            sizes = listing_lot_sizes[sa1_code]
            effective_lots[sa1_code] = {
                "median_m2": round(statistics.median(sizes), 1),
                "count": len(sizes),
            }

    for feature in sa1_geojson["features"]:
        props = feature["properties"]
        sa1_code = str(props.get("SA1_CODE_2021", ""))
        sa2_code = str(props.get("SA2_CODE_2021", ""))

        price_info = median_prices.get(sa1_code, {})
        props["median_price"]  = price_info.get("median_price", None)
        props["sale_count"]    = price_info.get("sale_count", 0)
        props["price_period"]  = "Last 24 months"

        lot_info = effective_lots.get(sa1_code, {})
        props["median_lot_m2"] = lot_info.get("median_m2", None)
        props["lot_count"]     = lot_info.get("count", 0)

        props["suburb"] = SA2_NAMES.get(sa2_code, props.get("SA2_NAME_2021", ""))

    with open(OUT_FILE, "w") as f:
        json.dump(sa1_geojson, f)

    n_price = sum(1 for f in sa1_geojson["features"] if f["properties"]["median_price"])
    n_lots  = sum(1 for f in sa1_geojson["features"] if f["properties"]["median_lot_m2"])

    print(f"\nWritten: {OUT_FILE}")
    print(f"  Features:   {len(sa1_geojson['features'])}")
    print(f"  With price: {n_price}")
    print(f"  With lots:  {n_lots}")


# ── Main ──────────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Building northbury/data/sa1.geojson")
    print("=" * 60)

    sa1_geojson = fetch_sa1_boundaries()

    print("\nLoading house listings …")
    listings = load_raw_listings()

    geocoded = []
    prices_by_sa1 = {}
    listing_lot_sizes = {}

    if listings:
        geocoded = geocode_listings(listings)
        if geocoded:
            prices_by_sa1, listing_lot_sizes = assign_to_sa1(geocoded, sa1_geojson)

    print(f"\nTotal listings geocoded: {len(geocoded)}")

    print("\nFetching Vicmap lot sizes …")
    vicmap_lot_sizes = fetch_vicmap_lot_sizes(sa1_geojson)

    merge_and_write(sa1_geojson, prices_by_sa1, vicmap_lot_sizes, listing_lot_sizes)


if __name__ == "__main__":
    main()
