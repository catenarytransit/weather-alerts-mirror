import unittest
from urllib.parse import urlsplit, parse_qs
from mirror_alerts import collect_alerts, LAYERS, DownloadTimeout


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

    def test_empty_layers(self):
        self.assertEqual(collect_alerts(lambda _: {"objectIds": []})["features"], [])


if __name__ == "__main__":
    unittest.main()
