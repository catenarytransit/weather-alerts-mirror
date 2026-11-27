"""Mirror NOAA WWA polygon layers and produce a filtered display GeoJSON."""

import json
import os
import sys
import subprocess
import time
from pathlib import Path
from urllib.parse import urlencode, unquote, urljoin, urlsplit

BASE_URL = "https://mapservices.weather.noaa.gov/eventdriven/rest/services/WWA/watch_warn_adv/MapServer"
LAYERS = {0: "CurrentWarnings", 1: "WatchesWarnings"}
BATCH_SIZE = 100
MINOR_URL = "https://api.weather.gov/alerts?active=true&severity=Minor&limit=500"
EXCLUDED_PRODUCTS = {"small craft advisory", "gale warning", "heavy freezing spray warning"}
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
                 "--user-agent", USER_AGENT, "--header", "Accept: application/geo+json, application/json", url],
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
    if result.get("exceededTransferLimit") or "objectIds" not in result:
        raise ValueError(f"Invalid object ID response for layer {layer}: {result!r}")
    ids = result["objectIds"]
    if ids is None:
        # ArcGIS can return null rather than [] when the layer is empty.
        # Confirm the count so an incomplete response is never published.
        count = query(layer, fetch, where="1=1", returnCountOnly="true", f="json")
        if type(count.get("count")) is not int or count["count"] != 0:
            raise ValueError(f"Null object IDs for layer {layer}, but count response was {count!r}")
        print(f"Layer {layer}: empty (null object IDs, count confirmed zero)", flush=True)
        return []
    if not isinstance(ids, list) or any(type(oid) is not int for oid in ids):
        raise ValueError(f"Invalid object ID list for layer {layer}: {result!r}")
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


def cap_id(value):
    if not isinstance(value, str):
        return ""
    value = value.strip()
    if value.startswith("https://api.weather.gov/alerts/"):
        value = value.removeprefix("https://api.weather.gov/alerts/")
    return unquote(value)


def minor_alert_ids(fetch=fetch_page, pause=time.sleep):
    # MapServer has no severity attribute. Query the CAP index for minor alerts
    # only, and use their IDs, never their geometries.
    url = MINOR_URL
    seen = set()
    ids = set()
    while url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.weather.gov" or url in seen:
            raise ValueError(f"Invalid or repeated minor-alert pagination URL: {url}")
        seen.add(url)
        page = fetch(url)
        if (page.get("type") != "FeatureCollection"
                or not isinstance(page.get("features"), list)):
            raise ValueError("Invalid NWS minor-alert response")
        for feature in page["features"]:
            properties = feature.get("properties") or {}
            if str(properties.get("severity", "")).strip().casefold() != "minor":
                raise ValueError("NWS minor-alert query returned a non-minor alert")
            aliases = {cap_id(properties.get("id")), cap_id(feature.get("id")),
                       cap_id(properties.get("@id"))} - {""}
            if not aliases:
                raise ValueError("Minor alert has no CAP ID")
            ids.update(aliases)
        pagination = page.get("pagination") or {}
        next_url = pagination.get("next")
        url = urljoin(url, next_url) if next_url else None
        if url:
            pause(30)
    print(f"NWS minor-alert index: {len(ids)} CAP IDs/aliases", flush=True)
    return ids


def on_screen_alerts(collection, minor_ids):
    features = []
    for feature in collection["features"]:
        properties = feature["properties"]
        product = str(properties.get("prod_type") or "").strip().casefold()
        severity = str(properties.get("severity") or "").strip().casefold()
        if (product in EXCLUDED_PRODUCTS or severity == "minor"
                or cap_id(properties.get("cap_id")) in minor_ids):
            continue
        features.append(feature)
    return {"type": "FeatureCollection", "features": features}


def write_geojson(output, collection):
    temporary = output.with_name(output.name + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as handle:
            json.dump(collection, handle, ensure_ascii=False, allow_nan=False, separators=(",", ":"))
            handle.write("\n")
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    print(f"Wrote {len(collection['features'])} alerts to {output}", flush=True)


def main():
    output = Path(sys.argv[1] if len(sys.argv) > 1 else "alerts.geojson")
    display_output = output.with_name(output.stem + "-on-screen.geojson")
    collection = collect_alerts()
    display = on_screen_alerts(collection, minor_alert_ids() if collection["features"] else set())
    print(f"Excluded {len(collection['features']) - len(display['features'])} on-screen alerts", flush=True)
    write_geojson(output, collection)
    write_geojson(display_output, display)


if __name__ == "__main__":
    main()
