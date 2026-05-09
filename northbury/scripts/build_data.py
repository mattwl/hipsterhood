#!/usr/bin/env python3
"""
build_data.py
=============
Builds northbury/data/sa1.geojson with:
  - Real SA1 boundaries from ABS REST API (ASGS 2021)
  - Median house sale price per SA1 (geocoded from raw_listings.json)
  - Median lot size per SA1 (Vicmap WFS, supplemented by land_m2 in listings)

Run:
  pip3 install requests pandas geopandas shapely geopy
  python3 northbury/scripts/build_data.py

Requires northbury/data/raw_listings.json — paste house-only sale data as a JSON array:
  [{"address": "98 Darebin Rd, Northcote VIC 3070", "price": 1316000, "land_m2": null}, ...]

To add more data: append entries to raw_listings.json and re-run. Supports both
Northcote (3070) and Thornbury (3071) addresses.
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


# -- Step 1: SA1 boundaries from ABS REST API --------------------------------

def fetch_sa1_boundaries():
    print("Fetching SA1 boundaries from ABS REST API ...")
    BBOX = "144.975,-37.800,145.025,-37.745"
    bases = [
        "https://geo.abs.gov.au/arcgis/rest/services/ASGS2021/SA1/MapServer",
        "https://geo.abs.gov.au/arcgis/rest/services/ASGS_2021/SA1/MapServer",
    ]
    for base in bases:
        for layer in ("0", "1", "2"):
            url = f"{base}/{layer}/query"
            probe = requests.get(url, params={
                "where": "1=1", "geometry": BBOX, "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*", "outSR": "4326", "f": "geojson", "resultRecordCount": 3,
            }, timeout=30)
            if probe.status_code != 200:
                continue
            features = probe.json().get("features", [])
            if not features:
                continue
            sample_props = features[0].get("properties", {})
            print(f"  Layer {base.split('/')[-1]}/{layer} has data. Fields: {list(sample_props.keys())}")
            r = requests.get(url, params={
                "where": "1=1", "geometry": BBOX, "geometryType": "esriGeometryEnvelope",
                "inSR": "4326", "spatialRel": "esriSpatialRelIntersects",
                "outFields": "*", "outSR": "4326", "f": "geojson", "resultRecordCount": 200,
            }, timeout=30)
            r.raise_for_status()
            geojson = r.json()
            sa2_name_field = next(
                (k for k in sample_props if "SA2" in k.upper() and "NAME" in k.upper()), None
            )
            if sa2_name_field:
                before = len(geojson.get("features", []))
                geojson["features"] = [
                    f for f in geojson["features"]
                    if any(s in str(f["properties"].get(sa2_name_field, ""))
                           for s in ("Thornbury", "Northcote"))
                ]
                print(f"  Filtered {before} -> {len(geojson['features'])} SA1s (Thornbury + Northcote only)")
            n = len(geojson.get("features", []))
            if n == 0:
                continue
            sa2_code_field = next(
                (k for k in sample_props if "SA2" in k.upper() and "CODE" in k.upper()), None
            )
            if sa2_code_field:
                for f in geojson["features"]:
                    code = str(f["properties"].get(sa2_code_field, ""))
                    f["properties"]["SA2_CODE_2021"] = code
                    if not f["properties"].get("SA2_NAME_2021"):
                        f["properties"]["SA2_NAME_2021"] = SA2_NAMES.get(code, "")
            sa1_code_field = next(
                (k for k in sample_props if "SA1" in k.upper() and "CODE" in k.upper()), None
            )
            if sa1_code_field and sa1_code_field != "SA1_CODE_2021":
                for f in geojson["features"]:
                    f["properties"]["SA1_CODE_2021"] = f["properties"].get(sa1_code_field, "")
            print(f"  Using {n} SA1 features")
            return geojson
    sys.exit("No SA1 features found -- ABS API may be down or bbox is wrong")


# -- Step 2: Load listings from raw_listings.json ----------------------------

def load_raw_listings() -> list:
    if not RAW_FILE.exists():
        sys.exit(
            f"ERROR: {RAW_FILE} not found.\n"
            "Add house sale data as a JSON array and re-run.\n"
            '{"address": "12 Smith St, Northcote VIC 3070", "price": 1200000, "land_m2": null}'
        )
    with open(RAW_FILE) as f:
        raw = json.load(f)
    valid = []
    skipped = 0
    for item in raw:
        if not isinstance(item, dict):
            skipped += 1
            continue
        prop_type = (item.get("property_type") or item.get("type") or "house").lower()
        if any(t in prop_type for t in ["apartment", "unit", "flat", "townhouse"]):
            skipped += 1
            continue
        addr = str(item.get("address", "")).strip()
        if len(addr) < 5:
            skipped += 1
            continue
        price = item.get("price") or 0
        if isinstance(price, str):
            price = int(re.sub(r"[^\d]", "", price) or "0")
        else:
            price = int(price) if price else 0
        # Allow price=0 (not disclosed); only reject implausible non-zero prices
        if price != 0 and (price < 100_000 or price > 15_000_000):
            skipped += 1
            continue
        land_m2 = item.get("land_m2") or item.get("land_size")
        try:
            land_m2 = float(land_m2) if land_m2 else None
        except (TypeError, ValueError):
            land_m2 = None
        valid.append({"address": addr, "price": price, "land_m2": land_m2})
    priced = sum(1 for l in valid if l["price"] > 0)
    print(f"  Loaded {len(valid)} listings ({priced} with price, {skipped} skipped)")
    return valid


# -- Step 3: Geocode addresses ------------------------------------------------

def geocode_listings(listings: list) -> list:
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
    except ImportError:
        print("  geopy not installed -- skipping geocoding (pip3 install geopy)")
        return []
    geolocator = Nominatim(user_agent="northbury-map/1.0")
    geocode = RateLimiter(geolocator.geocode, min_delay_seconds=1.1)
    geocoded = []
    print(f"  Geocoding {len(listings)} listings (1 req/sec) ...")
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
            print(f"    {i+1}/{len(listings)} geocoded ...")
    print(f"  Geocoded {len(geocoded)}/{len(listings)} listings")
    return geocoded


# -- Step 4: Assign to SA1 ---------------------------------------------------

def assign_to_sa1(listings_geocoded: list, sa1_geojson: dict) -> tuple:
    try:
        import geopandas as gpd
        from shapely.geometry import Point
    except ImportError:
        print("  geopandas not installed -- skipping SA1 assignment (pip3 install geopandas)")
        return {}, {}
    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
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
    print(f"  SA1s with price data: {len(prices_by_sa1)}")
    print(f"  SA1s with lot size from listings: {len(lots_by_sa1)}")
    return dict(prices_by_sa1), dict(lots_by_sa1)


# -- Step 5: Lot sizes from Vicmap WFS ---------------------------------------

_WFS_BASES = [
    "https://opendata.maps.vic.gov.au/geoserver/ows",
    "https://opendata.maps.vic.gov.au/geoserver/wfs",
    "https://opendata.maps.vic.gov.au/geoserver/vmpropertysmp/ows",
    "https://opendata.maps.vic.gov.au/geoserver/vmpropertysmp/wfs",
]

# Preferred layers tried first — land parcel polygons (one per land lot, not per strata title).
# "parcel_view" and "parcel_property" are title-based views that include tiny strata units;
# "v_parcel_mp" is the actual land parcel polygon layer.
_WFS_PREFERRED = [
    "open-data-platform:v_parcel_mp",
    "vmpropertysmp:PARCEL_MP",
    "VMPROPERTYSMP:PARCEL_MP",
    "PARCEL_MP",
]

_WFS_LAYER_CANDIDATES = [
    "open-data-platform:v_parcel_mp",
    "vmpropertysmp:PARCEL_MP",
    "VMPROPERTYSMP:PARCEL_MP",
    "PARCEL_SHP",
    "vmpropertysmp:PARCEL_SHP",
    "PARCEL_MP",
]

# Layers that match "PARCEL" but are title/strata views — skip them if a land-parcel layer works.
_WFS_SKIP = {"parcel_view", "parcel_property", "cl_tenure_parcel"}


def _wfs_discover_parcel_layers(base_url: str) -> list:
    for version in ("2.0.0", "1.1.0"):
        try:
            r = requests.get(base_url, params={
                "service": "WFS", "version": version, "request": "GetCapabilities"
            }, timeout=20)
            if r.status_code == 200 and "FeatureType" in r.text:
                names = re.findall(r"<(?:wfs:)?Name>([^<]+)</(?:wfs:)?Name>", r.text)
                parcel = [n for n in names if "PARCEL" in n.upper()]
                if parcel:
                    print(f"  GetCapabilities {base_url.split('/')[-1]} v{version}: {parcel}")
                    return parcel
        except Exception:
            pass
    return []


_WFS_PAGE = 2000  # features per page (safe for most WFS servers)


def _wfs_get_features(base_url: str, layer: str, bbox_v1: str, bbox_v2: str) -> list:
    # Try WFS 2.0.0 with pagination first (startIndex is standard in 2.0.0)
    all_features: list = []
    offset = 0
    while True:
        try:
            r = requests.get(base_url, params={
                "service": "WFS", "request": "GetFeature",
                "version": "2.0.0", "typeNames": layer,
                "count": _WFS_PAGE, "startIndex": offset,
                "BBOX": bbox_v2, "outputFormat": "application/json",
            }, timeout=60)
            if r.status_code == 200:
                feats = r.json().get("features", [])
                all_features.extend(feats)
                if len(feats) < _WFS_PAGE:
                    break  # last page
                offset += _WFS_PAGE
            else:
                break
        except Exception:
            break
    if all_features:
        return all_features

    # Fallback: WFS 1.1.0 with a high limit (no standard pagination)
    try:
        r = requests.get(base_url, params={
            "service": "WFS", "request": "GetFeature",
            "version": "1.1.0", "typeName": layer,
            "maxFeatures": 10000, "BBOX": bbox_v1,
            "outputFormat": "application/json", "srsName": "EPSG:4326",
        }, timeout=60)
        if r.status_code == 200:
            feats = r.json().get("features", [])
            if feats:
                return feats
    except Exception:
        pass
    return []


def fetch_vicmap_lot_sizes(sa1_geojson: dict) -> dict:
    try:
        import geopandas as gpd
    except ImportError:
        print("  geopandas not installed -- skipping Vicmap lot sizes")
        return {}
    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    minx, miny, maxx, maxy = sa1_gdf.total_bounds
    bbox_v1 = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    bbox_v2 = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    bbox_esri = f"{minx},{miny},{maxx},{maxy}"
    print("  Fetching Vicmap parcel data ...")
    parcels_features = []
    for base in _WFS_BASES:
        discovered = _wfs_discover_parcel_layers(base)
        # Try preferred land-parcel layers first, then other discovered layers (excluding
        # title/strata views which have one tiny polygon per dwelling unit), then candidates.
        preferred = [l for l in _WFS_PREFERRED if l in discovered]
        others = [l for l in discovered if l not in _WFS_PREFERRED
                  and not any(skip in l.lower() for skip in _WFS_SKIP)]
        fallbacks = [l for l in _WFS_LAYER_CANDIDATES if l not in discovered]
        layers_to_try = preferred + others + fallbacks
        for layer in layers_to_try:
            feats = _wfs_get_features(base, layer, bbox_v1, bbox_v2)
            if feats:
                print(f"  WFS {base.split('/')[-1]} / {layer}: {len(feats)} parcel features")
                parcels_features = feats
                break
        if parcels_features:
            break
    if not parcels_features:
        arcgis_services = [
            "https://services6.arcgis.com/GB33F62SbDxJjwEL/arcgis/rest/services/Vicmap_Property/FeatureServer",
            "https://services1.arcgis.com/vHnIGBHHqDR6y0CR/arcgis/rest/services/Vicmap_Property_Parcel/FeatureServer",
        ]
        for svc in arcgis_services:
            for layer_id in range(6):
                url = f"{svc}/{layer_id}/query"
                try:
                    r = requests.get(url, params={
                        "geometry": bbox_esri, "geometryType": "esriGeometryEnvelope",
                        "spatialRel": "esriSpatialRelIntersects",
                        "inSR": "4326", "outSR": "4326", "outFields": "*",
                        "returnGeometry": "true", "f": "geojson", "resultRecordCount": 5000,
                    }, timeout=30)
                    if r.status_code == 200:
                        feats = r.json().get("features", [])
                        if feats:
                            print(f"  ArcGIS layer {layer_id}: {len(feats)} features")
                            parcels_features = feats
                            break
                except Exception as e:
                    print(f"  ArcGIS error: {e}")
            if parcels_features:
                break
    if not parcels_features:
        print("  No Vicmap parcel data accessible -- lot sizes from Vicmap unavailable")
        return {}
    parcels_gdf = gpd.GeoDataFrame.from_features(parcels_features, crs="EPSG:4326")
    parcels_proj = parcels_gdf.to_crs("EPSG:7855")
    parcels_proj["area_m2"] = parcels_proj.geometry.area
    before_filter = len(parcels_proj)
    parcels_proj = parcels_proj[
        (parcels_proj["area_m2"] >= 50) & (parcels_proj["area_m2"] <= 5000)
    ]
    print(f"  After area filter (50–5000 m²): {len(parcels_proj)} of {before_filter} parcels")
    sa1_proj = sa1_gdf.to_crs("EPSG:7855")
    sa1_code_col = next(
        (c for c in sa1_proj.columns if "SA1" in c.upper() and "CODE" in c.upper()),
        "SA1_CODE_2021",
    )
    sa1_proj = sa1_proj.rename(columns={sa1_code_col: "sa1_code"})
    # Join on parcel centroids — avoids boundary-parcel ambiguity; each centroid
    # falls in exactly one SA1 polygon (strict within is correct for points).
    centroids_gdf = gpd.GeoDataFrame(
        parcels_proj[["area_m2"]],
        geometry=parcels_proj.geometry.centroid,
        crs="EPSG:7855",
    )
    joined = gpd.sjoin(
        centroids_gdf,
        sa1_proj[["sa1_code", "geometry"]],
        how="left", predicate="within",
    )
    lot_sizes = {}
    for sa1_code, group in joined.groupby("sa1_code"):
        lot_sizes[str(sa1_code)] = {
            "median_m2": round(group["area_m2"].median(), 1),
            "count": len(group),
        }
    print(f"  Computed lot sizes for {len(lot_sizes)} SA1s from Vicmap")
    return lot_sizes


# -- Step 6: Merge and write --------------------------------------------------

def merge_and_write(sa1_geojson, prices_by_sa1, vicmap_lot_sizes, listing_lot_sizes):
    median_prices = {
        code: {"median_price": int(statistics.median(prices)), "sale_count": len(prices)}
        for code, prices in prices_by_sa1.items() if prices
    }
    effective_lots = {}
    for sa1_code in set(list(vicmap_lot_sizes) + list(listing_lot_sizes)):
        if sa1_code in vicmap_lot_sizes:
            effective_lots[sa1_code] = vicmap_lot_sizes[sa1_code]
        else:
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
        props["price_period"]  = "Feb-May 2026"
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


# -- Main ---------------------------------------------------------------------

def main():
    print("=" * 60)
    print("Building northbury/data/sa1.geojson")
    print("=" * 60)
    sa1_geojson = fetch_sa1_boundaries()

    print("\nLoading house listings ...")
    listings = load_raw_listings()

    geocoded = []
    prices_by_sa1 = {}
    listing_lot_sizes = {}
    if listings:
        geocoded = geocode_listings(listings)
        if geocoded:
            prices_by_sa1, listing_lot_sizes = assign_to_sa1(geocoded, sa1_geojson)

    print(f"\nTotal listings geocoded: {len(geocoded)}")
    print("\nFetching Vicmap lot sizes ...")
    vicmap_lot_sizes = fetch_vicmap_lot_sizes(sa1_geojson)
    merge_and_write(sa1_geojson, prices_by_sa1, vicmap_lot_sizes, listing_lot_sizes)


if __name__ == "__main__":
    main()
