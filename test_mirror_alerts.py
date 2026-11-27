import unittest
from mirror_alerts import collect_alerts, START_URL


def feature(id):
    return {"type": "Feature", "id": id, "geometry": None, "properties": {}}


class PaginationTests(unittest.TestCase):
    def test_all_pages_and_duplicates(self):
        next_url = "https://api.weather.gov/alerts?cursor=opaque%2Btoken"
        pages = {
            START_URL: {"type": "FeatureCollection", "features": [feature("b")],
                        "pagination": {"next": next_url}},
            next_url: {"type": "FeatureCollection", "features": [feature("a"), feature("b")]},
        }
        result = collect_alerts(pages.__getitem__, lambda _: None)
        self.assertEqual(result["features"], [feature("a"), feature("b")])

    def test_repeated_cursor_fails(self):
        with self.assertRaises(ValueError):
            collect_alerts(lambda _: {"type": "FeatureCollection", "features": [],
                                     "pagination": {"next": START_URL}}, lambda _: None)

    def test_failed_second_page(self):
        def fetch(url):
            if url != START_URL:
                raise OSError("network failure")
            return {"type": "FeatureCollection", "features": [feature("a")],
                    "pagination": {"next": "?cursor=second"}}
        with self.assertRaises(OSError):
            collect_alerts(fetch, lambda _: None)

    def test_empty_snapshot(self):
        self.assertEqual(collect_alerts(lambda _: {"type": "FeatureCollection", "features": []}),
                         {"type": "FeatureCollection", "features": []})


if __name__ == "__main__":
    unittest.main()
