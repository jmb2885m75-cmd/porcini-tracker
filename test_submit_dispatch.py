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

    def test_invalid_location_fails(self):
        rc, h = self.run_main({"INPUT_LOCATION": "Nowhere", "INPUT_OBSERVATION_TYPE": "no_mushrooms"})
        self.assertEqual(rc, 1)
        self.assertEqual(h, [])


if __name__ == "__main__":
    unittest.main()
