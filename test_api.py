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


class DeleteObservationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log = Path(self.tmp.name) / "harvest_log.json"
        entries = [{"location": "Forest A", "date": "2024-09-01", "observation_type": "no_mushrooms",
                    "submitted_at": "2024-09-02T10:00:00Z", "reporter": "me"},
                   {"location": "Forest A", "date": "2024-09-03", "observation_type": "no_mushrooms", "issue": 7}]
        self.log.write_text(json.dumps({"schema_version": 1, "harvests": entries}))

    def tearDown(self):
        self.tmp.cleanup()

    def test_owner_deletes_and_audit_logged(self):
        status, body = api.delete_observation("Forest A|2024-09-02T10:00:00Z", "me", False, self.log)
        self.assertEqual((status, body["success"]), (200, True))
        self.assertEqual(len(json.loads(self.log.read_text())["harvests"]), 1)
        audit = (Path(self.tmp.name) / api.AUDIT_LOG_NAME).read_text()
        self.assertIn('"deleted_by": "me"', audit)
        self.assertEqual(api.delete_observation("Forest A|2024-09-02T10:00:00Z", "me", False, self.log)[0], 404)

    def test_other_user_forbidden_admin_allowed(self):
        self.assertEqual(api.delete_observation("Forest A|2024-09-02T10:00:00Z", "other", False, self.log)[0], 403)
        self.assertEqual(api.delete_observation("issue-7", "other", False, self.log)[0], 403)
        self.assertEqual(api.delete_observation("issue-7", "boss", True, self.log)[0], 200)
        self.assertEqual(api.delete_observation("", "boss", True, self.log)[0], 400)

    def test_delete_requires_auth_config(self):
        self.assertEqual(api.handle_delete_request(b"{}", {}, self.log)[0], 403)
        import os
        os.environ["OBSERVATION_API_KEY"] = "secret"
        try:
            self.assertEqual(api.handle_delete_request(b"{}", {}, self.log)[0], 401)
            body = json.dumps({"id": "issue-7"}).encode()
            self.assertEqual(api.handle_delete_request(body, {"Authorization": "Bearer " + "secret"}, self.log)[0], 200)
        finally:
            del os.environ["OBSERVATION_API_KEY"]


if __name__ == "__main__":
    unittest.main()
