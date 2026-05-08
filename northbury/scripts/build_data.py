#!/usr/bin/env python3
"""
build_data.py
=============
Builds northbury/data/sa1.geojson with:
  - Real SA1 boundaries from ABS REST API (ASGS 2021)
  - Median house sale price per SA1 (scraped from realestate.com.au, geocoded)
  - Median lot size per SA1 (computed from Vicmap parcel polygons)

Run:
  pip install requests pandas geopandas shapely geopy playwright
  python3 -m playwright install chromium
  python3 northbury/scripts/build_data.py
"""

import json
import time
import re
import sys
from pathlib import Path
from collections import defaultdict

import requests

SCRIPT_DIR = Path(__file__).parent
OUT_DIR    = SCRIPT_DIR.parent / "data"
OUT_DIR.mkdir(parents=True, exist_ok=True)
OUT_FILE   = OUT_DIR / "sa1.geojson"

# ABS SA2 codes for Thornbury and Northcote (ASGS Edition 3, 2021)
SA2_CODES = ["206021112", "206021499", "206021500"]
SA2_NAMES = {
    "206021112": "Thornbury",
    "206021499": "Northcote - East",
    "206021500": "Northcote - West",
}

SUBURB_TARGETS = [
    {"suburb": "Thornbury", "postcode": "3071"},
    {"suburb": "Northcote", "postcode": "3070"},
]


# ── Step 1: SA1 boundaries from ABS REST API ────────────────────────────────────────────

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

            # Probe with bbox to see if layer has data and what fields exist
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

            # Fetch all SA1s in the bbox
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
            n = len(geojson.get("features", []))
            print(f"  Got {n} SA1 features from bbox query")
            if n > 0:
                sa2_code_field = next((k for k in sample_props if "SA2" in k.upper() and "CODE" in k.upper()), None)
                sa2_name_field = next((k for k in sample_props if "SA2" in k.upper() and "NAME" in k.upper()), None)
                sa1_code_field = next((k for k in sample_props if "SA1" in k.upper() and "CODE" in k.upper()), None)
                # Filter to Thornbury/Northcote only, then normalise field names
                filtered = []
                for f in geojson["features"]:
                    p = f["properties"]
                    sa2_name = str(p.get(sa2_name_field, "")) if sa2_name_field else ""
                    if not any(s in sa2_name for s in ("Thornbury", "Northcote")):
                        continue
                    if sa2_code_field:
                        p["SA2_CODE_2021"] = str(p.get(sa2_code_field, ""))
                    if sa2_name_field:
                        p["SA2_NAME_2021"] = sa2_name
                        p["suburb"] = sa2_name
                    if sa1_code_field and sa1_code_field != "SA1_CODE_2021":
                        p["SA1_CODE_2021"] = str(p.get(sa1_code_field, ""))
                    filtered.append(f)
                geojson["features"] = filtered
                print(f"  Filtered to {len(filtered)} SA1s in Thornbury/Northcote")
                return geojson

    sys.exit("No SA1 features found — ABS API may be down or bbox is wrong")


# ── Step 2: Scrape sold house listings from realestate.com.au ─────────────────

def scrape_rea_listings(suburb: str, postcode: str) -> list:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("  playwright not installed — skipping REA scrape")
        return []

    slug = f"{suburb.lower().replace(' ', '-')}-vic-{postcode}"
    base_url = f"https://www.realestate.com.au/sold/property-house-in-{slug}/"

    listings = []
    print(f"  Scraping realestate.com.au for {suburb} (houses sold) …")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=False)
        ctx = browser.new_context(user_agent=(
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
        ))
        page = ctx.new_page()

        for page_num in range(1, 6):
            url = base_url if page_num == 1 else f"{base_url}?page={page_num}"
            try:
                page.goto(url, wait_until="networkidle", timeout=30000)
                time.sleep(2)
            except Exception as e:
                print(f"    Page {page_num}: load error ({e})")
                break

            content = page.content()
            found_this_page = _parse_rea_page(content, suburb)
            if not found_this_page:
                print(f"    Page {page_num}: no listings found (end of results)")
                break

            listings.extend(found_this_page)
            print(f"    Page {page_num}: {len(found_this_page)} listings (total: {len(listings)})")
            time.sleep(2)

        browser.close()

    return listings


def _parse_rea_page(html: str, suburb: str) -> list:
    listings = []
    m = re.search(r'window\.__NEXT_DATA__\s*=\s*(\{.*?\})(?:\s*;|\s*</script>)', html, re.DOTALL)
    if not m:
        return _parse_jsonld(html, suburb)
    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError:
        return _parse_jsonld(html, suburb)

    props = data.get("props", {}).get("pageProps", {})
    results = (
        props.get("searchResults", {}).get("results", []) or
        props.get("listings", []) or
        props.get("data", {}).get("results", [])
    )
    for item in results:
        listing = _extract_listing(item, suburb)
        if listing:
            listings.append(listing)
    return listings


def _parse_jsonld(html: str, suburb: str) -> list:
    listings = []
    for m in re.finditer(r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL):
        try:
            data = json.loads(m.group(1))
            items = data if isinstance(data, list) else [data]
            for item in items:
                listing = _extract_listing(item, suburb)
                if listing:
                    listings.append(listing)
        except json.JSONDecodeError:
            pass
    return listings


def _extract_listing(item, suburb):
    if not isinstance(item, dict):
        return None
    prop_type = (
        item.get("propertyType") or
        item.get("property", {}).get("propertyType") or
        item.get("@type") or ""
    ).lower()
    if any(t in prop_type for t in ["apartment", "unit", "flat", "townhouse"]):
        return None
    price_raw = (
        item.get("price") or item.get("soldPrice") or
        item.get("priceDetails", {}).get("soldPrice") or
        item.get("listing", {}).get("price") or 0
    )
    if isinstance(price_raw, str):
        price_raw = re.sub(r"[^\d]", "", price_raw)
        price_raw = int(price_raw) if price_raw else 0
    price = int(price_raw) if price_raw else 0
    if price < 100_000 or price > 15_000_000:
        return None
    addr = (
        item.get("address") or
        item.get("property", {}).get("address") or
        item.get("listing", {}).get("propertyDetails", {}).get("displayableAddress") or {}
    )
    if isinstance(addr, dict):
        street = addr.get("street") or addr.get("streetAddress") or ""
        sub = addr.get("suburb") or addr.get("addressLocality") or suburb
        state = addr.get("state") or addr.get("addressRegion") or "VIC"
        address_str = f"{street}, {sub} {state}".strip(", ")
    elif isinstance(addr, str):
        address_str = addr
    else:
        return None
    if not address_str or len(address_str) < 5:
        return None
    land_size = (
        item.get("landSize") or
        item.get("property", {}).get("landSize") or
        item.get("propertyDetails", {}).get("landArea") or None
    )
    return {"address": address_str, "price": price, "land_m2": float(land_size) if land_size else None}


# ── Step 3: Geocode addresses ───────────────────────────────────────────────────

def geocode_listings(listings: list) -> list:
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
    except ImportError:
        print("  geopy not installed — skipping geocoding")
        return []
    geolocator = Nominatim(user_agent="northbury-map/1.0")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)
    geocoded = []
    print(f"  Geocoding {len(listings)} listings …")
    for i, listing in enumerate(listings):
        query = listing["address"] + ", Melbourne, VIC, Australia"
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


# ── Step 4: Assign to SA1 ────────────────────────────────────────────────────────

def assign_to_sa1(listings_geocoded: list, sa1_geojson: dict) -> dict:
    try:
        import geopandas as gpd
        import pandas as pd
        from shapely.geometry import Point
    except ImportError:
        print("  geopandas not installed — skipping SA1 assignment")
        return {}
    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    sa1_gdf = sa1_gdf.rename(columns={"SA1_CODE_2021": "sa1_code"})
    pts = gpd.GeoDataFrame(
        listings_geocoded,
        geometry=[Point(l["lng"], l["lat"]) for l in listings_geocoded],
        crs="EPSG:4326"
    )
    joined = gpd.sjoin(pts, sa1_gdf[["sa1_code", "geometry"]], how="left", predicate="within")
    prices_by_sa1 = defaultdict(list)
    for _, row in joined.iterrows():
        if pd.notna(row.get("sa1_code")) and row["price"] > 0:
            prices_by_sa1[str(row["sa1_code"])].append(row["price"])
    return dict(prices_by_sa1)


# ── Step 5: Lot sizes from Vicmap ────────────────────────────────────────────────────

def fetch_vicmap_lot_sizes(sa1_geojson: dict) -> dict:
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError:
        print("  geopandas not installed — skipping lot size computation")
        return {}
    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    bbox = sa1_gdf.total_bounds
    minx, miny, maxx, maxy = bbox
    print("  Fetching Vicmap parcel data via ArcGIS REST …")
    url = (
        "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/"
        "Vicmap_Property/FeatureServer/6/query"
    )
    params = {
        "where": "1=1",
        "geometry": f"{minx},{miny},{maxx},{maxy}",
        "geometryType": "esriGeometryEnvelope",
        "spatialRel": "esriSpatialRelIntersects",
        "inSR": "4326", "outSR": "4326",
        "outFields": "PROPNUM,PARCEL_SFX,PROP_LGA_CODE",
        "returnGeometry": "true",
        "f": "geojson",
        "resultRecordCount": 2000,
    }
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        parcels_geojson = r.json()
        n_parcels = len(parcels_geojson.get("features", []))
        print(f"  Got {n_parcels} parcel features")
    except Exception as e:
        print(f"  Vicmap REST fetch failed ({e}) — lot sizes will be empty")
        return {}
    if n_parcels == 0:
        return {}
    parcels_gdf = gpd.GeoDataFrame.from_features(parcels_geojson["features"], crs="EPSG:4326")
    parcels_proj = parcels_gdf.to_crs("EPSG:7855")
    parcels_proj["area_m2"] = parcels_proj.geometry.area
    parcels_proj = parcels_proj[(parcels_proj["area_m2"] >= 50) & (parcels_proj["area_m2"] <= 5000)]
    sa1_proj = sa1_gdf.to_crs("EPSG:7855")
    sa1_proj = sa1_proj.rename(columns={"SA1_CODE_2021": "sa1_code"})
    joined = gpd.sjoin(parcels_proj[["area_m2", "geometry"]], sa1_proj[["sa1_code", "geometry"]], how="left", predicate="within")
    lot_sizes = {}
    for sa1_code, group in joined.groupby("sa1_code"):
        lot_sizes[str(sa1_code)] = {"median_m2": round(group["area_m2"].median(), 1), "count": len(group)}
    print(f"  Computed lot sizes for {len(lot_sizes)} SA1s")
    return lot_sizes


# ── Step 6: Merge and write ────────────────────────────────────────────────────────────────

def merge_and_write(sa1_geojson: dict, prices_by_sa1: dict, lot_sizes: dict):
    import statistics
    median_prices = {}
    for sa1_code, prices in prices_by_sa1.items():
        if prices:
            median_prices[sa1_code] = {"median_price": int(statistics.median(prices)), "sale_count": len(prices)}
    for feature in sa1_geojson["features"]:
        props = feature["properties"]
        sa1_code = str(props.get("SA1_CODE_2021", ""))
        sa2_code = str(props.get("SA2_CODE_2021", ""))
        price_info = median_prices.get(sa1_code, {})
        props["median_price"] = price_info.get("median_price", None)
        props["sale_count"]   = price_info.get("sale_count", 0)
        props["price_period"] = "Last 24 months"
        lot_info = lot_sizes.get(sa1_code, {})
        props["median_lot_m2"] = lot_info.get("median_m2", None)
        props["lot_count"]     = lot_info.get("count", 0)
        props["suburb"] = SA2_NAMES.get(sa2_code, props.get("SA2_NAME_2021", ""))
    with open(OUT_FILE, "w") as f:
        json.dump(sa1_geojson, f)
    print(f"\nWritten: {OUT_FILE}")
    print(f"  Features:   {len(sa1_geojson['features'])}")
    print(f"  With price: {sum(1 for f in sa1_geojson['features'] if f['properties']['median_price'])}")
    print(f"  With lots:  {sum(1 for f in sa1_geojson['features'] if f['properties']['median_lot_m2'])}")


# ── Main ────────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Building northbury/data/sa1.geojson")
    print("=" * 60)
    sa1_geojson = fetch_sa1_boundaries()
    all_listings = []
    for target in SUBURB_TARGETS:
        raw = scrape_rea_listings(target["suburb"], target["postcode"])
        all_listings.extend(raw)
    print(f"\nTotal listings scraped: {len(all_listings)}")
    geocoded = geocode_listings(all_listings) if all_listings else []
    prices_by_sa1 = assign_to_sa1(geocoded, sa1_geojson) if geocoded else {}
    print(f"SA1s with price data: {len(prices_by_sa1)}")
    lot_sizes = fetch_vicmap_lot_sizes(sa1_geojson)
    merge_and_write(sa1_geojson, prices_by_sa1, lot_sizes)


if __name__ == "__main__":
    main()
