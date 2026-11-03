import unittest
from datetime import date

import porcini as p
import soil
from test_porcini import LOC, series


def payload(clay, sand, silt, ph=None):
    layers = [{"name": n, "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": v}}, {"values": {"mean": v}}]}
              for n, v in (("clay", clay), ("sand", sand), ("silt", silt))]
    if ph is not None:
        layers.append({"name": "phh2o", "unit_measure": {"d_factor": 10}, "depths": [{"values": {"mean": ph}}]})
    return {"properties": {"layers": layers}}


class SoilTests(unittest.TestCase):
    def test_texture_classes(self):
        self.assertEqual(soil.usda_texture(5, 90, 5), "sand")
        self.assertEqual(soil.usda_texture(50, 20, 30), "clay")
        self.assertEqual(soil.usda_texture(20, 40, 40), "loam")

    def test_parse_payload(self):
        profile = soil.parse_soil_payload(payload(50, 900, 50, ph=52))
        self.assertEqual(profile["texture"], "sand")
        self.assertEqual(profile["ph"], 5.2)
        self.assertFalse(profile["calcareous"])
        self.assertIn("pH 5.2", soil.describe_soil(profile))
        self.assertTrue(soil.parse_soil_payload(payload(100, 300, 600, ph=79))["calcareous"])

    def test_parse_rejects_bad_payloads(self):
        self.assertIsNone(soil.parse_soil_payload({}))
        self.assertIsNone(soil.parse_soil_payload(payload(0, 0, 0)))
        self.assertEqual(soil.describe_soil(None), "")

    def test_cache_and_fail_soft(self):
        store, calls = {}, []
        self.assertIsNone(soil.ensure_soil_profile(store, 1, 2, fetch=lambda *a: calls.append(a)))
        self.assertNotIn("soil", store)
        profile = soil.parse_soil_payload(payload(100, 800, 100))
        self.assertEqual(soil.ensure_soil_profile(store, 1, 2, fetch=lambda *a: profile), profile)
        self.assertEqual(soil.ensure_soil_profile(store, 1, 2, fetch=lambda *a: self.fail("refetched")), profile)


class ElevationTests(unittest.TestCase):
    def test_resolution_order_and_fail_soft(self):
        boom = lambda *a: self.fail("fetched")
        self.assertEqual(soil.resolve_elevation(120, 40, 1, 2, fetch=boom), 120)
        self.assertEqual(soil.resolve_elevation(None, 40, 1, 2, fetch=boom), 40)
        self.assertEqual(soil.resolve_elevation(None, None, 1, 2, fetch=lambda *a: 35), 35)
        self.assertIsNone(soil.resolve_elevation(None, None, 1, 2, fetch=lambda *a: None))


class StatusTests(unittest.TestCase):
    def test_status_follows_configured_threshold(self):
        import report
        self.assertEqual(report.display_status(47, "✅ Viable conditions", 55), "⚠️ Watch closely")
        self.assertEqual(report.display_status(58, "⚠️ Watch closely", 55), "✅ Viable conditions")
        self.assertEqual(report.display_status(10, "💧 Too dry – low soil moisture", 55), "💧 Too dry – low soil moisture")


class InsightTests(unittest.TestCase):
    def test_factors_sum_to_score(self):
        recs = series(date(2025, 9, 1), 30, precipitation_sum=4.0)
        store = {}
        p.update_daily_scores(LOC, recs, store)
        checked = 0
        for day in store["daily_scores"].values():
            if day.get("factors"):
                self.assertEqual(len(day["factors"]), len(p.FACTOR_KEYS))
                self.assertEqual(sum(day["factors"]), day["score"])
                checked += 1
        self.assertGreater(checked, 0)

    def test_flush_hint(self):
        recs = series(date(2025, 9, 1), 10, precipitation_sum=0.0)
        self.assertIn("No significant rain", p.rain_flush_hint(recs, date(2025, 9, 10))["text"])
        recs[6]["precipitation_sum"] = 12.0
        hint = p.rain_flush_hint(recs, date(2025, 9, 10))
        self.assertEqual(hint["days_since_rain"], 3)
        self.assertIn("7–14 days", hint["text"])

    def test_recommendation(self):
        self.assertTrue(p.build_recommendation(90, "Viable conditions", 60, None).startswith("Go"))
        self.assertTrue(p.build_recommendation(30, "Viable conditions", 60, 3).startswith("Wait"))
        self.assertIn("unavailable", p.build_recommendation(0, "", 60, None, available=False))


if __name__ == "__main__":
    unittest.main()
