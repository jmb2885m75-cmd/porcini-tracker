"""Lightweight self-checks. Run: python -m unittest test_porcini -v (no network needed)."""
import json
import io
import re
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import porcini as p
import report as report_module


def rec(d, source=p.SOURCE_ARCHIVE, **kw):
    base = {"date": d, "source": source, "precipitation_sum": 0.0, "temperature_2m_max": 12.0, "temperature_2m_min": 6.0,
            "wind_speed_10m_max": 10.0, "soil_temperature_0_to_7cm_mean": 13.0, "relative_humidity_2m_mean": 80.0,
            "soil_moisture_0_to_7cm_mean": 0.3, "precip_peak_2h_mm": None}
    base.update(kw)
    return base


def series(start, days, **kw):
    return [rec(p.iso_date(start + timedelta(days=i)), **kw) for i in range(days)]


class MergeTests(unittest.TestCase):
    def test_open_meteo_soil_temperature_field_is_supported(self):
        self.assertIn("soil_temperature_0_to_7cm_mean", p.DAILY_FIELDS)
        self.assertNotIn("soil_temperature_0_to_10cm", p.DAILY_FIELDS)
        payload = {"daily": {
            "time": ["2025-09-01"],
            "soil_temperature_0_to_7cm_mean": [13.5],
        }}
        self.assertEqual(p.parse_open_meteo_payload(payload, p.SOURCE_ARCHIVE)[0]["soil_temperature_0_to_7cm_mean"], 13.5)

    def test_normalize_migrates_legacy_soil_temperature_key(self):
        rows, _ = p.normalize_records([{"date": "2025-09-01", "temperature_2m_max": 12, "soil_temperature_0_to_10cm": 13}], date(2025, 9, 2))
        self.assertEqual(rows[0]["soil_temperature_0_to_7cm_mean"], 13)
        self.assertNotIn("soil_temperature_0_to_10cm", rows[0])

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

    def test_archive_replaces_sample_record(self):
        sample = rec("2025-09-01", p.SOURCE_SAMPLE, precipitation_sum=1)
        archive = rec("2025-09-01", p.SOURCE_ARCHIVE, precipitation_sum=9)
        merged = p.merge_records([sample], [archive])
        self.assertEqual(merged[0]["source"], p.SOURCE_ARCHIVE)
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


class PrivacyTests(unittest.TestCase):
    @staticmethod
    def contains_coordinate_field(value):
        if isinstance(value, dict):
            return any(
                key in {"latitude", "longitude", "elevation_m"} or PrivacyTests.contains_coordinate_field(child)
                for key, child in value.items()
            )
        if isinstance(value, list):
            return any(PrivacyTests.contains_coordinate_field(child) for child in value)
        return False

    def test_committed_data_files_do_not_contain_coordinate_fields(self):
        for filename in ("porcini_db.json", "harvest_log.json", "alert_state.json"):
            path = Path(filename)
            if path.exists():
                with path.open(encoding="utf-8") as source:
                    self.assertFalse(self.contains_coordinate_field(json.load(source)), filename)
        report = p.REPORT_PATH.read_text(encoding="utf-8") if p.REPORT_PATH.exists() else ""
        self.assertNotRegex(report, r'"(?:latitude|longitude|elevation_m)"\s*:')

    def test_public_dashboard_uses_aliases_and_strict_csp(self):
        cfg = {"LOCATIONS": [{"name": "Private woodland"}]}
        analysis = [{
            "name": "Private woodland", "best_day": "N/A", "best_score": 0,
            "status": "Unavailable", "soil_moisture": 0.0, "quality": "",
            "records": [], "scores": {}, "harvests": [{"location": "Private woodland", "date": "2025-09-01"}],
            "backtest": {},
        }]
        report = p.generate_dashboard_html(
            cfg, analysis, "1. Private woodland — 80/100", harvest_log={"harvests": []}
        )
        self.assertIn("Location 1", report)
        self.assertNotIn("Private woodland", report)
        self.assertIn("Content-Security-Policy", report)
        self.assertIn("default-src 'none'", report)
        self.assertNotIn("{{ALERT_PREVIEW}}", report)
        self.assertIn("<h2>📨 Alert Preview</h2>", report)
        for marker in ("function todayStr()", "today-row", "today-line"):
            self.assertIn(marker, report)
        nonce = re.search(r"script-src 'nonce-([^']+)'", report)
        self.assertIsNotNone(nonce)
        self.assertIn(f'<script nonce="{nonce.group(1)}">', report)

    def test_dashboard_payload_embeds_alert_and_index_reads_it(self):
        cfg = {"ALERT_THRESHOLD": 65, "LOCATIONS": [{"name": "Private woodland"}]}
        report = p.generate_dashboard_html(cfg, [], "1. Private woodland — 80/100")
        match = re.search(r'<script id="porcini-data" type="application/json">(.*?)</script>', report, re.S)
        alert = json.loads(match.group(1))["alert"]
        self.assertIn("Location 1", alert["message"])
        self.assertNotIn("Private woodland", alert["message"])
        self.assertFalse(alert["will_send"])
        index = p.generate_index_html()
        self.assertIn("porcini_report.html", index)
        self.assertIn("porcini-data", index)
        self.assertNotIn("__CSP_NONCE__", index)
        self.assertNotIn("Weekend Porcini Forecast", index)

    def test_dashboard_template_requires_alert_preview_placeholder(self):
        with patch.object(report_module, "TEMPLATE_PATH", Mock(read_text=Mock(return_value="<!doctype html>"))):
            with self.assertRaisesRegex(
                ValueError,
                r"index\.template\.html is missing the \{\{ALERT_PREVIEW\}\} placeholder for alert preview injection",
            ):
                report_module.generate_dashboard_html({}, [])

    def test_location_coordinates_are_not_persisted(self):
        db = {"locations": {"Location 1": {
            "latitude": 12.3, "longitude": 45.6, "elevation_m": 7,
            "daily_records": [], "pending_rescore": [],
        }}}
        records = [rec("2025-09-20")]
        with patch.object(p, "fetch_archive_day_range", return_value=[]), \
                patch.object(p, "fetch_forecast_day_range", return_value=records), \
                patch.object(p, "backfill_runoff_data", return_value=[]):
            p.ensure_location_history("Location 1", db, 12.3, 45.6, 7, date(2025, 9, 20))
        self.assertFalse(self.contains_coordinate_field(db))

    def test_failed_archive_sync_does_not_advance_last_sync_date(self):
        db = {"locations": {"L": {"daily_records": [rec("2025-09-01", p.SOURCE_ARCHIVE)], "last_sync_date": "2025-09-10"}}}
        with patch.object(p, "fetch_archive_day_range", return_value=[]), \
                patch.object(p, "fetch_forecast_day_range", return_value=[rec("2025-09-20")]), \
                patch.object(p, "backfill_runoff_data", return_value=[]):
            p.ensure_location_history("L", db, 1.0, 2.0, 3, date(2025, 9, 20))
        self.assertEqual(db["locations"]["L"]["last_sync_date"], "2025-09-10")

    def test_database_load_strips_coordinates_from_unconfigured_locations(self):
        legacy = {"locations": {"Location 99": {
            "latitude": 1.2, "longitude": 3.4, "elevation_m": 5, "daily_records": [],
        }}}
        with patch.object(p, "load_json_safe", return_value=legacy):
            loaded = p.load_or_init_db()
        self.assertFalse(self.contains_coordinate_field(loaded))


@unittest.skipUnless(p.DB_PATH.exists() and p.REPORT_PATH.exists(), "runtime data files live on the data branch")
class HistoricalWeatherTests(unittest.TestCase):
    def test_2023_weather_covers_full_year_and_has_scores(self):
        db = p.load_json(p.DB_PATH, {})
        expected_dates = [(date(2023, 1, 1) + timedelta(days=i)).isoformat() for i in range(365)]
        required_fields = {
            "date", "temperature_2m_max", "temperature_2m_min", "precipitation_sum",
            "soil_temperature_0_to_7cm_mean",
            "relative_humidity_2m_mean", "soil_moisture_0_to_7cm_mean", "source",
        }
        for name, location in db["locations"].items():
            records = [r for r in location["daily_records"] if r["date"].startswith("2023-")]
            self.assertEqual([r["date"] for r in records], expected_dates, name)
            self.assertTrue(all(required_fields <= r.keys() for r in records), name)
            self.assertTrue(all(r["source"] == p.SOURCE_SAMPLE for r in records), name)
            self.assertTrue(all(day in location["daily_scores"] for day in expected_dates), name)

    def test_dashboard_payload_includes_2023_score_dates(self):
        report = p.REPORT_PATH.read_text(encoding="utf-8")
        match = re.search(r'<script id="porcini-data" type="application/json">(.*?)</script>', report, re.S)
        self.assertIsNotNone(match)
        payload = json.loads(match.group(1))
        for location in payload["locations"]:
            records = [row for row in location["records"] if row[0].startswith("2023-")]
            dates = {row[0] for row in records}
            self.assertTrue(any(score[0].startswith("2023-") for score in location["scores"]), location["name"])
            self.assertEqual(len(dates), 365, location["name"])
            self.assertEqual(len(records), 365, location["name"])
            self.assertTrue(all(row[8] == p.SOURCE_SAMPLE for row in records))
        self.assertIn("<option>sample</option>", report)
        self.assertIn("generated example weather, not observations", report)


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

    def test_alert_gap_is_configurable_and_defaults_to_five_days(self):
        state = self.state(last_score=40, last_status="⚠️ Watch closely", last_alert_date="2025-10-06")
        self.assertEqual(p.configured_alert_gap_days({}), 5)
        self.assertEqual(p.configured_alert_gap_days({"MIN_ALERT_GAP_DAYS": 2}), 2)
        self.assertEqual(p.configured_alert_gap_days({"MIN_ALERT_GAP_DAYS": "bad"}), 5)
        self.assertFalse(p.should_alert_for_location("A", 70, "✅ Viable conditions", 65, state, self.today))
        self.assertTrue(p.should_alert_for_location(
            "A", 70, "✅ Viable conditions", 65, state, self.today,
            p.configured_alert_gap_days({"MIN_ALERT_GAP_DAYS": 4}),
        ))


class VerdictTests(unittest.TestCase):
    def test_score_tiers_and_configured_go_threshold(self):
        cases = [
            (14, "🚫 Not worth it"),
            (15, "😐 Unlikely"),
            (35, "🤔 Long shot"),
            (50, "👀 Worth a look"),
            (54, "👀 Worth a look"),
            (55, "👍 Favourable conditions"),
            (64, "👍 Favourable conditions"),
            (65, "🔥 Very favourable conditions"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(p.score_verdict_tag(score, "⚠️ Watch closely"), expected)
        self.assertEqual(p.score_verdict_tag(70, "⚠️ Watch closely", 70), "🔥 Very favourable conditions")
        self.assertEqual(p.score_verdict_tag(69, "⚠️ Watch closely", 70), "👀 Worth a look")

    def test_special_statuses_override_score_tier(self):
        cases = [
            ("❌ Outside mushroom season", "🍂 Off season"),
            ("❄️ SEASON TERMINATED BY FROST", "❄️ Season over"),
            ("🔥 SEASON DELAYED BY HIGH SOIL TEMP", "🔥 Too warm – wait"),
            ("💧 Too dry – drought unbroken", "💧 Too dry – wait"),
            ("🔎 No mushrooms found (field observation)", "🔎 No mushrooms found (field observation)"),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(p.score_verdict_tag(0, status), expected)

    def test_score_color_clamps_red_and_green(self):
        self.assertEqual(p.score_color(27), "hsl(0 80% 58%)")
        self.assertEqual(p.score_color(30), "hsl(0 80% 58%)")
        self.assertEqual(p.score_color(60), "hsl(120 80% 58%)")
        self.assertEqual(p.score_color(100), "hsl(120 80% 58%)")


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
        self.assertIn("GO - 70/100 index", p.build_alert_message(r, "u", p.MODE_FINAL, 65))
        self.assertIn("70/100 index – 🔥 Very favourable conditions (Fri 2025-10-03)", p.build_alert_message(r, "u"))
        self.assertIn("Forecast", p.build_alert_message(r, "u"))

    def test_ranked_entries_and_dashboard_have_one_blank_line_between_them(self):
        message = p.build_alert_message(
            [
                ("A", 70, "Fri 2025-10-03", "⚠️ Watch closely", "first explanation"),
                ("B", 60, "Sat 2025-10-04", "⚠️ Watch closely", "second explanation"),
            ],
            "https://example.test",
        )
        self.assertIn("Why: first explanation\n\n2.", message)
        self.assertIn("Why: second explanation\n\n🌐 Dashboard: https://example.test", message)
        self.assertNotIn("Why: second explanation\n\n\n🌐 Dashboard", message)


LOC = {"tree_species": ["Spruce"], "aspect": "", "tree_density": "", "soil_pH": "acidic"}


class ConfigTests(unittest.TestCase):
    def test_load_config_prefers_config_json_environment(self):
        with patch.dict("os.environ", {"CONFIG_JSON": '{"ALERT_THRESHOLD": 70}'}):
            self.assertEqual(p.load_config(), {"ALERT_THRESHOLD": 70})

    def test_load_config_rejects_non_object_json(self):
        with patch.dict("os.environ", {"CONFIG_JSON": "[]"}):
            with self.assertRaisesRegex(ValueError, "JSON object"):
                p.load_config()


class NotificationTests(unittest.TestCase):
    def test_telegram_requires_api_success_response(self):
        response = Mock()
        response.json.return_value = {"ok": False, "description": "Unauthorized"}
        with patch.object(p.requests, "post", return_value=response):
            self.assertFalse(p.send_telegram("token", "chat", "message"))

    def test_telegram_accepts_api_success_response(self):
        response = Mock()
        response.json.return_value = {"ok": True, "result": {"message_id": 1}}
        with patch.object(p.requests, "post", return_value=response):
            self.assertTrue(p.send_telegram("token", "chat", "message"))

    def test_test_alert_returns_failure_when_delivery_fails(self):
        with (
            patch("sys.argv", ["porcini.py", "--test-alert"]),
            patch.object(p, "load_config", return_value={}),
            patch.object(p, "load_or_init_db", return_value={}),
            patch.object(p, "build_alert_state", return_value={}),
            patch.object(p, "dispatch_notification", return_value=False),
        ):
            self.assertEqual(p.main(), 1)

    def test_test_alert_returns_success_when_delivery_succeeds(self):
        with (
            patch("sys.argv", ["porcini.py", "--test-alert"]),
            patch.object(p, "load_config", return_value={}),
            patch.object(p, "load_or_init_db", return_value={}),
            patch.object(p, "build_alert_state", return_value={}),
            patch.object(p, "dispatch_notification", return_value=True),
        ):
            self.assertEqual(p.main(), 0)


class ScoringTests(unittest.TestCase):
    def test_default_season_includes_full_late_decay_window(self):
        self.assertTrue(p.month_day_window(date(2025, 12, 10)))
        self.assertFalse(p.month_day_window(date(2025, 12, 11)))
        self.assertEqual(p.LATE_SEASON_DECAY_END, (12, 1))
        self.assertEqual(p.late_season_decay_penalty(date(2025, 12, 1), LOC), p.LATE_SEASON_DECAY_MAX)

    def test_soil_temperature_uses_seven_prior_days_and_requires_five_values(self):
        target = date(2025, 10, 18)
        records = series(target - timedelta(days=7), 7, soil_temperature_0_to_7cm_mean=10.0)
        hist = p.History(records)
        # v18: soil temperature is a primary signal (up to +20, down to -10) instead of +3/-5.
        self.assertEqual(p.soil_temperature_score(hist, target), 20)
        self.assertEqual(p.score_breakdown(LOC, rec(target.isoformat()), hist)["soil_temperature"], 20)

        for record in records:
            record["soil_temperature_0_to_7cm_mean"] = 6.0
        self.assertEqual(p.soil_temperature_score(hist, target), 14)
        for record in records:
            record["soil_temperature_0_to_7cm_mean"] = 20.0
        self.assertEqual(p.soil_temperature_score(hist, target), 5)
        for record in records:
            record["soil_temperature_0_to_7cm_mean"] = 35.0
        self.assertEqual(p.soil_temperature_score(hist, target), -p.SOIL_TEMPERATURE_PENALTY_MAX)
        self.assertEqual(p.soil_temperature_score(p.History(records[:4]), target), 0)

    def test_soil_temperature_uses_configured_optimal_range(self):
        target = date(2025, 10, 18)
        hist = p.History(series(target - timedelta(days=7), 7, soil_temperature_0_to_7cm_mean=20.0))
        loc = dict(LOC, soil_temperature_optimal_range=[18, 22])
        self.assertEqual(p.soil_temperature_score(hist, target, loc), 20)
        self.assertEqual(p.soil_temperature_score(hist, target, dict(LOC, soil_temperature_optimal_range="bad")), 5)
        self.assertNotEqual(p.location_signature(loc), p.location_signature(LOC))

    def test_model_thresholds_host_tree_removal_and_weather_fields(self):
        self.assertFalse(hasattr(p, "HOST_TREES"))
        self.assertFalse(hasattr(p, "is_host_tree"))
        self.assertEqual(p.DEFAULT_ALERT_THRESHOLD, 45)
        self.assertEqual(p.VERY_FAVOURABLE_THRESHOLD, 65)
        self.assertEqual(tuple(tier[0] for tier in p.VERDICT_LOW_TIERS), (15, 35, 50))
        self.assertEqual((p.SCORE_COLOR_MIN, p.SCORE_COLOR_MAX), (30, 60))
        self.assertEqual(p.POST_FLUSH_FULL_BASE_SCORE, 30)
        self.assertEqual(p.INITIAL_ARCHIVE_DAYS, 3650)
        self.assertNotIn("wind_speed_10m_max", p.DAILY_FIELDS)

        records = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(records)
        day = records[-1]
        baseline = p.calculate_score_for_day({}, day, hist, [])[0]
        self.assertEqual(
            p.calculate_score_for_day({"tree_species": ["Birch", "Spruce"]}, day, hist, [])[0],
            baseline,
        )
        self.assertEqual(p.MODEL_VERSION, 18)

    def test_depletion_tiers_and_recovery_periods(self):
        self.assertEqual(p.FLUSH_DEPLETION_TIERS, ((1, 35), (3, 25), (6, 12)))
        self.assertEqual(p.DEFAULT_DEPLETION_RECOVERY_DAYS, 6)
        self.assertEqual(p.SMALL_HARVEST_DEPLETION_RECOVERY_DAYS, 5)

    def test_last_seven_days_of_rain_do_not_contribute_and_trigger_window_does(self):
        target = date(2025, 10, 18)
        records = series(target - timedelta(days=30), 31, precipitation_sum=0.0)
        for record in records[-8:-1]:  # days 1-7 before the scored day
            record["precipitation_sum"] = 10.0
        hist = p.History(records)
        self.assertEqual(p.score_breakdown(LOC, hist.get(target), hist)["rain"], 0)

        for record in records[-8:-1]:
            record["precipitation_sum"] = 0.0
        for record in records[-22:-8]:  # days 8-21 before the scored day
            record["precipitation_sum"] = 1.0
        hist = p.History(records)
        self.assertEqual(p.score_breakdown(LOC, hist.get(target), hist)["rain"], p.rainfall_score(14.0))

    def test_background_rain_uses_lower_weight_and_higher_saturation(self):
        target = date(2025, 10, 18)
        records = series(target - timedelta(days=60), 61, precipitation_sum=0.0)
        for record in records[-52:-22]:  # days 22-51 before the scored day
            record["precipitation_sum"] = 80.0 / 30
        hist = p.History(records)
        self.assertEqual(p.lagged_rain_points(hist, target), round(p.RECENT_RAIN_WEIGHT * p.FLUSH_RAIN_SCORE_MAX))
        for record in records[-22:-8]:
            record["precipitation_sum"] = 40.0 / 14
        self.assertEqual(p.lagged_rain_points(p.History(records), target), p.FLUSH_RAIN_SCORE_MAX)

    def test_score_gates_cap_total_when_rain_or_temperature_is_missing(self):
        self.assertIsNone(p.score_gate_cap(30, 20))
        self.assertEqual(p.score_gate_cap(4, 20), 25)
        self.assertEqual(p.score_gate_cap(30, 2), 20)
        self.assertEqual(p.score_gate_cap(0, 0), 20)
        dry = series(date(2025, 8, 20), 60, precipitation_sum=0.0)
        self.assertLessEqual(p.calculate_score_for_day(LOC, dry[-1], p.History(dry), [])[0], 25)
        cold = series(date(2025, 8, 20), 60, precipitation_sum=4.0, temperature_2m_max=30.0, temperature_2m_min=20.0)
        self.assertLessEqual(p.calculate_score_for_day(LOC, cold[-1], p.History(cold), [])[0], 20)

    def test_frost_penalties_are_softened(self):
        target = date(2025, 10, 18)
        records = series(target - timedelta(days=20), 21)
        hist = p.History(records)
        records[-2]["temperature_2m_min"] = -3.0
        self.assertEqual(p.frost_penalty(hist, target), 6)
        records[-2]["temperature_2m_min"] = -0.5
        self.assertEqual(p.frost_penalty(hist, target), 2)
        for record in records[-12:-1]:
            record["temperature_2m_min"] = -3.0
        self.assertEqual(p.frost_penalty(hist, target), 6 + p.FROST_REPEAT_PENALTY_MAX)

    def test_early_season_optimum_is_warmer_and_heat_penalty_is_softer(self):
        self.assertEqual(p.get_seasonal_params(date(2025, 8, 15))["optimal_temp_range"], (14.0, 19.0))
        self.assertEqual((p.HEAT_TMAX_C, p.HEAT_PENALTY, p.HUMIDITY_HIGH_BONUS), (26.0, 2, 3))

    def test_utcnow_removed_and_thresholds_match_model(self):
        source = Path(p.__file__).read_text(encoding="utf-8")
        self.assertNotIn("datetime.utcnow()", source)
        self.assertIn("datetime.now(timezone.utc)", source)
        self.assertEqual(p.score_verdict_tag(55, "⚠️ Watch closely"), "👍 Favourable conditions")

    def test_drought_percentile_handles_february_29_samples(self):
        target = date(2024, 2, 29)
        start = date(2010, 1, 1)
        records = series(start, (target - start).days, precipitation_sum=1.0)
        percentile = p.historical_rainfall_percentile(p.History(records), target)
        self.assertIsNotNone(percentile)
        self.assertEqual(percentile, 0.5)
        self.assertIsNotNone(
            p.historical_rainfall_percentile(p.History(records), date(2024, 2, 28))
        )

    def test_runoff_backfill_is_limited_to_four_hundred_days(self):
        today = datetime.now(timezone.utc).date()
        current = rec((today - timedelta(days=400)).isoformat())
        old = rec((today - timedelta(days=401)).isoformat())
        self.assertTrue(p.needs_runoff_backfill(current))
        self.assertFalse(p.needs_runoff_backfill(old))
        self.assertFalse(p.needs_runoff_backfill({"source": p.SOURCE_ARCHIVE}))

    def test_hard_freeze_threshold_and_season_termination(self):
        self.assertFalse(hasattr(p, "severe_freeze_recent"))
        target = date(2025, 11, 28)
        records = series(target - timedelta(days=30), 31)
        hist = p.History(records)
        records[-2]["temperature_2m_min"] = -4.0
        self.assertTrue(p.hard_freeze_recent(hist, target))
        records[-2]["temperature_2m_min"] = -1.0
        self.assertFalse(p.hard_freeze_recent(hist, target))

        records[-2]["temperature_2m_min"] = -4.0
        result = p.calculate_score_for_day(LOC, hist.get(target), hist, [])
        self.assertIn("Season terminated by frost", result[1])
        self.assertEqual(result[0], 0)

    def test_sparse_weather_windows_report_insufficient_data(self):
        target = date(2025, 10, 18)
        hist = p.History(series(target - timedelta(days=3), 4))
        score, status, _ = p.calculate_score_for_day(LOC, hist.get(target), hist, [])
        self.assertEqual(score, 0)
        self.assertIn("Insufficient", status)
        self.assertEqual(p.status_category(status), "insufficient")

    def test_rain_and_freeze_score_invariants(self):
        start, day = date(2025, 8, 1), date(2025, 10, 1)
        base_records = series(start, (day - start).days + 1, precipitation_sum=0.0)
        hist = p.History(base_records)
        rainfall_scores = []
        for mm in range(0, 21, 2):
            for record in hist.window(day - timedelta(days=1), p.FLUSH_RAIN_WINDOW_DAYS):
                record["precipitation_sum"] = mm
            rainfall_scores.append(p.calculate_score_for_day(LOC, hist.get(day), hist, [])[0])
        self.assertTrue(all(a <= b for a, b in zip(rainfall_scores, rainfall_scores[1:])))

        warm_records = series(start, (day - start).days + 1, temperature_2m_max=12.0, temperature_2m_min=2.0)
        warm = p.History(warm_records)
        baseline = p.calculate_score_for_day(LOC, warm.get(day), warm, [])[0]
        for freeze_days in range(1, p.FROST_REPEAT_WINDOW_DAYS + 1):
            frozen_records = [dict(record) for record in warm_records]
            for record in frozen_records[-freeze_days - 1:-1]:
                record["temperature_2m_min"] = -2.0
            frozen = p.History(frozen_records)
            self.assertLessEqual(p.calculate_score_for_day(LOC, frozen.get(day), frozen, [])[0], baseline)

    def test_no_mushrooms_observation_caps_score_on_observed_date_only(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=5.0)
        hist = p.History(recs)
        observed = date(2025, 9, 25)
        no_find = [{"date": observed.isoformat(), "observation_type": "no_mushrooms"}]
        score = p.calculate_score_for_day(LOC, hist.get(observed), hist, no_find)
        self.assertEqual(score[0], 20)
        self.assertIn("No mushrooms found", score[1])
        next_day = p.calculate_score_for_day(LOC, hist.get(observed + timedelta(days=1)), hist, no_find)
        self.assertGreater(next_day[0], score[0])

    def test_weekday_does_not_change_favourability(self):
        recs = series(date(2025, 7, 1), 90, precipitation_sum=3.0)
        h = p.History(recs)
        saturday, monday = date(2025, 9, 13), date(2025, 9, 15)
        sat_score = p.calculate_score_for_day(LOC, h.get(saturday), h, [])[0]
        mon_score = p.calculate_score_for_day(LOC, h.get(monday), h, [])[0]
        self.assertEqual(sat_score, mon_score)

    def test_rain_score_uses_lagged_trigger_window(self):
        recs = series(date(2025, 8, 1), 90, precipitation_sum=0.0)
        h = p.History(recs)
        day = date(2025, 10, 18)
        window = h.window(day - timedelta(days=p.TRIGGER_RAIN_LAG_DAYS + 1), p.TRIGGER_RAIN_WINDOW_DAYS)
        for record in window:
            record["precipitation_sum"] = 1.0
        wet_score = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        for record in window:
            record["precipitation_sum"] = 0.0
        dry_score = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        self.assertGreater(wet_score, dry_score)

    def test_temperature_score_uses_20_day_mean_air_temperature(self):
        recs = series(date(2025, 9, 1), 50, precipitation_sum=1.0, temperature_2m_max=20.0, temperature_2m_min=6.0)
        h = p.History(recs)
        day = date(2025, 10, 18)
        optimal = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        for record in h.window(day - timedelta(days=1), 20):
            record["temperature_2m_max"] = 12.0
            record["temperature_2m_min"] = 0.0
        cooler = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        self.assertEqual(optimal - cooler, 4)

    def test_missing_weather_coverage_omits_rolling_signal(self):
        recs = series(date(2025, 9, 1), 50)
        h = p.History(recs)
        day = date(2025, 10, 18)
        for record in h.window(day - timedelta(days=1), 26)[:6]:
            record["precipitation_sum"] = None
        for record in h.window(day - timedelta(days=1), 20)[:5]:
            record["temperature_2m_min"] = None
        self.assertIsNone(p.observed_rainfall_total(h, day, 26))
        self.assertIsNone(p.mean_air_temperature(h, day, 20))

    def test_score_explanation_uses_supported_weather_windows_in_dashboard_and_alert(self):
        target = date(2025, 10, 4)
        recs = series(date(2025, 9, 1), 40, temperature_2m_max=20.0)
        trigger = target - timedelta(days=8)
        trigger_record = next(r for r in recs if r["date"] == trigger.isoformat())
        trigger_record.update(precipitation_sum=12.0, temperature_2m_max=14.0)
        explanation = p.explain_score(LOC, recs, p.format_display_date(target), "✅ Viable conditions")
        self.assertIn("12.8°C mean / 20 days", explanation)
        self.assertIn("12 mm rain / 26 days", explanation)
        self.assertNotIn("Key signals", explanation)
        self.assertNotIn("not a measured chance", explanation)

        message = p.build_alert_message(
            [("Oak & Beech Spot", 82, p.format_display_date(target), "✅ Viable conditions", explanation)],
            "https://example.test",
        )
        self.assertIn("Why: " + explanation, message)
        report = p.generate_dashboard_html(
            {"ALERT_THRESHOLD": 65},
            [{"name": "Oak & Beech Spot", "best_score": 82, "best_day": p.format_display_date(target),
              "status": "✅ Viable conditions", "quality": "", "soil_moisture": 0.2,
              "explanation": explanation, "records": [],
              "scores": {target.isoformat(): {"score": 82, "status": "✅ Viable conditions", "quality": ""}},
              "harvests": [], "backtest": {}}],
        )
        self.assertIn("score-explanation", report)
        self.assertIn("12 mm rain / 26 days", report)
        self.assertIn("12.8°C mean / 20 days", report)

    def test_air_humidity_adjusts_score(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        dry = series(date(2025, 8, 20), 60, precipitation_sum=3.0, relative_humidity_2m_mean=40.0)
        humid = series(date(2025, 8, 20), 60, precipitation_sum=3.0, relative_humidity_2m_mean=90.0)
        self.assertEqual(base - 3, p.calculate_score_for_day(LOC, dry[-1], p.History(dry), [])[0])
        self.assertEqual(base + 3, p.calculate_score_for_day(LOC, humid[-1], p.History(humid), [])[0])
        forecast_only = series(date(2025, 8, 20), 60, source=p.SOURCE_FORECAST, precipitation_sum=3.0, relative_humidity_2m_mean=90.0)
        self.assertEqual(p.humidity_adjustment(p.History(forecast_only), date(2025, 10, 18)), 0)

    def test_seasonal_params_and_location_overrides(self):
        self.assertEqual(p.get_seasonal_params(date(2025, 9, 1))["rain_window"], 14)
        self.assertEqual(p.get_seasonal_params(date(2025, 10, 1))["optimal_temp_range"], (8.0, 14.0))
        self.assertEqual(p.get_seasonal_params(date(2025, 12, 1))["rain_window"], 30)
        loc = {"optimal_temp_range": [10, 15], "seasonal_params": {"mid": {"rain_window": 20, "optimal_temp": [9, 11]}}}
        mid = p.get_seasonal_params(date(2025, 10, 1), loc)
        self.assertEqual((mid["rain_window"], mid["optimal_temp_range"]), (20, (9.0, 11.0)))
        self.assertEqual(p.get_seasonal_params(date(2025, 9, 1), loc)["optimal_temp_range"], (10.0, 15.0))

    def test_recency_weight_and_distribution_score(self):
        ref = date(2025, 10, 1)
        self.assertEqual(p.weight_by_recency(date(2025, 1, 1), ref), 1.0)
        self.assertEqual(p.weight_by_recency(date(2020, 1, 1), ref), 0.5)
        steady = p.History(series(date(2025, 9, 1), 40, precipitation_sum=3.0))
        recs = series(date(2025, 9, 1), 40, precipitation_sum=0.0)
        recs[10]["precipitation_sum"] = 78.0
        burst = p.History(recs)
        day = date(2025, 10, 5)
        self.assertGreater(p.rainfall_distribution_score(steady, day), p.rainfall_distribution_score(burst, day))

    def test_post_flush_bonus_and_calibration_messages(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        find = [{"date": (date.fromisoformat(day["date"]) - timedelta(days=10)).isoformat(), "yield_tier": "small"}]
        base = p.calculate_score_for_day(LOC, day, h, [])
        boosted = p.calculate_score_for_day(LOC, day, h, find)
        self.assertEqual(boosted[0], base[0] + p.POST_FLUSH_BONUS)
        msgs = p.calibration_messages("A", {"find_hit_rate": 0.4, "find_days": 6, "threshold": 55, "suggested_threshold": 45})
        self.assertIn("ALERT_THRESHOLD=45", msgs[0])

        recent_find = [dict(find[0], date=(date.fromisoformat(day["date"]) - timedelta(days=3)).isoformat())]
        self.assertEqual(p.calculate_score_for_day(LOC, day, h, recent_find)[0], base[0] - 12)

    def test_low_soil_moisture_penalizes_without_same_day_rain(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(recs)
        baseline = p.calculate_score_for_day(LOC, recs[-1], hist, [])
        dry_day = dict(recs[-1], soil_moisture_0_to_7cm_mean=0.195, precipitation_sum=0.0)
        dry = p.calculate_score_for_day(LOC, dry_day, hist, [])
        self.assertEqual(dry[0], baseline[0] - 10 - p.soil_moisture_points(recs[-1]["soil_moisture_0_to_7cm_mean"]))
        self.assertEqual(dry[1], "💧 Too dry – low soil moisture")
        self.assertTrue(dry[1].startswith("💧"))
        self.assertFalse(dry[1].isupper())
        explanation = p.explain_score(LOC, recs[:-1] + [dry_day], p.format_display_date(date(2025, 10, 18)), dry[1])
        self.assertIn("soil moisture 0.195 m³/m³", explanation)

    def test_exhaustion_status_is_icon_prefixed_sentence_case(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(recs)
        day = date(2025, 10, 18)
        harvests = [{"date": (day - timedelta(days=1)).isoformat(), "cap_stage": "old_overripe"}]
        status = p.calculate_score_for_day(LOC, hist.get(day), hist, harvests)[1]
        self.assertEqual(status, "🍂 Exhaustion / post-flush cooling off")
        self.assertTrue(status.startswith("🍂"))
        self.assertFalse(status.isupper())

    def test_compact_explanation_special_cases_and_fallbacks(self):
        day = date(2025, 10, 18)
        recs = series(date(2025, 10, 18), 1, precipitation_sum=None, temperature_2m_max=None,
                      temperature_2m_min=None, soil_moisture_0_to_7cm_mean=None)
        display_day = p.format_display_date(day)
        self.assertEqual(p.explain_score(LOC, recs, display_day, "🔎 No mushrooms found (field observation)"),
                         "no-find observation caps score")
        self.assertEqual(p.explain_score(LOC, recs, display_day, "❌ Outside mushroom season"),
                         "outside season")
        self.assertEqual(p.explain_score(LOC, recs, display_day, "⚠️ Watch closely"),
                         "no standout weather signal")
        self.assertEqual(p.explain_score(LOC, recs, "N/A", "⚠️ Watch closely"), "weather unavailable")

    def test_new_status_labels_keep_their_categories(self):
        self.assertEqual(p.status_category("💧 Too dry – low soil moisture"), "dry")
        self.assertEqual(p.status_category("🍂 Exhaustion / post-flush cooling off"), "exhausted")

    def test_soil_moisture_ramp_host_trees_and_season_override(self):
        self.assertEqual([p.soil_moisture_points(v) for v in (0.1, 0.2, 0.28, 0.35, 0.5)], [-10, -10, 0, 5, 5])
        self.assertFalse(p.month_day_window(date(2025, 8, 20), {"season_start": "09-01"}))
        self.assertTrue(p.month_day_window(date(2025, 12, 10), {"season_end": "12-15"}))
        self.assertTrue(p.month_day_window(date(2025, 8, 20), {"season_start": "bad"}))

    def test_frost_penalty_and_invalid_alert_threshold(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(recs)
        base = p.calculate_score_for_day(LOC, recs[-1], hist, [])[0]
        for r in hist.window(date.fromisoformat(recs[-1]["date"]) - timedelta(days=1), 1):
            r["temperature_2m_min"] = -5.0
        self.assertEqual(p.calculate_score_for_day(LOC, recs[-1], hist, [])[0], base - p.FROST_PENALTY)
        self.assertEqual(p.alert_threshold({"ALERT_THRESHOLD": "abc"}), p.DEFAULT_ALERT_THRESHOLD)

    def _frost_hist(self, tmins):
        recs = series(date(2025, 11, 1), len(tmins) + 1, precipitation_sum=3.0)
        for r, t in zip(recs, tmins):
            r["temperature_2m_min"] = t
        return p.History(recs), date.fromisoformat(recs[-1]["date"])

    def test_frost_penalty_threshold_edges(self):
        warm = [5.0] * 14
        for tmin, expected in ((0.0, 0), (-0.1, p.FROST_LIGHT_PENALTY), (p.FROST_TMIN_C, p.FROST_LIGHT_PENALTY), (-1.9, p.FROST_PENALTY), (p.FROST_TMIN_C - 0.1, p.FROST_PENALTY)):
            hist, day = self._frost_hist(warm[:-1] + [tmin])
            self.assertEqual(p.frost_penalty(hist, day), expected, tmin)
        hist, day = self._frost_hist([None] * 14)
        self.assertEqual(p.frost_penalty(hist, day), 0)

    def test_repeated_frost_nights_add_capped_penalty(self):
        hist, day = self._frost_hist([5.0] * 10 + [-1.0, 5.0, 5.0, -1.0])
        self.assertEqual(p.frost_penalty(hist, day), p.FROST_LIGHT_PENALTY + p.FROST_REPEAT_PENALTY_PER_NIGHT)
        hist, day = self._frost_hist([-5.0] * 14)
        self.assertEqual(p.frost_penalty(hist, day), p.FROST_PENALTY + p.FROST_REPEAT_PENALTY_MAX)

    def test_repeated_freezing_suppresses_late_peak_but_mild_late_season_stays_moderate(self):
        def late_hist(tmin):
            recs = series(date(2025, 10, 20), 40, precipitation_sum=3.0)
            for r in recs:
                r["temperature_2m_min"] = tmin(r["date"])
                r["temperature_2m_max"] = 11.0
            return p.History(recs)
        mild = late_hist(lambda d: 5.0)
        freezing = late_hist(lambda d: -4.0 if d >= "2025-11-10" else 5.0)
        day = date(2025, 11, 21)
        mild_score = p.calculate_score_for_day(LOC, mild.get(day), mild, [])[0]
        frozen_score = p.calculate_score_for_day(LOC, freezing.get(day), freezing, [])[0]
        self.assertGreaterEqual(mild_score, 45)
        self.assertLessEqual(frozen_score, mild_score - p.FROST_PENALTY - p.FROST_REPEAT_PENALTY_MAX)
        self.assertEqual(p.score_breakdown(LOC, freezing.get(day), freezing)["frost"], -(p.FROST_PENALTY + p.FROST_REPEAT_PENALTY_MAX))

    def test_late_season_decay_ramp_and_hemisphere(self):
        north = {"latitude": 52.4}
        self.assertEqual(p.late_season_decay_penalty(date(2024, 11, 15), north), 0)
        self.assertEqual(p.late_season_decay_penalty(date(2024, 10, 20), north), 0)
        self.assertEqual(p.late_season_decay_penalty(date(2024, 12, 10), north), p.LATE_SEASON_DECAY_MAX)
        self.assertEqual(p.late_season_decay_penalty(date(2024, 12, 20), north), p.LATE_SEASON_DECAY_MAX)
        self.assertTrue(0 < p.late_season_decay_penalty(date(2024, 11, 28), north) < p.LATE_SEASON_DECAY_MAX)
        self.assertEqual(p.late_season_decay_penalty(date(2024, 11, 28), {"latitude": -35.0}), 0)

    def test_nov_28_2024_after_frost_is_no_longer_favourable(self):
        recs = series(date(2024, 9, 1), 90, precipitation_sum=4.0)
        for r in recs:
            r["temperature_2m_min"] = 4.0
            r["temperature_2m_max"] = 10.0
        for r in recs:
            if r["date"] in ("2024-11-21", "2024-11-22", "2024-11-23"):
                r["temperature_2m_min"] = -1.9
        hist = p.History(recs)
        day = date(2024, 11, 28)
        daily = hist.get(day)
        score = p.calculate_score_for_day(LOC, daily, hist, [])[0]
        no_frost = p.History([dict(r, temperature_2m_min=4.0) for r in recs])
        mild_score = p.calculate_score_for_day(LOC, daily, no_frost, [])[0]
        self.assertLess(score, p.VERY_FAVOURABLE_THRESHOLD)
        # v18: softer frost penalties (6 instead of 10 for a hard freeze) mean this scenario no longer
        # drops below the alert threshold; it must still score below the unfrozen scenario.
        self.assertLess(score, mild_score)
        breakdown = p.score_breakdown(LOC, daily, hist)
        self.assertEqual(breakdown["late_season_decay"], -p.late_season_decay_penalty(day, LOC))
        self.assertLess(breakdown["frost"], 0)

    def test_hard_freeze_in_late_season_ends_season_but_not_in_mild_autumn(self):
        recs = series(date(2024, 9, 1), 90, precipitation_sum=4.0)
        for r in recs:
            r["temperature_2m_min"] = -1.9 if r["date"] == "2024-11-26" else 4.0
        hist = p.History(recs)
        late = p.calculate_score_for_day(LOC, hist.get(date(2024, 11, 28)), hist, [])
        self.assertEqual(p.score_verdict_tag(late[0], late[1]), p.VERDICT_SPECIAL_TAGS["terminated"])
        early = p.calculate_score_for_day(LOC, hist.get(date(2024, 11, 10)), hist, [])
        self.assertNotEqual(p.status_category(early[1]), "terminated")
        recs2 = [dict(r, temperature_2m_min=-1.9 if r["date"] == "2024-10-20" else 4.0) for r in recs]
        hist2 = p.History(recs2)
        mid = p.calculate_score_for_day(LOC, hist2.get(date(2024, 10, 22)), hist2, [])
        self.assertNotEqual(p.status_category(mid[1]), "terminated")

    def test_mild_early_season_score_unchanged_by_decay(self):
        recs = series(date(2024, 9, 1), 90, precipitation_sum=4.0)
        hist = p.History(recs)
        for day in (date(2024, 10, 10), date(2024, 11, 10), date(2024, 11, 15)):
            self.assertEqual(p.score_breakdown(LOC, hist.get(day), hist)["late_season_decay"], 0)
        daily = hist.get(date(2024, 11, 10))
        self.assertEqual(p.calculate_score_for_day(LOC, daily, hist, [])[0],
                         p.calculate_score_for_day(dict(LOC, latitude=-35.0), daily, hist, [])[0])

    def test_high_soil_moisture_adds_five_points(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(recs)
        baseline = p.calculate_score_for_day(LOC, recs[-1], hist, [])[0]
        wet_day = dict(recs[-1], soil_moisture_0_to_7cm_mean=0.36)
        self.assertEqual(p.calculate_score_for_day(LOC, wet_day, hist, [])[0], baseline + 5 - p.soil_moisture_points(recs[-1]["soil_moisture_0_to_7cm_mean"]))

    def test_rain_score_saturates_without_inventing_a_wet_penalty(self):
        self.assertEqual(p.rainfall_score(0), 0)
        self.assertEqual(p.rainfall_score(20), 15)
        self.assertEqual(p.rainfall_score(p.RAINFALL_TRIGGER_SATURATION_MM), p.FLUSH_RAIN_SCORE_MAX)
        self.assertEqual(p.rainfall_score(40, p.RAINFALL_BACKGROUND_SATURATION_MM), 15)
        self.assertEqual(p.rainfall_score(p.RAINFALL_BACKGROUND_SATURATION_MM, p.RAINFALL_BACKGROUND_SATURATION_MM), p.FLUSH_RAIN_SCORE_MAX)
        self.assertEqual(p.rainfall_score(250), p.FLUSH_RAIN_SCORE_MAX)
        self.assertEqual(p.rainfall_score(-5), 0)

    def test_scored_day_weather_is_excluded_from_prior_day_windows(self):
        recs = series(date(2025, 9, 1), 50, precipitation_sum=1.0,
                      temperature_2m_max=13.0, temperature_2m_min=13.0)
        hist = p.History(recs)
        day = recs[-1]
        baseline = p.calculate_score_for_day(LOC, day, hist, [])[0]
        day["precipitation_sum"] = 1000.0
        day["temperature_2m_max"] = 40.0
        day["temperature_2m_min"] = -20.0
        self.assertEqual(p.calculate_score_for_day(LOC, day, hist, [])[0], baseline)

    def test_long_term_drought_penalty_uses_five_same_calendar_day_samples(self):
        end = date(2025, 10, 18)
        short_start = date(2021, 1, 1)
        short_records = series(short_start, (end - short_start).days, precipitation_sum=2.0)
        for record in short_records:
            if end - timedelta(days=90) <= p.parse_date(record["date"]) < end:
                record["precipitation_sum"] = 0.0
        self.assertIsNone(p.historical_rainfall_percentile(p.History(short_records), end))

        start = date(2015, 1, 1)
        records = series(start, (end - start).days, precipitation_sum=2.0)
        for record in records:
            if end - timedelta(days=90) <= p.parse_date(record["date"]) < end:
                record["precipitation_sum"] = 0.0
        for year in range(2020, 2025):
            for offset in range(1, 91):
                sample = date(year, 10, 18) - timedelta(days=offset)
                records[(sample - start).days]["precipitation_sum"] = 1.0
        records.append(rec(end.isoformat()))
        self.assertEqual(p.historical_rainfall_percentile(p.History(records), end), 0.0)
        hist = p.History(records)
        first = p.calculate_score_for_day(LOC, hist.get(end), hist, [])
        self.assertEqual(first, p.calculate_score_for_day(LOC, hist.get(end), hist, []))

    def test_backtest_uses_visits_only_and_excludes_same_day_observation(self):
        recs = series(date(2025, 8, 1), 60, precipitation_sum=5.0)
        found = {"date": "2025-09-05", "observation_type": "harvest", "yield_tier": "small"}
        no_find = {"date": "2025-09-26", "observation_type": "no_mushrooms"}
        results = p.backtest_accuracy(LOC, recs, [found, no_find], 40)
        self.assertEqual(results["observed_days"], 2)
        self.assertEqual(results["find_days"], 1)
        self.assertEqual(results["no_find_days"], 1)
        self.assertEqual(results["finds_above_threshold"], 1)
        self.assertEqual(results["no_finds_above_threshold"], 1)
        self.assertGreater(results["mean_score_on_no_find_days"], 20)

    def test_auc_perfect_separation(self):
        self.assertEqual(p.compute_auc([80, 90, 100], [10, 20, 30]), 1.0)

    def test_auc_inverted_separation(self):
        self.assertEqual(p.compute_auc([10, 20, 30], [80, 90, 100]), 0.0)

    def test_auc_all_tied(self):
        self.assertEqual(p.compute_auc([50, 50, 50], [50, 50, 50]), 0.5)

    def test_auc_with_ties(self):
        self.assertEqual(p.compute_auc([50, 60], [50, 40]), 0.875)

    def test_auc_empty_lists(self):
        self.assertIsNone(p.compute_auc([], [50]))
        self.assertIsNone(p.compute_auc([50], []))
        self.assertIsNone(p.compute_auc([], []))

    def test_backtest_includes_auc(self):
        recs = series(date(2025, 8, 1), 60, precipitation_sum=5.0)
        found = {"date": "2025-09-05", "observation_type": "harvest", "yield_tier": "small"}
        no_find = {"date": "2025-09-26", "observation_type": "no_mushrooms"}
        results = p.backtest_accuracy(LOC, recs, [found, no_find], 40)
        self.assertIsNotNone(results["auc"])
        self.assertIsNone(p.backtest_accuracy(LOC, recs, [found], 40)["auc"])

    def test_dashboard_payload_auc_display(self):
        def display(finds, no_finds, auc):
            item = {"records": [], "scores": {}, "harvests": [], "backtest": {"auc": auc, "find_days": finds, "no_find_days": no_finds}}
            return p.dashboard_payload({"LOCATIONS": [{}]}, [item], "default")["locations"][0]["auc_display"]
        self.assertEqual(display(15, 16, 0.8), "AUC 0.80 (15 finds / 16 no-finds)")
        self.assertEqual(display(3, 20, 0.8), "not enough visits yet (3/20)")
        self.assertEqual(display(0, 0, None), "")

    def test_flush_depletion_penalty_tiers(self):
        h = [date(2025, 9, 18)]
        expected = {0: 0, 1: -35, 2: -25, 3: -25, 4: -12, 6: -12, 7: 0}
        for days, penalty in expected.items():
            self.assertEqual(p.flush_depletion_penalty(h, h[0] + timedelta(days=days)), penalty, days)
        self.assertEqual(p.flush_depletion_penalty([], date(2025, 9, 20)), 0)

    def test_depletion_lowers_scores_after_harvest_but_not_on_harvest_day(self):
        recs = series(date(2025, 8, 1), 60, precipitation_sum=5.0)
        h = p.History(recs)
        find = [{"date": "2025-09-18", "observation_type": "harvest", "yield_tier": "large", "cap_stage": "prime"}]
        base = lambda d: p.calculate_score_for_day(LOC, h.get(date.fromisoformat(d)), h, [])[0]
        with_find = lambda d: p.calculate_score_for_day(LOC, h.get(date.fromisoformat(d)), h, find)
        self.assertEqual(with_find("2025-09-18")[0], base("2025-09-18"))
        self.assertLess(with_find("2025-09-19")[0], base("2025-09-19"))
        self.assertIn("depleted", with_find("2025-09-19")[1])
        small = [dict(find[0], yield_tier="small")]
        self.assertGreater(p.calculate_score_for_day(LOC, h.get(date(2025, 9, 19)), h, small)[0], with_find("2025-09-19")[0])

    def test_backtest_separates_post_harvest_days(self):
        recs = series(date(2025, 8, 1), 60, precipitation_sum=5.0)
        found = {"date": "2025-09-18", "observation_type": "harvest", "yield_tier": "large"}
        second = {"date": "2025-09-20", "observation_type": "harvest", "yield_tier": "large"}
        results = p.backtest_accuracy(LOC, recs, [found, second], 200)
        self.assertEqual(results["true_false_negatives"], 1)
        self.assertEqual(results["post_harvest_low_find_days"], 1)

    def test_affinity_limited_to_two_years(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        recent = [{"date": "2025-05-01", "yield_tier": "large"}, {"date": "2025-06-01", "yield_tier": "medium"}]
        old = [{"date": "2022-05-01", "yield_tier": "large"}, {"date": "2022-06-01", "yield_tier": "medium"}]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        self.assertEqual(p.calculate_score_for_day(LOC, day, h, recent)[0], base + 5)
        self.assertEqual(p.calculate_score_for_day(LOC, day, h, old)[0], base)

    def test_quality_flags(self):
        hot = series(date(2025, 8, 20), 30, precipitation_sum=5.0, temperature_2m_max=22.0)
        cool = series(date(2025, 8, 20), 30, precipitation_sum=5.0, temperature_2m_max=11.0)
        self.assertEqual(p.calculate_score_for_day(LOC, hot[-1], p.History(hot), [])[2],
                         "Warmer week: higher maggot risk, pick early")
        self.assertEqual(p.calculate_score_for_day(LOC, cool[-1], p.History(cool), [])[2],
                         "Cool week: firm caps likely")

    def test_quality_warning_does_not_change_flush_score(self):
        target = date(2025, 10, 18)
        start = target - timedelta(days=30)
        hot = series(start, 31, temperature_2m_max=13.0, temperature_2m_min=13.0)
        cool = series(start, 31, temperature_2m_max=13.0, temperature_2m_min=13.0)
        for records in (hot, cool):
            for record in records:
                record["date"] = p.parse_date(record["date"]).isoformat()
        for record in hot[23:30]:
            record["temperature_2m_max"] = 20.0
            record["temperature_2m_min"] = 6.0
        hot_score = p.calculate_score_for_day(LOC, hot[-1], p.History(hot), [])
        cool_score = p.calculate_score_for_day(LOC, cool[-1], p.History(cool), [])
        self.assertEqual(hot_score[0], cool_score[0])
        self.assertEqual(hot_score[2], "Warmer week: higher maggot risk, pick early")
        self.assertEqual(cool_score[2], "Cool week: firm caps likely")

    def test_duplicate_host_labels_do_not_stack_and_additional_hosts_are_supported(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        duplicate_spruce = dict(LOC, tree_species=["Spruce", "Norway Spruce"])
        birch_chestnut = dict(LOC, tree_species=["Birch", "Chestnut"])
        self.assertEqual(p.calculate_score_for_day(duplicate_spruce, day, h, [])[0], base)
        self.assertEqual(p.calculate_score_for_day(birch_chestnut, day, h, [])[0], base)

    def test_daily_scores_incremental(self):
        recs = series(date(2025, 9, 1), 20)
        store = {}
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 20)
        self.assertEqual(set(store["daily_scores"]), {r["date"] for r in recs})
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 0)  # archive days are not rescored
        recs[-1]["source"] = p.SOURCE_FORECAST
        store["daily_scores"][recs[-1]["date"]]["source"] = p.SOURCE_FORECAST
        self.assertEqual(p.update_daily_scores(LOC, recs, store), 1)  # forecast days are

    def test_sample_daily_scores_are_incrementally_cached(self):
        records = [rec("2025-09-01", p.SOURCE_SAMPLE)]
        store = {}
        self.assertEqual(p.update_daily_scores(LOC, records, store), 1)
        self.assertEqual(p.update_daily_scores(LOC, records, store), 0)
        records[0]["source"] = p.SOURCE_ARCHIVE
        self.assertEqual(p.update_daily_scores(LOC, records, store), 1)


class ForecastSnapshotTests(unittest.TestCase):
    def test_snapshot_history_is_bounded_and_replaces_same_day(self):
        db = {}
        start = date(2025, 1, 1)
        for offset in range(p.MAX_FORECAST_SNAPSHOTS + 5):
            run = start + timedelta(days=offset)
            p.store_forecast_snapshot(db, run, {"Location 1": [[p.iso_date(run + timedelta(days=1)), 42]]})
        p.store_forecast_snapshot(db, start + timedelta(days=34), {"Location 1": [["2025-02-05", 88]]})
        snapshots = db["forecast_snapshots"]
        self.assertEqual(len(snapshots), p.MAX_FORECAST_SNAPSHOTS)
        self.assertEqual(snapshots[-1]["scores"]["Location 1"], [["2025-02-05", 88]])

    def test_snapshot_backtest_metrics_and_small_sample_warning(self):
        observations = []
        snapshots = []
        start = date(2025, 9, 1)
        for index in range(10):
            day = start + timedelta(days=index)
            found = index < 5
            observations.append({
                "location": "Actual Spot", "date": p.iso_date(day),
                "observation_type": "harvest" if found else "no_mushrooms",
                "yield_tier": "small" if found else "",
            })
            snapshots.append({
                "run_date": p.iso_date(day - timedelta(days=1)),
                "scores": {"Location 1": [[p.iso_date(day), 80 if found else 20]]},
            })
        cfg = {"LOCATIONS": [{"name": "Actual Spot"}]}
        metrics = p.forecast_snapshot_backtest(
            {"forecast_snapshots": snapshots}, cfg, {"harvests": observations}, 60
        )
        self.assertEqual(metrics["samples"], 10)
        self.assertEqual(metrics["hit_rate"], 1.0)
        self.assertEqual(metrics["false_alarms"], 0)
        self.assertEqual(metrics["auc"], 1.0)
        self.assertIn("AUC: 1.000", p.format_forecast_backtest(metrics))

        small = p.forecast_snapshot_backtest(
            {"forecast_snapshots": snapshots[:2]}, cfg, {"harvests": observations[:2]}, 60
        )
        self.assertIsNone(small["auc"])
        self.assertIn("WARNING: sample too small", p.format_forecast_backtest(small))

    def test_backtest_cli_reads_snapshots_without_running_weather_sync(self):
        output = io.StringIO()
        with (
            patch("sys.argv", ["porcini.py", "--backtest"]),
            patch.object(p, "load_config", return_value={"LOCATIONS": []}),
            patch.object(p, "load_json_safe", return_value={"forecast_snapshots": []}),
            patch.object(p, "load_harvest_log", return_value={"harvests": []}),
            redirect_stdout(output),
        ):
            self.assertEqual(p.main(), 0)
        self.assertIn("Forecast snapshot backtest", output.getvalue())
        self.assertIn("WARNING: sample too small", output.getvalue())


class ForecastUncertaintyAndTimingTests(unittest.TestCase):
    def test_rain_window_mix_counts_archive_and_forecast_days(self):
        target = date(2025, 10, 1)
        records = series(target - timedelta(days=26), 27)
        for record in records[-7:]:
            record["source"] = p.SOURCE_FORECAST
        mix = p.forecast_rain_window_mix(records, {}, target)
        self.assertEqual(mix, {
            "forecast_days": 6, "archive_days": 20, "window_days": 26, "coverage_days": 26,
        })
        self.assertTrue(p.heavily_forecast_dependent(mix))
        self.assertFalse(p.heavily_forecast_dependent({
            "forecast_days": 2, "archive_days": 24, "window_days": 26, "coverage_days": 26,
        }))

    def test_rain_window_mix_uses_seasonal_window_and_available_rain_only(self):
        target = date(2025, 9, 20)
        records = series(target - timedelta(days=14), 15)
        for record in records[-4:]:
            record["source"] = p.SOURCE_FORECAST
        records[-2]["precipitation_sum"] = None
        mix = p.forecast_rain_window_mix(records, {}, target)
        self.assertEqual(mix["window_days"], 14)
        self.assertEqual(mix["forecast_days"], 2)
        self.assertEqual(mix["archive_days"], 11)
        self.assertEqual(mix["coverage_days"], 13)

    def test_dashboard_payload_exposes_forecast_rain_provenance(self):
        target = date(2025, 10, 1)
        records = series(target - timedelta(days=26), 27)
        for record in records[-7:]:
            record["source"] = p.SOURCE_FORECAST
        item = {
            "name": "Actual Spot", "records": records, "scores": {}, "harvests": [],
            "backtest": {},
        }
        payload = p.dashboard_payload({"LOCATIONS": [{"name": "Actual Spot"}]}, [item], p.MODE_DEFAULT)
        self.assertEqual(payload["locations"][0]["name"], "Location 1")
        self.assertIn([p.iso_date(target), 6, 20, 26, 26], payload["locations"][0]["rain_mix"])

    def test_flush_timing_reports_first_forecast_crossing_only(self):
        today = date(2025, 9, 1)
        records = [
            rec(p.iso_date(today), p.SOURCE_FORECAST),
            rec(p.iso_date(today + timedelta(days=1)), p.SOURCE_FORECAST),
            rec(p.iso_date(today + timedelta(days=2)), p.SOURCE_FORECAST),
        ]
        scores = {
            p.iso_date(today): {"score": 40},
            p.iso_date(today + timedelta(days=1)): {"score": 50},
            p.iso_date(today + timedelta(days=2)): {"score": 65},
        }
        self.assertEqual(p.estimate_days_until_threshold(records, scores, today, 60), 2)
        scores[p.iso_date(today)]["score"] = 60
        self.assertEqual(p.estimate_days_until_threshold(records, scores, today, 60), 0)
        scores[p.iso_date(today)]["score"] = 40
        scores[p.iso_date(today + timedelta(days=2))]["score"] = 55
        self.assertIsNone(p.estimate_days_until_threshold(records, scores, today, 60))


class RunoffBackfillTests(unittest.TestCase):
    @staticmethod
    def hourly(day, peak):
        vals = [0.0] * 24
        vals[5], vals[6] = peak, 0.0
        return {"hourly": {"time": [f"{day}T{h:02d}:00" for h in range(24)], "precipitation": vals}}

    def test_backfill_fills_and_marks_basis(self):
        today = datetime.now(timezone.utc).date()
        first_day, second_day = p.iso_date(today), p.iso_date(today + timedelta(days=1))
        records = [rec(first_day, precip_peak_2h_mm=None, precipitation_sum=20.0), rec(second_day, source=p.SOURCE_FORECAST)]
        changed = p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=lambda *a: self.hourly(first_day, 14.0))
        self.assertEqual(changed, [first_day])
        self.assertEqual(records[0]["precip_peak_2h_mm"], 14.0)
        self.assertEqual(records[0]["precip_peak_basis"], p.PEAK_BASIS_BACKFILL)
        self.assertNotIn("precip_peak_basis", records[1])
        self.assertTrue(p.has_runoff(p.History(records), today + timedelta(days=1)))

    def test_failure_is_soft_and_retried(self):
        records = [rec(p.iso_date(datetime.now(timezone.utc).date()))]
        self.assertEqual(p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=lambda *a: None), [])
        def boom(*a):
            raise RuntimeError("down")
        self.assertEqual(p.backfill_runoff_data(records, 0, 0, 0, fetch_hourly=boom), [])
        self.assertTrue(p.needs_runoff_backfill(records[0]))

    def test_backfill_forces_rescore(self):
        loc = {"name": "A", "aspect": "N"}
        today = datetime.now(timezone.utc).date()
        records = [rec(p.iso_date(today + timedelta(days=i)), precipitation_sum=20.0) for i in range(3)]
        store = {}
        p.update_daily_scores(loc, records, store)
        store["pending_rescore"] = [p.iso_date(today + timedelta(days=1))]
        self.assertEqual(p.update_daily_scores(loc, records, store), 1)


class WeatherFailureTests(unittest.TestCase):
    def test_fetch_json_distinguishes_empty_payload_from_request_failure(self):
        response = Mock()
        response.json.return_value = None
        with patch.object(p.WEATHER_SESSION, "get", return_value=response):
            self.assertIsNone(p.fetch_json("https://weather.example", {}))
        with patch.object(p.WEATHER_SESSION, "get", side_effect=p.requests.ConnectionError):
            with self.assertRaises(p.WeatherRequestError):
                p.fetch_json("https://weather.example", {})
        retry = p.WEATHER_SESSION.get_adapter("https://").max_retries
        self.assertEqual(retry.total, 3)
        self.assertEqual(retry.backoff_factor, 0.5)

    def test_main_notifies_and_fails_when_location_sync_raises(self):
        cfg = {"LOCATIONS": [{"name": "Private spot", "latitude": 1, "longitude": 2}]}
        with (
            patch("sys.argv", ["porcini.py"]),
            patch.object(p, "load_config", return_value=cfg),
            patch.object(p, "load_or_init_db", return_value={"locations": {"Location 1": {}}}),
            patch.object(p, "build_alert_state", return_value={"locations": {}}),
            patch.object(p, "load_harvest_log", return_value={"harvests": []}),
            patch.object(p, "ensure_location_history", side_effect=p.WeatherRequestError("request failed")),
            patch.object(p, "dispatch_notification", return_value=True) as send,
        ):
            self.assertEqual(p.main(), 1)
        self.assertIn("Location 1", send.call_args.args[1])
        self.assertIn("weather sync failed", send.call_args.args[1])

    def test_main_notifies_and_fails_when_configured_data_is_stale(self):
        cfg = {"LOCATIONS": [{"name": "Private spot", "latitude": 1, "longitude": 2}]}
        today = datetime.now(timezone.utc).date()
        records = [rec(p.iso_date(today), p.SOURCE_FORECAST)]

        def update_scores(location, daily_records, store):
            store["daily_scores"] = {}
            return 0

        with (
            patch("sys.argv", ["porcini.py"]),
            patch.object(p, "load_config", return_value=cfg),
            patch.object(p, "load_or_init_db", return_value={"locations": {"Location 1": {}}}),
            patch.object(p, "build_alert_state", return_value={"locations": {}}),
            patch.object(p, "load_harvest_log", return_value={"harvests": []}),
            patch.object(p, "ensure_location_history", return_value=records),
            patch.object(p, "update_daily_scores", side_effect=update_scores),
            patch.object(p, "backtest_accuracy", return_value={}),
            patch.object(p, "calibration_messages", return_value=[]),
            patch.object(p, "find_weekend_best", return_value=(0, "N/A", "", "")),
            patch.object(p, "check_db_integrity", return_value=[
                "Location 1: last sync is more than 3 days old"
            ]),
            patch.object(p, "dispatch_notification", return_value=True) as send,
        ):
            self.assertEqual(p.main(), 1)
        self.assertIn("more than 3 days old", send.call_args.args[1])

    def test_main_applies_configured_alert_gap(self):
        cfg = {
            "MIN_ALERT_GAP_DAYS": 2,
            "LOCATIONS": [{"name": "Synthetic spot", "latitude": 1, "longitude": 2}],
        }
        today = datetime.now(timezone.utc).date()
        records = [rec(p.iso_date(today), p.SOURCE_FORECAST)]
        db = {"locations": {"Location 1": {}}}

        def update_scores(location, daily_records, store):
            store["daily_scores"] = {
                p.iso_date(today): {"score": 70, "status": "✅ Viable conditions", "source": p.SOURCE_FORECAST}
            }
            return 1

        with (
            patch("sys.argv", ["porcini.py"]),
            patch.object(p, "load_config", return_value=cfg),
            patch.object(p, "load_or_init_db", return_value=db),
            patch.object(p, "build_alert_state", return_value={"locations": {"Location 1": {}}}),
            patch.object(p, "load_harvest_log", return_value={"harvests": []}),
            patch.object(p, "ensure_location_history", return_value=records),
            patch.object(p, "update_daily_scores", side_effect=update_scores),
            patch.object(p, "backtest_accuracy", return_value={}),
            patch.object(p, "calibration_messages", return_value=[]),
            patch.object(p, "find_weekend_best", return_value=(
                70, p.format_display_date(today), "✅ Viable conditions", ""
            )),
            patch.object(p, "should_alert_for_location", return_value=False) as should_alert,
            patch.object(p, "check_db_integrity", return_value=[]),
            patch.object(p, "save_json"),
            patch.object(p, "generate_dashboard_html", return_value=""),
            patch("pathlib.Path.write_text"),
        ):
            self.assertEqual(p.main(), 0)
        self.assertEqual(should_alert.call_args.args[-1], 2)
        self.assertEqual(
            db["forecast_snapshots"][-1]["scores"]["Location 1"],
            [[p.iso_date(today), 70]],
        )

    def test_empty_fetch_does_not_replace_history_with_empty_data(self):
        db = {"locations": {}}
        with patch.object(p, "fetch_archive_day_range", return_value=[]), patch.object(p, "fetch_forecast_day_range", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "refusing to save an empty forecast"):
                p.ensure_location_history("A", db, 0, 0, 0, date(2025, 9, 20))
        self.assertEqual(db["locations"], {})

    def test_dashboard_restores_weather_overview_and_marks_missing_scores(self):
        report = p.generate_dashboard_html(
            {"ALERT_THRESHOLD": 65},
            [{"name": "A", "best_score": 0, "best_day": "N/A", "status": "📡 Weather data unavailable",
              "quality": "", "soil_moisture": 0, "records": [], "scores": {}, "harvests": [], "backtest": {}}],
        )
        self.assertIn("Rain &amp; Temperature Overview", report)
        self.assertIn("Weather overview unavailable: no weather records have been loaded.", report)
        self.assertIn(">N/A</div>", report)
        self.assertNotIn(">0%</div>", report)

    def test_season_toggle_defaults_on_with_visible_active_style(self):
        tpl = Path(report_module.__file__).with_name("index.template.html").read_text(encoding="utf-8")
        self.assertRegex(tpl, r'id="seasonOnly"[^>]*aria-pressed="true"')
        self.assertIn('.season-toggle[aria-pressed="true"] { background: #166534;', tpl)
        wf = Path(report_module.__file__).with_name(".github") / "workflows" / "update_report.yml"
        self.assertIn("- 'index.template.html'", wf.read_text(encoding="utf-8"))

    def test_dashboard_renders_verdict_and_score_color_everywhere(self):
        status = "⚠️ Watch closely"
        report = p.generate_dashboard_html(
            {"ALERT_THRESHOLD": 65},
            [{"name": "A", "best_score": 37, "best_day": "Sun. 04 Oct. 2026", "status": status,
              "quality": "", "soil_moisture": 0.2, "records": [],
              "scores": {"2026-10-04": {"score": 37, "status": status, "quality": ""}},
              "harvests": [], "backtest": {}}],
        )
        self.assertIn("style='color:hsl(28 80% 58%)'>37/100</span>", report)
        self.assertIn("<span class='verdict-tag'>🤔 Long shot</span>", report)
        self.assertIn('["2026-10-04", 37, "⚠️ Watch closely", "", "🤔 Long shot", "hsl(28 80% 58%)"]', report)
        self.assertIn("Favourability index: ' + sm[1] + '/100 — ' + sm[4]", report)
        self.assertIn("bar.style.background = row ? row[5]", report)
        self.assertIn("seasonOnly", report)
        self.assertIn('id="seasonOnly" class="season-toggle" aria-pressed="true"', report)
        self.assertIn("s[new Date().getFullYear()] = 1", report)
        self.assertIn("$('range').value = cur || String(new Date().getFullYear())", report)


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
