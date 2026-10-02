"""Lightweight self-checks. Run: python -m unittest test_porcini -v (no network needed)."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import patch

import porcini as p


def rec(d, source=p.SOURCE_ARCHIVE, **kw):
    base = {"date": d, "source": source, "precipitation_sum": 0.0, "temperature_2m_max": 12.0, "temperature_2m_min": 6.0,
            "wind_speed_10m_max": 10.0, "soil_temperature_0_to_10cm": 13.0, "relative_humidity_2m_mean": 80.0,
            "soil_moisture_0_to_7cm_mean": 0.3, "precip_peak_2h_mm": None}
    base.update(kw)
    return base


def series(start, days, **kw):
    return [rec(p.iso_date(start + timedelta(days=i)), **kw) for i in range(days)]


class MergeTests(unittest.TestCase):
    def test_archive_replaces_forecast_and_keeps_forecast_only_dates(self):
        existing = [rec("2025-09-01", p.SOURCE_FORECAST, precipitation_sum=1), rec("2025-09-02", p.SOURCE_FORECAST)]
        merged = p.merge_records(existing, [rec("2025-09-01", precipitation_sum=9)])
        self.assertEqual([r["date"] for r in merged], ["2025-09-01", "2025-09-02"])
        self.assertEqual(merged[0]["source"], p.SOURCE_ARCHIVE)
        self.assertEqual(merged[0]["precipitation_sum"], 9)
        self.assertEqual(merged[1]["source"], p.SOURCE_FORECAST)

    def test_forecast_never_replaces_archive(self):
        merged = p.merge_records([rec("2025-09-01", precipitation_sum=9)], [rec("2025-09-01", p.SOURCE_FORECAST, precipitation_sum=1)])
        self.assertEqual(merged[0]["precipitation_sum"], 9)

    def test_empty_archive_lag_record_ignored(self):
        merged = p.merge_records([rec("2025-09-01", p.SOURCE_FORECAST)], [rec("2025-09-01", temperature_2m_max=None)])
        self.assertEqual(merged[0]["source"], p.SOURCE_FORECAST)

    def test_normalize_drops_bad_and_infers_legacy_source(self):
        today = date(2025, 9, 20)
        legacy = [{"date": "2025-09-01", "temperature_2m_max": 10}, {"date": "2025-09-19", "temperature_2m_max": 10}, {"date": "bad"}, "x"]
        out, dropped = p.normalize_records(legacy, today)
        self.assertEqual([r["source"] for r in out], [p.SOURCE_ARCHIVE, p.SOURCE_FORECAST])
        self.assertEqual(dropped, 2)


class IntegrityTests(unittest.TestCase):
    def test_healthy_and_broken(self):
        recs = series(date(2025, 9, 1), 5)
        db = {"schema_version": p.SCHEMA_VERSION, "locations": {"A": {"daily_records": recs, "last_sync_date": "2025-09-05"}}}
        self.assertEqual(p.check_db_integrity(db, date(2025, 9, 5)), [])
        db["locations"]["A"]["daily_records"] = [recs[0], recs[0], recs[3]]
        self.assertTrue(p.check_db_integrity(db, date(2025, 9, 5)))


class AntiSpamTests(unittest.TestCase):
    today = date(2025, 10, 10)

    def state(self, **kw):
        return {"locations": {"A": kw}}

    def test_requires_all_three_conditions(self):
        viable, watch = "✅ Viable conditions", "⚠️ Watch closely"
        ok = self.state(last_score=40, last_status=watch, last_alert_date="2025-10-01")
        self.assertTrue(p.should_alert_for_location("A", 70, viable, 65, ok, self.today))
        # too soon
        soon = self.state(last_score=40, last_status=watch, last_alert_date="2025-10-07")
        self.assertFalse(p.should_alert_for_location("A", 70, viable, 65, soon, self.today))
        # no crossing (already above)
        above = self.state(last_score=70, last_status=watch, last_alert_date="2025-10-01")
        self.assertFalse(p.should_alert_for_location("A", 80, viable, 65, above, self.today))
        # crossing but same status bucket
        same = self.state(last_score=40, last_status=viable, last_alert_date="2025-10-01")
        self.assertFalse(p.should_alert_for_location("A", 70, viable, 65, same, self.today))
        # 5 days alone must not alert
        self.assertFalse(p.should_alert_for_location("A", 10, watch, 65, self.state(last_score=10, last_status=watch, last_alert_date="2025-09-01"), self.today))


class ModeTests(unittest.TestCase):
    def test_modes(self):
        utc = timezone.utc
        self.assertEqual(p.detect_run_mode(datetime(2025, 10, 2, 18, 5, tzinfo=utc)), p.MODE_OUTLOOK)
        self.assertEqual(p.detect_run_mode(datetime(2025, 10, 3, 6, 5, tzinfo=utc)), p.MODE_FINAL)
        self.assertEqual(p.detect_run_mode(datetime(2025, 10, 3, 8, 0, tzinfo=utc)), p.MODE_DEFAULT)
        self.assertEqual(p.detect_run_mode(datetime(2025, 10, 4, 6, 0, tzinfo=utc)), p.MODE_DEFAULT)

    def test_messages_differ(self):
        r = [("A", 70, "Fri 2025-10-03", "ok")]
        self.assertIn("Outlook", p.build_alert_message(r, "u", p.MODE_OUTLOOK))
        self.assertIn("GO - 70%", p.build_alert_message(r, "u", p.MODE_FINAL, 65))
        self.assertIn("Forecast", p.build_alert_message(r, "u"))


LOC = {"tree_species": ["Spruce"], "aspect": "", "tree_density": "", "soil_pH": "acidic"}


class ConfigTests(unittest.TestCase):
    def test_load_config_prefers_config_json_environment(self):
        with patch.dict("os.environ", {"CONFIG_JSON": '{"ALERT_THRESHOLD": 70}'}):
            self.assertEqual(p.load_config(), {"ALERT_THRESHOLD": 70})

    def test_load_config_rejects_non_object_json(self):
        with patch.dict("os.environ", {"CONFIG_JSON": "[]"}):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                p.load_config()


class ScoringTests(unittest.TestCase):
    def test_timing_bonus_and_penalty(self):
        # rain trigger 7-12 days before: Sat (+15) vs Mon (-20)
        start = date(2025, 9, 1)
        recs = series(start, 40, precipitation_sum=3.0)
        recs[5]["precipitation_sum"] = 30.0  # 2025-09-06
        h = p.History(recs)
        sat, mon = date(2025, 9, 13), date(2025, 9, 15)  # lags 7 and 9 after the trigger
        s_sat = p.calculate_score_for_day(LOC, h.get(sat), h, [])[0]
        s_mon = p.calculate_score_for_day(LOC, h.get(mon), h, [])[0]
        self.assertGreater(s_sat, s_mon + 30)

    def test_wet_baseline_vs_humidity_penalty(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        dry = series(date(2025, 8, 20), 60, precipitation_sum=3.0, relative_humidity_2m_mean=40.0)
        self.assertEqual(base - 15, p.calculate_score_for_day(LOC, dry[-1], p.History(dry), [])[0])

    def test_affinity_limited_to_two_years(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        recent = [{"date": "2025-05-01", "yield_tier": "large"}, {"date": "2025-06-01", "yield_tier": "medium"}]
        old = [{"date": "2022-05-01", "yield_tier": "large"}, {"date": "2022-06-01", "yield_tier": "medium"}]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        self.assertEqual(p.calculate_score_for_day(LOC, day, h, recent)[0], base + 10)
        self.assertEqual(p.calculate_score_for_day(LOC, day, h, old)[0], base)

    def test_quality_flags(self):
        hot = series(date(2025, 8, 20), 30, precipitation_sum=5.0, temperature_2m_max=22.0)
        cool = series(date(2025, 8, 20), 30, precipitation_sum=5.0, temperature_2m_max=11.0)
        self.assertIn("MAGGOT", p.calculate_score_for_day(LOC, hot[-1], p.History(hot), [])[2])
        self.assertIn("PRIME", p.calculate_score_for_day(LOC, cool[-1], p.History(cool), [])[2])

    def test_microclimate_window(self):
        self.assertEqual(p.moisture_retention_days({"aspect": "north"}), 10)
        self.assertEqual(p.moisture_retention_days({"aspect": "south"}), 5)

    def test_daily_scores_incremental(self):
        recs = series(date(2025, 9, 1), 20)
        store = {}
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 20)
        self.assertEqual(set(store["daily_scores"]), {r["date"] for r in recs})
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 0)  # archive days are not rescored
        recs[-1]["source"] = p.SOURCE_FORECAST
        store["daily_scores"][recs[-1]["date"]]["source"] = p.SOURCE_FORECAST
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 1)  # forecast days are


class RunoffBackfillTests(unittest.TestCase):
    @staticmethod
    def hourly(day, peak):
        vals = [0.0] * 24
        vals[5], vals[6] = peak, 0.0
        return {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(24)], "precipitation": vals}}

    def test_backfill_fills_and_marks_basis(self):
        records = [rec("2025-09-01", precip_peak_2h_mm=None, precipitation_sum=20.0), rec("2025-09-02", source=p.SOURCE_FORECAST)]
        changed = p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=lambda *a: self.hourly("2025-09-01", 14.0))
        self.assertEqual(changed, ["2025-09-01"])
        self.assertEqual(records[0]["precip_peak_2h_mm"], 14.0)
        self.assertEqual(records[0]["precip_peak_basis"], p.PEAK_BASIS_BACKFILL)
        self.assertNotIn("precip_peak_basis", records[1])
        self.assertTrue(p.has_runoff(p.History(records), date(2025, 9, 1)))

    def test_failure_is_soft_and_retried(self):
        records = [rec("2025-09-01")]
        self.assertEqual(p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=lambda *a: None), [])
        def boom(*a):
            raise RuntimeError("down")
        self.assertEqual(p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=boom), [])
        self.assertTrue(p.needs_runoff_backfill(records[0]))

    def test_backfill_forces_rescore(self):
        loc = {"name": "A", "aspect": "N"}
        records = [rec(f"2025-09-0{i}", precipitation_sum=20.0) for i in range(1, 4)]
        store = {}
        p.update_daily_scores(loc, records, store)
        store["pending_rescore"] = ["2025-09-02"]
        self.assertEqual(p.update_daily_scores(loc, records, store), 1)


class LegacyMigrationTests(unittest.TestCase):
    def test_quarantine_and_integrity(self):
        db = {"schema_version": p.SCHEMA_VERSION, "locations": {}, "2024-10-02": {"precipitation_sum": 5.0}, "Old Spot": {"time": []}}
        self.assertTrue(any("top-level date keys" in i for i in p.check_db_integrity(db)))
        self.assertEqual(p.migrate_legacy_entries(db), 2)
        self.assertEqual(db[p.LEGACY_KEY]["2024-10-02"], {"precipitation_sum": 5.0})
        self.assertEqual(p.check_db_integrity(db, date(2025, 1, 1)), [])
        self.assertEqual(p.migrate_legacy_entries(db), 0)


class FridayPolicyTests(unittest.TestCase):
    friday = date(2025, 10, 10)

    def test_both_modes(self):
        thursday_alerted = {"locations": {"A": {"last_alert_date": "2025-10-09", "last_alert_mode": p.MODE_OUTLOOK}, "B": {}}}
        only, allp = p.FRIDAY_POLICY_THURSDAY_ONLY, p.FRIDAY_POLICY_ALL_ABOVE
        self.assertTrue(p.should_confirm_for_location("A", thursday_alerted, self.friday, only, 10, 65))
        self.assertFalse(p.should_confirm_for_location("B", thursday_alerted, self.friday, only, 90, 65))
        self.assertTrue(p.should_confirm_for_location("B", thursday_alerted, self.friday, allp, 70, 65))
        self.assertFalse(p.should_confirm_for_location("B", thursday_alerted, self.friday, allp, 40, 65))
        thursday_alerted["locations"]["A"]["last_confirmation_date"] = "2025-10-10"
        self.assertFalse(p.should_confirm_for_location("A", thursday_alerted, self.friday, allp, 90, 65))

    def test_default_policy(self):
        self.assertEqual(p.resolve_friday_policy({}), p.FRIDAY_POLICY_THURSDAY_ONLY)
        self.assertEqual(p.resolve_friday_policy({"FRIDAY_POLICY": "bogus"}), p.FRIDAY_POLICY_THURSDAY_ONLY)
        self.assertEqual(p.resolve_friday_policy({"FRIDAY_POLICY": p.FRIDAY_POLICY_ALL_ABOVE}), p.FRIDAY_POLICY_ALL_ABOVE)


if __name__ == "__main__":
    unittest.main()
