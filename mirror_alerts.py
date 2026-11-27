"""Mirror both NOAA WWA polygon layers into a single GeoJSON file."""

import json
import os
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode

BASE_URL = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/WWA/watch_warn_adv/MapServer"
LAYERS = {0: "CurrentWarnings", 1: "WatchesWarnings"}
BATCH_SIZE = 100
USER_AGENT = "CatenaryTransit/weather-alerts-mirror (https://github.com/catenarytransit/weather-alerts-mirror)"


class DownloadTimeout(RuntimeError):
    pass


def fetch_page(url):
    # curl bounds the entire transfer, including connection and response body,
    # unlike urllib's socket inactivity timeout.
    for attempt in range(1, 3):
        started = time.monotonic()
        print(f"GET {url} (attempt {attempt}/2, deadline 30s)", flush=True)
        try:
            result = subprocess.run(
                ["curl", "--fail", "--silent", "--show-error", "--compressed",
                 "--connect-timeout", "8", "--max-time", "30",
                 "--user-agent", USER_AGENT, url],
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=35,
            )
        except subprocess.TimeoutExpired as error:
            raise DownloadTimeout(f"Request exceeded 35s: {url}") from error

        elapsed = time.monotonic() - started
        if result.returncode == 0:
            print(f"Received {len(result.stdout):,} bytes in {elapsed:.1f}s", flush=True)
            data = json.loads(result.stdout)
            if not isinstance(data, dict) or "error" in data:
                raise ValueError(f"ArcGIS query failed: {data}")
            return data

        message = result.stderr.decode("utf-8", errors="replace").strip()
        print(f"curl exit {result.returncode} after {elapsed:.1f}s: {message}", file=sys.stderr, flush=True)
        if result.returncode == 28:
            # Split feature requests immediately on timeouts; metadata can retry.
            if "objectIds=" in url:
                raise DownloadTimeout(message)
            if attempt == 2:
                raise DownloadTimeout(message)
        elif result.returncode not in (5, 6, 7, 18, 22, 35, 52, 55, 56) or attempt == 2:
            raise RuntimeError(f"Download failed: {message}")
        print("Retrying in 2s", flush=True)
        time.sleep(2)


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
    try:
        result = query(layer, fetch, where="1=1", objectIds=",".join(map(str, ids)),
                       outFields="*", returnGeometry="true", outSR=4326,
                       returnZ="false", returnM="false", f="geojson")
    except DownloadTimeout:
        if len(ids) <= 1:
            raise
        print(f"Layer {layer}: {len(ids)} features timed out; splitting batch", flush=True)
        middle = len(ids) // 2
        return fetch_batch(layer, ids[:middle], fetch) + fetch_batch(layer, ids[middle:], fetch)
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
        print(f"Layer {layer} ({LAYERS[layer]}): requesting object IDs", flush=True)
        ids = object_ids(layer, fetch)
        print(f"Layer {layer}: {len(ids)} object IDs", flush=True)
        for start in range(0, len(ids), batch_size):
            end = min(start + batch_size, len(ids))
            print(f"Layer {layer}: fetching {start + 1}-{end} of {len(ids)}", flush=True)
            features.extend(fetch_batch(layer, ids[start:end], fetch))
        print(f"Layer {layer}: checking completeness", flush=True)
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
