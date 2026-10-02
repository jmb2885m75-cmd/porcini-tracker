"""Harvest intake: parse/validate GitHub issue-form payloads and merge them into the tracker data model.

Offline only (no network). The repository-backed store is harvest_log.json.
"""
import argparse
import json
import os
import re
import sys
from datetime import date
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

HARVEST_LOG_PATH = Path("harvest_log.json")
LOG_SCHEMA_VERSION = 1
YIELD_TIERS = ("small", "medium", "large")
CAP_STAGES = ("buttons_young", "prime", "old_overripe")
OBSERVATION_TYPES = ("harvest", "no_mushrooms")
TRUSTED_ASSOCIATIONS = ("OWNER", "MEMBER", "COLLABORATOR")
MAX_NOTES_LEN = 300
MAX_WEIGHT_G = 100000

# Issue-form headings (see .github/ISSUE_TEMPLATE/harvest.yml) -> record keys
FIELD_LABELS = {
    "location / spot": "location",
    "date": "date",
    "observation type": "observation_type",
    "yield tier": "yield_tier",
    "cap stage": "cap_stage",
    "weight (g)": "weight_g",
    "notes": "notes",
}
_BLANK = {"", "_no response_", "none"}


class HarvestError(ValueError):
    pass


def parse_issue_body(body: str) -> Dict[str, str]:
    """Split a GitHub issue-form body ('### Label' sections) into raw field values."""
    fields: Dict[str, str] = {}
    current: Optional[str] = None
    buf: List[str] = []

    def flush() -> None:
        if current is not None:
            value = "\n".join(buf).strip()
            fields[current] = "" if value.lower() in _BLANK else value

    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            flush()
            current = FIELD_LABELS.get(line[4:].strip().lower())
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    return fields


def _clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def validate_harvest(raw: Dict[str, str], known_locations: Optional[Iterable[str]] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """Return a clean harvest record or raise HarvestError."""
    today = today or date.today()
    location = _clean_text(raw.get("location", ""))
    if not location:
        raise HarvestError("location is required")
    if known_locations is not None:
        known = list(known_locations)
        match = next((k for k in known if k.lower() == location.lower()), None)
        if match is None:
            raise HarvestError(f"unknown location '{location}'")
        location = match
    try:
        day = date.fromisoformat(raw.get("date", "").strip())
    except ValueError:
        raise HarvestError("date must be YYYY-MM-DD")
    if day > today:
        raise HarvestError("date is in the future")
    observation_type = raw.get("observation_type", "").strip().lower() or "harvest"
    if observation_type not in OBSERVATION_TYPES:
        raise HarvestError(f"observation type must be one of {', '.join(OBSERVATION_TYPES)}")
    record: Dict[str, Any] = {"location": location, "date": day.isoformat(), "observation_type": observation_type}
    if observation_type == "harvest":
        tier = raw.get("yield_tier", "").strip().lower()
        if tier not in YIELD_TIERS:
            raise HarvestError(f"yield tier must be one of {', '.join(YIELD_TIERS)}")
        stage = raw.get("cap_stage", "").strip().lower()
        if stage not in CAP_STAGES:
            raise HarvestError(f"cap stage must be one of {', '.join(CAP_STAGES)}")
        record.update({"yield_tier": tier, "cap_stage": stage})
    weight = raw.get("weight_g", "").strip()
    if weight and observation_type == "harvest":
        try:
            grams = float(weight)
        except ValueError:
            raise HarvestError("weight must be a number of grams")
        if not 0 <= grams <= MAX_WEIGHT_G:
            raise HarvestError("weight out of range")
        record["weight_g"] = int(grams) if grams == int(grams) else grams
    notes = _clean_text(raw.get("notes", ""))[:MAX_NOTES_LEN]
    if notes:
        record["notes"] = notes
    return record


def is_trusted_actor(author_association: str, author: str = "", owner: str = "") -> bool:
    if owner and author and author.lower() == owner.lower():
        return True
    return (author_association or "").upper() in TRUSTED_ASSOCIATIONS


def load_harvest_log(path: Path = HARVEST_LOG_PATH) -> Dict[str, Any]:
    empty = {"schema_version": LOG_SCHEMA_VERSION, "harvests": []}
    if not path.exists():
        return empty
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise HarvestError(f"{path} is unreadable; refusing to overwrite it")
    if not isinstance(data, dict) or not isinstance(data.get("harvests"), list):
        raise HarvestError(f"{path} has an unexpected structure")
    return data


def save_harvest_log(log: Dict[str, Any], path: Path = HARVEST_LOG_PATH) -> None:
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(log, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)


def append_harvest(log: Dict[str, Any], record: Dict[str, Any], issue_number: Optional[int] = None, reporter: str = "") -> bool:
    """Append record to the log. Returns False if this issue was already ingested (idempotent)."""
    if issue_number is not None and any(h.get("issue") == issue_number for h in log["harvests"]):
        return False
    entry = dict(record)
    if issue_number is not None:
        entry["issue"] = issue_number
    if reporter:
        entry["reporter"] = reporter
    log["harvests"].append(entry)
    return True


def _key(h: Dict[str, Any]) -> Tuple[Any, ...]:
    return (h.get("date"), h.get("observation_type", "harvest"), h.get("yield_tier"), h.get("cap_stage"), h.get("weight_g"))


def merge_harvests(config_harvests: Optional[List[Dict[str, Any]]], log: Optional[Dict[str, Any]], location_name: str) -> List[Dict[str, Any]]:
    """Config past_harvests + repository log entries for this location, de-duplicated, sorted by date.

    Each result carries an 'origin' tag ('config' or 'log'); scoring ignores extra keys.
    """
    merged: List[Dict[str, Any]] = []
    seen = set()
    sources = [("config", config_harvests or [])]
    sources.append(("log", [h for h in (log or {}).get("harvests", []) if isinstance(h, dict) and h.get("location") == location_name]))
    for origin, items in sources:
        for h in items:
            if not isinstance(h, dict) or not h.get("date") or _key(h) in seen:
                continue
            seen.add(_key(h))
            entry = {k: v for k, v in h.items() if k not in ("location", "issue", "reporter")}
            entry["origin"] = origin
            merged.append(entry)
    return sorted(merged, key=lambda h: h["date"])


def main(argv: Optional[List[str]] = None) -> int:
    """Workflow hook: ingest one issue (inputs from env vars so issue text is never shell-interpolated)."""
    parser = argparse.ArgumentParser(description="Ingest a harvest issue into harvest_log.json")
    parser.add_argument("--config", default="config.json")
    args = parser.parse_args(argv)
    if not is_trusted_actor(os.environ.get("ISSUE_AUTHOR_ASSOCIATION", ""), os.environ.get("ISSUE_AUTHOR", ""), os.environ.get("REPO_OWNER", "")):
        print("[HARVEST] rejected: author is not trusted")
        return 2
    cfg_path = Path(args.config)
    known = None
    if cfg_path.exists():
        known = [l.get("name") for l in json.loads(cfg_path.read_text(encoding="utf-8")).get("LOCATIONS", [])]
    try:
        record = validate_harvest(parse_issue_body(os.environ.get("ISSUE_BODY", "")), known)
        log = load_harvest_log()
        added = append_harvest(log, record, int(os.environ["ISSUE_NUMBER"]), os.environ.get("ISSUE_AUTHOR", ""))
    except (HarvestError, KeyError, ValueError) as exc:
        print(f"[HARVEST] rejected: {exc}")
        return 1
    if added:
        save_harvest_log(log)
    print("[HARVEST] accepted" if added else "[HARVEST] already ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
