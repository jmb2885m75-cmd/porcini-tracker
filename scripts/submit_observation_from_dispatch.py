"""Ingest one observation from workflow_dispatch inputs (INPUT_* env vars) into harvest_log.json."""
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harvest import (HARVEST_LOG_PATH, HarvestError, _read_locations, append_harvest, load_harvest_log,  # noqa: E402
                     save_harvest_log, validate_harvest)

FIELDS = ("location", "date", "observation_type", "yield_tier", "cap_stage", "weight_g", "notes")
IGNORED = ("submitted_at", "reporter")


def read_inputs(env=os.environ):
    raw = {name: (env.get("INPUT_" + name.upper()) or "").strip() for name in FIELDS}
    raw["weight_g"] = raw["weight_g"].replace(",", ".")
    return raw


def main(env=os.environ, config_path: Path = Path("config.json"), log_path: Path = HARVEST_LOG_PATH) -> int:
    raw = read_inputs(env)
    try:
        known = _read_locations(config_path)
        record = validate_harvest(raw, known)
    except HarvestError as exc:
        print("Observation rejected: " + " ".join(exc.problems), file=sys.stderr)
        return 1
    reporter = (env.get("INPUT_REPORTER") or env.get("GITHUB_ACTOR") or "").strip()[:60]
    try:
        log = load_harvest_log(log_path)
        same = {k: v for k, v in record.items() if k not in IGNORED}
        if any({k: v for k, v in h.items() if k not in IGNORED + ("issue",)} == same for h in log["harvests"]):
            print("Identical observation already recorded; nothing to do.")
            return 0
        record["submitted_at"] = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        append_harvest(log, record, reporter=reporter)
        save_harvest_log(log, log_path)
    except (HarvestError, OSError) as exc:
        print(f"Could not save the observation: {exc}", file=sys.stderr)
        return 1
    print(f"Recorded {record.get('observation_type')} at {record.get('location')} on {record.get('date')}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
