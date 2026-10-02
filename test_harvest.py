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

    def test_no_mushrooms_observation_needs_no_harvest_details(self):
        raw = h.parse_issue_body(BODY)
        raw["observation_type"] = "no_mushrooms"
        raw["yield_tier"] = ""
        raw["cap_stage"] = ""
        raw["weight_g"] = ""
        self.assertEqual(h.validate_harvest(raw, KNOWN, TODAY), {
            "location": KNOWN[0], "date": "2026-09-15", "observation_type": "no_mushrooms",
        })

    def test_missing_observation_type_defaults_to_harvest(self):
        raw = h.parse_issue_body(BODY)
        raw.pop("observation_type")
        self.assertEqual(h.validate_harvest(raw, KNOWN, TODAY)["observation_type"], "harvest")

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


class StoreMergeTests(unittest.TestCase):
    def test_append_idempotent_per_issue(self):
        log = {"harvests": []}
        rec = h.validate_harvest(h.parse_issue_body(BODY), KNOWN, TODAY)
        self.assertTrue(h.append_harvest(log, rec, 7, "me"))
        self.assertFalse(h.append_harvest(log, rec, 7, "me"))
        self.assertEqual(len(log["harvests"]), 1)

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


if __name__ == "__main__":
    unittest.main()
