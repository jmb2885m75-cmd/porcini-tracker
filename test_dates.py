"""Date formatting/parsing and no-find observation consistency. Run: python -m unittest test_dates -v"""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import dates
import harvest as h
import porcini as p
from test_porcini import LOC, rec, series


class DateUtilTests(unittest.TestCase):
    def test_display_format(self):
        self.assertEqual(dates.format_display_date(date(2026, 10, 2)), "02 October 2026")
        self.assertEqual(dates.format_display_date("2026-10-02"), "02 October 2026")
        self.assertEqual(dates.format_display_date(datetime(2026, 10, 2, 23, 59, tzinfo=timezone.utc)), "02 October 2026")

    def test_accepted_inputs_normalize_to_iso(self):
        for text in ("2026-10-01", "01.10.2026", "01 October 2026", "1 october 2026", " 01. October 2026 "):
            self.assertEqual(dates.normalize_to_iso(text), "2026-10-01", text)

    def test_invalid_inputs(self):
        for bad in ("2026-02-30", "31.04.2026", "01 Octobr 2026", "2026.10.02", "10/01/2026", "", None, 5):
            with self.assertRaises(ValueError, msg=repr(bad)):
                dates.normalize_to_iso(bad)

    def test_date_only_has_no_timezone_shift(self):
        for tz in (timezone(timedelta(hours=-11)), timezone(timedelta(hours=13))):
            self.assertEqual(dates.format_display_date(datetime(2026, 10, 1, 0, 30, tzinfo=tz)), "01 October 2026")
        self.assertEqual(dates.parse_user_date("2026-10-01"), date(2026, 10, 1))

    def test_weekend_best_uses_display_format(self):
        recs = series(date(2026, 9, 25), 10, precipitation_sum=5.0)
        _, label, _, _ = p.find_weekend_best(LOC, recs, [], None, date(2026, 10, 1))
        self.assertRegex(label, r"^[A-Z][a-z]{2} \d{2} [A-Z][a-z]+ \d{4}$")

    def test_harvest_intake_accepts_all_formats(self):
        for text in ("2026-09-15", "15.09.2026", "15 September 2026"):
            raw = {"location": "A", "date": text, "observation_type": "no_mushrooms"}
            self.assertEqual(h.validate_harvest(raw, None, date(2026, 10, 1))["date"], "2026-09-15")
        with self.assertRaises(h.HarvestError):
            h.validate_harvest({"location": "A", "date": "31.02.2026", "observation_type": "no_mushrooms"}, None, date(2026, 10, 1))


class NoFindConsistencyTests(unittest.TestCase):
    def setUp(self):
        self.recs = series(date(2026, 8, 20), 60, precipitation_sum=5.0)
        self.hist = p.History(self.recs)
        self.day = date(2026, 10, 1)

    def score(self, harvests):
        return p.calculate_score_for_day(LOC, self.hist.get(self.day), self.hist, harvests)

    def test_same_day_no_find_lowers_forecast_in_any_input_format(self):
        baseline = self.score([])[0]
        for text in ("2026-10-01", "01.10.2026", "01 October 2026"):
            score = self.score([{"date": text, "observation_type": "no_mushrooms"}])
            self.assertLessEqual(score[0], 20, text)
            self.assertLess(score[0], baseline)

    def test_no_find_cap_not_lifted_by_affinity_bonus(self):
        prior = [{"date": f"2026-09-{d:02d}", "yield_tier": "large", "cap_stage": "prime"} for d in (10, 15)]
        no_find = {"date": "2026-10-01", "observation_type": "no_mushrooms"}
        self.assertLessEqual(self.score(prior + [no_find])[0], 20)

    def test_zero_observations_are_kept(self):
        zero = rec("2026-09-01", precipitation_sum=0, temperature_2m_max=0.0)
        self.assertTrue(p.has_observation(zero))
        self.assertFalse(p.has_observation(rec("2026-09-01", temperature_2m_max=None)))
        merged = p.merge_records([], [zero])
        self.assertEqual(merged[0]["precipitation_sum"], 0)
        self.assertEqual(merged[0]["temperature_2m_max"], 0.0)

    def test_new_observation_invalidates_cached_scores(self):
        a = p.location_signature(dict(LOC, past_harvests=[]))
        b = p.location_signature(dict(LOC, past_harvests=[{"date": "2026-10-01", "observation_type": "no_mushrooms"}]))
        self.assertNotEqual(a, b)
        store = {}
        loc = dict(LOC, past_harvests=[])
        p.update_daily_scores(loc, self.recs, store)
        before = store["daily_scores"]["2026-10-01"]["score"]
        loc["past_harvests"] = [{"date": "2026-10-01", "observation_type": "no_mushrooms"}]
        p.update_daily_scores(loc, self.recs, store)
        self.assertLessEqual(store["daily_scores"]["2026-10-01"]["score"], 20)
        self.assertLess(store["daily_scores"]["2026-10-01"]["score"], before)


class DashboardUiTests(unittest.TestCase):
    def test_accessible_explanations_and_daily_timeline_are_generated(self):
        analysis = [{
            "name": "Test spot",
            "best_day": "Fri 02 October 2026",
            "best_score": 71,
            "status": "✅ Viable conditions",
            "soil_moisture": 0.3,
            "quality": "",
            "records": [{
                "date": "2026-10-02", "temperature_2m_max": 14, "temperature_2m_min": 7,
                "precipitation_sum": 5, "wind_speed_10m_max": 10,
                "soil_temperature_0_to_7cm_mean": 13, "relative_humidity_2m_mean": 80,
                "soil_moisture_0_to_7cm_mean": 0.3, "source": "forecast",
            }],
            "scores": {"2026-10-02": {"score": 71, "status": "✅ Viable conditions", "quality": ""}},
            "harvests": [{"date": "2026-10-02", "observation_type": "no_mushrooms", "origin": "log"}],
            "backtest": {},
        }]
        html = p.generate_dashboard_html({"ALERT_THRESHOLD": 65}, analysis)
        self.assertIn('<dialog id="info-dialog"', html)
        self.assertIn('aria-modal="true"', html)
        self.assertIn('aria-haspopup="dialog"', html)
        self.assertIn("7 forecast days in total", html)
        self.assertIn("not a measured chance", html)
        self.assertIn("valid evidence", html)
        self.assertIn('id="timeline-scroll"', html)
        self.assertIn("ArrowLeft", html)
        self.assertIn("day-prev", html)
        self.assertIn("🍄' : '❌'", html)
        self.assertIn("02 October 2026", html)


if __name__ == "__main__":
    unittest.main()
