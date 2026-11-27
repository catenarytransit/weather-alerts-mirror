"""Fetch every page of active NWS alerts into a single GeoJSON file."""

import json
import os
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlsplit
from urllib.request import Request, urlopen

START_URL = "https://api.weather.gov/alerts?active=true&limit=500"
HEADERS = {
    "Accept": "application/geo+json",
    "User-Agent": "CatenaryTransit/weather-alerts-mirror (https://github.com/catenarytransit/weather-alerts-mirror)",
}


def fetch_page(url):
    for attempt in range(5):
        try:
            with urlopen(Request(url, headers=HEADERS), timeout=45) as response:
                return json.load(response)
        except (HTTPError, URLError, TimeoutError) as error:
            if isinstance(error, HTTPError) and error.code not in (408, 429, 500, 502, 503, 504):
                raise
            if attempt == 4:
                raise
            delay = 30 * (2 ** attempt)
            if isinstance(error, HTTPError):
                retry_after = error.headers.get("Retry-After", "")
                if retry_after.isdigit():
                    delay = max(delay, int(retry_after))
            print(f"Request failed: {error}; retrying in {delay}s", file=sys.stderr, flush=True)
            time.sleep(delay)


def collect_alerts(fetch=fetch_page, pause=time.sleep):
    url = START_URL
    seen = set()
    features = {}
    pages = 0

    while url:
        parsed = urlsplit(url)
        if parsed.scheme != "https" or parsed.netloc != "api.weather.gov":
            raise ValueError(f"Unexpected pagination URL: {url}")
        if url in seen:
            raise ValueError(f"Repeated pagination URL: {url}")
        seen.add(url)

        page = fetch(url)
        if page.get("type") != "FeatureCollection" or not isinstance(page.get("features"), list):
            raise ValueError("Expected a GeoJSON FeatureCollection")
        for feature in page["features"]:
            if (feature.get("type") != "Feature" or "geometry" not in feature
                    or not isinstance(feature.get("properties"), dict)):
                raise ValueError("Invalid GeoJSON alert feature")
            alert_id = feature.get("id") or feature["properties"].get("id")
            if not isinstance(alert_id, str) or not alert_id:
                raise ValueError("Alert is missing its ID")
            features.setdefault(alert_id, feature)

        pages += 1
        print(f"Page {pages}: {len(page['features'])} alerts, {len(features)} unique", flush=True)
        pagination = page.get("pagination")
        if pagination is not None and not isinstance(pagination, dict):
            raise ValueError("Invalid pagination metadata")
        next_url = (pagination or {}).get("next")
        if next_url is not None and not isinstance(next_url, str):
            raise ValueError("Invalid next-page link")
        url = urljoin(url, next_url) if next_url else None
        if url:
            # NWS recommends spacing alert requests by at least 30 seconds.
            pause(30)

    return {
        "type": "FeatureCollection",
        "features": [features[key] for key in sorted(features)],
    }


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
