#!/usr/bin/env python3
"""
Porcini tracker with multi-year weather archive, scoring, alerts, and dashboard generation.
"""

import argparse
import bisect
import hashlib
import html
import json
import math
import os
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests

from dates import format_display_date, parse_user_date
from harvest import HarvestError, build_observation_log, load_harvest_log, merge_harvests, summarize_observations

CONFIG_PATH = Path("config.json")
DB_PATH = Path("porcini_db.json")
ALERT_STATE_PATH = Path("alert_state.json")
REPORT_PATH = Path("porcini_report.html")
INDEX_PATH = Path("index.html")
ALERT_MARKER_START = "<!-- ALERT_PREVIEW_START -->"
ALERT_MARKER_END = "<!-- ALERT_PREVIEW_END -->"

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"

INITIAL_ARCHIVE_DAYS = 730

HOST_TREES = {"birch", "beech", "chestnut", "fir", "oak", "pine", "spruce"}


def load_json(path: Path, default: Any = None) -> Any:
    if not path.exists():
        return default if default is not None else {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_json(path: Path, payload: Any) -> None:
    # Write to a temp file then rename so a crash never leaves a half-written JSON file behind.
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")
    os.replace(tmp, path)


def load_config() -> Dict[str, Any]:
    config_json = os.environ.get("CONFIG_JSON")
    if config_json:
        cfg = json.loads(config_json)
        if not isinstance(cfg, dict):
            raise ValueError("CONFIG_JSON must contain a JSON object")
    else:
        cfg = load_json(CONFIG_PATH)
    if not cfg:
        raise FileNotFoundError(f"Set CONFIG_JSON or provide a local {CONFIG_PATH}")
    return cfg


def ensure_notification_settings(cfg: Dict[str, Any]) -> Dict[str, Any]:
    settings = cfg.setdefault("NOTIFICATION_SETTINGS", {})
    provider = str(settings.get("provider") or settings.get("provider_name") or "").lower()
    if provider in ("", "telegram"):
        settings.setdefault("provider", "telegram")
        settings.setdefault("token", settings.get("telegram_token", ""))
        settings.setdefault("chat_id_or_recipient", settings.get("telegram_chat_id", ""))
    elif provider == "pushover":
        settings.setdefault("api_token", settings.get("token", ""))
        settings.setdefault("user_key", settings.get("user", ""))
    elif provider == "twilio":
        settings.setdefault("account_sid", settings.get("sid", ""))
        settings.setdefault("auth_token", settings.get("token", ""))
    cfg.setdefault("ALERT_THRESHOLD", 65)
    return cfg


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 30) -> Optional[Dict[str, Any]]:
    try:
        response = requests.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"[WARN] URL fetch failed: {url} :: {exc}")
        return None


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


def parse_date(value: str) -> date:
    return parse_user_date(value)


def month_day_window(dt: date) -> bool:
    aug_15 = date(dt.year, 8, 15)
    dec_01 = date(dt.year, 12, 1)
    return aug_15 <= dt <= dec_01


SCHEMA_VERSION = 3
# Bump when the scoring rules change; forces every stored daily score to be recomputed.
# Score penalty applied 1..N days after a visit that found nothing (flush not started yet)
NO_FIND_PENALTY = (25, 20, 15, 10, 5)
MODEL_VERSION = 8  # 8: score uses 20-day temperature and 26-day rainfall; removes calendar/lunar bonuses
ARCHIVE_LAG_DAYS = 5
SOURCE_ARCHIVE = "archive"
SOURCE_FORECAST = "forecast"
# Merge policy for the same date (see merge_records): archive > forecast; a newer forecast
# replaces an older forecast; forecast-only (future / not yet archived) dates are preserved.
SOURCE_RANK = {SOURCE_FORECAST: 1, SOURCE_ARCHIVE: 2}

MODE_DEFAULT = "default"
MODE_OUTLOOK = "weekend_outlook"
MODE_FINAL = "final_confirmation"
MODE_LABELS = {
    MODE_DEFAULT: "Daily run",
    MODE_OUTLOOK: "Thursday weekend outlook",
    MODE_FINAL: "Friday final go/no-go",
}

# Friday final-confirmation targeting (config key FRIDAY_POLICY):
#   thursday_alerted_only - only spots that received the Thursday outlook alert (the default / original behaviour)
#   all_above_threshold   - every spot whose best weekend score is at or above ALERT_THRESHOLD
FRIDAY_POLICY_THURSDAY_ONLY = "thursday_alerted_only"
FRIDAY_POLICY_ALL_ABOVE = "all_above_threshold"
FRIDAY_POLICIES = (FRIDAY_POLICY_THURSDAY_ONLY, FRIDAY_POLICY_ALL_ABOVE)

MIN_ALERT_GAP_DAYS = 5
AFFINITY_LOOKBACK_DAYS = 730  # "proven spot affinity" only counts harvests from the last 2 years
RUNOFF_MIN_DAY_MM = 15.0
FLUSH_RAIN_WINDOW_DAYS = 26
FLUSH_TEMPERATURE_WINDOW_DAYS = 20
FLUSH_RAIN_SCORE_MAX = 30
FLUSH_TEMPERATURE_SCORE_MAX = 20
VERDICT_LOW_TIERS = (
    (25, "🚫 Not worth it"),
    (45, "😐 Unlikely"),
    (60, "🤔 Long shot"),
)
VERDICT_WORTH_LOOK = "👀 Worth a look"
VERDICT_GO = "👍 Good chance – go"
VERDICT_DEFINITE_GO = "🔥 Definitely go for it"
VERDICT_SPECIAL_TAGS = {
    "terminated": "❄️ Season over",
    "off_season": "🍂 Off season",
    "delayed": "🔥 Too warm – wait",
    "dry": "💧 Too dry – wait",
}
SCORE_COLOR_MIN = 40
SCORE_COLOR_MAX = 70

DAILY_FIELDS = [
    "precipitation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "wind_speed_10m_max",
    "soil_temperature_0_to_7cm_mean",
    "relative_humidity_2m_mean",
    "soil_moisture_0_to_7cm_mean",
]
# precip_peak_2h_mm is derived from the hourly precipitation series (busiest 2 consecutive hours of the day).
RECORD_FIELDS = DAILY_FIELDS + ["precip_peak_2h_mm"]

# precip_peak_basis says how precip_peak_2h_mm was obtained, so approximated vs observed data is explicit:
#   "hourly"          - fully observed: derived from the hourly series fetched together with the daily record
#   "hourly_backfill" - derived later from a separate hourly archive request (same data, filled in afterwards)
#   "unavailable"     - the hourly request succeeded but had no data for this day; no runoff penalty can apply
#   (missing)         - not yet backfilled (older records, or a backfill request failed); retried on the next run
# Nothing is ever estimated from daily totals: a missing peak simply means "no runoff penalty".
PEAK_BASIS_HOURLY = "hourly"
PEAK_BASIS_BACKFILL = "hourly_backfill"
PEAK_BASIS_UNAVAILABLE = "unavailable"
BACKFILL_CHUNK_DAYS = 365
BACKFILL_MAX_CHUNKS_PER_RUN = 4  # bounds API calls per run; the rest is picked up by the next run (TODO: one-time manual run if needed)


def peak_precip_by_day(hourly: Dict[str, Any]) -> Dict[str, float]:
    """Return {date: precipitation (mm) in the busiest 2 consecutive hours} from an hourly payload."""
    by_day: Dict[str, List[float]] = {}
    for stamp, value in zip(hourly.get("time", []) or [], hourly.get("precipitation", []) or []):
        by_day.setdefault(str(stamp)[:10], []).append(float(value or 0))
    peaks: Dict[str, float] = {}
    for day, hours in by_day.items():
        if len(hours) < 2:
            peaks[day] = round(sum(hours), 2)
        else:
            peaks[day] = round(max(hours[i] + hours[i + 1] for i in range(len(hours) - 1)), 2)
    return peaks


def parse_open_meteo_payload(payload: Optional[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    if not payload or "daily" not in payload:
        return []
    daily = payload["daily"]
    peaks = peak_precip_by_day(payload.get("hourly") or {})
    records: List[Dict[str, Any]] = []
    for idx, date_str in enumerate(daily.get("time", [])):
        item: Dict[str, Any] = {"date": date_str, "source": source}
        for field in DAILY_FIELDS:
            values = daily.get(field) or []
            item[field] = values[idx] if idx < len(values) else None
        item["precip_peak_2h_mm"] = peaks.get(date_str)
        if item["precip_peak_2h_mm"] is not None:
            item["precip_peak_basis"] = PEAK_BASIS_HOURLY
        records.append(item)
    return records


def fetch_archive_day_range(latitude: float, longitude: float, elevation: int, start: date, end: date) -> List[Dict[str, Any]]:
    payload = fetch_json(
        ARCHIVE_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "start_date": iso_date(start),
            "end_date": iso_date(end),
            "daily": ",".join(DAILY_FIELDS),
            "hourly": "precipitation",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    return parse_open_meteo_payload(payload, SOURCE_ARCHIVE)


def fetch_archive_hourly_precip(latitude: float, longitude: float, elevation: int, start: date, end: date) -> Optional[Dict[str, Any]]:
    """Hourly precipitation only (used for the runoff backfill). Returns None on any API failure."""
    return fetch_json(
        ARCHIVE_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "start_date": iso_date(start),
            "end_date": iso_date(end),
            "hourly": "precipitation",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )


def needs_runoff_backfill(rec: Dict[str, Any]) -> bool:
    return rec.get("source") == SOURCE_ARCHIVE and rec.get("precip_peak_2h_mm") is None and rec.get("precip_peak_basis") is None


def backfill_runoff_data(records: List[Dict[str, Any]], latitude: float, longitude: float, elevation: int,
                         fetch_hourly: Any = None, max_chunks: int = BACKFILL_MAX_CHUNKS_PER_RUN) -> List[str]:
    """Fill precip_peak_2h_mm on older archive records (in place) from hourly precipitation.

    Fails soft: a failed/empty chunk leaves its records untouched (they are retried next run) and never
    raises. Returns the dates that were changed so their scores can be recomputed.
    """
    fetch_hourly = fetch_hourly or fetch_archive_hourly_precip
    pending = sorted((r for r in records if needs_runoff_backfill(r)), key=lambda r: r["date"])
    changed: List[str] = []
    chunks = 0
    while pending and chunks < max_chunks:
        start = parse_date(pending[0]["date"])
        end = start + timedelta(days=BACKFILL_CHUNK_DAYS - 1)
        chunk = [r for r in pending if parse_date(r["date"]) <= end]
        pending = pending[len(chunk):]
        chunks += 1
        try:
            payload = fetch_hourly(latitude, longitude, elevation, start, parse_date(chunk[-1]["date"]))
        except Exception as exc:  # partial API failure must never break the run
            print(f"[WARN] Runoff backfill chunk {iso_date(start)} failed: {exc}")
            continue
        hourly = payload.get("hourly") if isinstance(payload, dict) else None
        if not hourly or not hourly.get("time"):
            print(f"[WARN] Runoff backfill chunk starting {iso_date(start)} returned no hourly data; will retry next run")
            continue
        peaks = peak_precip_by_day(hourly)
        for rec in chunk:
            peak = peaks.get(rec["date"])
            rec["precip_peak_2h_mm"] = peak
            rec["precip_peak_basis"] = PEAK_BASIS_BACKFILL if peak is not None else PEAK_BASIS_UNAVAILABLE
            changed.append(rec["date"])
    if pending:
        print(f"[INFO] {len(pending)} records still await runoff backfill (continues on the next run)")
    return changed


def fetch_forecast_day_range(latitude: float, longitude: float, elevation: int) -> List[Dict[str, Any]]:
    payload = fetch_json(
        FORECAST_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "past_days": 7,
            "forecast_days": 7,
            "daily": ",".join(DAILY_FIELDS),
            "hourly": "precipitation",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    return parse_open_meteo_payload(payload, SOURCE_FORECAST)


def load_json_safe(path: Path, default: Any) -> Any:
    """Load JSON; if the file is unreadable/corrupt, move it aside (never overwrite it) and return default."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.utcnow().strftime('%Y%m%dT%H%M%S')}")
        print(f"[WARN] {path} is unreadable ({exc}); moving it to {backup} and starting fresh")
        try:
            path.replace(backup)
        except OSError as move_exc:
            print(f"[WARN] Could not back up corrupt file: {move_exc}")
        return default


def load_or_init_db() -> Dict[str, Any]:
    db = load_json_safe(DB_PATH, {})
    if not isinstance(db, dict):
        db = {}
    if not isinstance(db.get("locations"), dict):
        db["locations"] = {}
    # Schema history: 1 = {"locations": {name: {daily_records}}}; 2 adds source/precip_peak_2h_mm per
    # record, per-location daily_scores + backtest, and this version marker. v1 data is migrated in
    # normalize_records() (missing "source" is inferred from the archive lag).
    # v3 adds precip_peak_basis per record and moves legacy top-level entries into LEGACY_KEY.
    moved = migrate_legacy_entries(db)
    if moved:
        print(f"[INFO] Quarantined {moved} legacy top-level entries under '{LEGACY_KEY}'")
    db["schema_version"] = SCHEMA_VERSION
    return db


LEGACY_KEY = "legacy_quarantine"
KNOWN_TOP_LEVEL_KEYS = {"locations", "schema_version", "meta", LEGACY_KEY}


def migrate_legacy_entries(db: Dict[str, Any]) -> int:
    """Quarantine pre-schema top-level entries (date-keyed rows and per-spot columnar payloads).

    They are MOVED, not deleted, to db[LEGACY_KEY]: the old rows carry no location or source information, so
    converting them into location records could silently mix up data. Moving keeps the data intact while
    clearing the top level. TODO: a one-time manual review can convert them into per-location records.
    Returns the number of entries moved.
    """
    legacy = db.get(LEGACY_KEY)
    if not isinstance(legacy, dict):
        legacy = {}
    moved = 0
    for key in [k for k in db if k not in KNOWN_TOP_LEVEL_KEYS]:
        legacy[key] = db.pop(key)
        moved += 1
    if legacy:
        db[LEGACY_KEY] = legacy
    return moved


def valid_date_string(value: Any) -> bool:
    try:
        parse_date(value)
        return True
    except (TypeError, ValueError):
        return False


def has_observation(record: Dict[str, Any]) -> bool:
    return record.get("temperature_2m_max") is not None


def merge_records(records: List[Dict[str, Any]], new_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Stitch archive and forecast data into one series, one record per date.

    Policy (explicit, per date):
      * archive observations replace forecast placeholders for the same date;
      * a forecast never replaces an archive record;
      * a newer forecast replaces an older forecast (forecasts get revised);
      * an archive record with no observation (archive lag returns nulls) is ignored;
      * dates covered only by forecast data (recent/future) are preserved.
    """
    by_date: Dict[str, Dict[str, Any]] = {}
    for item in list(records) + list(new_records):
        d = item.get("date")
        if not valid_date_string(d):
            continue
        source = item.get("source", SOURCE_ARCHIVE)
        if source == SOURCE_ARCHIVE and not has_observation(item):
            continue
        current = by_date.get(d)
        if current is None:
            by_date[d] = item
            continue
        cur_rank = SOURCE_RANK.get(current.get("source"), 0)
        new_rank = SOURCE_RANK.get(source, 0)
        if new_rank > cur_rank or (new_rank == cur_rank == SOURCE_RANK[SOURCE_FORECAST]):
            by_date[d] = item
    return [by_date[d] for d in sorted(by_date)]


def normalize_records(records: Any, today: date) -> Tuple[List[Dict[str, Any]], int]:
    """Drop malformed entries, infer legacy sources, de-duplicate. Returns (records, dropped_count)."""
    if not isinstance(records, list):
        return [], 0
    cleaned: List[Dict[str, Any]] = []
    for rec in records:
        if not isinstance(rec, dict) or not valid_date_string(rec.get("date")):
            continue
        rec = dict(rec)
        if "soil_temperature_0_to_7cm_mean" not in rec and "soil_temperature_0_to_10cm" in rec:
            rec["soil_temperature_0_to_7cm_mean"] = rec.pop("soil_temperature_0_to_10cm")
        if rec.get("source") not in SOURCE_RANK:
            legacy_final = parse_date(rec["date"]) <= today - timedelta(days=ARCHIVE_LAG_DAYS)
            rec["source"] = SOURCE_ARCHIVE if legacy_final else SOURCE_FORECAST
        cleaned.append(rec)
    merged = merge_records([], cleaned)
    return merged, len(records) - len(merged)


def check_db_integrity(db: Dict[str, Any], today: Optional[date] = None) -> List[str]:
    """Minimal health check. Returns a list of human-readable problems (empty list = healthy)."""
    today = today or datetime.now(timezone.utc).date()
    issues: List[str] = []
    if not isinstance(db, dict) or not isinstance(db.get("locations"), dict):
        return ["database has no 'locations' mapping"]
    if db.get("schema_version") != SCHEMA_VERSION:
        issues.append(f"schema_version is {db.get('schema_version')!r}, expected {SCHEMA_VERSION}")
    leftover_dates = [k for k in db if valid_date_string(k)]
    if leftover_dates:
        issues.append(f"{len(leftover_dates)} legacy top-level date keys (e.g. {min(leftover_dates)}); run the engine to quarantine them")
    other = [k for k in db if k not in KNOWN_TOP_LEVEL_KEYS and not valid_date_string(k)]
    if other:
        issues.append(f"unexpected top-level keys: {', '.join(map(str, other[:5]))}")
    for name, loc in db["locations"].items():
        records = loc.get("daily_records") if isinstance(loc, dict) else None
        if not isinstance(records, list):
            issues.append(f"{name}: daily_records missing or not a list")
            continue
        dates = [r.get("date") for r in records if isinstance(r, dict)]
        if len(dates) != len(records) or not all(valid_date_string(d) for d in dates):
            issues.append(f"{name}: malformed records or invalid dates")
            continue
        if dates != sorted(dates):
            issues.append(f"{name}: records are not sorted by date")
        if len(set(dates)) != len(dates):
            issues.append(f"{name}: duplicate dates")
        bad_source = [r["date"] for r in records if r.get("source") not in SOURCE_RANK]
        if bad_source:
            issues.append(f"{name}: {len(bad_source)} records without a valid source")
        if dates:
            span = (parse_date(max(dates)) - parse_date(min(dates))).days + 1
            if span != len(set(dates)):
                issues.append(f"{name}: {span - len(set(dates))} missing days between {min(dates)} and {max(dates)}")
        stray = [d for d in (loc.get("daily_scores") or {}) if d not in set(dates)]
        if stray:
            issues.append(f"{name}: {len(stray)} daily_scores without a matching record")
        last_sync = loc.get("last_sync_date")
        if last_sync and valid_date_string(last_sync) and (today - parse_date(last_sync)).days > 3:
            issues.append(f"{name}: last sync {last_sync} is more than 3 days old")
    return issues


def ensure_location_history(location_name: str, db: Dict[str, Any], latitude: float, longitude: float, elevation: int, today: Optional[date] = None) -> List[Dict[str, Any]]:
    today = today or datetime.now(timezone.utc).date()
    loc_data = db["locations"].get(location_name)
    if not isinstance(loc_data, dict):
        loc_data = {}
    records, dropped = normalize_records(loc_data.get("daily_records"), today)
    if dropped:
        print(f"[WARN] {location_name}: dropped/merged {dropped} malformed or duplicate records")

    archive_dates = [parse_date(r["date"]) for r in records if r["source"] == SOURCE_ARCHIVE]
    forecast_dates = [parse_date(r["date"]) for r in records if r["source"] == SOURCE_FORECAST]
    # Start the archive fetch at the first date after the last archived day or at the earliest forecast
    # placeholder, whichever is earlier, so placeholders get replaced once the archive catches up.
    starts = [max(archive_dates) + timedelta(days=1)] if archive_dates else [today - timedelta(days=INITIAL_ARCHIVE_DAYS)]
    starts += [min(forecast_dates)] if forecast_dates else []
    start, end = min(starts), today - timedelta(days=ARCHIVE_LAG_DAYS)
    if start <= end:
        records = merge_records(records, fetch_archive_day_range(latitude, longitude, elevation, start, end))
    records = merge_records(records, fetch_forecast_day_range(latitude, longitude, elevation))
    if not records:
        raise RuntimeError(f"{location_name}: weather API returned no usable records; refusing to save an empty forecast")
    backfilled = backfill_runoff_data(records, latitude, longitude, elevation)
    # Runoff looks at the last 3 days, so each backfilled day can change the next two days' scores too.
    pending = set(loc_data.get("pending_rescore") or [])
    for d in backfilled:
        pending.update(iso_date(parse_date(d) + timedelta(days=i)) for i in range(3))
    loc_data["pending_rescore"] = sorted(pending)

    loc_data.update({
        "latitude": latitude,
        "longitude": longitude,
        "elevation_m": elevation,
        "daily_records": records,
        "last_sync_date": iso_date(today),
        "record_count": len(records),
        "merge_policy": "archive replaces forecast for the same date; forecast-only dates are kept; newer forecast replaces older forecast",
    })
    db["locations"][location_name] = loc_data
    return records


class History:
    """Date-indexed view over a sorted list of daily records."""

    def __init__(self, records: List[Dict[str, Any]]):
        self.records = records
        self.dates = [r["date"] for r in records]

    def between(self, start: date, end: date) -> List[Dict[str, Any]]:
        lo = bisect.bisect_left(self.dates, iso_date(start))
        hi = bisect.bisect_right(self.dates, iso_date(end))
        return self.records[lo:hi]

    def window(self, end: date, days: int) -> List[Dict[str, Any]]:
        """The `days` calendar days ending on (and including) `end`."""
        return self.between(end - timedelta(days=days - 1), end)

    def get(self, day: date) -> Optional[Dict[str, Any]]:
        found = self.between(day, day)
        return found[0] if found else None


def _precip(rec: Optional[Dict[str, Any]]) -> float:
    return float((rec or {}).get("precipitation_sum") or 0)


def observed_rainfall_total(hist: History, day: date, days: int) -> Optional[float]:
    """Return a rolling rain total only when at least 80% of its daily values are present."""
    records = hist.window(day, days)
    values = [r.get("precipitation_sum") for r in records if r.get("precipitation_sum") is not None]
    if len(values) < math.ceil(days * 0.8):
        return None
    return sum(float(value) for value in values)


def mean_air_temperature(hist: History, day: date, days: int) -> Optional[float]:
    """Mean daily air temperature, using (max + min) / 2 when at least 80% of days are complete."""
    records = hist.window(day, days)
    values = [
        (float(r["temperature_2m_max"]) + float(r["temperature_2m_min"])) / 2
        for r in records
        if r.get("temperature_2m_max") is not None and r.get("temperature_2m_min") is not None
    ]
    if len(values) < math.ceil(days * 0.8):
        return None
    return sum(values) / len(values)


def has_runoff(hist: History, day: date) -> bool:
    """Heavy-rain runoff: a day (last 3) with >= 15 mm where >50% fell in its busiest 2 hours.

    Needs hourly data (precip_peak_2h_mm); records without it (not yet backfilled, see backfill_runoff_data) get no penalty.
    """
    for rec in hist.window(day, 3):
        total, peak = _precip(rec), rec.get("precip_peak_2h_mm")
        if peak is not None and total >= RUNOFF_MIN_DAY_MM and float(peak) > 0.5 * total:
            return True
    return False


def calculate_score_for_day(location: Dict[str, Any], daily: Dict[str, Any], historical: Any, past_harvests: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    """Return a 0–100 favourability index, not a calibrated probability."""
    hist = historical if isinstance(historical, History) else History(historical)
    score = 0
    status = "🟡 Monitoring"
    quality = ""

    d = parse_date(daily["date"])
    no_find_today = any(
        h.get("observation_type") == "no_mushrooms" and valid_date_string(h.get("date")) and parse_date(h["date"]) == d
        for h in past_harvests
    )
    if not month_day_window(d):
        return 0, "❌ Outside mushroom season", ""

    rainfall = observed_rainfall_total(hist, d, FLUSH_RAIN_WINDOW_DAYS)
    if rainfall is not None:
        score += round(min(FLUSH_RAIN_SCORE_MAX, rainfall * FLUSH_RAIN_SCORE_MAX / 100))

    average_temperature = mean_air_temperature(hist, d, FLUSH_TEMPERATURE_WINDOW_DAYS)
    if average_temperature is not None:
        score += max(
            0,
            round(FLUSH_TEMPERATURE_SCORE_MAX - abs(average_temperature - 13) * 2),
        )

    if has_runoff(hist, d):
        score -= 5

    soil_moisture = daily.get("soil_moisture_0_to_7cm_mean")
    if soil_moisture is not None:
        if soil_moisture > 0.35:
            score += 5
        elif soil_moisture <= 0.20:
            score -= 10
            status = "TOO DRY - LOW SOIL MOISTURE"

    tree_species = location.get("tree_species", [])
    normalized_species = {str(tree).strip().lower() for tree in tree_species} if isinstance(tree_species, list) else set()
    if any(any(host in tree for host in HOST_TREES) for tree in normalized_species):
        score += 10
    if str(location.get("soil_pH", "")).strip().lower() == "alkaline":
        score -= 5

    for harvest in past_harvests:
        harvest_date = harvest.get("date")
        if not valid_date_string(harvest_date):
            continue
        delta_days = (d - parse_date(harvest_date)).days
        if harvest.get("observation_type") == "no_mushrooms":
            if delta_days == 0:
                score = min(score, 20)
                status = "🔎 No mushrooms found (field observation)"
            elif 1 <= delta_days <= len(NO_FIND_PENALTY):
                score -= NO_FIND_PENALTY[delta_days - 1]
                if status == "🟡 Monitoring":
                    status = "🔎 Recent empty visit lowers odds"
            continue
        if harvest.get("cap_stage") == "buttons_young" and 1 <= delta_days <= 4:
            score += 10
        elif harvest.get("cap_stage") == "old_overripe" and 1 <= delta_days <= 7:
            score -= 10
            status = "🍂 EXHAUSTION / POST-FLUSH COOLING OFF"

    # Prior medium/large finds at this site add a modest affinity signal.
    positive = sum(
        1 for h in past_harvests
        if h.get("yield_tier") in {"medium", "large"} and valid_date_string(h.get("date"))
        and 0 < (d - parse_date(h["date"])).days <= AFFINITY_LOOKBACK_DAYS
    )
    if positive >= 2:
        score += 5

    # Quality / risk uses the 7-day average of daily max temperature.
    temps = [float(r["temperature_2m_max"]) for r in hist.window(d, 7) if r.get("temperature_2m_max") is not None]
    if temps:
        avg_max = sum(temps) / len(temps)
        if avg_max > 18:
            quality = "⚠️ HIGH MAGGOT RISK - Harvest early while small."
        elif 8 <= avg_max <= 15:
            quality = "💎 PRIME QUALITY - Firm, bug-free caps expected."

    if no_find_today:
        score = min(score, 20)
    score = max(0, min(100, score))
    if not status or status == "🟡 Monitoring":
        status = "✅ Viable conditions" if score >= 65 else "⚠️ Watch closely"
    return int(score), status, quality


def location_signature(location: Dict[str, Any]) -> str:
    """Fingerprint of the config fields that influence scores; a change recomputes the stored series."""
    keys = ("tree_species", "soil_pH", "past_harvests")
    blob = json.dumps({k: location.get(k) for k in keys}, sort_keys=True, default=str)
    return f"{MODEL_VERSION}:{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


def update_daily_scores(location: Dict[str, Any], records: List[Dict[str, Any]], loc_store: Dict[str, Any]) -> int:
    """Maintain loc_store['daily_scores'] = {date: {score, status, quality, source}} incrementally.

    Archive-sourced days are final and scored once; forecast-sourced days are rescored each run. Everything
    is rescored when the model version or this location's scoring-relevant config changes.
    Returns the number of days (re)scored.
    """
    signature = location_signature(location)
    scores = loc_store.get("daily_scores")
    if not isinstance(scores, dict) or loc_store.get("scores_signature") != signature:
        scores = {}
    hist = History(records)
    harvests = location.get("past_harvests", [])
    force = set(loc_store.get("pending_rescore") or [])
    updated = 0
    result: Dict[str, Any] = {}
    for rec in records:
        d = rec["date"]
        prev = scores.get(d)
        if d not in force and isinstance(prev, dict) and prev.get("source") == SOURCE_ARCHIVE and rec["source"] == SOURCE_ARCHIVE:
            result[d] = prev
            continue
        score, status, quality = calculate_score_for_day(location, rec, hist, harvests)
        result[d] = {"score": score, "status": status, "quality": quality, "source": rec["source"]}
        updated += 1
    loc_store["daily_scores"] = result
    loc_store["scores_signature"] = signature
    loc_store["pending_rescore"] = []
    return updated


def backtest_accuracy(daily_scores: Dict[str, Any], harvests: List[Dict[str, Any]], threshold: int) -> Dict[str, Any]:
    """Compare stored scores with logged medium/large harvests (hit = score >= threshold on the harvest date).

    Only as good as the harvest log. TODO: calibrate weights from this output and, if a remote
    observation store is ever added, pull its harvests here (no live remote DB integration exists today).
    """
    in_season = [v["score"] for v in daily_scores.values() if not str(v.get("status", "")).startswith("❌")]
    on_harvest = [
        daily_scores[h["date"]]["score"] for h in harvests
        if h.get("yield_tier") in {"medium", "large"} and h.get("date") in daily_scores
    ]
    hits = sum(1 for s in on_harvest if s >= threshold)
    return {
        "threshold": threshold,
        "evaluated_harvests": len(on_harvest),
        "hits": hits,
        "hit_rate": round(hits / len(on_harvest), 3) if on_harvest else None,
        "mean_score_on_harvest_days": round(sum(on_harvest) / len(on_harvest), 1) if on_harvest else None,
        "mean_score_in_season": round(sum(in_season) / len(in_season), 1) if in_season else None,
    }


def find_weekend_best(location: Dict[str, Any], records: List[Dict[str, Any]], harvests: List[Dict[str, Any]], scores: Optional[Dict[str, Any]] = None, today: Optional[date] = None) -> Tuple[int, str, str, str]:
    """Best Fri/Sat/Sun score within the coming 7 days (today..today+6)."""
    today = today or datetime.now(timezone.utc).date()
    hist = History(records)
    valid_days = []
    for record in records:
        dt = parse_date(record["date"])
        if dt.weekday() in {4, 5, 6} and today <= dt <= today + timedelta(days=6):
            stored = (scores or {}).get(record["date"])
            if stored:
                score, status, flag = stored["score"], stored["status"], stored.get("quality", "")
            else:
                score, status, flag = calculate_score_for_day(location, record, hist, harvests)
            valid_days.append((dt, score, status, flag))
    if not valid_days:
        return 0, "N/A", "", ""
    best_date, score, status, flag = max(valid_days, key=lambda item: item[1])
    return score, format_display_date(best_date), status, flag


def explain_score(location: Dict[str, Any], records: List[Dict[str, Any]], best_day: str, status: str) -> str:
    """Summarize the strongest weather signals behind a scored day without implying certainty."""
    try:
        day = parse_date(best_day)
    except (TypeError, ValueError):
        return "Weather context is unavailable; local habitat and field observations also affect the score."
    hist = History(records)
    daily = hist.get(day)
    if not daily:
        return "Weather context for this day is unavailable; local habitat and field observations also affect the score."

    signals = []
    if "no mushrooms found" in status.lower():
        signals.append("a no-find field observation limits the score")
    elif "outside mushroom season" in status.lower():
        signals.append("this date is outside the model's mushroom season")
    else:
        rainfall = observed_rainfall_total(hist, day, FLUSH_RAIN_WINDOW_DAYS)
        if rainfall is not None:
            signals.append(f"{rainfall:.0f} mm of rain over the preceding {FLUSH_RAIN_WINDOW_DAYS} days")

        average_temperature = mean_air_temperature(hist, day, FLUSH_TEMPERATURE_WINDOW_DAYS)
        if average_temperature is not None:
            signals.append(
                f"mean air temperature of {average_temperature:.1f}°C over the preceding {FLUSH_TEMPERATURE_WINDOW_DAYS} days"
            )

        soil_moisture = daily.get("soil_moisture_0_to_7cm_mean")
        if soil_moisture is not None and soil_moisture <= 0.20:
            signals.append(f"measured soil moisture is low at {float(soil_moisture):.3f} m³/m³")
        elif soil_moisture is not None and soil_moisture > 0.35:
            signals.append("measured soil moisture is high")
        if has_runoff(hist, day):
            signals.append("concentrated heavy rain may have caused runoff")

    if not signals:
        signals.append("no standout recent weather signal was found")
    return (
        "Key signals: " + "; ".join(signals)
        + ". This heuristic favourability index is not a measured chance; it also reflects local habitat and logged observations."
    )


def detect_run_mode(now: Optional[datetime] = None) -> str:
    """UTC schedule awareness. Windows tolerate GitHub's cron start delays.

    Thursday 17:00-20:59 UTC -> weekend outlook (cron 18:00); Friday 05:00-07:59 UTC -> final confirmation
    (cron 06:00). The regular 08:00 daily run and manual runs outside those windows use the default mode.
    """
    now = now or datetime.now(timezone.utc)
    if now.weekday() == 3 and 17 <= now.hour <= 20:
        return MODE_OUTLOOK
    if now.weekday() == 4 and 5 <= now.hour <= 7:
        return MODE_FINAL
    return MODE_DEFAULT


def build_alert_state() -> Dict[str, Any]:
    state = load_json_safe(ALERT_STATE_PATH, {"locations": {}})
    if not isinstance(state, dict) or not isinstance(state.get("locations"), dict):
        state = {"locations": {}}
    return state


def status_category(status: Optional[str]) -> str:
    """Collapse status text into coarse buckets; only a bucket change is a 'significant' status change."""
    s = (status or "").lower()
    if "frost" in s or "terminated" in s:
        return "terminated"
    if "delayed" in s:
        return "delayed"
    if "exhaustion" in s:
        return "exhausted"
    if "too dry" in s:
        return "dry"
    if "outside" in s:
        return "off_season"
    if "viable" in s:
        return "viable"
    return "watch"


def score_verdict_tag(score: int, status: Optional[str], threshold: int = 65) -> str:
    status_text = (status or "").lower()
    if "no mushrooms found" in status_text:
        return "🔎 No mushrooms found (field observation)"
    category = status_category(status)
    if category in VERDICT_SPECIAL_TAGS:
        return VERDICT_SPECIAL_TAGS[category]
    for upper_bound, label in VERDICT_LOW_TIERS:
        if score < upper_bound:
            return label
    if score >= max(75, threshold):
        return VERDICT_DEFINITE_GO
    if score >= threshold:
        return VERDICT_GO
    return VERDICT_WORTH_LOOK


def score_color(score: int) -> str:
    hue = round(max(0, min(1, (score - SCORE_COLOR_MIN) / (SCORE_COLOR_MAX - SCORE_COLOR_MIN))) * 120)
    return f"hsl({hue} 80% 58%)"


def should_alert_for_location(location_name: str, current_score: int, status: str, threshold: int, state: Dict[str, Any], today: Optional[date] = None) -> bool:
    """All three conditions must hold: threshold crossed upward, significant status change, >=5 days since last alert.

    last_score / last_status are the previous run's values (updated every run, see main()).
    """
    loc_state = state["locations"].get(location_name, {})
    last_score = loc_state.get("last_score")
    last_score = -1 if last_score is None else last_score
    last_date = loc_state.get("last_alert_date")
    today = today or datetime.now(timezone.utc).date()
    days_since = (today - parse_date(last_date)).days if last_date and valid_date_string(last_date) else 999
    crossed = last_score < threshold <= current_score
    status_changed = status_category(loc_state.get("last_status")) != status_category(status)
    return crossed and status_changed and days_since >= MIN_ALERT_GAP_DAYS


def resolve_friday_policy(cfg: Dict[str, Any]) -> str:
    policy = cfg.get("FRIDAY_POLICY", FRIDAY_POLICY_THURSDAY_ONLY)
    if policy not in FRIDAY_POLICIES:
        print(f"[WARN] Unknown FRIDAY_POLICY {policy!r}; using {FRIDAY_POLICY_THURSDAY_ONLY!r}")
        return FRIDAY_POLICY_THURSDAY_ONLY
    return policy


def should_confirm_for_location(location_name: str, state: Dict[str, Any], today: Optional[date] = None,
                                policy: str = FRIDAY_POLICY_THURSDAY_ONLY, best_score: Optional[int] = None,
                                threshold: int = 65) -> bool:
    """Friday final confirmation, at most once per day per spot. Who is eligible depends on `policy`:

    thursday_alerted_only: only spots that got a Thursday outlook alert within the last 2 days (default).
    all_above_threshold:   every spot whose best weekend score is >= threshold.
    """
    loc_state = state["locations"].get(location_name, {})
    today = today or datetime.now(timezone.utc).date()
    if loc_state.get("last_confirmation_date") == iso_date(today):
        return False
    if policy == FRIDAY_POLICY_ALL_ABOVE:
        return best_score is not None and best_score >= threshold
    last_date = loc_state.get("last_alert_date")
    if loc_state.get("last_alert_mode") != MODE_OUTLOOK or not last_date or not valid_date_string(last_date):
        return False
    return 0 <= (today - parse_date(last_date)).days <= 2


def send_telegram(token: str, chat_id: str, message: str) -> bool:
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {"chat_id": chat_id, "text": message, "disable_web_page_preview": True}
    try:
        response = requests.post(url, data=payload, timeout=20)
        response.raise_for_status()
        result = response.json()
        if not isinstance(result, dict) or result.get("ok") is not True:
            description = result.get("description", "invalid API response") if isinstance(result, dict) else "invalid API response"
            print(f"[WARN] Telegram alert failed: {description}")
            return False
        return True
    except Exception as exc:
        print(f"[WARN] Telegram alert failed: {str(exc).replace(token, '***')}")
        return False


def send_pushover(api_token: str, user_key: str, message: str) -> bool:
    if not api_token or not user_key:
        return False
    payload = {"token": api_token, "user": user_key, "message": message, "title": "Porcini Tracker"}
    try:
        response = requests.post("https://api.pushover.net/1/messages.json", data=payload, timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[WARN] Pushover alert failed: {exc}")
        return False


def send_twilio(account_sid: str, auth_token: str, from_number: str, to_number: str, message: str) -> bool:
    if not (account_sid and auth_token and from_number and to_number):
        return False
    payload = {"From": from_number, "To": to_number, "Body": message}
    url = f"https://api.twilio.com/2010-04-01/Accounts/{account_sid}/Messages.json"
    try:
        response = requests.post(url, data=payload, auth=(account_sid, auth_token), timeout=20)
        response.raise_for_status()
        return True
    except Exception as exc:
        print(f"[WARN] Twilio alert failed: {exc}")
        return False


def dispatch_notification(cfg: Dict[str, Any], message: str) -> bool:
    settings = cfg.get("NOTIFICATION_SETTINGS", {})
    provider = str(settings.get("provider") or "").lower()
    if provider == "telegram":
        return send_telegram(settings.get("token", ""), settings.get("chat_id_or_recipient", ""), message)
    elif provider == "pushover":
        return send_pushover(settings.get("api_token", ""), settings.get("user_key", ""), message)
    elif provider == "twilio":
        return send_twilio(settings.get("account_sid", ""), settings.get("auth_token", ""), settings.get("from_number", ""), settings.get("to_number", ""), message)
    else:
        print("[WARN] No active notification provider configured")
        return False



def build_alert_message(results: List[Tuple[str, int, str, str]], dashboard_url: str, mode: str = MODE_DEFAULT, threshold: int = 65) -> str:
    ranking = sorted(results, key=lambda item: item[1], reverse=True)[:3]
    if mode == MODE_OUTLOOK:
        lines = ["🍄 Weekend Porcini Outlook (Thursday, Ranked):"]
    elif mode == MODE_FINAL:
        lines = ["🍄 Final Go/No-Go Confirmation (Friday):"]
    else:
        lines = ["🍄 Weekend Porcini Forecast (Ranked):"]
    for index, result in enumerate(ranking, 1):
        name, score, best_day, status = result[:4]
        verdict = f"{'GO' if score >= threshold else 'NO-GO'} - " if mode == MODE_FINAL else ""
        tag = score_verdict_tag(score, status, threshold)
        lines.append(f"{index}. {name} - {verdict}{score}% – {tag} ({best_day}) | {status}")
        if len(result) > 4 and result[4]:
            lines.append(f"   Why: {result[4]}")
    lines.append("")
    lines.append(f"🌐 Dashboard: {dashboard_url}")
    return "\n".join(lines)


DASHBOARD_TEMPLATE = r"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>Porcini Tracker Dashboard</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #0b1220; --surface: #151f30; --surface-raised: #1c293b; --border: #34445a;
      --text: #edf2f7; --muted: #b8c4d4; --accent: #a7f3d0; --focus: #fbbf24;
      --space-1: .35rem; --space-2: .65rem; --space-3: 1rem; --space-4: 1.4rem;
      --radius: 12px; --small: .82rem; --body: 1rem; --heading: 1.35rem;
    }
    * { box-sizing: border-box; }
    body { font: var(--body)/1.5 system-ui, sans-serif; background: var(--bg); color: var(--text); margin: 0; padding: clamp(1rem, 3vw, 2rem); }
    .wrap { max-width: 1100px; margin: 0 auto; }
    h1 { color: var(--accent); font-size: clamp(1.7rem, 4vw, 2.2rem); line-height: 1.2; margin: 0 0 var(--space-1); }
    h2 { color: var(--text); font-size: var(--heading); line-height: 1.3; margin: var(--space-4) 0 var(--space-2); }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(230px, 1fr)); gap: var(--space-3); }
    .card { min-width: 0; background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius); padding: clamp(.9rem, 2vw, 1.2rem); margin-bottom: var(--space-3); }
    .score-line { display: flex; flex-wrap: wrap; align-items: center; gap: .65rem; }
    .score { font-size: clamp(2.3rem, 6vw, 3rem); font-weight: 800; line-height: 1; }
    .verdict-tag { display: inline-block; background: var(--surface-raised); border: 1px solid var(--border); border-radius: 999px; padding: .25rem .65rem; font-size: var(--small); font-weight: 700; }
    .name { font-size: 1.12rem; margin-top: var(--space-2); font-weight: 700; }
    .meta { color: var(--muted); margin-top: var(--space-2); font-size: var(--small); }
    .best-day strong { color: var(--text); margin-left: .3rem; }
    .badge { display: inline-block; background: #065f46; border-radius: 999px; padding: 2px 10px; font-size: var(--small); }
    .go { background: #065f46; } .nogo { background: #7f1d1d; }
    .alert { background: var(--bg); border: 1px solid var(--border); border-radius: 8px; padding: 12px; white-space: pre-wrap; }
    .note { color: #fcd34d; font-size: var(--small); }
    .toolbar { display: flex; flex-wrap: wrap; align-items: end; gap: var(--space-2); margin: 0 0 var(--space-2); }
    .toolbar label, .field { display: grid; gap: 3px; color: var(--muted); font-size: var(--small); }
    .season-toggle[aria-pressed="true"] { background: var(--accent, #2e7d32); color: #fff; font-weight: 700; }
    .toolbar strong { align-self: center; }
    #chartbox { position: relative; }
    #chart { display: block; width: 100%; height: auto; background: var(--bg); border-radius: 8px; }
    #tip { position: absolute; display: none; pointer-events: none; background: #030712; border: 1px solid #64748b; border-radius: 8px; padding: 8px 10px; font-size: var(--small); max-width: min(290px, 80vw); z-index: 5; }
    .info-button { border-radius: 50%; color: var(--accent); font-size: 1rem; line-height: 1; padding: 4px 7px; }
    dialog { width: min(34rem, calc(100% - 2rem)); max-height: 80vh; overflow: auto; background: var(--surface); color: var(--text); border: 1px solid #718096; border-radius: var(--radius); padding: 1.25rem; }
    dialog::backdrop { background: rgb(3 7 18 / 75%); }
    #timeline-scroll { overflow-x: auto; overscroll-behavior-x: contain; padding: 8px 2px 12px; }
    #timeline { display: flex; gap: 3px; min-width: max-content; align-items: stretch; }
    .timeline-day { position: relative; display: flex; flex-direction: column; align-items: center; justify-content: flex-end; width: 42px; min-height: 80px; padding: 14px 3px 5px; margin: 0; border-radius: 7px; }
    .timeline-day.weekend { background: #273449; }
    .timeline-day.forecast { border-style: dashed; }
    .timeline-day.selected { outline: 2px solid var(--accent); outline-offset: 1px; }
    .timeline-day.today::after { content: "Today"; position: absolute; top: 0; color: var(--focus); font-size: .65rem; }
    .timeline-day .month { position: absolute; top: 0; font-size: .65rem; color: var(--accent); }
    .timeline-day .tick { height: 7px; border-left: 1px solid #9ca3af; }
    .timeline-day .day-number { font-weight: bold; }
    .timeline-day .observation { min-height: 1.2em; font-size: .9rem; }
    .timeline-day .heat { width: 100%; height: 4px; background: var(--border); border-radius: 3px; overflow: hidden; }
    .timeline-day .heat span { display: block; height: 100%; }
    .legend { display: flex; flex-wrap: wrap; gap: var(--space-1); margin: var(--space-2) 0; }
    .legend-chip { display: inline-flex; align-items: center; gap: .35rem; background: var(--surface-raised); border: 1px solid var(--border); border-radius: 999px; padding: .25rem .6rem; color: var(--muted); font-size: var(--small); }
    .legend-chip input { accent-color: var(--accent); margin: 0; }
    .line-key { display: inline-block; width: 1rem; border-top: 2px solid; }
    .line-score { border-color: #34d399; } .line-rain { border-color: #60a5fa; } .line-temp { border-color: #fb923c; }
    .table-wrap { overflow-x: auto; border-radius: 8px; }
    table { width: 100%; border-collapse: collapse; font-size: var(--small); }
    th, td { text-align: left; padding: .55rem .6rem; border-bottom: 1px solid var(--border); white-space: nowrap; }
    th { color: var(--accent); background: var(--surface-raised); font-weight: 700; }
    tbody tr:nth-child(even) { background: rgb(255 255 255 / 2%); }
    .weather-table th:nth-child(n+2), .weather-table td:nth-child(n+2), .observation-table th:nth-child(n+2), .observation-table td:nth-child(n+2) { text-align: right; }
    tr.row { cursor: pointer; } tr.row:hover { background: var(--surface-raised); }
    input, select, button { background: var(--bg); color: var(--text); border: 1px solid #607086; border-radius: 7px; padding: .55rem .7rem; font: inherit; }
    button { cursor: pointer; }
    button:hover { background: var(--surface-raised); border-color: #94a3b8; }
    :focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }
    .pin { cursor: pointer; }
    .log-summary { display: flex; flex-wrap: wrap; gap: var(--space-2); margin: 0 0 var(--space-2); }
    .log-summary .chip { background: var(--surface-raised); border: 1px solid var(--border); border-radius: var(--radius); padding: .4rem .7rem; font-size: var(--small); color: var(--muted); }
    .log-summary .chip strong { color: var(--text); display: block; }
    .log-table td.notes { white-space: normal; min-width: 12rem; max-width: 26rem; }
    .log-table td.num, .log-table th.num { text-align: right; }
    .type-found { color: var(--accent); } .type-none { color: #fca5a5; }
    #logform { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: var(--space-2); align-items: end; }
    #harvest-fields { display: contents; }
    #harvest-fields[hidden] { display: none; }
    #logform button { justify-self: start; }
    details { margin-top: var(--space-2); }
    summary { color: var(--accent); cursor: pointer; }
    @media (max-width: 600px) {
      #logform { grid-template-columns: 1fr; }
      #harvest-fields { display: contents; }
      .toolbar > * { flex: 1 1 auto; }
      .toolbar input, .toolbar select { width: 100%; }
    }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🍄 Porcini Tracker Dashboard</h1>
    <div class="meta"><span class="badge">__MODE_LABEL__</span> Generated __GENERATED__ UTC</div>
    <div class="grid">__CARDS__</div>__ALERT__
    <h2>🌦 Rain &amp; Temperature Overview <button class="info-button" type="button" data-info="weather" aria-label="How to read the weather factors" aria-haspopup="dialog" aria-controls="info-dialog">ⓘ</button></h2>
    <div class="card">
      <div class="toolbar"><label for="loc">Location<select id="loc"></select></label>
        <button type="button" id="prev-loc">◀ Previous Location</button>
        <button type="button" id="next-loc">Next Location ▶</button>
        <strong id="loc-indicator" aria-live="polite"></strong></div>
      <div id="weather-label" class="meta"></div>
      <div class="table-wrap"><table class="weather-table"><thead><tr><th>Date</th><th>High °C</th><th>Low °C</th><th>Rain mm</th></tr></thead><tbody id="weather-rows"></tbody></table></div>
    </div>
    <h2>📈 Daily Score History &amp; Field Observations <button class="info-button" type="button" data-info="score" aria-label="How the forecast score is calculated" aria-haspopup="dialog" aria-controls="info-dialog">ⓘ</button></h2>
    <div class="card">
      <div class="toolbar"><label for="range">Date range<select id="range"></select></label><button type="button" id="seasonOnly" class="season-toggle" aria-pressed="false" title="Show only Aug 15 – Dec 1">Season Only</button></div>
      <div id="chartbox"><svg id="chart" viewBox="0 0 900 320" role="img" aria-label="Daily favourability score with optional rain and temperature trends"></svg><div id="tip"></div></div>
      <div class="meta">Hover or tap for daily details. Dashed line marks the alert threshold.</div>
      <div class="legend" aria-label="Chart and timeline legend">
        <span class="legend-chip"><span class="line-key line-score"></span>Score %</span>
        <label class="legend-chip"><input id="rain-toggle" type="checkbox" checked> <span class="line-key line-rain"></span>Rain</label>
        <label class="legend-chip"><input id="temp-toggle" type="checkbox" checked> <span class="line-key line-temp"></span>Temperature</label>
        <span class="legend-chip">🍄 Found</span><span class="legend-chip">❌ No find</span>
        <span class="legend-chip">Solid: recorded · Dashed: forecast</span>
      </div>
      <div class="meta">Choose a date or use previous/next; arrow keys also move through days.</div>
      <div class="toolbar">
        <button id="day-prev" type="button" aria-label="Previous day">&larr; Previous day</button>
        <label for="selected-date">Choose date:</label> <input type="date" id="selected-date" />
        <button id="day-next" type="button" aria-label="Next day">Next day &rarr;</button>
        <strong id="selected-label" aria-live="polite"></strong>
      </div>
      <div id="timeline-scroll" tabindex="0" role="group" aria-label="Daily forecast timeline. Use left and right arrow keys to change day.">
        <div id="timeline"></div>
      </div>
      <div id="selected-day" class="meta" aria-live="polite"></div>
      <div id="pins" class="meta"></div>
      <div id="backtest" class="meta"></div>
    </div>
    <h2>🔎 Observation Browser <button class="info-button" type="button" data-info="observations" aria-label="How field observations affect the score" aria-haspopup="dialog" aria-controls="info-dialog">ⓘ</button></h2>
    <div class="card">
      <div class="toolbar"><label for="q">Date filter<input id="q" placeholder="ISO prefix, e.g. 2025-09" /></label>
      <label for="src">Weather source<select id="src"><option value="">All sources</option><option>archive</option><option>forecast</option></select></label></div>
      <div class="table-wrap"><table class="observation-table"><thead><tr><th>Date</th><th>Score</th><th>Tmax</th><th>Rain mm</th><th>Wind</th><th>Soil °C</th><th>RH %</th><th>Source</th></tr></thead><tbody id="rows"></tbody></table></div>
      <div class="toolbar"><button id="prev">&laquo; Previous</button><button id="next">Next &raquo;</button><span id="pageinfo" class="meta"></span></div>
    </div>
__OBSERVATION_LOG__
    <h2>📝 Log Field Observation</h2>
    <div class="card">
      <form id="logform">
        <label class="field" for="ldate">Observation date<input type="date" id="ldate" required /></label>
        <label class="field" for="lobservation">Visit result<select id="lobservation"><option value="harvest">Found mushrooms</option><option value="no_mushrooms">Visited, found no mushrooms</option></select></label>
        <span id="harvest-fields">
          <label class="field" for="ltier">Harvest size<select id="ltier"><option>small</option><option>medium</option><option>large</option></select></label>
          <label class="field" for="lstage">Mushroom stage<select id="lstage"><option>buttons_young</option><option>prime</option><option>old_overripe</option></select></label>
          <label class="field" for="lweight">Weight (g)<input type="number" id="lweight" min="0" /></label>
        </span>
        <label class="field" for="lnotes">Notes<input id="lnotes" placeholder="Optional notes" /></label>
        <button type="submit">Submit via GitHub issue</button>
        <p id="draft-status" class="note" aria-live="polite">Drafts are saved only in this browser until you submit the GitHub issue and intake succeeds.</p>
        <a id="draft-issue-link" hidden target="_blank" rel="noopener">Open prefilled GitHub issue</a>
      </form>
      <details><summary>How observations are submitted and saved</summary><p class="note">Save a draft, open the prefilled GitHub issue, then click GitHub's Submit new issue button. Opening the issue page does not save the observation. Only issues from the repository owner or collaborators are accepted. The shared log updates after intake succeeds; browser drafts stay local until cleared here. A no-find report caps that day's score at 20.</p></details>
      <div class="toolbar"><button id="export">Export drafts (JSON)</button><button id="clearlogs">Clear drafts</button></div>
      <pre id="exported" class="alert" style="display:none"></pre>
    </div>
  </div>
  <dialog id="info-dialog" aria-labelledby="info-title" aria-describedby="info-copy" aria-modal="true">
    <h2 id="info-title"></h2>
    <div id="info-copy"></div>
    <button id="info-close" type="button">Close</button>
  </dialog>
  <script id="porcini-data" type="application/json">__DATA__</script>
  <script>
  (function () {
    var D = JSON.parse(document.getElementById('porcini-data').textContent);
    var LOG_KEY = 'porcini_logs_v1', PAGE = 15;
    var LABELS = { small: 'Small', medium: 'Medium', large: 'Large', buttons_young: 'Buttons / young', prime: 'Prime', old_overripe: 'Old / overripe' };
    function $(id) { return document.getElementById(id); }
    function loadLogs() { try { return JSON.parse(localStorage.getItem(LOG_KEY) || '[]'); } catch (e) { return []; } }
    function saveLogs(l) { try { localStorage.setItem(LOG_KEY, JSON.stringify(l)); } catch (e) { alert('Could not save: storage unavailable'); } }
    var MONTHS = ['Jan.', 'Feb.', 'Mar.', 'Apr.', 'May', 'Jun.', 'Jul.', 'Aug.', 'Sep.', 'Oct.', 'Nov.', 'Dec.'];
    var WEEKDAYS = ['Sun.', 'Mon.', 'Tue.', 'Wed.', 'Thu.', 'Fri.', 'Sat.'];
    function fmtDate(s) {
      var m = /^(\d{4})-(\d{2})-(\d{2})$/.exec(s || '');
      if (!m) return s;
      var day = new Date(s + 'T00:00:00Z');
      return isNaN(day.getTime()) ? s : WEEKDAYS[day.getUTCDay()] + ' ' + m[3] + ' ' + MONTHS[+m[2] - 1] + ' ' + m[1];
    }
    function localToday() {
      var d = new Date();
      return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0');
    }
    var INFO = {
      weather: ['Weather indicators', [
        'Daily high and low are averaged to form a 20-day mean air temperature signal. A value near 13°C contributes most to the heuristic score; this is based on one regional porcini study, not a universal optimum.',
        'The main rain signal is the total over the preceding 26 days. That window reflects a regional porcini preprint; its point conversion is an uncalibrated index, not a yield prediction.',
        'Measured 0–7 cm soil moisture is a smaller supporting signal. Soil texture and local calibration affect what a given volumetric moisture value means; soil temperature and air humidity do not directly add score points.',
        'Concentrated heavy rain can apply a small runoff penalty. Temperature shocks, assumed slope/canopy rain-retention adjustments, and 120-day rainfall cutoffs are not used.',
        'Weather source “archive” means recorded past weather; “forecast” means weather-model data. Weather inputs guide the score; they do not confirm mushrooms are present.'
      ]],
      score: ['How the forecast score works', [
        'The weather feed supplies 7 forecast days in total (including today) and the previous 7 days of recent weather. That means it can score up to 6 calendar days after today; beyond that, this dashboard has no forward weather forecast. Older observed weather is kept in the archive.',
        'For each scored day, the model uses the preceding 26-day rainfall total and preceding 20-day mean air temperature when enough daily weather values are available. The score also includes measured soil moisture, a modest host-tree/site signal, and field observations.',
        'The score does not vary by weekday or lunar phase. The cited preprint found associations in one central European beech-forest setting; applying it elsewhere needs local validation. No-find observations cap that date at 20 and reduce scores for five following days.',
        'The displayed percentage is a 0–100 favourability score, not a measured chance that mushrooms will be found. For example, 60% means the model rates conditions as fairly favourable; it does not mean a 60-in-100 guarantee. The usual alert threshold is 65.',
        'Verdict guide at the default 65 threshold: below 25 Not worth it; 25–44 Unlikely; 45–59 Long shot; 60–64 Worth a look; 65–74 Good chance – go; 75+ Definitely go for it. The go boundary follows the configured ALERT_THRESHOLD. Tags provide meaning in addition to the red-to-green score colour.',
        'The chart’s light rain and temperature lines add weather context on their own visible-range scales; missing readings leave gaps, and the score line remains the main signal.',
        'Quality notes are separate from the score: they use recent average high temperatures to flag possible maggot risk or prime-quality conditions.',
        'Scores are estimates and become less dependable further into the 7-day weather forecast. Conditions and local growing spots can differ from the weather grid.'
      ]],
      observations: ['How observations affect the forecast', [
        'A visit with no mushrooms is valid evidence, not missing data. On that date it caps the score at 20, even if weather or other bonuses would have made it higher.',
        'A no-find observation also lowers scores on the next five days (the effect fades from day to day). It does not change unrelated dates.',
        'A recent button-stage find modestly raises scores for a few days; an old/overripe find modestly lowers them. Repeated medium or large finds can add a small site-familiarity bonus. These weights are heuristic, not calibrated.',
        '🍄 marks a recorded find and ❌ marks a visit where none were found. Draft observations stay in this browser until submitted and accepted.'
      ]]
    };
    var infoDialog = $('info-dialog');
    function openInfo(key) {
      var info = INFO[key];
      if (!info) return;
      $('info-title').textContent = info[0];
      var body = $('info-copy'); body.textContent = '';
      info[1].forEach(function (text) { var p = document.createElement('p'); p.textContent = text; body.appendChild(p); });
      infoDialog.showModal();
    }
    document.querySelectorAll('[data-info]').forEach(function (button) {
      button.addEventListener('click', function () { openInfo(button.dataset.info); });
    });
    $('info-close').addEventListener('click', function () { infoDialog.close(); });
    infoDialog.addEventListener('click', function (ev) { if (ev.target === infoDialog) infoDialog.close(); });
    var NS = 'http://www.w3.org/2000/svg';
    function el(name, attrs, text) { var e = document.createElementNS(NS, name); for (var k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; return e; }
    var loc = D.locations[0], pinned = [], page = 0, selectedDate = '', activeTimelineDates = [];
    if (!loc) { $('chart').replaceWith(document.createTextNode('No locations configured.')); return; }
    D.locations.forEach(function (l, i) { var o = document.createElement('option'); o.value = i; o.textContent = l.name; $('loc').appendChild(o); });
    function years() { var s = {}; loc.scores.forEach(function (r) { s[r[0].slice(0, 4)] = 1; }); return Object.keys(s).sort(); }
    function fillRange() {
      var cur = $('range').value; $('range').innerHTML = '';
      ['all', '365'].concat(years()).forEach(function (v) { var o = document.createElement('option'); o.value = v; o.textContent = v === 'all' ? 'All years' : v === '365' ? 'Last 365 days' : v; $('range').appendChild(o); });
      if (cur) $('range').value = cur;
    }
    function inSeason(d) { var md = d.slice(5, 10); return md >= '08-15' && md <= '12-01'; }
    function inRange(d) { if ($('seasonOnly').getAttribute('aria-pressed') === 'true' && !inSeason(d)) return false; var r = $('range').value || 'all'; if (r === 'all') return true; if (r === '365') return Date.parse(d) >= Date.now() - 365 * 864e5; return d.slice(0, 4) === r; }
    function harvestsFor() {
      var h = (loc.harvests || []).map(function (x) { return Object.assign({}, x, { origin: x.origin === 'log' ? 'harvest_log.json' : 'config' }); });
      loadLogs().filter(function (x) { return x.location === loc.name; }).forEach(function (x) { h.push(Object.assign({ origin: 'draft (unsynced)' }, x)); });
      return h;
    }
    function recMap() { var m = {}; loc.records.forEach(function (r) { m[r[0]] = r; }); return m; }
    function scoreMap() { var m = {}; loc.scores.forEach(function (r) { m[r[0]] = r; }); return m; }
    function weatherOverview() {
      var body = $('weather-rows'), today = new Date().toISOString().slice(0, 10);
      body.textContent = '';
      var future = loc.records.filter(function (r) { return r[0] >= today; }).slice(0, 7);
      var rows = future.length ? future : loc.records.slice(-7).reverse();
      $('weather-label').textContent = rows.length ? (future.length ? 'Forecast and current day' : 'Forecast unavailable; showing latest weather observations') : 'Weather overview unavailable: no weather records have been loaded.';
      rows.forEach(function (r) {
        var tr = document.createElement('tr');
        [r[0], r[1], r[2], r[3]].forEach(function (v, i) {
          var td = document.createElement('td');
          td.textContent = v == null ? '—' : (i === 0 ? fmtDate(v) : v + (i < 3 ? '°' : ''));
          tr.appendChild(td);
        });
        body.appendChild(tr);
      });
    }
    function describe(date, extra) {
      var rm = recMap()[date], sm = scoreMap()[date], out = [fmtDate(date)];
      if (sm) { out.push('Score: ' + sm[1] + '% – ' + sm[4] + ' — ' + sm[2]); if (sm[3]) out.push(sm[3]); }
      if (rm) {
        var weather = [];
        if (rm[1] != null) weather.push('Tmax ' + rm[1] + '°C');
        if (rm[2] != null) weather.push('Tmin ' + rm[2] + '°C');
        if (rm[3] != null) weather.push('Rain ' + rm[3] + ' mm');
        if (rm[4] != null) weather.push('wind ' + rm[4] + ' km/h');
        if (rm[6] != null) weather.push('RH ' + rm[6] + '%');
        if (weather.length) out.push(weather.join(', ') + ' (' + rm[8] + ')');
      }
      (extra || []).forEach(function (h) { out.push((h.observation_type === 'no_mushrooms' ? '🔎 No mushrooms found' : '🍄 ' + (h.yield_tier || '?') + ' / ' + (h.cap_stage || '?')) + ' (' + h.origin + ')' + (h.weight_g ? ' / ' + h.weight_g + ' g' : '') + (h.notes ? ' - ' + h.notes : '')); });
      return out;
    }
    function setLines(node, lines) { node.textContent = ''; lines.forEach(function (t, i) { var d = document.createElement('div'); d.textContent = t; if (i === 0) d.style.fontWeight = 'bold'; node.appendChild(d); }); }
    function renderPins() {
      var box = $('pins'); box.textContent = '';
      pinned.forEach(function (p, i) { var d = document.createElement('div'); d.textContent = '📌 ' + describe(p.date, p.h).join(' | ') + ' (click to unpin)'; d.style.cursor = 'pointer'; d.onclick = function () { pinned.splice(i, 1); renderPins(); }; box.appendChild(d); });
    }
    function pin(date) { var hs = harvestsFor().filter(function (h) { return h.date === date; }); if (!pinned.some(function (p) { return p.date === date; })) pinned.push({ date: date, h: hs }); renderPins(); }
    function nearestDate(target, dates) {
      if (!dates.length) return '';
      var best = dates[0], distance = Infinity, time = Date.parse(target + 'T00:00:00Z');
      dates.forEach(function (d) { var delta = Math.abs(Date.parse(d + 'T00:00:00Z') - time); if (delta < distance) { best = d; distance = delta; } });
      return best;
    }
    function initialDate() { return nearestDate(new Date().toISOString().slice(0, 10), loc.scores.map(function (r) { return r[0]; })); }
    function renderSelectedDay() {
      var sm = scoreMap()[selectedDate], rm = recMap()[selectedDate], hs = harvestsFor().filter(function (h) { return h.date === selectedDate; });
      $('selected-label').textContent = selectedDate ? 'Selected: ' + fmtDate(selectedDate) : 'No score dates available';
      $('selected-date').value = selectedDate;
      var lines = selectedDate ? [fmtDate(selectedDate)] : [];
      if (sm) lines.push('Model score: ' + sm[1] + '% — ' + sm[4] + ' — ' + sm[2], 'Weather: ' + (rm && rm[8] === 'forecast' ? 'forecast' : 'recorded'));
      else if (selectedDate) lines.push('No score is available for this day.');
      hs.forEach(function (h) { lines.push((h.observation_type === 'no_mushrooms' ? '❌ Visited, no mushrooms found' : '🍄 Mushrooms found') + (h.origin === 'draft (unsynced)' ? ' (unsynced draft)' : '')); });
      setLines($('selected-day'), lines);
    }
    function renderTimeline() {
      var days = loc.scores;
      if (!selectedDate && days.length) selectedDate = initialDate();
      var target = selectedDate || initialDate(), centerDate = nearestDate(target, days.map(function (r) { return r[0]; }));
      var center = days.findIndex(function (r) { return r[0] === centerDate; });
      var start = Math.max(0, center - 17), end = Math.min(days.length, start + 35);
      start = Math.max(0, end - 35);
      activeTimelineDates = days.slice(start, end).map(function (r) { return r[0]; });
      var timeline = $('timeline'), records = recMap(), scores = scoreMap(), visits = harvestsFor();
      timeline.textContent = '';
      activeTimelineDates.forEach(function (d, i) {
        var row = scores[d], weather = records[d], observations = visits.filter(function (h) { return h.date === d; });
        var day = document.createElement('button');
        day.type = 'button'; day.className = 'timeline-day';
        var weekday = new Date(d + 'T00:00:00Z').getUTCDay();
        if (weekday === 0 || weekday === 6) day.classList.add('weekend');
        if (weather && weather[8] === 'forecast') day.classList.add('forecast');
        if (d === selectedDate) day.classList.add('selected');
        if (d === new Date().toISOString().slice(0, 10)) day.classList.add('today');
        day.setAttribute('aria-pressed', d === selectedDate ? 'true' : 'false');
        var previous = activeTimelineDates[i - 1], dateParts = d.split('-');
        if ((dateParts[2] === '01' || !previous || previous.slice(0, 7) !== d.slice(0, 7)) && d !== new Date().toISOString().slice(0, 10)) {
          var month = document.createElement('span'); month.className = 'month'; month.textContent = MONTHS[+dateParts[1] - 1].slice(0, 3); day.appendChild(month);
        }
        var tick = document.createElement('span'); tick.className = 'tick'; tick.setAttribute('aria-hidden', 'true'); day.appendChild(tick);
        var number = document.createElement('span'); number.className = 'day-number'; number.textContent = dateParts[2]; day.appendChild(number);
        var marker = document.createElement('span'); marker.className = 'observation'; marker.textContent = observations.map(function (h) { return h.observation_type === 'no_mushrooms' ? '❌' : '🍄'; }).join(''); day.appendChild(marker);
        var heat = document.createElement('span'); heat.className = 'heat'; heat.setAttribute('aria-hidden', 'true');
        var bar = document.createElement('span'), score = row ? row[1] : 0;
        bar.style.width = Math.max(0, Math.min(100, score)) + '%'; bar.style.background = row ? row[5] : '';
        heat.appendChild(bar); day.appendChild(heat);
        day.title = fmtDate(d) + (row ? ', score ' + score + '%' : '') + (observations.length ? ', ' + observations.map(function (h) { return h.observation_type === 'no_mushrooms' ? 'no mushrooms found' : 'mushrooms found'; }).join(', ') : '');
        day.setAttribute('aria-label', day.title);
        day.addEventListener('click', function () { selectedDate = d; renderTimeline(); var button = Array.prototype.find.call($('timeline').children, function (item) { return item.getAttribute('aria-label').indexOf(fmtDate(d)) === 0; }); if (button) button.focus(); });
        timeline.appendChild(day);
      });
      renderSelectedDay();
    }
    function moveSelectedDay(direction) {
      var all = loc.scores.map(function (r) { return r[0]; }), index = all.indexOf(selectedDate);
      if (index < 0) index = all.indexOf(nearestDate(selectedDate || initialDate(), all));
      var next = all[Math.max(0, Math.min(all.length - 1, index + direction))];
      if (next) { selectedDate = next; renderTimeline(); var button = Array.prototype.find.call($('timeline').children, function (item) { return item.getAttribute('aria-label').indexOf(fmtDate(next)) === 0; }); if (button) button.focus(); }
    }
    function draw() {
      var svg = $('chart'), tip = $('tip'); svg.textContent = '';
      var pts = loc.scores.filter(function (r) { return inRange(r[0]); });
      if (!pts.length) { svg.appendChild(el('text', { x: 20, y: 40, fill: '#cbd5e1' }, 'No data in range')); return; }
      var W = 900, H = 320, L = 36, R = 145, T = 10, B = 24;
      var t0 = Date.parse(pts[0][0]), t1 = Date.parse(pts[pts.length - 1][0]);
      function x(t) { return L + (W - L - R) * ((t - t0) / Math.max(1, t1 - t0)); }
      function y(v) { return T + (H - T - B) * (1 - v / 100); }
      [0, 25, 50, 75, 100].forEach(function (v) { svg.appendChild(el('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: '#374151' })); svg.appendChild(el('text', { x: 4, y: y(v) + 4, fill: '#9ca3af', 'font-size': 11 }, String(v))); });
      svg.appendChild(el('line', { x1: L, x2: W - R, y1: y(D.threshold), y2: y(D.threshold), stroke: '#f59e0b', 'stroke-dasharray': '5 4' }));
      svg.appendChild(el('text', { x: L, y: H - 6, fill: '#9ca3af', 'font-size': 11 }, fmtDate(pts[0][0])));
      svg.appendChild(el('text', { x: W - R, y: H - 6, fill: '#9ca3af', 'font-size': 11, 'text-anchor': 'end' }, fmtDate(pts[pts.length - 1][0])));
      var records = recMap();
      function drawWeatherSeries(index, className, color, enabled) {
        if (!enabled) return null;
        var values = pts.map(function (r) {
          var record = records[r[0]], value = record && record[index];
          return typeof value === 'number' && isFinite(value) ? value : null;
        });
        var visible = values.filter(function (value) { return value !== null; });
        if (!visible.length) return null;
        var low = Math.min.apply(null, visible), high = Math.max.apply(null, visible);
        function weatherY(value) { return T + (H - T - B) * (1 - (high === low ? .5 : (value - low) / (high - low))); }
        var run = [];
        function appendRun() {
          if (run.length > 1) svg.appendChild(el('polyline', { points: run.join(' '), fill: 'none', stroke: color, 'stroke-width': 1.7, opacity: .42, class: 'weather-series ' + className, 'aria-hidden': 'true' }));
          else if (run.length === 1) {
            var xy = run[0].split(',');
            svg.appendChild(el('circle', { cx: xy[0], cy: xy[1], r: 2, fill: color, opacity: .5, class: 'weather-series ' + className, 'aria-hidden': 'true' }));
          }
        }
        values.forEach(function (value, i) {
          if (value === null) { appendRun(); run = []; }
          else run.push(x(Date.parse(pts[i][0])) + ',' + weatherY(value));
        });
        appendRun();
        return { min: low, max: high };
      }
      var rain = drawWeatherSeries(3, 'weather-rain', '#60a5fa', $('rain-toggle').checked);
      var temperature = drawWeatherSeries(1, 'weather-temperature', '#fb923c', $('temp-toggle').checked);
      function rangeText(value) { return Math.round(value * 10) / 10; }
      svg.appendChild(el('text', { x: W - 5, y: T + 12, fill: '#93c5fd', 'font-size': 10, 'text-anchor': 'end' }, rain ? 'rain max ' + rangeText(rain.max) + ' mm' : 'rain unavailable'));
      svg.appendChild(el('text', { x: W - 5, y: T + 26, fill: '#fdba74', 'font-size': 10, 'text-anchor': 'end' }, temperature ? 'temp ' + rangeText(temperature.min) + '–' + rangeText(temperature.max) + ' °C' : 'temp unavailable'));
      svg.appendChild(el('polyline', { points: pts.map(function (r) { return x(Date.parse(r[0])) + ',' + y(r[1]); }).join(' '), fill: 'none', stroke: '#34d399', 'stroke-width': 2.8, class: 'score-series' }));
      pts.forEach(function (r) { svg.appendChild(el('circle', { cx: x(Date.parse(r[0])), cy: y(r[1]), r: 2.2, fill: r[5], class: 'score-point', 'aria-hidden': 'true' })); });
      var sm = scoreMap(), cursor = el('line', { y1: T, y2: H - B, stroke: '#6b7280', visibility: 'hidden' });
      svg.appendChild(cursor);
      var markerOffsets = {};
      harvestsFor().forEach(function (h) {
        var t = Date.parse(h.date); if (isNaN(t) || t < t0 || t > t1) return;
        var offset = markerOffsets[h.date] || 0; markerOffsets[h.date] = offset + 18;
        var found = h.observation_type !== 'no_mushrooms';
        var c = el('text', { x: x(t), y: y(sm[h.date] ? sm[h.date][1] : 0) - offset, 'text-anchor': 'middle', 'font-size': 17, class: 'pin', tabindex: '0', role: 'button' }, found ? '🍄' : '❌');
        var markerLabel = fmtDate(h.date) + (found ? ': mushrooms found' : ': visited, no mushrooms found');
        c.setAttribute('aria-label', markerLabel); c.appendChild(el('title', {}, markerLabel));
        c.addEventListener('click', function (ev) { ev.stopPropagation(); pin(h.date); selectedDate = h.date; renderTimeline(); setLines(tip, describe(h.date, harvestsFor().filter(function (item) { return item.date === h.date; }))); tip.style.display = 'block'; });
        c.addEventListener('keydown', function (ev) { if (ev.key === 'Enter' || ev.key === ' ') { ev.preventDefault(); pin(h.date); } });
        svg.appendChild(c);
      });
      svg.onmousemove = function (ev) {
        var box = svg.getBoundingClientRect(), px = (ev.clientX - box.left) * (W / box.width), t = t0 + (px - L) / (W - L - R) * (t1 - t0), best = pts[0], bd = Infinity;
        pts.forEach(function (r) { var dd = Math.abs(Date.parse(r[0]) - t); if (dd < bd) { bd = dd; best = r; } });
        var hs = harvestsFor().filter(function (h) { return h.date === best[0]; });
        setLines(tip, describe(best[0], hs)); tip.dataset.date = best[0]; tip.style.display = 'block';
        tip.style.left = Math.min(ev.clientX - box.left + 12, box.width - 290) + 'px'; tip.style.top = (ev.clientY - box.top + 12) + 'px';
        cursor.setAttribute('x1', x(Date.parse(best[0]))); cursor.setAttribute('x2', x(Date.parse(best[0]))); cursor.setAttribute('visibility', 'visible');
      };
      svg.onmouseleave = function () { tip.style.display = 'none'; cursor.setAttribute('visibility', 'hidden'); };
      svg.onclick = function (ev) {
        if (ev.target.classList && ev.target.classList.contains('pin')) return;
        if (tip.dataset.date) { pin(tip.dataset.date); selectedDate = tip.dataset.date; renderTimeline(); }
      };
    }
    function rows() {
      var q = $('q').value.trim(), s = $('src').value, sm = scoreMap();
      var list = loc.records.filter(function (r) { return inRange(r[0]) && (!q || r[0].indexOf(q) === 0) && (!s || r[8] === s); }).reverse();
      var pages = Math.max(1, Math.ceil(list.length / PAGE)); page = Math.min(page, pages - 1);
      var body = $('rows'); body.textContent = '';
      list.slice(page * PAGE, page * PAGE + PAGE).forEach(function (r) {
        var tr = document.createElement('tr'); tr.className = 'row';
        [fmtDate(r[0]), sm[r[0]] ? sm[r[0]][1] : '', r[1], r[3], r[4], r[5], r[6], r[8]].forEach(function (v) { var td = document.createElement('td'); td.textContent = v == null ? '' : v; tr.appendChild(td); });
        tr.onclick = function () { pin(r[0]); };
        body.appendChild(tr);
      });
      $('pageinfo').textContent = 'Page ' + (page + 1) + ' / ' + pages + ' (' + list.length + ' days)';
    }
    function backtest() {
      var b = loc.backtest || {};
      $('backtest').textContent = b.evaluated_harvests ? 'Backtest: ' + b.hits + '/' + b.evaluated_harvests + ' medium/large harvests fell on days scoring >= ' + b.threshold + ' (mean score on harvest days ' + b.mean_score_on_harvest_days + ' vs in-season mean ' + b.mean_score_in_season + '). Small sample; indicative only.' : 'Backtest: no medium/large harvests with a stored score yet.';
    }
    function refresh() { fillRange(); draw(); rows(); backtest(); weatherOverview(); renderTimeline(); }
    function showLocation(index) {
      var n = D.locations.length, i = ((index % n) + n) % n;
      loc = D.locations[i]; $('loc').value = i; pinned = []; page = 0; selectedDate = initialDate();
      $('loc-indicator').textContent = loc.name + ' (Location ' + (i + 1) + ' of ' + n + ')';
      renderPins(); refresh();
    }
    $('loc').onchange = function () { showLocation(+this.value); };
    $('prev-loc').onclick = function () { showLocation(+$('loc').value - 1); };
    $('next-loc').onclick = function () { showLocation(+$('loc').value + 1); };
    $('loc-indicator').textContent = loc.name + ' (Location 1 of ' + D.locations.length + ')';
    $('seasonOnly').onclick = function () { this.setAttribute('aria-pressed', this.getAttribute('aria-pressed') === 'true' ? 'false' : 'true'); page = 0; draw(); rows(); };
    $('range').onchange = function () { page = 0; draw(); rows(); };
    $('rain-toggle').onchange = $('temp-toggle').onchange = draw;
    $('q').oninput = $('src').onchange = function () { page = 0; rows(); };
    $('prev').onclick = function () { page = Math.max(0, page - 1); rows(); };
    $('next').onclick = function () { page++; rows(); };
    $('day-prev').onclick = function () { moveSelectedDay(-1); };
    $('day-next').onclick = function () { moveSelectedDay(1); };
    $('selected-date').onchange = function () { if (this.value) { selectedDate = this.value; renderTimeline(); } };
    $('timeline-scroll').addEventListener('keydown', function (ev) {
      if (ev.key === 'ArrowLeft' || ev.key === 'ArrowRight') { ev.preventDefault(); moveSelectedDay(ev.key === 'ArrowLeft' ? -1 : 1); }
    });
    function toggleHarvestFields() { $('harvest-fields').hidden = $('lobservation').value !== 'harvest'; }
    $('lobservation').onchange = toggleHarvestFields;
    toggleHarvestFields();
    function setObservationDate() { if (!$('ldate').value) $('ldate').value = localToday(); }
    setObservationDate();
    $('logform').onsubmit = function (e) {
      e.preventDefault(); var logs = loadLogs();
      var entry = { location: loc.name, date: $('ldate').value, observation_type: $('lobservation').value, notes: $('lnotes').value };
      if (entry.observation_type === 'harvest') {
        entry.yield_tier = $('ltier').value; entry.cap_stage = $('lstage').value;
        entry.weight_g = $('lweight').value === '' ? undefined : +$('lweight').value;
      }
      logs.push(entry); saveLogs(logs); this.reset(); draw();
      toggleHarvestFields();
      var q = new URLSearchParams({ template: 'harvest.yml', title: 'Observation: ' + entry.location + ' ' + fmtDate(entry.date), labels: 'harvest', location: entry.location, date: entry.date, observation_type: entry.observation_type === 'harvest' ? 'Found mushrooms' : 'No mushrooms found', yield_tier: LABELS[entry.yield_tier] || '', cap_stage: LABELS[entry.cap_stage] || '', weight_g: entry.weight_g || '', notes: entry.notes });
      var issueLink = $('draft-issue-link');
      issueLink.href = 'https://github.com/' + D.repo + '/issues/new?' + q.toString();
      issueLink.hidden = false;
      $('draft-status').textContent = 'Draft saved in this browser only. Open the issue link and submit it on GitHub; it will not enter the shared log until intake succeeds.';
      setObservationDate();
    };
    $('export').onclick = function () { var pre = $('exported'); pre.style.display = 'block'; pre.textContent = JSON.stringify(loadLogs(), null, 2); };
    $('clearlogs').onclick = function () { if (confirm('Delete all drafts stored in this browser?')) { saveLogs([]); draw(); } };
    (function () {
      var lf = $('log-loc'), tf = $('log-type');
      if (!lf || !tf) return;
      function filterLog() {
        var shown = 0;
        document.querySelectorAll('#log-rows tr').forEach(function (tr) {
          var ok = (!lf.value || tr.dataset.location === lf.value) && (!tf.value || tr.dataset.type === tf.value);
          tr.hidden = !ok; if (ok) shown++;
        });
        $('log-count').textContent = shown + ' shown';
      }
      lf.onchange = filterLog; tf.onchange = filterLog; filterLog();
    })();
    refresh();
  })();
  </script>
</body>
</html>
"""


def dashboard_payload(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    def rnd(value: Any) -> Any:
        return round(value, 2) if isinstance(value, float) else value

    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    locations = []
    for item in analysis:
        records = item.get("records", [])
        scores = item.get("scores", {})
        locations.append({
            "name": item["name"],
            "scores": [[d, v["score"], v["status"], v.get("quality", ""),
                        score_verdict_tag(v["score"], v["status"], threshold), score_color(v["score"])]
                       for d, v in sorted(scores.items())],
            # [date, tmax, tmin, rain, wind, soil_temp, rh, soil_moisture, source]
            "records": [[r["date"]] + [rnd(r.get(k)) for k in ("temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max", "soil_temperature_0_to_7cm_mean", "relative_humidity_2m_mean", "soil_moisture_0_to_7cm_mean")] + [r.get("source", "")] for r in records],
            "harvests": item.get("harvests", []),
            "backtest": item.get("backtest", {}),
        })
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg.get("GITHUB_REPOSITORY") or "jmb2885m75-cmd/porcini-tracker"
    return {"mode": mode, "threshold": threshold, "repo": repo, "locations": locations}


_TIER_LABELS = {"small": "Small", "medium": "Medium", "large": "Large"}
_STAGE_LABELS = {"buttons_young": "Buttons / young", "prime": "Prime", "old_overripe": "Old / overripe"}


def render_observation_log(entries: List[Dict[str, Any]], repo: str) -> str:
    """Static HTML overview of every recorded input (config + harvest_log.json), newest first."""
    esc = lambda v: html.escape(str(v))

    def show_date(value: Any) -> str:
        try:
            return format_display_date(value)
        except ValueError:
            return str(value)

    summary = summarize_observations(entries)
    total_g = sum(r["weight_g"] for r in summary)
    chips = [f"<div class='chip'><strong>{len(entries)}</strong>observations</div>",
             f"<div class='chip'><strong>{sum(r['harvests'] for r in summary)}</strong>harvests</div>",
             f"<div class='chip'><strong>{sum(r['no_mushrooms'] for r in summary)}</strong>no-mushroom visits</div>",
             f"<div class='chip'><strong>{total_g:g} g</strong>recorded weight</div>"]
    summary_rows = "".join(
        f"<tr><td>{esc(r['location'])}</td><td class='num'>{r['harvests']}</td><td class='num'>{r['no_mushrooms']}</td>"
        f"<td class='num'>{r['weight_g']:g}</td><td>{esc(show_date(r['last_date']))}</td></tr>"
        for r in summary if r["last_date"])
    rows = []
    for e in entries:
        none = e.get("observation_type") == "no_mushrooms"
        issue = e.get("issue")
        link = f"<a href='https://github.com/{esc(repo)}/issues/{esc(issue)}' rel='noopener'>#{esc(issue)}</a>" if isinstance(issue, int) and not isinstance(issue, bool) else "—"
        weight = e.get("weight_g")
        rows.append(
            f"<tr data-location=\"{esc(e.get('location'))}\" data-type=\"{'no_mushrooms' if none else 'harvest'}\">"
            f"<td>{esc(show_date(e['date']))}</td><td>{esc(e.get('location'))}</td>"
            f"<td class='{'type-none' if none else 'type-found'}'>{'❌ No mushrooms' if none else '🍄 Harvest'}</td>"
            f"<td>{esc(_TIER_LABELS.get(e.get('yield_tier'), e.get('yield_tier') or '—'))}</td>"
            f"<td>{esc(_STAGE_LABELS.get(e.get('cap_stage'), e.get('cap_stage') or '—'))}</td>"
            f"<td class='num'>{esc(weight) if weight not in (None, '') else '—'}</td>"
            f"<td class='notes'>{esc(e.get('notes') or '—')}</td><td>{esc(e.get('origin', ''))}</td>"
            f"<td>{esc(e.get('reporter') or '—')}</td><td>{link}</td></tr>")
    orphans = sorted({str(e.get("location")) for e in entries if e.get("origin") == "log (location not configured)"})
    orphan_note = (f"<p class=\"meta orphan-note\">⚠️ Observations for locations that are no longer configured are still listed here "
                   f"but have no chart or score: {esc(', '.join(orphans))}. Add the location back to the config to see them in the Daily Score History.</p>") if orphans else ""
    locations = sorted({str(e.get("location")) for e in entries})
    options = "".join(f"<option value=\"{esc(l)}\">{esc(l)}</option>" for l in locations)
    if not entries:
        return ("    <h2 id='observation-log'>📒 Observation Log</h2>\n    <div class='card'><div class='meta'>No observations recorded yet. This overview includes configured past harvests and accepted issue submissions; browser-local drafts are not shared or listed here.</div></div>")
    return f"""    <h2 id="observation-log">📒 Observation Log</h2>
    <div class="card">
      <div class="log-summary">{''.join(chips)}</div>
      {orphan_note}
      <p class="meta">This overview contains configured past harvests and observations accepted into the shared log. Browser-local drafts are not shared or listed here.</p>
      <details><summary>Counts per location</summary><div class="table-wrap"><table class="log-table"><thead><tr><th>Location</th><th class="num">Harvests</th><th class="num">No mushrooms</th><th class="num">Total g</th><th>Last visit</th></tr></thead><tbody>{summary_rows}</tbody></table></div></details>
      <div class="toolbar"><label for="log-loc">Location<select id="log-loc"><option value="">All locations</option>{options}</select></label>
      <label for="log-type">Type<select id="log-type"><option value="">All types</option><option value="harvest">Harvest</option><option value="no_mushrooms">No mushrooms</option></select></label>
      <span id="log-count" class="meta"></span></div>
      <div class="table-wrap"><table class="log-table"><thead><tr><th>Date</th><th>Location</th><th>Type</th><th>Yield</th><th>Cap stage</th><th class="num">Weight g</th><th>Notes</th><th>Source</th><th>Reported by</th><th>Issue</th></tr></thead><tbody id="log-rows">{''.join(rows)}</tbody></table></div>
    </div>"""


def generate_dashboard_html(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], alert_message: str = "", alert_will_send: bool = False, mode: str = MODE_DEFAULT, harvest_log: Optional[Dict[str, Any]] = None) -> str:
    """Self-contained dashboard (inline CSS/JS/data, no CDN). Static hosting only; harvests come from harvest_log.json via the issue workflow."""
    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    cards = []
    for item in analysis:
        available = item["best_day"] != "N/A"
        verdict = ""
        score = item["best_score"]
        score_display = "N/A"
        if available:
            score_display = (
                f"<span class='score' style='color:{score_color(score)}'>{score}%</span>"
                f"<span class='verdict-tag'>{html.escape(score_verdict_tag(score, item['status'], threshold))}</span>"
            )
        if mode == MODE_FINAL and available:
            go = score >= threshold
            verdict = f"<div><span class='badge {'go' if go else 'nogo'}'>{'GO' if go else 'NO-GO'}</span></div>"
        quality = f"<div class='meta'>{html.escape(item['quality'])}</div>" if item.get("quality") else ""
        explanation = f"<div class='meta score-explanation'>{html.escape(item['explanation'])}</div>" if item.get("explanation") else ""
        cards.append(
            f"<div class='card'><div class='score-line'>{score_display}</div><div class='name'>{html.escape(str(item['name']))}</div>{verdict}"
            f"<div class='meta best-day'>Best day<strong>{html.escape(str(item['best_day']))}</strong></div><div class='meta'>Status: {html.escape(str(item['status']))}</div>{explanation}{quality}"
            f"<div class='meta'>Moisture: {item['soil_moisture']:.2f} m³/m³</div></div>"
        )
    alert_status = "This message will be sent with this run." if alert_will_send else "Preview only: no alert is triggered by this run."
    alert_section = ""
    if alert_message:
        alert_section = f"""
    <h2>📨 Alert Preview</h2>
    <div class="card">
      <div class="meta">{html.escape(alert_status)}</div>
      <pre class="alert">{html.escape(alert_message)}</pre>
      <div class="meta">Fields sent per ranked spot (top 3): rank, location name, best weekend score (%), best day, status. The final line is the dashboard link.</div>
    </div>"""
    payload = dashboard_payload(cfg, analysis, mode)
    observation_log = render_observation_log(build_observation_log(cfg.get("LOCATIONS", []), harvest_log), payload["repo"])
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    return (
        DASHBOARD_TEMPLATE
        .replace("__MODE_LABEL__", html.escape(MODE_LABELS.get(mode, MODE_LABELS[MODE_DEFAULT])))
        .replace("__GENERATED__", (lambda n: f"{format_display_date(n)} {n:%H:%M}")(datetime.now(timezone.utc)))
        .replace("__CARDS__", "".join(cards))
        .replace("__ALERT__", alert_section)
        .replace("__DATA__", data)
        .replace("__OBSERVATION_LOG__", observation_log)
    )


def resolve_dashboard_url() -> str:
    return "https://jmb2885m75-cmd.github.io/porcini-tracker/"


def inject_alert_into_index(alert_message: str, alert_will_send: bool) -> None:
    if not alert_message or not INDEX_PATH.exists():
        return
    content = INDEX_PATH.read_text(encoding="utf-8")
    start = content.find(ALERT_MARKER_START)
    end = content.find(ALERT_MARKER_END)
    status = "This message will be sent with this run." if alert_will_send else "Preview only: no alert is triggered by this run."
    block = (
        f"{ALERT_MARKER_START}\n"
        f"        <div class=\"card\">\n"
        f"            <h2>📨 Alert Preview</h2>\n"
        f"            <p><small>{html.escape(status)}</small></p>\n"
        f"            <pre style=\"white-space: pre-wrap; background: #111827; border: 1px solid #374151; border-radius: 8px; padding: 12px;\">{html.escape(alert_message)}</pre>\n"
        f"        </div>\n"
        f"        {ALERT_MARKER_END}"
    )
    if start != -1 and end > start:
        content = content[:start] + block + content[end + len(ALERT_MARKER_END):]
    else:
        anchor = '<div class="card">\n            <h2>📜 Harvest Archive Log'
        idx = content.find(anchor)
        if idx == -1:
            print("[WARN] Could not locate insertion point for alert preview in index.html")
            return
        content = content[:idx] + block + "\n\n        " + content[idx:]
    INDEX_PATH.write_text(content, encoding="utf-8")



def main() -> int:
    parser = argparse.ArgumentParser(description="Porcini tracker forecast engine")
    parser.add_argument("--test-alert", action="store_true", help="Send a test alert using current notification config")
    parser.add_argument("--check-db", action="store_true", help="Run the archive integrity check and exit (non-zero if problems found)")
    parser.add_argument("--mode", choices=["auto", MODE_DEFAULT, MODE_OUTLOOK, MODE_FINAL], default="auto", help="Run mode; 'auto' detects it from the current UTC day/time")
    args = parser.parse_args()

    if args.check_db:
        issues = check_db_integrity(load_json_safe(DB_PATH, {}))
        for issue in issues:
            print(f"[DB] {issue}")
        print("[DB] OK" if not issues else f"[DB] {len(issues)} problem(s) found")
        return 1 if issues else 0

    cfg = ensure_notification_settings(load_config())
    db = load_or_init_db()
    alert_state = build_alert_state()

    if args.test_alert:
        if dispatch_notification(cfg, "🍄 Porcini test alert: notification system is working."):
            print("[INFO] Test alert delivered")
            return 0
        print("[WARN] Test alert was not delivered")
        return 1

    now = datetime.now(timezone.utc)
    today = now.date()
    mode = detect_run_mode(now) if args.mode == "auto" else args.mode
    print(f"[INFO] Run mode: {mode}")

    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    friday_policy = resolve_friday_policy(cfg)
    dashboard_url = resolve_dashboard_url()

    try:
        harvest_log = load_harvest_log()
    except HarvestError as exc:
        print(f"[WARN] {exc}; continuing with config past_harvests only")
        harvest_log = {"harvests": []}

    analyses: List[Dict[str, Any]] = []
    alert_queue: List[Tuple[str, int, str, str]] = []

    for location in cfg.get("LOCATIONS", []):
        name = location.get("name")
        latitude = float(location.get("latitude"))
        longitude = float(location.get("longitude"))
        elevation = int(location.get("elevation_m", 0))
        harvests = merge_harvests(location.get("past_harvests", []), harvest_log, name)
        records = ensure_location_history(name, db, latitude, longitude, elevation, today)
        loc_store = db["locations"][name]
        update_daily_scores(dict(location, past_harvests=harvests), records, loc_store)
        loc_store["backtest"] = backtest_accuracy(loc_store["daily_scores"], harvests, threshold)

        best_score, best_day, status, quality = find_weekend_best(location, records, harvests, loc_store["daily_scores"], today)
        moisture = 0.0
        if records:
            moisture = float(records[-1].get("soil_moisture_0_to_7cm_mean") or 0.0)

        analyses.append({
            "name": name,
            "best_score": best_score,
            "best_day": best_day,
            "status": (status or "Monitoring") if best_day != "N/A" else "Weather data unavailable",
            "explanation": explain_score(location, records, best_day, status) if best_day != "N/A" else "",
            "soil_moisture": moisture,
            "quality": quality,
            "records": records,
            "scores": loc_store["daily_scores"],
            "harvests": harvests,
            "backtest": loc_store["backtest"],
        })

        available = best_day != "N/A"
        current_status = (status or "Monitoring") if available else "Weather data unavailable"
        send = False
        loc_state = alert_state["locations"].get(name, {})
        if mode == MODE_FINAL and available:
            send = should_confirm_for_location(name, alert_state, today, friday_policy, best_score, threshold)
            if send:
                loc_state["last_confirmation_date"] = iso_date(today)
        elif mode != MODE_FINAL and available:
            send = should_alert_for_location(name, best_score, current_status, threshold, alert_state, today)
            if send:
                loc_state["last_alert_date"] = iso_date(today)
                loc_state["last_alert_mode"] = mode
        if send:
            explanation = explain_score(location, records, best_day, current_status)
            alert_queue.append((name, best_score, best_day, current_status, explanation))
        if available:
            # Keep alert-crossing state unchanged when there is no score to compare.
            loc_state["last_score"] = best_score
            loc_state["last_status"] = current_status
        alert_state["locations"][name] = loc_state

    db["meta"] = {"last_run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "last_run_mode": mode, "model_version": MODEL_VERSION}
    save_json(DB_PATH, db)

    # Without a queued alert the ranking is only a preview on the dashboard (nothing is sent).
    alert_results = alert_queue or [(a["name"], a["best_score"], a["best_day"], a["status"], a["explanation"]) for a in analyses if a["best_day"] != "N/A"]
    message = build_alert_message(alert_results, dashboard_url, mode, threshold) if alert_results else ""
    report_html = generate_dashboard_html(cfg, analyses, message, bool(alert_queue), mode, harvest_log)
    REPORT_PATH.write_text(report_html, encoding="utf-8")
    inject_alert_into_index(message, bool(alert_queue))

    print("\n### Porcini Summary")
    for item in analyses:
        print(f"- {item['name']}: {item['best_score']}% on {item['best_day']} | {item['status']} | moisture {item['soil_moisture']:.2f} m³/m³")

    if alert_queue:
        print("\n[INFO] Dispatching alert message")
        if not dispatch_notification(cfg, message):
            print("[WARN] Alert not delivered; alert state not updated so it will be retried")
            alert_state = build_alert_state()

    save_json(ALERT_STATE_PATH, alert_state)
    return 0


if __name__ == "__main__":
    sys.exit(main())
