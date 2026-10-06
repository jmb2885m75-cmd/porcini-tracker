import json
import tempfile
import unittest
from pathlib import Path

from scripts.submit_observation_from_dispatch import main


class DispatchTests(unittest.TestCase):
    def run_main(self, env):
        d = Path(tempfile.mkdtemp())
        cfg, log = d / "config.json", d / "log.json"
        cfg.write_text(json.dumps({"LOCATIONS": [{"name": "Spot A"}]}))
        rc = main(env, cfg, log)
        return rc, (json.loads(log.read_text())["harvests"] if log.exists() else [])

    def test_valid_and_blank_weight(self):
        rc, h = self.run_main({"INPUT_LOCATION": "Spot A", "INPUT_OBSERVATION_TYPE": "no_mushrooms", "INPUT_WEIGHT_G": " ", "GITHUB_ACTOR": "me"})
        self.assertEqual(rc, 0)
        self.assertEqual(h[0]["reporter"], "me")

    def test_public_location_alias_resolves_to_configured_location(self):
        rc, h = self.run_main({"INPUT_LOCATION": "Location 1", "INPUT_OBSERVATION_TYPE": "no_mushrooms"})
        self.assertEqual(rc, 0)
        self.assertEqual(h[0]["location"], "Spot A")

    def test_invalid_location_fails(self):
        rc, h = self.run_main({"INPUT_LOCATION": "Nowhere", "INPUT_OBSERVATION_TYPE": "no_mushrooms"})
        self.assertEqual(rc, 1)
        self.assertEqual(h, [])

    def test_harvest_workflow_passes_issue_number_through_environment(self):
        workflow = Path(".github/workflows/harvest_intake.yml").read_text(encoding="utf-8")
        self.assertIn("ISSUE: ${{ github.event.issue.number }}", workflow)
        commit_step = workflow.split("git commit -m \"🍄 Record harvest", 1)[1].splitlines()[0]
        self.assertNotIn("${{", commit_step)
        self.assertIn("$ISSUE", commit_step)


if __name__ == "__main__":
    unittest.main()
