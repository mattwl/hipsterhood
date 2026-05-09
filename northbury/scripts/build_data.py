#!/usr/bin/env python3
"""
build_data.py
=============
Builds northbury/data/sa1.geojson with:
  - Real SA1 boundaries from ABS REST API (ASGS 2021)
  - Median house sale price per SA1 (loaded from raw_listings.json, geocoded)
  - Median lot size per SA1 (from raw_listings.json land_m2 field, or Vicmap WFS)

Run:
  pip install requests pandas geopandas shapely geopy curl-cffi
  python3 northbury/scripts/build_data.py

Listings source (in priority order):
  1. northbury/data/raw_listings.json  — if present, used directly (skips scraping)
     Format: [{"address": "12 Smith St, Thornbury VIC 3071", "price": 1250000, "land_m2": 420}, ...]
  2. realestate.com.au scraper         — requires curl-cffi for Cloudflare bypass
     pip install curl-cffi             — falls back to cloudscraper, then plain requests
"""

import json
import re
import sys
import statistics
import time
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


# ── Step 1: SA1 boundaries from ABS REST API ────────────────────────────────────────────────

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


# ── Step 2: Fetch listings (scrape or load from file) ────────────────────────

SUBURB_TARGETS = [
    {"suburb": "Thornbury", "postcode": "3071"},
    {"suburb": "Northcote", "postcode": "3070"},
]

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-AU,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
}


def _get_session():
    """Return a requests-like session that bypasses Cloudflare if possible."""
    # curl-cffi impersonates Chrome TLS fingerprint — most effective vs Cloudflare
    try:
        from curl_cffi import requests as cffi_requests
        session = cffi_requests.Session(impersonate="chrome124")
        print("  Using curl-cffi (Chrome TLS impersonation)")
        return session, "cffi"
    except ImportError:
        pass

    # cloudscraper handles JS challenge cookies
    try:
        import cloudscraper
        session = cloudscraper.create_scraper(browser={"browser": "chrome", "platform": "darwin", "mobile": False})
        print("  Using cloudscraper")
        return session, "cloudscraper"
    except ImportError:
        pass

    # Plain requests — likely blocked by Cloudflare but worth a try
    s = requests.Session()
    s.headers.update(_HEADERS)
    print("  Using plain requests (may be blocked by Cloudflare)")
    return s, "requests"


def _fetch_page(session, url: str) -> str:
    try:
        r = session.get(url, headers=_HEADERS, timeout=30)
        return r.text
    except Exception as e:
        print(f"    Fetch error: {e}")
        return ""


def scrape_rea_listings(suburb: str, postcode: str) -> list:
    slug = suburb.lower().replace(" ", "-")
    base_url = f"https://www.realestate.com.au/sold/property-house-in-{slug}%2C+vic+{postcode}/"

    session, method = _get_session()
    listings = []
    print(f"  Scraping REA for {suburb} (houses, sold) …")

    for page_num in range(1, 11):
        url = base_url if page_num == 1 else f"{base_url}?page={page_num}"
        html = _fetch_page(session, url)
        if not html:
            break

        found = _parse_rea_page(html, suburb)

        # Cloudflare detection
        if not found and page_num == 1:
            title_m = re.search(r"<title>(.*?)</title>", html, re.I)
            title = title_m.group(1) if title_m else "(none)"
            print(f"    Page 1 debug — title: {title!r}, html length: {len(html)}")
            if len(html) < 5000 or "challenge" in title.lower() or not title_m:
                print(f"    Cloudflare block detected with {method}. Try: pip3 install curl-cffi")
                break

        if not found:
            print(f"    Page {page_num}: no listings (end of results)")
            break

        listings.extend(found)
        print(f"    Page {page_num}: {len(found)} listings (total: {len(listings)})")
        time.sleep(1.5)

    return listings


def _parse_rea_page(html: str, suburb: str) -> list:
    listings = []
    m = re.search(r"window\.__NEXT_DATA__\s*=\s*(\{.*?\})(?:\s*;|\s*</script>)", html, re.DOTALL)
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
    print(f"    [debug] pageProps keys: {list(props.keys())[:10]}, results: {len(results)}")
    for item in results:
        listing = _extract_listing(item, suburb)
        if listing:
            listings.append(listing)
    return listings


def _parse_jsonld(html: str, suburb: str) -> list:
    listings = []
    for m in re.finditer(
        r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html, re.DOTALL
    ):
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
        sub    = addr.get("suburb") or addr.get("addressLocality") or suburb
        state  = addr.get("state") or addr.get("addressRegion") or "VIC"
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


def load_raw_listings() -> list:
    """Load and validate northbury/data/raw_listings.json."""
    if not RAW_FILE.exists():
        return []
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
        price = item.get("price", 0)
        if isinstance(price, str):
            price = int(re.sub(r"[^\d]", "", price) or "0")
        price = int(price) if price else 0
        if price < 100_000 or price > 15_000_000:
            skipped += 1
            continue
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


def get_listings() -> list:
    """Use raw_listings.json if present, otherwise scrape REA."""
    if RAW_FILE.exists():
        print(f"  Found raw_listings.json — loading instead of scraping")
        return load_raw_listings()

    print("  raw_listings.json not found — scraping realestate.com.au …")
    print("  (pip3 install curl-cffi  for best Cloudflare bypass)")
    all_listings = []
    for target in SUBURB_TARGETS:
        results = scrape_rea_listings(target["suburb"], target["postcode"])
        all_listings.extend(results)
    print(f"  Total scraped: {len(all_listings)} listings")

    if all_listings:
        with open(RAW_FILE, "w") as f:
            json.dump(all_listings, f, indent=2)
        print(f"  Saved to {RAW_FILE} for reuse")

    return all_listings


# ── Step 3: Geocode addresses ───────────────────────────────────────────────────

def geocode_listings(listings: list) -> list:
    try:
        from geopy.geocoders import Nominatim
        from geopy.extra.rate_limiter import RateLimiter
    except ImportError:
        print("  geopy not installed — skipping geocoding (pip3 install geopy)")
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
        print("  geopandas not installed — skipping SA1 assignment (pip3 install geopandas)")
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

_WFS_BASES = [
    "https://opendata.maps.vic.gov.au/geoserver/ows",
    "https://opendata.maps.vic.gov.au/geoserver/wfs",
    "https://opendata.maps.vic.gov.au/geoserver/vmpropertysmp/ows",
    "https://opendata.maps.vic.gov.au/geoserver/vmpropertysmp/wfs",
]

_WFS_LAYER_CANDIDATES = [
    "vmpropertysmp:PARCEL_MP",
    "VMPROPERTYSMP:PARCEL_MP",
    "PARCEL_SHP",
    "vmpropertysmp:PARCEL_SHP",
    "PARCEL_MP",
]


def _wfs_discover_parcel_layers(base_url: str) -> list:
    """Run WFS GetCapabilities and return any layer names containing PARCEL."""
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


def _wfs_get_features(base_url: str, layer: str, bbox_v1: str, bbox_v2: str) -> list:
    """Try WFS 1.1.0 then 2.0.0 to fetch features for a layer."""
    attempts = [
        # WFS 1.1.0: typeName, maxFeatures, srsName
        {"version": "1.1.0", "typeName": layer,  "maxFeatures": 3000, "BBOX": bbox_v1,
         "outputFormat": "application/json", "srsName": "EPSG:4326"},
        # WFS 2.0.0: typeNames (plural), count
        {"version": "2.0.0", "typeNames": layer, "count": 3000,       "BBOX": bbox_v2,
         "outputFormat": "application/json"},
    ]
    for params in attempts:
        try:
            r = requests.get(base_url, params={"service": "WFS", "request": "GetFeature", **params}, timeout=30)
            if r.status_code == 200:
                try:
                    data = r.json()
                    if data.get("features"):
                        return data["features"]
                except Exception:
                    pass
        except Exception:
            pass
    return []


def fetch_vicmap_lot_sizes(sa1_geojson: dict) -> dict:
    try:
        import geopandas as gpd
        import pandas as pd
    except ImportError:
        print("  geopandas not installed — skipping Vicmap lot sizes")
        return {}

    sa1_gdf = gpd.GeoDataFrame.from_features(sa1_geojson["features"], crs="EPSG:4326")
    minx, miny, maxx, maxy = sa1_gdf.total_bounds
    bbox_v1 = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    bbox_v2 = f"{minx},{miny},{maxx},{maxy},EPSG:4326"
    bbox_esri = f"{minx},{miny},{maxx},{maxy}"

    print("  Fetching Vicmap parcel data …")
    parcels_features = []

    # Try each WFS base, discover layers via GetCapabilities, then fetch
    for base in _WFS_BASES:
        discovered = _wfs_discover_parcel_layers(base)
        layers_to_try = discovered + [l for l in _WFS_LAYER_CANDIDATES if l not in discovered]
        for layer in layers_to_try:
            feats = _wfs_get_features(base, layer, bbox_v1, bbox_v2)
            if feats:
                print(f"  WFS {base.split('/')[-1]} / {layer}: {len(feats)} parcel features")
                parcels_features = feats
                break
        if parcels_features:
            break

    # ArcGIS REST fallbacks
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
                        feats = data.get("features", [])
                        if feats:
                            print(f"  ArcGIS {svc.split('/')[-2]} layer {layer_id}: {len(feats)} features")
                            parcels_features = feats
                            break
                    elif r.status_code not in (400, 404):
                        print(f"  ArcGIS {svc.split('/')[-2]}/{layer_id}: HTTP {r.status_code}")
                except Exception as e:
                    print(f"  ArcGIS error: {e}")
            if parcels_features:
                break

    if not parcels_features:
        print("  No Vicmap parcel data accessible — lot sizes from Vicmap unavailable")
        return {}

    parcels_gdf  = gpd.GeoDataFrame.from_features(
        {"type": "FeatureCollection", "features": parcels_features}["features"], crs="EPSG:4326"
    )
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


# ── Step 6: Merge and write ─────────────────────────────────────────────────────

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


# ── Main ────────────────────────────────────────────────────────────────────────────────

def main():
    print("=" * 60)
    print("Building northbury/data/sa1.geojson")
    print("=" * 60)

    sa1_geojson = fetch_sa1_boundaries()

    print("\nLoading house listings …")
    listings = get_listings()

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
