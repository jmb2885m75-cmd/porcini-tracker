"""Harvest intake: parse/validate GitHub issue-form payloads and merge them into the tracker data model.

Offline only (no network). The repository-backed store is harvest_log.json.
"""
import argparse
import difflib
import html
import json
import os
import re
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from dates import parse_user_date

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

# Friendly issue-form option labels (and legacy raw values) -> stored enum values
OBSERVATION_ALIASES = {
    "harvest": "harvest", "found mushrooms": "harvest", "found mushrooms (harvest)": "harvest",
    "no_mushrooms": "no_mushrooms", "no mushrooms": "no_mushrooms", "no mushrooms found": "no_mushrooms",
    "visited, found no mushrooms": "no_mushrooms",
}
YIELD_ALIASES = {"small": "small", "medium": "medium", "large": "large"}
CAP_ALIASES = {
    "buttons_young": "buttons_young", "buttons / young": "buttons_young", "buttons/young": "buttons_young", "buttons": "buttons_young", "young": "buttons_young",
    "prime": "prime",
    "old_overripe": "old_overripe", "old / overripe": "old_overripe", "old/overripe": "old_overripe", "old": "old_overripe", "overripe": "old_overripe",
}
ERROR_FILE_ENV = "HARVEST_ERROR_FILE"
FORM_PATH = Path(".github/ISSUE_TEMPLATE/harvest.yml")
FORM_LOCATIONS_START = "  # BEGIN LOCATION FIELD"
FORM_LOCATIONS_END = "  # END LOCATION FIELD"


class HarvestError(ValueError):
    """Validation failure. `problems` lists every individual issue found (str(exc) joins them)."""

    def __init__(self, message: Any, hint: str = ""):
        problems = list(message) if isinstance(message, (list, tuple)) else [message]
        super().__init__("; ".join(problems))
        self.problems = problems
        self.hint = hint


def _choice(value: str, aliases: Dict[str, str]) -> Optional[str]:
    return aliases.get(re.sub(r"\s+", " ", (value or "").strip().lower()))


def _parse_weight(text: str) -> float:
    """Grams from '450', '450 g', '1,5 kg' ... Raises ValueError."""
    match = re.fullmatch(r"([0-9]+(?:[.,][0-9]+)?)\s*(kg|g|gr|grams?)?", text.strip().lower())
    if not match:
        raise ValueError(text)
    grams = float(match.group(1).replace(",", "."))
    return grams * 1000 if match.group(2) == "kg" else grams


def _match_location(location: str, known: List[str]) -> Optional[str]:
    wanted = _clean_text(html.unescape(location)).lower()
    return next((k for k in known if _clean_text(html.unescape(str(k))).lower() == wanted), None)


def parse_issue_body(body: str) -> Dict[str, str]:
    """Split a GitHub issue-form body ('### Label' sections) into raw field values."""
    fields: Dict[str, str] = {}
    unknown: List[str] = []
    current: Optional[str] = None
    buf: List[str] = []

    def flush() -> None:
        if current is not None:
            value = "\n".join(buf).strip()
            fields[current] = "" if value.lower() in _BLANK else value

    for line in (body or "").replace("\r\n", "\n").split("\n"):
        if line.startswith("### "):
            flush()
            label = line[4:].strip()
            current = FIELD_LABELS.get(label.lower())
            if current is None:
                unknown.append(label)
            buf = []
        elif current is not None:
            buf.append(line)
    flush()
    if unknown:
        raise HarvestError("Unrecognized issue-form field heading(s): " + ", ".join(unknown) + ".")
    return fields


def _clean_text(value: str) -> str:
    value = re.sub(r"[\x00-\x1f\x7f]+", " ", value or "")
    return re.sub(r"\s+", " ", value).strip()


def validate_harvest(raw: Dict[str, str], known_locations: Optional[Iterable[str]] = None, today: Optional[date] = None) -> Dict[str, Any]:
    """Return a clean harvest record or raise HarvestError listing every problem found."""
    today = today or date.today()
    problems: List[str] = []
    hints: List[str] = []

    location = _clean_text(raw.get("location", ""))
    if not location:
        problems.append("Location is required.")
    elif known_locations is not None:
        known = [k for k in known_locations if k]
        match = _match_location(location, known)
        if match is None:
            problems.append(f"Unknown location '{location}'.")
            close = difflib.get_close_matches(location, known, n=1, cutoff=0.5)
            if close:
                problems[-1] += f" Did you mean '{close[0]}'?"
            if known:
                hints.append("Valid locations: " + ", ".join(f"'{k}'" for k in known))
        else:
            location = match

    day: Optional[date] = None
    date_text = _clean_text(raw.get("date", "")).lower()
    if date_text in ("", "today"):
        day = today
    elif date_text == "yesterday":
        day = today - timedelta(days=1)
    else:
        try:
            day = parse_user_date(raw.get("date", ""))
        except ValueError:
            problems.append("Date must be yyyy-MM-dd, dd.MM.yyyy or dd MMMM yyyy (or 'today' / 'yesterday') and a real calendar date.")
    if day is not None and day > today:
        problems.append(f"Date {day.isoformat()} is in the future.")

    type_text = raw.get("observation_type", "")
    observation_type = _choice(type_text, OBSERVATION_ALIASES) if type_text.strip() else None
    if observation_type is None:
        problems.append("Observation type is required; choose 'Found mushrooms' or 'No mushrooms found'.")

    record: Dict[str, Any] = {}
    if observation_type == "harvest":
        tier = _choice(raw.get("yield_tier", ""), YIELD_ALIASES)
        if tier is None:
            problems.append(f"Yield tier is required when mushrooms were found (one of {', '.join(YIELD_TIERS)}).")
        stage = _choice(raw.get("cap_stage", ""), CAP_ALIASES)
        if stage is None:
            problems.append("Cap stage is required when mushrooms were found (Buttons / young, Prime or Old / overripe).")
        weight = raw.get("weight_g", "").strip()
        if weight:
            try:
                grams = _parse_weight(weight)
            except ValueError:
                problems.append(f"Weight '{weight}' must be a number of grams, e.g. 450.")
            else:
                if not 0 <= grams <= MAX_WEIGHT_G:
                    problems.append(f"Weight must be between 0 and {MAX_WEIGHT_G} g.")
                else:
                    record["weight_g"] = int(grams) if grams == int(grams) else grams
        if tier and stage:
            record.update({"yield_tier": tier, "cap_stage": stage})
    if problems:
        raise HarvestError(problems, "\n".join(hints))

    notes = _clean_text(raw.get("notes", ""))[:MAX_NOTES_LEN]
    result: Dict[str, Any] = {"location": location, "date": day.isoformat(), "observation_type": observation_type}
    for key in ("yield_tier", "cap_stage", "weight_g"):
        if key in record:
            result[key] = record[key]
    if notes:
        result["notes"] = notes
    return result


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
    """Insert or update an issue record. Returns False when the stored record is unchanged."""
    entry = dict(record)
    if issue_number is not None:
        entry["issue"] = issue_number
    if reporter:
        entry["reporter"] = reporter
    if issue_number is not None:
        for index, existing in enumerate(log["harvests"]):
            if existing.get("issue") == issue_number:
                if existing == entry:
                    return False
                log["harvests"][index] = entry
                return True
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


def build_observation_log(locations: Optional[List[Dict[str, Any]]], log: Optional[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Every recorded input (config past_harvests + harvest_log.json), newest first.

    Same de-duplication rule as merge_harvests (config wins over an identical log entry), but keeps location,
    issue number and reporter, and includes log entries for locations that are no longer configured.
    Each entry carries origin 'config' or 'log'.
    """
    entries: List[Dict[str, Any]] = []
    seen = set()

    def add(item: Any, location: Any, origin: str) -> None:
        if not isinstance(item, dict) or not item.get("date"):
            return
        key = (location,) + _key(item)
        if key in seen:
            return
        seen.add(key)
        entry = dict(item, location=location, origin=origin)
        entry.setdefault("observation_type", "harvest")
        entries.append(entry)

    for loc in locations or []:
        for item in loc.get("past_harvests", []) or []:
            add(item, loc.get("name"), "config")
    for item in (log or {}).get("harvests", []):
        add(item, item.get("location") if isinstance(item, dict) else None, "log")
    return sorted(entries, key=lambda e: (str(e["date"]), e.get("issue") or 0), reverse=True)


def summarize_observations(entries: Iterable[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Per-location counts: harvests, no-mushroom visits and total recorded grams."""
    summary: Dict[Any, Dict[str, Any]] = {}
    for e in entries:
        row = summary.setdefault(e.get("location"), {"location": e.get("location"), "harvests": 0, "no_mushrooms": 0, "weight_g": 0, "last_date": ""})
        if e.get("observation_type") == "no_mushrooms":
            row["no_mushrooms"] += 1
        else:
            row["harvests"] += 1
            if isinstance(e.get("weight_g"), (int, float)):
                row["weight_g"] += e["weight_g"]
        row["last_date"] = max(row["last_date"], str(e.get("date", "")))
    return sorted(summary.values(), key=lambda r: str(r["location"]))


def render_location_field(known: List[str]) -> str:
    """Issue-form block for the location field: a dropdown when locations are configured, else free text."""
    lines = [FORM_LOCATIONS_START]
    if known:
        lines += ["  - type: dropdown", "    id: location", "    attributes:", "      label: Location / spot",
                  "      description: Pick the spot you visited.", "      options:"]
        lines += [f"        - {json.dumps(k, ensure_ascii=False)}" for k in known]
    else:
        lines += ["  - type: input", "    id: location", "    attributes:", "      label: Location / spot",
                  "      description: Must match a configured location name (the dashboard form pre-fills it)."]
    lines += ["    validations:", "      required: true", FORM_LOCATIONS_END]
    return "\n".join(lines)


def sync_issue_form(known: List[str], path: Path = FORM_PATH) -> bool:
    """Rewrite the location field of the issue form from config. Returns True if the file changed."""
    text = path.read_text(encoding="utf-8")
    start, end = text.find(FORM_LOCATIONS_START), text.find(FORM_LOCATIONS_END)
    if start == -1 or end < start:
        raise HarvestError(f"{path} is missing the location field markers")
    new = text[:start] + render_location_field(known) + text[end + len(FORM_LOCATIONS_END):]
    if new == text:
        return False
    path.write_text(new, encoding="utf-8")
    return True


def _read_locations(cfg_path: Path) -> Optional[List[str]]:
    if not cfg_path.exists():
        return None
    try:
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise HarvestError(f"Could not read configuration: {exc}.") from exc
    if not isinstance(cfg, dict) or not isinstance(cfg.get("LOCATIONS"), list) or not cfg["LOCATIONS"]:
        raise HarvestError("Configuration must be an object containing a non-empty LOCATIONS list.")
    if any(not isinstance(location, dict) or not location.get("name") for location in cfg["LOCATIONS"]):
        raise HarvestError("Every configured location must have a name.")
    return [location["name"] for location in cfg["LOCATIONS"]]


def _report_error(exc: HarvestError) -> None:
    print(f"[HARVEST] rejected: {exc}")
    target = os.environ.get(ERROR_FILE_ENV)
    if target:
        lines = [f"- {p}" for p in exc.problems]
        if exc.hint:
            lines += ["", exc.hint]
        Path(target).write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: Optional[List[str]] = None) -> int:
    """Workflow hook: ingest one issue (inputs from env vars so issue text is never shell-interpolated)."""
    parser = argparse.ArgumentParser(description="Ingest a harvest issue into harvest_log.json")
    parser.add_argument("--config", default="config.json")
    parser.add_argument("--sync-form", action="store_true", help="Regenerate the issue form's location dropdown from the config and exit")
    args = parser.parse_args(argv)
    if args.sync_form:
        try:
            known = _read_locations(Path(args.config))
            if known is None:
                raise HarvestError("Repository configuration is unavailable; confirm the CONFIG_JSON Actions secret is set.")
            changed = sync_issue_form(known)
        except (HarvestError, OSError) as exc:
            print(f"[HARVEST] could not update issue form: {exc}")
            return 1
        print("[HARVEST] issue form updated" if changed else "[HARVEST] issue form already up to date")
        return 0
    cfg_path = Path(args.config)
    try:
        if os.environ.get("ISSUE_HAS_HARVEST_LABEL", "").lower() != "true":
            raise HarvestError("This observation is missing the 'harvest' label. Add the label and edit the issue to retry intake.")
        if not is_trusted_actor(os.environ.get("ISSUE_AUTHOR_ASSOCIATION", ""), os.environ.get("ISSUE_AUTHOR", ""), os.environ.get("REPO_OWNER", "")):
            raise HarvestError("Only the repository owner, members, or collaborators can submit observations.")
        known = _read_locations(cfg_path)
        if known is None:
            raise HarvestError("Repository configuration is unavailable. Confirm the CONFIG_JSON Actions secret is set.")
        record = validate_harvest(parse_issue_body(os.environ.get("ISSUE_BODY", "")), known)
        log = load_harvest_log()
        added = append_harvest(log, record, int(os.environ["ISSUE_NUMBER"]), os.environ.get("ISSUE_AUTHOR", ""))
    except HarvestError as exc:
        _report_error(exc)
        return 1
    except (KeyError, ValueError) as exc:
        _report_error(HarvestError(f"Could not process the issue ({exc!r})."))
        return 1
    if added:
        save_harvest_log(log)
    print("[HARVEST] accepted" if added else "[HARVEST] already ingested")
    return 0


if __name__ == "__main__":
    sys.exit(main())
