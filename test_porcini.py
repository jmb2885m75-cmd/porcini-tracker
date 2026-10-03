"""Lightweight self-checks. Run: python -m unittest test_porcini -v (no network needed)."""
import unittest
from datetime import date, datetime, timedelta, timezone
from unittest.mock import Mock, patch

import porcini as p


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


class VerdictTests(unittest.TestCase):
    def test_score_tiers_and_configured_go_threshold(self):
        cases = [
            (24, "🚫 Not worth it"),
            (25, "😐 Unlikely"),
            (45, "🤔 Long shot"),
            (60, "👀 Worth a look"),
            (64, "👀 Worth a look"),
            (65, "👍 Good chance – go"),
            (74, "👍 Good chance – go"),
            (75, "🔥 Definitely go for it"),
        ]
        for score, expected in cases:
            with self.subTest(score=score):
                self.assertEqual(p.score_verdict_tag(score, "⚠️ Watch closely"), expected)
        self.assertEqual(p.score_verdict_tag(70, "⚠️ Watch closely", 70), "👍 Good chance – go")
        self.assertEqual(p.score_verdict_tag(69, "⚠️ Watch closely", 70), "👀 Worth a look")

    def test_special_statuses_override_score_tier(self):
        cases = [
            ("❌ Outside mushroom season", "🍂 Off season"),
            ("❄️ SEASON TERMINATED BY FROST", "❄️ Season over"),
            ("🔥 SEASON DELAYED BY HIGH SOIL TEMP", "🔥 Too warm – wait"),
            ("TOO DRY - DROUGHT UNBROKEN", "💧 Too dry – wait"),
            ("🔎 No mushrooms found (field observation)", "🔎 No mushrooms found (field observation)"),
        ]
        for status, expected in cases:
            with self.subTest(status=status):
                self.assertEqual(p.score_verdict_tag(0, status), expected)

    def test_score_color_clamps_red_and_green(self):
        self.assertEqual(p.score_color(37), "hsl(0 80% 58%)")
        self.assertEqual(p.score_color(40), "hsl(0 80% 58%)")
        self.assertEqual(p.score_color(70), "hsl(120 80% 58%)")
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
        self.assertIn("GO - 70%", p.build_alert_message(r, "u", p.MODE_FINAL, 65))
        self.assertIn("70% – 👍 Good chance – go (Fri 2025-10-03)", p.build_alert_message(r, "u"))
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
    def test_no_mushrooms_observation_caps_score_on_observed_date_only(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=5.0)
        hist = p.History(recs)
        observed = date(2025, 9, 25)
        no_find = [{"date": observed.isoformat(), "observation_type": "no_mushrooms"}]
        score = p.calculate_score_for_day(LOC, hist.get(observed), hist, no_find)
        self.assertLessEqual(score[0], 20)
        self.assertIn("No mushrooms found", score[1])
        next_day = p.calculate_score_for_day(LOC, hist.get(observed + timedelta(days=1)), hist, no_find)
        self.assertGreater(next_day[0], score[0])

    def test_weekday_does_not_change_favourability(self):
        recs = series(date(2025, 9, 1), 40, precipitation_sum=3.0)
        h = p.History(recs)
        saturday, monday = date(2025, 9, 13), date(2025, 9, 15)
        sat_score = p.calculate_score_for_day(LOC, h.get(saturday), h, [])[0]
        mon_score = p.calculate_score_for_day(LOC, h.get(monday), h, [])[0]
        self.assertEqual(sat_score, mon_score)

    def test_rain_score_uses_26_day_total(self):
        recs = series(date(2025, 9, 1), 50, precipitation_sum=0.0)
        h = p.History(recs)
        day = date(2025, 10, 18)
        for record in h.window(day, 26):
            record["precipitation_sum"] = 1.0
        wet_score = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        for record in h.window(day, 26):
            record["precipitation_sum"] = 0.0
        dry_score = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        self.assertEqual(wet_score - dry_score, 8)

    def test_temperature_score_uses_20_day_mean_air_temperature(self):
        recs = series(date(2025, 9, 1), 50, temperature_2m_max=20.0, temperature_2m_min=6.0)
        h = p.History(recs)
        day = date(2025, 10, 18)
        optimal = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        for record in h.window(day, 20):
            record["temperature_2m_max"] = 16.0
            record["temperature_2m_min"] = 2.0
        cooler = p.calculate_score_for_day(LOC, h.get(day), h, [])[0]
        self.assertEqual(optimal - cooler, 8)

    def test_missing_weather_coverage_omits_rolling_signal(self):
        recs = series(date(2025, 9, 1), 50)
        h = p.History(recs)
        day = date(2025, 10, 18)
        for record in h.window(day, 26)[:6]:
            record["precipitation_sum"] = None
        for record in h.window(day, 20)[:5]:
            record["temperature_2m_min"] = None
        self.assertIsNone(p.observed_rainfall_total(h, day, 26))
        self.assertIsNone(p.mean_air_temperature(h, day, 20))

    def test_score_explanation_mentions_recent_rain_and_cooling_in_dashboard_and_alert(self):
        target = date(2025, 10, 4)
        recs = series(date(2025, 9, 1), 40, temperature_2m_max=20.0)
        trigger = target - timedelta(days=8)
        trigger_record = next(r for r in recs if r["date"] == trigger.isoformat())
        trigger_record.update(precipitation_sum=12.0, temperature_2m_max=14.0)
        explanation = p.explain_score(LOC, recs, p.format_display_date(target), "✅ Viable conditions")
        self.assertIn("mean air temperature", explanation)
        self.assertIn("preceding 26 days", explanation)

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
        self.assertIn("rain and a sharp temperature drop", report)

    def test_air_humidity_is_not_a_scoring_input(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        h = p.History(recs)
        day = recs[-1]
        base = p.calculate_score_for_day(LOC, day, h, [])[0]
        dry = series(date(2025, 8, 20), 60, precipitation_sum=3.0, relative_humidity_2m_mean=40.0)
        self.assertEqual(base, p.calculate_score_for_day(LOC, dry[-1], p.History(dry), [])[0])

    def test_low_soil_moisture_penalizes_without_same_day_rain(self):
        recs = series(date(2025, 8, 20), 60, precipitation_sum=3.0)
        hist = p.History(recs)
        baseline = p.calculate_score_for_day(LOC, recs[-1], hist, [])
        dry_day = dict(recs[-1], soil_moisture_0_to_7cm_mean=0.195, precipitation_sum=0.0)
        dry = p.calculate_score_for_day(LOC, dry_day, hist, [])
        self.assertEqual(dry[0], baseline[0] - 10)
        self.assertEqual(dry[1], "TOO DRY - LOW SOIL MOISTURE")
        explanation = p.explain_score(LOC, recs[:-1] + [dry_day], p.format_display_date(date(2025, 10, 18)), dry[1])
        self.assertIn("soil moisture is low at 0.195 m³/m³", explanation)

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
        self.assertIn("MAGGOT", p.calculate_score_for_day(LOC, hot[-1], p.History(hot), [])[2])
        self.assertIn("PRIME", p.calculate_score_for_day(LOC, cool[-1], p.History(cool), [])[2])

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


class WeatherFailureTests(unittest.TestCase):
    def test_empty_fetch_does_not_replace_history_with_empty_data(self):
        db = {"locations": {}}
        with patch.object(p, "fetch_archive_day_range", return_value=[]), patch.object(p, "fetch_forecast_day_range", return_value=[]):
            with self.assertRaisesRegex(RuntimeError, "refusing to save an empty forecast"):
                p.ensure_location_history("A", db, 0, 0, 0, date(2025, 9, 20))
        self.assertEqual(db["locations"], {})

    def test_dashboard_restores_weather_overview_and_marks_missing_scores(self):
        report = p.generate_dashboard_html(
            {"ALERT_THRESHOLD": 65},
            [{"name": "A", "best_score": 0, "best_day": "N/A", "status": "Weather data unavailable",
              "quality": "", "soil_moisture": 0, "records": [], "scores": {}, "harvests": [], "backtest": {}}],
        )
        self.assertIn("Rain &amp; Temperature Overview", report)
        self.assertIn("Weather overview unavailable: no weather records have been loaded.", report)
        self.assertIn(">N/A</div>", report)
        self.assertNotIn(">0%</div>", report)

    def test_dashboard_renders_verdict_and_score_color_everywhere(self):
        status = "⚠️ Watch closely"
        report = p.generate_dashboard_html(
            {"ALERT_THRESHOLD": 65},
            [{"name": "A", "best_score": 37, "best_day": "Sun. 04 Oct. 2026", "status": status,
              "quality": "", "soil_moisture": 0.2, "records": [],
              "scores": {"2026-10-04": {"score": 37, "status": status, "quality": ""}},
              "harvests": [], "backtest": {}}],
        )
        self.assertIn("style='color:hsl(0 80% 58%)'>37%</span>", report)
        self.assertIn("<span class='verdict-tag'>😐 Unlikely</span>", report)
        self.assertIn('["2026-10-04", 37, "⚠️ Watch closely", "", "😐 Unlikely", "hsl(0 80% 58%)"]', report)
        self.assertIn("Score: ' + sm[1] + '% – ' + sm[4]", report)
        self.assertIn("bar.style.background = row ? row[5]", report)
        self.assertIn("seasonOnly", report)


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
