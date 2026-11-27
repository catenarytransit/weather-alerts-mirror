import unittest
from urllib.parse import urlsplit, parse_qs
from mirror_alerts import (collect_alerts, LAYERS, DownloadTimeout,
                           on_screen_alerts, minor_alert_ids, MINOR_URL)


def mock_server(url):
    parsed = urlsplit(url)
    layer = int(parsed.path.split("/")[-2])
    params = parse_qs(parsed.query)
    if params.get("returnIdsOnly") == ["true"]:
        return {"objectIds": [1, 2, 3] if layer == 0 else [1]}
    ids = list(map(int, params["objectIds"][0].split(",")))
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "id": oid,
         "geometry": {"type": "Polygon", "coordinates": [[[0,0],[1,0],[1,1],[0,0]]]},
         "properties": {"objectid": oid}} for oid in ids
    ]}


class MapServerTests(unittest.TestCase):
    def test_all_layers_and_batches(self):
        result = collect_alerts(mock_server, batch_size=2)
        self.assertEqual({f["id"] for f in result["features"]}, {"0:1", "0:2", "0:3", "1:1"})
        self.assertTrue(all(f["properties"]["noaa_layer_name"] in LAYERS.values()
                            for f in result["features"]))

    def test_transfer_limit_splits_batch(self):
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            if "," in params.get("objectIds", [""])[0]:
                return {"exceededTransferLimit": True}
            return mock_server(url)
        self.assertEqual(len(collect_alerts(fetch)["features"]), 4)

    def test_timeout_splits_batch(self):
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            if "," in params.get("objectIds", [""])[0]:
                raise DownloadTimeout("mock timeout")
            return mock_server(url)
        self.assertEqual(len(collect_alerts(fetch)["features"]), 4)

    def test_missing_feature_fails(self):
        def fetch(url):
            result = mock_server(url)
            if "features" in result:
                result["features"] = []
            return result
        with self.assertRaises(ValueError):
            collect_alerts(fetch)

    def test_arcgis_error_fails(self):
        with self.assertRaises(ValueError):
            collect_alerts(lambda _: {"error": {"code": 500}})

    def test_null_object_ids_with_zero_count(self):
        def fetch(url):
            params = parse_qs(urlsplit(url).query)
            if params.get("returnCountOnly") == ["true"]:
                return {"count": 0}
            return {"objectIdFieldName": "objectid", "objectIds": None}
        self.assertEqual(collect_alerts(fetch)["features"], [])

    def test_null_object_ids_with_positive_count_fails(self):
        def fetch(url):
            if "returnCountOnly=true" in url:
                return {"count": 5}
            return {"objectIds": None}
        with self.assertRaises(ValueError):
            collect_alerts(fetch)

    def test_empty_layers(self):
        self.assertEqual(collect_alerts(lambda _: {"objectIds": []})["features"], [])


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


if __name__ == "__main__":
    unittest.main()
