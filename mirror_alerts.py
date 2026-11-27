"""Mirror both NOAA WWA polygon layers into a single GeoJSON file."""

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

BASE_URL = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/WWA/watch_warn_adv/MapServer"
LAYERS = {1: "WatchesWarnings", 0: "CurrentWarnings"}
BATCH_SIZE = 200
HEADERS = {
    "Accept": "application/json, application/geo+json",
    "User-Agent": "CatenaryTransit/weather-alerts-mirror (https://github.com/catenarytransit/weather-alerts-mirror)",
}


def fetch_page(url):
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=45) as response:
                result = json.load(response)
                if not isinstance(result, dict) or "error" in result:
                    raise ValueError(f"ArcGIS query failed: {result}")
                return result
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            delay = 2 ** attempt
            if isinstance(error, HTTPError):
                retry_after = error.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
            print(f"Request failed: {error}; retrying in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)


def query(layer, fetch, **params):
    result = fetch(f"{BASE_URL}/{layer}/query?{urlencode(params)}")
    if not isinstance(result, dict) or "error" in result:
        raise ValueError(f"ArcGIS layer {layer} query failed: {result}")
    return result


def object_ids(layer, fetch):
    result = query(layer, fetch, where="1=1", returnIdsOnly="true", f="json")
    ids = result.get("objectIds")
    if (not isinstance(ids, list) or any(type(oid) is not int for oid in ids)
            or result.get("exceededTransferLimit")):
        raise ValueError(f"Invalid object ID list for layer {layer}")
    return sorted(set(ids))


def fetch_batch(layer, ids, fetch):
    result = query(layer, fetch, objectIds=",".join(map(str, ids)),
                   outFields="*", returnGeometry="true", outSR=4326,
                   returnZ="false", returnM="false", f="geojson")
    if result.get("exceededTransferLimit"):
        if len(ids) <= 1:
            raise ValueError(f"Transfer limit exceeded for a single feature in layer {layer}")
        middle = len(ids) // 2
        return fetch_batch(layer, ids[:middle], fetch) + fetch_batch(layer, ids[middle:], fetch)
    if result.get("type") != "FeatureCollection" or not isinstance(result.get("features"), list):
        raise ValueError(f"Expected GeoJSON FeatureCollection for layer {layer}")

    found = {}
    for feature in result["features"]:
        properties = feature.get("properties")
        if (feature.get("type") != "Feature" or "geometry" not in feature
                or not isinstance(properties, dict)):
            raise ValueError(f"Invalid GeoJSON feature in layer {layer}")
        oid = properties.get("objectid", feature.get("id"))
        if type(oid) is not int or oid in found:
            raise ValueError(f"Invalid or duplicate object ID in layer {layer}: {oid}")
        # Object IDs are local to a layer; CAP IDs can span multiple geometries.
        feature["id"] = f"{layer}:{oid}"
        properties["noaa_layer"] = layer
        properties["noaa_layer_name"] = LAYERS[layer]
        found[oid] = feature
    if set(found) != set(ids):
        raise ValueError(f"Layer {layer} changed or returned incomplete results")
    return [found[oid] for oid in ids]


def collect_alerts(fetch=fetch_page, batch_size=BATCH_SIZE):
    if batch_size < 1:
        raise ValueError("Batch size must be positive")
    features = []
    for layer in LAYERS:
        ids = object_ids(layer, fetch)
        for start in range(0, len(ids), batch_size):
            features.extend(fetch_batch(layer, ids[start:start + batch_size], fetch))
        if object_ids(layer, fetch) != ids:
            raise ValueError(f"Layer {layer} changed during download; retry on next run")
        print(f"Layer {layer} ({LAYERS[layer]}): {len(ids)} features", flush=True)
    return {"type": "FeatureCollection", "features": features}


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "alerts.geojson")
    collection = collect_alerts()
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(collection, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Wrote {len(collection['features'])} alerts to {output}", flush=True)


if __name__ == "__main__":
    main()
