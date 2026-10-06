"""Direct observation submission: POST /api/submit-observation validates and appends to harvest_log.json."""
import argparse
import hmac
import json
import os
import re
import sys
import threading
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

import requests

from harvest import (HARVEST_LOG_PATH, HarvestError, _read_locations, append_harvest, entry_id, load_harvest_log,
                     save_harvest_log, validate_harvest)

ENDPOINT = "/api/submit-observation"
DELETE_ENDPOINT = "/api/delete-observation"
AUDIT_LOG_NAME = "harvest_deletions.log"
FIELDS = ("location", "date", "observation_type", "yield_tier", "cap_stage", "weight_g", "notes")
MAX_BODY = 16 * 1024
_LOCK = threading.Lock()


def _fail(status: int, message: str) -> Tuple[int, Dict[str, Any]]:
    return status, {"success": False, "message": message, "entry": None}


def identify_submitter(token: str, repo: str = "", api_key: str = "") -> Optional[str]:
    """Return the submitter name for a valid API key or a GitHub token with push access to `repo`; None if rejected."""
    if not token:
        return None
    if api_key and hmac.compare_digest(token, api_key):
        return "api-key"
    if not repo:
        return None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    try:
        user = requests.get("https://api.github.com/user", headers=headers, timeout=10)
        info = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
    except requests.RequestException:
        return None
    if user.status_code != 200 or info.status_code != 200:
        return None
    perms = info.json().get("permissions") or {}
    if perms.get("push") or perms.get("admin"):
        return str(user.json().get("login", "")) or None
    return None


def identify_caller(token: str, repo: str = "", api_key: str = "") -> Optional[Tuple[str, bool]]:
    """Return (name, is_admin) for a valid API key (admin) or GitHub token with push access; None if rejected."""
    if not token:
        return None
    if api_key and hmac.compare_digest(token, api_key):
        return "api-key", True
    if not repo:
        return None
    headers = {"Authorization": "Bearer " + token, "Accept": "application/vnd.github+json"}
    try:
        user = requests.get("https://api.github.com/user", headers=headers, timeout=10)
        info = requests.get(f"https://api.github.com/repos/{repo}", headers=headers, timeout=10)
    except requests.RequestException:
        return None
    if user.status_code != 200 or info.status_code != 200:
        return None
    perms = info.json().get("permissions") or {}
    name = str(user.json().get("login", ""))
    if name and (perms.get("push") or perms.get("admin")):
        return name, bool(perms.get("admin"))
    return None


def delete_observation(entry_key: Any, caller: str, is_admin: bool, log_path: Path = HARVEST_LOG_PATH) -> Tuple[int, Dict[str, Any]]:
    """Remove one harvest_log.json entry (own entries, or any entry for an admin) and audit-log it. Returns (http_status, body)."""
    if not isinstance(entry_key, str) or not entry_key:
        return _fail(400, "An entry id is required.")
    try:
        with _LOCK:
            log = load_harvest_log(log_path)
            index = next((i for i, e in enumerate(log["harvests"]) if entry_id(e) == entry_key), None)
            if index is None:
                return _fail(404, "Entry not found; it may already have been deleted.")
            entry = log["harvests"][index]
            if not is_admin and entry.get("reporter") != caller:
                return _fail(403, "You can only delete entries you submitted.")
            del log["harvests"][index]
            save_harvest_log(log, log_path)
            audit = {"deleted_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"), "deleted_by": caller, "entry": entry}
            with open(log_path.with_name(AUDIT_LOG_NAME), "a", encoding="utf-8") as fh:
                fh.write(json.dumps(audit, ensure_ascii=False) + "\n")
    except (HarvestError, OSError) as exc:
        return _fail(500, f"Could not delete the observation: {exc}")
    return 200, {"success": True, "message": "Observation deleted.", "entry": entry}


def handle_delete_request(body: bytes, headers, log_path: Path = HARVEST_LOG_PATH) -> Tuple[int, Dict[str, Any]]:
    """Authenticate (always required) and process one deletion."""
    api_key = os.environ.get("OBSERVATION_API_KEY", "")
    repo = os.environ.get("OBSERVATION_REPO", "")
    if not (api_key or repo):
        return _fail(403, "Deleting is disabled: set OBSERVATION_API_KEY or OBSERVATION_REPO on the server.")
    auth = headers.get("Authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("X-API-Key", "").strip()
    caller = identify_caller(token, repo, api_key)
    if caller is None:
        return _fail(401, "A valid API key or GitHub token for the repository owner or a collaborator is required.")
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail(400, "Request body is not valid JSON.")
    if not isinstance(payload, dict):
        return _fail(400, "Request body must be a JSON object.")
    return delete_observation(payload.get("id"), caller[0], caller[1], log_path)


def submit_observation(payload: Any, known_locations, log_path: Path = HARVEST_LOG_PATH, reporter: str = "") -> Tuple[int, Dict[str, Any]]:
    """Validate `payload` with the harvest-intake rules and append it to the log. Returns (http_status, body)."""
    if not isinstance(payload, dict):
        return _fail(400, "Request body must be a JSON object.")
    raw = {k: "" if payload.get(k) is None else str(payload[k]) for k in FIELDS}
    try:
        record = validate_harvest(raw, known_locations)
    except HarvestError as exc:
        message = " ".join(exc.problems)
        return _fail(422, message)
    record["submitted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    try:
        with _LOCK:
            log = load_harvest_log(log_path)
            append_harvest(log, record, reporter=reporter)
            save_harvest_log(log, log_path)
    except (HarvestError, OSError) as exc:
        return _fail(500, f"Could not save the observation: {exc}")
    entry = dict(record)
    if reporter:
        entry["reporter"] = reporter
    return 200, {"success": True, "message": "Observation saved.", "entry": entry}


def handle_request(body: bytes, headers, config_path: Path, log_path: Path = HARVEST_LOG_PATH) -> Tuple[int, Dict[str, Any]]:
    """Authenticate (when OBSERVATION_API_KEY or OBSERVATION_REPO is configured) and process one submission."""
    api_key = os.environ.get("OBSERVATION_API_KEY", "")
    repo = os.environ.get("OBSERVATION_REPO", "")
    reporter = ""
    if api_key or repo:
        auth = headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("X-API-Key", "").strip()
        who = identify_submitter(token, repo, api_key)
        if who is None:
            return _fail(401, "A valid API key or GitHub token for the repository owner or a collaborator is required.")
        reporter = who
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return _fail(400, "Request body is not valid JSON.")
    try:
        known = _read_locations(config_path)
    except HarvestError as exc:
        return _fail(500, str(exc))
    if known is None:
        return _fail(500, "Configuration is unavailable on the server.")
    raw_location = str(payload.get("location", "")) if isinstance(payload, dict) else ""
    alias = re.fullmatch(r"Location ([1-9][0-9]*)", raw_location)
    if alias:
        index = int(alias.group(1)) - 1
        if index >= len(known):
            return _fail(422, "Unknown location.")
        payload = dict(payload, location=known[index])
    if not reporter and isinstance(payload, dict) and payload.get("reporter"):
        reporter = str(payload["reporter"])[:60]
    return submit_observation(payload, known, log_path, reporter)


def make_handler(config_path: Path, log_path: Path):
    class Handler(BaseHTTPRequestHandler):
        def _send(self, status: int, body: Dict[str, Any]) -> None:
            data = json.dumps(body).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.end_headers()
            self.wfile.write(data)

        def do_OPTIONS(self) -> None:
            self.send_response(204)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "POST, DELETE, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization, X-API-Key")
            self.end_headers()

        def do_DELETE(self) -> None:
            if self.path.split("?")[0] != DELETE_ENDPOINT:
                return self._send(*_fail(404, "Not found."))
            length = self._length()
            if not 0 < length <= MAX_BODY:
                return self._send(*_fail(400, "Request body missing or too large."))
            self._send(*handle_delete_request(self.rfile.read(length), self.headers, log_path))

        def _length(self) -> int:
            try:
                return int(self.headers.get("Content-Length", "0"))
            except ValueError:
                return -1

        def do_POST(self) -> None:
            if self.path.split("?")[0] != ENDPOINT:
                return self._send(*_fail(404, "Not found."))
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(*_fail(400, "Request body missing or too large."))
            self._send(*handle_request(self.rfile.read(length), self.headers, config_path, log_path))

    return Handler


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="Serve POST /api/submit-observation")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--log", default=str(HARVEST_LOG_PATH))
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    args = parser.parse_args(argv)
    server = ThreadingHTTPServer((args.host, args.port), make_handler(Path(args.config), Path(args.log)))
    print(f"[API] listening on http://{args.host}:{args.port}{ENDPOINT}")
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
