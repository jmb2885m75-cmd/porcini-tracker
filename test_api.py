import json
import tempfile
import unittest
from pathlib import Path

import api

KNOWN = ["Forest A"]


class SubmitObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "harvest_log.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_valid_submission_is_appended(self):
        payload = {"location": "forest a", "date": "2024-09-01", "observation_type": "harvest",
                   "yield_tier": "small", "cap_stage": "prime", "weight_g": 120, "notes": "ok"}
        status, body = api.submit_observation(payload, KNOWN, self.log, reporter="me")
        self.assertEqual(status, 200)
        self.assertTrue(body["success"])
        self.assertEqual(body["entry"]["location"], "Forest A")
        self.assertEqual(body["entry"]["reporter"], "me")
        stored = json.loads(self.log.read_text())["harvests"]
        self.assertEqual(len(stored), 1)
        self.assertIn("submitted_at", stored[0])

    def test_invalid_submission_rejected_and_not_saved(self):
        status, body = api.submit_observation({"location": "Nowhere", "date": "2024-09-01", "observation_type": "no_mushrooms"}, KNOWN, self.log)
        self.assertEqual(status, 422)
        self.assertFalse(body["success"])
        self.assertFalse(self.log.exists())

    def test_non_object_rejected(self):
        self.assertEqual(api.submit_observation([], KNOWN, self.log)[0], 400)

    def test_invalid_json_and_auth(self):
        cfg = Path(self.tmp.name) / "config.json"
        cfg.write_text(json.dumps({"LOCATIONS": [{"name": "Forest A"}]}))
        self.assertEqual(api.handle_request(b"{", {}, cfg, self.log)[0], 400)
        good = json.dumps({"location": "Forest A", "date": "2024-09-01", "observation_type": "no_mushrooms"}).encode()
        self.assertEqual(api.handle_request(good, {}, cfg, self.log)[0], 200)
        import os
        os.environ["OBSERVATION_API_KEY"] = "secret"
        try:
            self.assertEqual(api.handle_request(good, {}, cfg, self.log)[0], 401)
            status, body = api.handle_request(good, {"Authorization": "Bearer " + "secret"}, cfg, self.log)
            self.assertEqual((status, body["entry"]["reporter"]), (200, "api-key"))
        finally:
            del os.environ["OBSERVATION_API_KEY"]


if __name__ == "__main__":
    unittest.main()
