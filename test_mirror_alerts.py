import unittest
from urllib.parse import urlsplit, parse_qs
from mirror_alerts import (collect_alerts, LAYERS, DownloadTimeout,
                           on_screen_alerts, minor_alert_ids, MINOR_URL,
                           collect_with_fallback, archive_features)


def geojson_page(ids, **extra):
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": oid,
         "geometry": {"type": "Polygon", "coordinates": [[[0,0],[1,0],[1,1],[0,0]]]},
         "properties": {"objectid": oid}} for oid in ids
    ], **extra}


def mock_server(url, layers=None, requests=None):
    if layers is None:
        layers = {0: [1, 2, 3], 1: [1]}
    parsed = urlsplit(url)
    layer = int(parsed.path.split("/")[-2])
    params = parse_qs(parsed.query)
    if requests is not None:
        requests.append((layer, params))
    assert params["where"] == ["1=1"]
    assert "objectIds" not in params and "returnIdsOnly" not in params
    ids = layers[layer]
    if params.get("returnCountOnly") == ["true"]:
        assert params["f"] == ["json"]
        return {"count": len(ids)}
    assert params["f"] == ["geojson"]
    assert params["orderByFields"] == ["objectid ASC"]
    assert params["returnGeometry"] == ["true"]
    assert params["outSR"] == ["4326"]
    offset = int(params["resultOffset"][0])
    size = int(params["resultRecordCount"][0])
    # Deliberately omit the transfer flag to exercise count-based pagination.
    return geojson_page(ids[offset:offset + size])


class MapServerTests(unittest.TestCase):
    def test_all_layers_and_batches(self):
        requests = []
        result = collect_alerts(lambda url: mock_server(url, requests=requests), batch_size=2)
        self.assertEqual([f["id"] for f in result["features"]], ["0:1", "0:2", "0:3", "1:1"])
        self.assertEqual(
            [(f["properties"]["noaa_layer"], f["properties"]["noaa_layer_name"])
             for f in result["features"]],
            [(0, LAYERS[0])] * 3 + [(1, LAYERS[1])])
        self.assertEqual(
            [(layer, int(p["resultOffset"][0]), int(p["resultRecordCount"][0]))
             for layer, p in requests if "resultOffset" in p],
            [(0, 0, 2), (0, 2, 2), (1, 0, 2)])
        self.assertEqual([layer for layer, p in requests if "returnCountOnly" in p],
                         [0, 0, 1, 1])

    def test_transfer_limit_continues_short_and_empty_pages(self):
        offsets = []

        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            layer = int(urlsplit(url).path.split("/")[-2])
            if layer == 0 and "resultOffset" in params:
                offset = int(params["resultOffset"][0])
                offsets.append(offset)
                self.assertEqual(params["resultRecordCount"], ["2"])
                return {
                    0: lambda: geojson_page([1], exceededTransferLimit=True),
                    2: lambda: geojson_page([], properties={"exceededTransferLimit": True}),
                    4: lambda: geojson_page([2, 3], exceededTransferLimit=False),
                }[offset]()
            return mock_server(url)

        result = collect_alerts(fetch, batch_size=2)
        self.assertEqual([f["id"] for f in result["features"]], ["0:1", "0:2", "0:3", "1:1"])
        self.assertEqual(offsets, [0, 2, 4])

    def test_timeout_reduces_page_size_and_retries_same_offset(self):
        requests = []

        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            layer = int(urlsplit(url).path.split("/")[-2])
            if layer == 0 and "resultOffset" in params:
                offset = int(params["resultOffset"][0])
                size = int(params["resultRecordCount"][0])
                requests.append((offset, size))
                if offset == 2 and size > 1:
                    raise DownloadTimeout("mock timeout")
            return mock_server(url)

        result = collect_alerts(fetch, batch_size=2)
        self.assertEqual([f["id"] for f in result["features"]], ["0:1", "0:2", "0:3", "1:1"])
        self.assertEqual(requests, [(0, 2), (2, 2), (2, 1)])

    def test_missing_feature_fails(self):
        def fetch(url):
            result = mock_server(url)
            if "features" in result:
                result["features"] = result["features"][:-1]
            return result
        with self.assertRaisesRegex(ValueError, "incomplete or changed"):
            collect_alerts(fetch)

    def test_arcgis_error_fails(self):
        with self.assertRaisesRegex(ValueError, "ArcGIS layer"):
            collect_alerts(lambda _: {"error": {"code": 500}})

    def test_invalid_geojson_page_fails_even_with_zero_count(self):
        def fetch(url):
            if "returnCountOnly=true" in url:
                return {"count": 0}
            return {"objectIdFieldName": "objectid", "objectIds": None}
        with self.assertRaisesRegex(ValueError, "Invalid GeoJSON page"):
            collect_alerts(fetch)

    def test_empty_page_with_positive_count_fails(self):
        def fetch(url):
            if "returnCountOnly=true" in url:
                return {"count": 5}
            return geojson_page([])
        with self.assertRaisesRegex(ValueError, "incomplete or changed"):
            collect_alerts(fetch)

    def test_empty_layers(self):
        requests = []
        result = collect_alerts(
            lambda url: mock_server(url, layers={0: [], 1: []}, requests=requests))
        self.assertEqual(result, geojson_page([]))
        self.assertEqual([layer for layer, p in requests if "returnCountOnly" in p],
                         [0, 0, 1, 1])
        self.assertEqual([layer for layer, p in requests if "resultOffset" in p], [0, 1])

    def test_changed_count_restarts_layer_without_partial_results(self):
        counts = iter([3, 4, 3, 3])
        offsets = []

        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            layer = int(urlsplit(url).path.split("/")[-2])
            if layer == 0:
                if "returnCountOnly" in params:
                    return {"count": next(counts)}
                offsets.append(int(params["resultOffset"][0]))
            return mock_server(url)

        result = collect_alerts(fetch, batch_size=2)
        self.assertEqual([f["id"] for f in result["features"]], ["0:1", "0:2", "0:3", "1:1"])
        self.assertEqual(offsets, [0, 2, 0, 2])

    def test_duplicate_across_pages_fails(self):
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            if params.get("resultOffset") == ["2"]:
                return geojson_page([2])
            return mock_server(url)
        with self.assertRaisesRegex(ValueError, "repeated or unordered object ID"):
            collect_alerts(fetch, batch_size=2)

    def test_invalid_count_fails(self):
        for count in (None, -1, True, "3"):
            with self.subTest(count=count):
                with self.assertRaisesRegex(ValueError, "Invalid count"):
                    collect_alerts(lambda _: {"count": count})

    def test_timeout_at_minimum_page_size_fails(self):
        def fetch(url):
            if "resultOffset=" in url:
                raise DownloadTimeout("mock timeout")
            return mock_server(url)
        with self.assertRaises(DownloadTimeout):
            collect_alerts(fetch, batch_size=1)


class DisplayTests(unittest.TestCase):
    def test_filter_preserves_full_snapshot(self):
        properties = [
            {"prod_type": "Small Craft Advisory"},
            {"prod_type": "GALE WARNING"},
            {"prod_type": "Heavy Freezing Spray Warning"},
            {"prod_type": "Flood Advisory", "severity": " Minor "},
            {"prod_type": "Flood Advisory", "cap_id": "urn:oid:minor"},
            {"prod_type": "Tornado Warning", "severity": "Severe"},
            {"prod_type": "Wind Advisory"},
            {"prod_type": "Gale Watch"},
        ]
        collection = {"type": "FeatureCollection", "features": [
            {"type": "Feature", "id": i, "geometry": None, "properties": p}
            for i, p in enumerate(properties)]}
        display = on_screen_alerts(collection, {"urn:oid:minor"})
        self.assertEqual([f["id"] for f in display["features"]], [5, 6, 7])
        self.assertEqual(len(collection["features"]), 8)
        self.assertIs(display["features"][0], collection["features"][5])

    def test_minor_index_follows_cursor_and_normalizes_ids(self):
        second = "https://api.weather.gov/alerts?cursor=abc"
        pages = {
            MINOR_URL: {"type": "FeatureCollection", "features": [
                {"id": "https://api.weather.gov/alerts/urn%3Aoid%3Afirst",
                 "properties": {"severity": "Minor", "id": "urn:oid:first"}}
            ], "pagination": {"next": second}},
            second: {"type": "FeatureCollection", "features": [
                {"properties": {"severity": "Minor", "id": "urn:oid:second"}}
            ]}
        }
        self.assertEqual(minor_alert_ids(pages.__getitem__, lambda _: None),
                         {"urn:oid:first", "urn:oid:second"})

    def test_invalid_minor_index_fails(self):
        with self.assertRaises(ValueError):
            minor_alert_ids(lambda _: {"type": "FeatureCollection", "features": [
                {"properties": {"severity": "Severe", "id": "urn:oid:wrong"}}
            ]}, lambda _: None)


class ArchiveTests(unittest.TestCase):
    def test_empty_primary_uses_fallback(self):
        empty = {"type": "FeatureCollection", "features": []}
        full = {"type": "FeatureCollection", "features": [{"id": 1}]}
        self.assertEqual(collect_with_fallback(lambda: empty, lambda: full), full)

    def test_nonempty_primary_skips_fallback(self):
        full = {"type": "FeatureCollection", "features": [{"id": 1}]}
        def fail():
            raise AssertionError("fallback should not run")
        self.assertEqual(collect_with_fallback(lambda: full, fail), full)

    def test_both_empty_fail(self):
        empty = {"type": "FeatureCollection", "features": []}
        with self.assertRaises(ValueError):
            collect_with_fallback(lambda: empty, lambda: empty)

    def test_archive_fields_normalized(self):
        feature = {"type": "Feature", "geometry": {"type": "Polygon", "coordinates": []},
                   "properties": {"PROD_TYPE": "Gale Warning", "CAP_ID": "urn:oid:1"}}
        result = archive_features({"type": "FeatureCollection", "features": [feature]}, "current_all")
        self.assertEqual(result[0]["properties"]["prod_type"], "Gale Warning")
        self.assertEqual(result[0]["properties"]["cap_id"], "urn:oid:1")


if __name__ == "__main__":
    unittest.main()
