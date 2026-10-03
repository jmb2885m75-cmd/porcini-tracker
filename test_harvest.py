"""Offline tests for harvest intake parsing/validation and merge. Run: python -m unittest test_harvest -v"""
import tempfile
import unittest
from datetime import date
from pathlib import Path

import harvest as h

BODY = """### Location / spot

East Berlin Pine & Oak Ridge

### Date

2026-09-15

### Observation type

harvest

### Yield tier

large

### Cap stage

prime

### Weight (g)

450

### Notes

_No response_
"""
TODAY = date(2026, 10, 1)
KNOWN = ["East Berlin Pine & Oak Ridge"]


class ParseValidateTests(unittest.TestCase):
    def test_parse_and_validate(self):
        rec = h.validate_harvest(h.parse_issue_body(BODY), KNOWN, TODAY)
        self.assertEqual(rec, {"location": KNOWN[0], "date": "2026-09-15", "observation_type": "harvest", "yield_tier": "large", "cap_stage": "prime", "weight_g": 450})

    def test_unknown_issue_form_heading_is_rejected(self):
        with self.assertRaisesRegex(h.HarvestError, "Unrecognized issue-form field heading"):
            h.parse_issue_body(BODY + "\n### New field\n\nvalue\n")

    def test_no_mushrooms_observation_needs_no_harvest_details(self):
        raw = h.parse_issue_body(BODY)
        raw["observation_type"] = "no_mushrooms"
        raw["yield_tier"] = ""
        raw["cap_stage"] = ""
        raw["weight_g"] = ""
        self.assertEqual(h.validate_harvest(raw, KNOWN, TODAY), {
            "location": KNOWN[0], "date": "2026-09-15", "observation_type": "no_mushrooms",
        })

    def test_missing_observation_type_is_rejected(self):
        raw = h.parse_issue_body(BODY)
        raw.pop("observation_type")
        with self.assertRaisesRegex(h.HarvestError, "Observation type is required"):
            h.validate_harvest(raw, KNOWN, TODAY)

    def test_location_case_insensitive_and_unknown_rejected(self):
        raw = h.parse_issue_body(BODY)
        raw["location"] = raw["location"].upper()
        self.assertEqual(h.validate_harvest(raw, KNOWN, TODAY)["location"], KNOWN[0])
        raw["location"] = "Nowhere"
        with self.assertRaises(h.HarvestError):
            h.validate_harvest(raw, KNOWN, TODAY)

    def test_invalid_fields_rejected(self):
        for key, bad in [("date", "15/09/2026"), ("date", "2027-01-01"), ("yield_tier", "huge"), ("cap_stage", "x"), ("weight_g", "abc"), ("weight_g", "-5")]:
            raw = h.parse_issue_body(BODY)
            raw[key] = bad
            with self.assertRaises(h.HarvestError, msg=f"{key}={bad}"):
                h.validate_harvest(raw, KNOWN, TODAY)

    def test_notes_sanitised(self):
        raw = h.parse_issue_body(BODY)
        raw["notes"] = "a\x00b\n\nc " + "x" * 500
        notes = h.validate_harvest(raw, KNOWN, TODAY)["notes"]
        self.assertTrue(notes.startswith("a b c"))
        self.assertLessEqual(len(notes), h.MAX_NOTES_LEN)

    def test_trusted_actor(self):
        self.assertTrue(h.is_trusted_actor("COLLABORATOR"))
        self.assertTrue(h.is_trusted_actor("NONE", "Me", "me"))
        self.assertFalse(h.is_trusted_actor("NONE", "stranger", "me"))
        self.assertFalse(h.is_trusted_actor("CONTRIBUTOR"))


class IntakeUsabilityTests(unittest.TestCase):
    def raw(self, **kw):
        raw = h.parse_issue_body(BODY)
        raw.update(kw)
        return raw

    def test_friendly_form_labels_are_accepted(self):
        rec = h.validate_harvest(self.raw(observation_type="Found mushrooms", yield_tier="Medium", cap_stage="Old / overripe", weight_g="1,5 kg"), KNOWN, TODAY)
        self.assertEqual((rec["observation_type"], rec["yield_tier"], rec["cap_stage"], rec["weight_g"]), ("harvest", "medium", "old_overripe", 1500))
        rec = h.validate_harvest(self.raw(observation_type="No mushrooms found", yield_tier="", cap_stage=""), KNOWN, TODAY)
        self.assertEqual(rec["observation_type"], "no_mushrooms")

    def test_empty_today_and_yesterday_dates(self):
        self.assertEqual(h.validate_harvest(self.raw(date=""), KNOWN, TODAY)["date"], "2026-10-01")
        self.assertEqual(h.validate_harvest(self.raw(date="Today"), KNOWN, TODAY)["date"], "2026-10-01")
        self.assertEqual(h.validate_harvest(self.raw(date="yesterday"), KNOWN, TODAY)["date"], "2026-09-30")

    def test_all_problems_reported_with_location_hint(self):
        with self.assertRaises(h.HarvestError) as ctx:
            h.validate_harvest(self.raw(location="East Berlin Pine and Oak", yield_tier="", cap_stage="", weight_g="lots"), KNOWN, TODAY)
        self.assertEqual(len(ctx.exception.problems), 4)
        self.assertIn("Did you mean 'East Berlin Pine & Oak Ridge'", ctx.exception.problems[0])
        self.assertIn("Valid locations", ctx.exception.hint)

    def test_html_escaped_location_matches(self):
        self.assertEqual(h.validate_harvest(self.raw(location="East Berlin Pine &amp; Oak Ridge"), KNOWN, TODAY)["location"], KNOWN[0])

    def test_main_writes_error_file(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            err = Path(d) / "err.md"
            cfg = Path(d) / "config.json"
            cfg.write_text('{"LOCATIONS": [{"name": "A"}]}')
            env = {"ISSUE_AUTHOR_ASSOCIATION": "OWNER", "ISSUE_BODY": "### Location / spot\n\nB\n", "ISSUE_NUMBER": "5", "HARVEST_ERROR_FILE": str(err)}
            with patch.dict(os.environ, env):
                self.assertEqual(h.main(["--config", str(cfg)]), 1)
            self.assertIn("Unknown location 'B'", err.read_text())

    def test_main_accepts_trusted_issue_without_label(self):
        import os
        from unittest.mock import patch
        with tempfile.TemporaryDirectory() as d:
            cfg = Path(d) / "config.json"
            cfg.write_text('{"LOCATIONS": [{"name": "East Berlin Pine & Oak Ridge"}]}')
            env = {"ISSUE_AUTHOR_ASSOCIATION": "OWNER", "ISSUE_BODY": BODY, "ISSUE_NUMBER": "5", "ISSUE_AUTHOR": "me"}
            saved = {}
            with patch.dict(os.environ, env), patch.object(h, "load_harvest_log", return_value={"harvests": []}), \
                    patch.object(h, "save_harvest_log", side_effect=lambda log: saved.update(log)):
                self.assertEqual(h.main(["--config", str(cfg)]), 0)
            self.assertEqual(saved["harvests"][0]["issue"], 5)

    def test_sync_issue_form_switches_location_to_dropdown(self):
        with tempfile.TemporaryDirectory() as d:
            form = Path(d) / "harvest.yml"
            form.write_text("body:\n" + h.render_location_field([]) + "\n")
            self.assertTrue(h.sync_issue_form(["A & B", "C"], form))
            self.assertIn("type: dropdown", form.read_text())
            self.assertIn('- "A & B"', form.read_text())
            self.assertFalse(h.sync_issue_form(["A & B", "C"], form))

    def test_issue_form_template_has_markers_and_friendly_options(self):
        text = Path(".github/ISSUE_TEMPLATE/harvest.yml").read_text()
        self.assertIn(h.FORM_LOCATIONS_START, text)
        self.assertIn("Buttons / young", text)


class ObservationLogTests(unittest.TestCase):
    LOCS = [{"name": "A", "past_harvests": [{"date": "2026-09-15", "yield_tier": "large", "cap_stage": "prime", "weight_g": 450}]}]
    LOG = {"harvests": [
        {"location": "A", "date": "2026-09-15", "yield_tier": "large", "cap_stage": "prime", "weight_g": 450, "issue": 1},
        {"location": "A", "date": "2026-10-02", "observation_type": "no_mushrooms", "issue": 13},
        {"location": "Retired", "date": "2026-09-20", "yield_tier": "small", "cap_stage": "prime", "issue": 4, "notes": "<b>x</b>"},
    ]}

    def test_build_log_merges_dedupes_and_sorts_newest_first(self):
        entries = h.build_observation_log(self.LOCS, self.LOG)
        self.assertEqual([(e["date"], e["origin"]) for e in entries], [("2026-10-02", "log"), ("2026-09-20", "log (location not configured)"), ("2026-09-15", "config")])
        self.assertEqual(entries[0]["issue"], 13)
        self.assertEqual(entries[2]["location"], "A")
        self.assertEqual(h.build_observation_log(None, None), [])

    def test_summary_counts(self):
        rows = {r["location"]: r for r in h.summarize_observations(h.build_observation_log(self.LOCS, self.LOG))}
        self.assertEqual((rows["A"]["harvests"], rows["A"]["no_mushrooms"], rows["A"]["weight_g"], rows["A"]["last_date"]), (1, 1, 450, "2026-10-02"))

    def test_dashboard_renders_log_table(self):
        import porcini as p
        out = p.generate_dashboard_html({"ALERT_THRESHOLD": 65, "LOCATIONS": self.LOCS}, [], harvest_log=self.LOG)
        self.assertIn('id="observation-log"', out)
        self.assertIn("/issues/13", out)
        self.assertIn("Reported by", out)
        self.assertIn("Browser-local drafts are not shared", out)
        self.assertIn("&lt;b&gt;x&lt;/b&gt;", out)
        self.assertNotIn("<b>x</b>", out)
        self.assertIn("Retired", out)
        section = out[out.index('id="observation-log"'):]
        self.assertLess(section.index("Fri. 02 Oct. 2026"), section.index("Tue. 15 Sep. 2026"))

    def test_unconfigured_location_is_flagged_not_dropped(self):
        import porcini as p
        entries = h.build_observation_log(self.LOCS, self.LOG)
        self.assertTrue(any(e["origin"] == "log (location not configured)" and e["location"] == "Retired" for e in entries))
        out = p.generate_dashboard_html({"ALERT_THRESHOLD": 65, "LOCATIONS": self.LOCS}, [], harvest_log=self.LOG)
        self.assertIn("no longer configured", out)

    def test_dashboard_without_observations(self):
        import porcini as p
        self.assertIn("No observations recorded yet", p.generate_dashboard_html({"ALERT_THRESHOLD": 65}, []))


class StoreMergeTests(unittest.TestCase):
    def test_append_idempotent_per_issue(self):
        log = {"harvests": []}
        rec = h.validate_harvest(h.parse_issue_body(BODY), KNOWN, TODAY)
        self.assertTrue(h.append_harvest(log, rec, 7, "me"))
        self.assertFalse(h.append_harvest(log, rec, 7, "me"))
        self.assertEqual(len(log["harvests"]), 1)
        revised = dict(rec, date="2026-09-16")
        self.assertTrue(h.append_harvest(log, revised, 7, "me"))
        self.assertEqual(len(log["harvests"]), 1)
        self.assertEqual(log["harvests"][0]["date"], "2026-09-16")

    def test_save_load_roundtrip_and_corrupt(self):
        with tempfile.TemporaryDirectory() as d:
            path = Path(d) / "harvest_log.json"
            self.assertEqual(h.load_harvest_log(path)["harvests"], [])
            log = {"schema_version": 1, "harvests": [{"location": "A", "date": "2026-09-01"}]}
            h.save_harvest_log(log, path)
            self.assertEqual(h.load_harvest_log(path), log)
            path.write_text("{bad")
            with self.assertRaises(h.HarvestError):
                h.load_harvest_log(path)

    def test_merge_dedupes_filters_location_and_tags_origin(self):
        cfg = [{"date": "2026-09-15", "yield_tier": "large", "cap_stage": "prime", "weight_g": 450}]
        log = {"harvests": [
            {"location": "A", "date": "2026-09-15", "yield_tier": "large", "cap_stage": "prime", "weight_g": 450, "issue": 1},
            {"location": "A", "date": "2026-09-01", "yield_tier": "medium", "cap_stage": "prime", "issue": 2, "reporter": "me"},
            {"location": "B", "date": "2026-09-02", "yield_tier": "small", "cap_stage": "prime"},
        ]}
        merged = h.merge_harvests(cfg, log, "A")
        self.assertEqual([(m["date"], m["origin"]) for m in merged], [("2026-09-01", "log"), ("2026-09-15", "config")])
        self.assertNotIn("reporter", merged[0])
        self.assertEqual(h.merge_harvests(None, None, "A"), [])

    def test_merged_log_harvest_affects_scoring_inputs(self):
        import porcini as p
        loc = {"name": "A", "past_harvests": []}
        merged = h.merge_harvests(loc["past_harvests"], {"harvests": [{"location": "A", "date": "2026-09-01", "yield_tier": "large", "cap_stage": "prime"}]}, "A")
        self.assertNotEqual(p.location_signature(loc), p.location_signature(dict(loc, past_harvests=merged)))

    def test_dashboard_marks_sources(self):
        import porcini as p
        html_out = p.generate_dashboard_html({"ALERT_THRESHOLD": 65}, [])
        self.assertIn("harvest_log.json", html_out)
        self.assertIn("unsynced draft", html_out)
        self.assertIn("Visited, found no mushrooms", html_out)
        self.assertIn("function localToday()", html_out)
        self.assertIn("Open prefilled GitHub issue", html_out)
        self.assertIn("click GitHub's Submit new issue button", html_out)


if __name__ == "__main__":
    unittest.main()
