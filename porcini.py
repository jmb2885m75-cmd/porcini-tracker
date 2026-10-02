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
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

from harvest import HarvestError, load_harvest_log, merge_harvests

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

HOST_TREE_SCORES = {
    "Norway Spruce": 15,
    "Spruce": 15,
    "Beech": 15,
    "Oak": 12,
    "Pine": 10,
}


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
    cfg = load_json(CONFIG_PATH)
    if not cfg:
        raise FileNotFoundError(f"Missing {CONFIG_PATH}")
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
    return datetime.strptime(value, "%Y-%m-%d").date()


def month_day_window(dt: date) -> bool:
    aug_15 = date(dt.year, 8, 15)
    dec_01 = date(dt.year, 12, 1)
    return aug_15 <= dt <= dec_01


def lunar_phase_fraction(day: date) -> float:
    known_new_moon = datetime(2000, 1, 6)
    days_since = (datetime.combine(day, datetime.min.time()) - known_new_moon).days
    cycle = 29.53
    return (days_since % cycle) / cycle



SCHEMA_VERSION = 3
# Bump when the scoring rules change; forces every stored daily score to be recomputed.
MODEL_VERSION = 3  # 3: runoff rule also applies to records backfilled from hourly data
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
RAIN_TRIGGER_MM = 10.0
THERMAL_SHOCK_DROP_C = 5.0
TRIGGER_LOOKBACK_DAYS = 14
RUNOFF_MIN_DAY_MM = 15.0

DAILY_FIELDS = [
    "precipitation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "wind_speed_10m_max",
    "soil_temperature_0_to_10cm",
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


def _sum_precip(recs: Iterable[Dict[str, Any]]) -> float:
    return sum(_precip(r) for r in recs)


def moisture_retention_days(location: Dict[str, Any]) -> int:
    """Length (days) of the rain window that still counts as 'wet' for this spot.

    Base is 7 days. North aspect holds moisture 3 days longer; south dries 1.5x faster (7 / 1.5 ~ 5 days).
    Dense canopy keeps +1 day, open/sparse canopy loses 1 day. Only the rain window is adjusted: the
    grid soil-moisture value from the weather API cannot see aspect or canopy.
    """
    aspect = str(location.get("aspect", "")).lower()
    days = 7.0
    if aspect == "north":
        days += 3
    elif aspect == "south":
        days /= 1.5
    density = str(location.get("tree_density", "")).lower()
    if "dense" in density:
        days += 1
    elif "open" in density or "sparse" in density:
        days -= 1
    return max(3, int(round(days)))


def is_thermal_shock(hist: History, day: date) -> bool:
    """Max temperature drops >= THERMAL_SHOCK_DROP_C below the mean of the previous 3 days."""
    rec = hist.get(day)
    if not rec or rec.get("temperature_2m_max") is None:
        return False
    prev = [r["temperature_2m_max"] for r in hist.between(day - timedelta(days=3), day - timedelta(days=1)) if r.get("temperature_2m_max") is not None]
    if len(prev) < 2:
        return False
    return (sum(prev) / len(prev)) - float(rec["temperature_2m_max"]) >= THERMAL_SHOCK_DROP_C


def find_flush_trigger(hist: History, day: date) -> Optional[Tuple[date, str]]:
    """Scan the 14-day lookback for a rain / thermal-shock trigger whose 7-12 day fruiting lag lands on `day`.

    Kinds: 'rain+shock' (coupled, strongest), 'rain', 'shock'. Returns the most recent active trigger.
    """
    for lag in range(7, 13):
        t = day - timedelta(days=lag)
        rec = hist.get(t)
        if rec is None:
            continue
        rain = _precip(rec) >= RAIN_TRIGGER_MM or _precip(hist.get(t + timedelta(days=1))) >= RAIN_TRIGGER_MM
        shock = is_thermal_shock(hist, t)
        if rain and shock:
            return t, "rain+shock"
        if rain:
            return t, "rain"
        if shock:
            return t, "shock"
    return None


def find_drought_rebound(hist: History, day: date) -> Optional[date]:
    """Event rule: >=45 mm over 7 days (ending e) after a dry spell (<100 mm in the 120 days before that burst).

    Returns the event day e if it happened within the last 14 days.
    """
    for back in range(0, 15):
        e = day - timedelta(days=back)
        if _sum_precip(hist.window(e, 7)) < 45:
            continue
        if _sum_precip(hist.window(e - timedelta(days=7), 120)) < 100:
            return e
    return None


def has_runoff(hist: History, day: date) -> bool:
    """Heavy-rain runoff: a day (last 3) with >= 15 mm where >50% fell in its busiest 2 hours.

    Needs hourly data (precip_peak_2h_mm); records without it (not yet backfilled, see backfill_runoff_data) get no penalty.
    """
    for rec in hist.window(day, 3):
        total, peak = _precip(rec), rec.get("precip_peak_2h_mm")
        if peak is not None and total >= RUNOFF_MIN_DAY_MM and float(peak) > 0.5 * total:
            return True
    return False


def post_rain_wind_days(hist: History, day: date) -> int:
    """Days with max wind > 30 km/h in the 5 days after the most recent rain event (>=10 mm) in the last 14 days."""
    events = [r for r in hist.window(day - timedelta(days=1), TRIGGER_LOOKBACK_DAYS) if _precip(r) >= RAIN_TRIGGER_MM]
    if not events:
        return 0
    event_day = parse_date(events[-1]["date"])
    after = hist.between(event_day + timedelta(days=1), min(event_day + timedelta(days=5), day))
    return sum(1 for r in after if r.get("wind_speed_10m_max") is not None and r["wind_speed_10m_max"] > 30)


def calculate_score_for_day(location: Dict[str, Any], daily: Dict[str, Any], historical: Any, past_harvests: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    """Score one day using only data on/before that day (no look-ahead, no dependence on 'today')."""
    hist = historical if isinstance(historical, History) else History(historical)
    score = 0
    status = "🟡 Monitoring"
    quality = ""

    d = parse_date(daily["date"])
    if not month_day_window(d):
        return 0, "❌ Outside mushroom season", ""

    if daily.get("temperature_2m_min") is not None and daily["temperature_2m_min"] < -2:
        frost_count = sum(
            1 for rec in hist.window(d, 7)
            if (rec.get("temperature_2m_min") is not None and rec["temperature_2m_min"] < -2)
            or (rec.get("soil_temperature_0_to_10cm") is not None and rec["soil_temperature_0_to_10cm"] < 3)
        )
        if frost_count >= 2:
            return 0, "❄️ SEASON TERMINATED BY FROST", ""

    soil_temp = daily.get("soil_temperature_0_to_10cm")
    if soil_temp is not None and soil_temp > 20:
        return 0, "🔥 SEASON DELAYED BY HIGH SOIL TEMP", ""
    if soil_temp is not None and 10 <= soil_temp <= 18:
        score += 15

    rainfall_120 = _sum_precip(hist.window(d, 120))
    rainfall_recent = _sum_precip(hist.window(d, moisture_retention_days(location)))
    rebound = find_drought_rebound(hist, d)
    if rebound is not None:
        # Drought rebound / super-flush is an event: bonus applies for 14 days after the breaking rain,
        # with an extra boost when the usual 7-14 day mycelial response window is reached.
        score += 15
        if (d - rebound).days >= 7:
            score += 10
        status = "🔥 DROUGHT BROKEN - High potential for massive super-flush!"
    elif rainfall_120 < 100:
        return 20, "TOO DRY - DROUGHT UNBROKEN", ""
    elif rainfall_recent >= 20:
        score += 30

    trigger = find_flush_trigger(hist, d)
    if trigger is not None:
        score += {"rain+shock": 25, "rain": 10, "shock": 10}[trigger[1]]
        # Weekend timing: trigger lag lands the flush on Fri/Sat/Sun (+15) or Mon/Tue/Wed (-20).
        if d.weekday() in (4, 5, 6):
            score += 15
        elif d.weekday() in (0, 1, 2):
            score -= 20

    if has_runoff(hist, d):
        score -= 10

    soil_moisture = daily.get("soil_moisture_0_to_7cm_mean")
    if soil_moisture is not None:
        if soil_moisture > 0.35:
            score += 10
        elif soil_moisture < 0.18 and (daily.get("precipitation_sum") or 0) > 5:
            score -= 15

    for tree_name, pts in HOST_TREE_SCORES.items():
        if tree_name in location.get("tree_species", []):
            score += pts
    if location.get("soil_pH") == "alkaline":
        score = min(score, 60)

    if soil_temp is not None and 12 <= soil_temp <= 17:
        score += 15

    # Growth phase = the 12 days leading up to the scored day.
    rh_values = [r["relative_humidity_2m_mean"] for r in hist.window(d, 12) if r.get("relative_humidity_2m_mean") is not None]
    if rh_values and sum(rh_values) / len(rh_values) < 60:
        score -= 15

    if post_rain_wind_days(hist, d) > 2:
        score -= 10

    last_seen = location.get("last_seen_fly_agaric")
    if last_seen and valid_date_string(last_seen):
        if 0 <= (d - parse_date(last_seen)).days <= 10:
            score += 15

    phase = lunar_phase_fraction(d)
    if 0.35 <= phase <= 0.65:
        score += 5

    for harvest in past_harvests:
        harvest_date = harvest.get("date")
        if not valid_date_string(harvest_date):
            continue
        delta_days = (d - parse_date(harvest_date)).days
        if harvest.get("cap_stage") == "buttons_young" and 1 <= delta_days <= 4:
            score += 20
        elif harvest.get("cap_stage") == "old_overripe" and 1 <= delta_days <= 7:
            score -= 25
            status = "🍂 EXHAUSTION / POST-FLUSH COOLING OFF"

    # Proven spot affinity: >=2 medium/large harvests in the 2 years BEFORE the scored day -> +10.
    positive = sum(
        1 for h in past_harvests
        if h.get("yield_tier") in {"medium", "large"} and valid_date_string(h.get("date"))
        and 0 < (d - parse_date(h["date"])).days <= AFFINITY_LOOKBACK_DAYS
    )
    if positive >= 2:
        score += 10

    # Quality / risk uses the 7-day average of daily max temperature.
    temps = [float(r["temperature_2m_max"]) for r in hist.window(d, 7) if r.get("temperature_2m_max") is not None]
    if temps:
        avg_max = sum(temps) / len(temps)
        if avg_max > 18:
            quality = "⚠️ HIGH MAGGOT RISK - Harvest early while small."
        elif 8 <= avg_max <= 15:
            quality = "💎 PRIME QUALITY - Firm, bug-free caps expected."

    score = max(0, min(100, score))
    if not status or status == "🟡 Monitoring":
        status = "✅ Viable conditions" if score >= 65 else "⚠️ Watch closely"
    return int(score), status, quality


def location_signature(location: Dict[str, Any]) -> str:
    """Fingerprint of the config fields that influence scores; a change recomputes the stored series."""
    keys = ("tree_species", "tree_density", "aspect", "soil_pH", "past_harvests", "last_seen_fly_agaric")
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
    return score, best_date.strftime("%a %Y-%m-%d"), status, flag


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
    if "drought broken" in s or "super-flush" in s:
        return "superflush"
    if "outside" in s:
        return "off_season"
    if "viable" in s:
        return "viable"
    return "watch"


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
        return True
    except Exception as exc:
        print(f"[WARN] Telegram alert failed: {exc}")
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
    for index, (name, score, best_day, status) in enumerate(ranking, 1):
        verdict = f"{'GO' if score >= threshold else 'NO-GO'} - " if mode == MODE_FINAL else ""
        lines.append(f"{index}. {name} - {verdict}{score}% ({best_day}) | {status}")
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
    body { font-family: Arial, sans-serif; background: #111827; color: #e5e7eb; margin: 0; padding: 24px; }
    .wrap { max-width: 960px; margin: 0 auto; }
    h1 { color: #a7f3d0; }
    .grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }
    .card { background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 18px; margin-bottom: 16px; }
    .score { font-size: 2rem; font-weight: bold; color: #a7f3d0; }
    .name { font-size: 1.1rem; margin-top: 6px; font-weight: bold; }
    .meta { color: #cbd5e1; margin-top: 8px; }
    .badge { display: inline-block; background: #065f46; border-radius: 999px; padding: 2px 10px; font-size: .85rem; }
    .go { background: #065f46; } .nogo { background: #7f1d1d; }
    .alert { background: #111827; border: 1px solid #374151; border-radius: 8px; padding: 12px; white-space: pre-wrap; }
    .note { color: #fbbf24; font-size: .9rem; }
    #chartbox { position: relative; }
    svg { width: 100%; height: auto; background: #111827; border-radius: 8px; }
    #tip { position: absolute; display: none; pointer-events: none; background: #030712; border: 1px solid #4b5563; border-radius: 8px; padding: 8px 10px; font-size: .85rem; max-width: 280px; z-index: 5; }
    table { width: 100%; border-collapse: collapse; font-size: .85rem; }
    th, td { text-align: left; padding: 4px 6px; border-bottom: 1px solid #374151; }
    tr.row { cursor: pointer; } tr.row:hover { background: #374151; }
    input, select, button { background: #111827; color: #e5e7eb; border: 1px solid #4b5563; border-radius: 6px; padding: 6px; margin: 2px; }
    button { cursor: pointer; }
    .pin { cursor: pointer; }
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🍄 Porcini Tracker Dashboard</h1>
    <div class="meta"><span class="badge">__MODE_LABEL__</span> Generated __GENERATED__ UTC</div>
    <div class="grid">__CARDS__</div>__ALERT__
    <h2>📈 Daily Score History &amp; Harvest Pins</h2>
    <div class="card">
      <select id="loc"></select>
      <select id="range"></select>
      <div id="chartbox"><svg id="chart" viewBox="0 0 900 320"></svg><div id="tip"></div></div>
      <div class="meta">Hover for details. Click a harvest pin (circle) or a table row to pin it below. Dashed line = alert threshold.</div>
      <div id="pins" class="meta"></div>
      <div id="backtest" class="meta"></div>
    </div>
    <h2>🔎 Observation Browser</h2>
    <div class="card">
      <input id="q" placeholder="Filter by date (e.g. 2025-09)" />
      <select id="src"><option value="">all sources</option><option>archive</option><option>forecast</option></select>
      <table><thead><tr><th>Date</th><th>Score</th><th>Tmax</th><th>Rain mm</th><th>Wind</th><th>Soil °C</th><th>RH %</th><th>Src</th></tr></thead><tbody id="rows"></tbody></table>
      <button id="prev">&laquo; Prev</button><button id="next">Next &raquo;</button> <span id="pageinfo" class="meta"></span>
    </div>
    <h2>📝 Log Field Observation</h2>
    <div class="card">
      <form id="logform">
        <input type="date" id="ldate" required />
        <select id="ltier"><option>small</option><option>medium</option><option>large</option></select>
        <select id="lstage"><option>buttons_young</option><option>prime</option><option>old_overripe</option></select>
        <input type="number" id="lweight" placeholder="weight g" min="0" />
        <input id="lnotes" placeholder="notes" />
        <button type="submit">Submit via GitHub issue</button>
      </form>
      <div class="note">Submitting opens a pre-filled GitHub issue (sign-in required). Only issues from the repository owner or collaborators are accepted: a workflow then appends the harvest to <code>harvest_log.json</code>, rescores and republishes this page, usually within a few minutes. Until then it is only an unsynced draft in this browser's localStorage (blue pin); the source of truth is the repository file. Red pins are synced harvests.</div>
      <button id="export">Export drafts (JSON)</button><button id="clearlogs">Clear drafts</button>
      <pre id="exported" class="alert" style="display:none"></pre>
    </div>
  </div>
  <script id="porcini-data" type="application/json">__DATA__</script>
  <script>
  (function () {
    var D = JSON.parse(document.getElementById('porcini-data').textContent);
    var LOG_KEY = 'porcini_logs_v1', PAGE = 15;
    function $(id) { return document.getElementById(id); }
    function loadLogs() { try { return JSON.parse(localStorage.getItem(LOG_KEY) || '[]'); } catch (e) { return []; } }
    function saveLogs(l) { try { localStorage.setItem(LOG_KEY, JSON.stringify(l)); } catch (e) { alert('Could not save: storage unavailable'); } }
    var NS = 'http://www.w3.org/2000/svg';
    function el(name, attrs, text) { var e = document.createElementNS(NS, name); for (var k in attrs) e.setAttribute(k, attrs[k]); if (text) e.textContent = text; return e; }
    var loc = D.locations[0], pinned = [], page = 0;
    if (!loc) { $('chart').replaceWith(document.createTextNode('No locations configured.')); return; }
    D.locations.forEach(function (l, i) { var o = document.createElement('option'); o.value = i; o.textContent = l.name; $('loc').appendChild(o); });
    function years() { var s = {}; loc.scores.forEach(function (r) { s[r[0].slice(0, 4)] = 1; }); return Object.keys(s).sort(); }
    function fillRange() {
      var cur = $('range').value; $('range').innerHTML = '';
      ['all', '365'].concat(years()).forEach(function (v) { var o = document.createElement('option'); o.value = v; o.textContent = v === 'all' ? 'All years' : v === '365' ? 'Last 365 days' : v; $('range').appendChild(o); });
      if (cur) $('range').value = cur;
    }
    function inRange(d) { var r = $('range').value || 'all'; if (r === 'all') return true; if (r === '365') return Date.parse(d) >= Date.now() - 365 * 864e5; return d.slice(0, 4) === r; }
    function harvestsFor() {
      var h = (loc.harvests || []).map(function (x) { return Object.assign({}, x, { origin: x.origin === 'log' ? 'harvest_log.json' : 'config' }); });
      loadLogs().filter(function (x) { return x.location === loc.name; }).forEach(function (x) { h.push(Object.assign({ origin: 'draft (unsynced)' }, x)); });
      return h;
    }
    function recMap() { var m = {}; loc.records.forEach(function (r) { m[r[0]] = r; }); return m; }
    function scoreMap() { var m = {}; loc.scores.forEach(function (r) { m[r[0]] = r; }); return m; }
    function describe(date, extra) {
      var rm = recMap()[date], sm = scoreMap()[date], out = [date];
      if (sm) { out.push('Score: ' + sm[1] + '% - ' + sm[2]); if (sm[3]) out.push(sm[3]); }
      if (rm) out.push('Tmax ' + rm[1] + '°C, rain ' + rm[3] + ' mm, wind ' + rm[4] + ' km/h, RH ' + rm[6] + '% (' + rm[8] + ')');
      (extra || []).forEach(function (h) { out.push('🍄 ' + h.origin + ': ' + (h.yield_tier || '?') + ' / ' + (h.cap_stage || '?') + (h.weight_g ? ' / ' + h.weight_g + ' g' : '') + (h.notes ? ' - ' + h.notes : '')); });
      return out;
    }
    function setLines(node, lines) { node.textContent = ''; lines.forEach(function (t, i) { var d = document.createElement('div'); d.textContent = t; if (i === 0) d.style.fontWeight = 'bold'; node.appendChild(d); }); }
    function renderPins() {
      var box = $('pins'); box.textContent = '';
      pinned.forEach(function (p, i) { var d = document.createElement('div'); d.textContent = '📌 ' + describe(p.date, p.h).join(' | ') + ' (click to unpin)'; d.style.cursor = 'pointer'; d.onclick = function () { pinned.splice(i, 1); renderPins(); }; box.appendChild(d); });
    }
    function pin(date) { var hs = harvestsFor().filter(function (h) { return h.date === date; }); if (!pinned.some(function (p) { return p.date === date; })) pinned.push({ date: date, h: hs }); renderPins(); }
    function draw() {
      var svg = $('chart'), tip = $('tip'); svg.textContent = '';
      var pts = loc.scores.filter(function (r) { return inRange(r[0]); });
      if (!pts.length) { svg.appendChild(el('text', { x: 20, y: 40, fill: '#cbd5e1' }, 'No data in range')); return; }
      var W = 900, H = 320, L = 36, R = 10, T = 10, B = 24;
      var t0 = Date.parse(pts[0][0]), t1 = Date.parse(pts[pts.length - 1][0]);
      function x(t) { return L + (W - L - R) * ((t - t0) / Math.max(1, t1 - t0)); }
      function y(v) { return T + (H - T - B) * (1 - v / 100); }
      [0, 25, 50, 75, 100].forEach(function (v) { svg.appendChild(el('line', { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: '#374151' })); svg.appendChild(el('text', { x: 4, y: y(v) + 4, fill: '#9ca3af', 'font-size': 11 }, String(v))); });
      svg.appendChild(el('line', { x1: L, x2: W - R, y1: y(D.threshold), y2: y(D.threshold), stroke: '#f59e0b', 'stroke-dasharray': '5 4' }));
      svg.appendChild(el('text', { x: L, y: H - 6, fill: '#9ca3af', 'font-size': 11 }, pts[0][0]));
      svg.appendChild(el('text', { x: W - R, y: H - 6, fill: '#9ca3af', 'font-size': 11, 'text-anchor': 'end' }, pts[pts.length - 1][0]));
      svg.appendChild(el('polyline', { points: pts.map(function (r) { return x(Date.parse(r[0])) + ',' + y(r[1]); }).join(' '), fill: 'none', stroke: '#34d399', 'stroke-width': 1.5 }));
      var sm = scoreMap(), cursor = el('line', { y1: T, y2: H - B, stroke: '#6b7280', visibility: 'hidden' });
      svg.appendChild(cursor);
      harvestsFor().forEach(function (h) {
        var t = Date.parse(h.date); if (isNaN(t) || t < t0 || t > t1) return;
        var c = el('circle', { cx: x(t), cy: y(sm[h.date] ? sm[h.date][1] : 0), r: 6, fill: h.origin === 'draft (unsynced)' ? '#60a5fa' : '#f87171', stroke: '#fff', class: 'pin' });
        c.addEventListener('click', function () { pin(h.date); });
        svg.appendChild(c);
      });
      var overlay = el('rect', { x: L, y: T, width: W - L - R, height: H - T - B, fill: 'transparent' });
      overlay.addEventListener('mousemove', function (ev) {
        var box = svg.getBoundingClientRect(), px = (ev.clientX - box.left) * (W / box.width), t = t0 + (px - L) / (W - L - R) * (t1 - t0), best = pts[0], bd = Infinity;
        pts.forEach(function (r) { var dd = Math.abs(Date.parse(r[0]) - t); if (dd < bd) { bd = dd; best = r; } });
        var hs = harvestsFor().filter(function (h) { return h.date === best[0]; });
        setLines(tip, describe(best[0], hs)); tip.style.display = 'block';
        tip.style.left = Math.min(ev.clientX - box.left + 12, box.width - 290) + 'px'; tip.style.top = (ev.clientY - box.top + 12) + 'px';
        cursor.setAttribute('x1', x(Date.parse(best[0]))); cursor.setAttribute('x2', x(Date.parse(best[0]))); cursor.setAttribute('visibility', 'visible');
      });
      overlay.addEventListener('mouseleave', function () { tip.style.display = 'none'; cursor.setAttribute('visibility', 'hidden'); });
      overlay.addEventListener('click', function (ev) { var lines = tip.firstChild; if (lines) pin(lines.textContent); });
      svg.appendChild(overlay);
    }
    function rows() {
      var q = $('q').value.trim(), s = $('src').value, sm = scoreMap();
      var list = loc.records.filter(function (r) { return inRange(r[0]) && (!q || r[0].indexOf(q) === 0) && (!s || r[8] === s); }).reverse();
      var pages = Math.max(1, Math.ceil(list.length / PAGE)); page = Math.min(page, pages - 1);
      var body = $('rows'); body.textContent = '';
      list.slice(page * PAGE, page * PAGE + PAGE).forEach(function (r) {
        var tr = document.createElement('tr'); tr.className = 'row';
        [r[0], sm[r[0]] ? sm[r[0]][1] : '', r[1], r[3], r[4], r[5], r[6], r[8]].forEach(function (v) { var td = document.createElement('td'); td.textContent = v == null ? '' : v; tr.appendChild(td); });
        tr.onclick = function () { pin(r[0]); };
        body.appendChild(tr);
      });
      $('pageinfo').textContent = 'Page ' + (page + 1) + ' / ' + pages + ' (' + list.length + ' days)';
    }
    function backtest() {
      var b = loc.backtest || {};
      $('backtest').textContent = b.evaluated_harvests ? 'Backtest: ' + b.hits + '/' + b.evaluated_harvests + ' medium/large harvests fell on days scoring >= ' + b.threshold + ' (mean score on harvest days ' + b.mean_score_on_harvest_days + ' vs in-season mean ' + b.mean_score_in_season + '). Small sample; indicative only.' : 'Backtest: no medium/large harvests with a stored score yet.';
    }
    function refresh() { fillRange(); draw(); rows(); backtest(); }
    $('loc').onchange = function () { loc = D.locations[+this.value]; pinned = []; page = 0; renderPins(); refresh(); };
    $('range').onchange = function () { page = 0; draw(); rows(); };
    $('q').oninput = $('src').onchange = function () { page = 0; rows(); };
    $('prev').onclick = function () { page = Math.max(0, page - 1); rows(); };
    $('next').onclick = function () { page++; rows(); };
    $('logform').onsubmit = function (e) {
      e.preventDefault(); var logs = loadLogs();
      var entry = { location: loc.name, date: $('ldate').value, yield_tier: $('ltier').value, cap_stage: $('lstage').value, weight_g: +$('lweight').value || undefined, notes: $('lnotes').value };
      logs.push(entry); saveLogs(logs); this.reset(); draw();
      var q = new URLSearchParams({ template: 'harvest.yml', title: 'Harvest: ' + entry.location + ' ' + entry.date, labels: 'harvest', location: entry.location, date: entry.date, yield_tier: entry.yield_tier, cap_stage: entry.cap_stage, weight_g: entry.weight_g || '', notes: entry.notes });
      window.open('https://github.com/' + D.repo + '/issues/new?' + q.toString(), '_blank', 'noopener');
    };
    $('export').onclick = function () { var pre = $('exported'); pre.style.display = 'block'; pre.textContent = JSON.stringify(loadLogs(), null, 2); };
    $('clearlogs').onclick = function () { if (confirm('Delete all drafts stored in this browser?')) { saveLogs([]); draw(); } };
    refresh();
  })();
  </script>
</body>
</html>
"""


def dashboard_payload(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    def rnd(value: Any) -> Any:
        return round(value, 2) if isinstance(value, float) else value

    locations = []
    for item in analysis:
        records = item.get("records", [])
        scores = item.get("scores", {})
        locations.append({
            "name": item["name"],
            "scores": [[d, v["score"], v["status"], v.get("quality", "")] for d, v in sorted(scores.items())],
            # [date, tmax, tmin, rain, wind, soil_temp, rh, soil_moisture, source]
            "records": [[r["date"]] + [rnd(r.get(k)) for k in ("temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max", "soil_temperature_0_to_10cm", "relative_humidity_2m_mean", "soil_moisture_0_to_7cm_mean")] + [r.get("source", "")] for r in records],
            "harvests": item.get("harvests", []),
            "backtest": item.get("backtest", {}),
        })
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg.get("GITHUB_REPOSITORY") or "jmb2885m75-cmd/porcini-tracker"
    return {"mode": mode, "threshold": int(cfg.get("ALERT_THRESHOLD", 65)), "repo": repo, "locations": locations}


def generate_dashboard_html(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], alert_message: str = "", alert_will_send: bool = False, mode: str = MODE_DEFAULT) -> str:
    """Self-contained dashboard (inline CSS/JS/data, no CDN). Static hosting only; harvests come from harvest_log.json via the issue workflow."""
    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    cards = []
    for item in analysis:
        verdict = ""
        if mode == MODE_FINAL:
            go = item["best_score"] >= threshold
            verdict = f"<div><span class='badge {'go' if go else 'nogo'}'>{'GO' if go else 'NO-GO'}</span></div>"
        quality = f"<div class='meta'>{html.escape(item['quality'])}</div>" if item.get("quality") else ""
        cards.append(
            f"<div class='card'><div class='score'>{item['best_score']}%</div><div class='name'>{html.escape(str(item['name']))}</div>{verdict}"
            f"<div class='meta'>Best day: {html.escape(str(item['best_day']))} | Status: {html.escape(str(item['status']))}</div>{quality}"
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
    data = json.dumps(dashboard_payload(cfg, analysis, mode), ensure_ascii=False).replace("</", "<\\/")
    return (
        DASHBOARD_TEMPLATE
        .replace("__MODE_LABEL__", html.escape(MODE_LABELS.get(mode, MODE_LABELS[MODE_DEFAULT])))
        .replace("__GENERATED__", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"))
        .replace("__CARDS__", "".join(cards))
        .replace("__ALERT__", alert_section)
        .replace("__DATA__", data)
    )


def resolve_dashboard_url(cfg: Dict[str, Any]) -> str:
    configured = cfg.get("FTP_SETTINGS", {}).get("dashboard_url") or cfg.get("DATABASE_SETTINGS", {}).get("dashboard_url") or ""
    if configured and "yourdomain.com" not in configured:
        return configured
    endpoint = cfg.get("DATABASE_SETTINGS", {}).get("endpoint_url") or ""
    if endpoint:
        return endpoint.rsplit("/", 1)[0] + "/"
    return configured or "https://example.invalid/porcini_report.html"


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
        dispatch_notification(cfg, "🍄 Porcini test alert: notification system is working.")
        return 0

    now = datetime.now(timezone.utc)
    today = now.date()
    mode = detect_run_mode(now) if args.mode == "auto" else args.mode
    print(f"[INFO] Run mode: {mode}")

    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    friday_policy = resolve_friday_policy(cfg)
    dashboard_url = resolve_dashboard_url(cfg)

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
            "status": status or "Monitoring",
            "soil_moisture": moisture,
            "quality": quality,
            "records": records,
            "scores": loc_store["daily_scores"],
            "harvests": harvests,
            "backtest": loc_store["backtest"],
        })

        current_status = status or "Monitoring"
        send = False
        loc_state = alert_state["locations"].get(name, {})
        if mode == MODE_FINAL:
            send = should_confirm_for_location(name, alert_state, today, friday_policy, best_score, threshold)
            if send:
                loc_state["last_confirmation_date"] = iso_date(today)
        else:
            send = should_alert_for_location(name, best_score, current_status, threshold, alert_state, today)
            if send:
                loc_state["last_alert_date"] = iso_date(today)
                loc_state["last_alert_mode"] = mode
        if send:
            alert_queue.append((name, best_score, best_day, current_status))
        # last_score / last_status always track the previous run so the next run can detect a crossing.
        loc_state["last_score"] = best_score
        loc_state["last_status"] = current_status
        alert_state["locations"][name] = loc_state

    db["meta"] = {"last_run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "last_run_mode": mode, "model_version": MODEL_VERSION}
    save_json(DB_PATH, db)

    # Without a queued alert the ranking is only a preview on the dashboard (nothing is sent).
    alert_results = alert_queue or [(a["name"], a["best_score"], a["best_day"], a["status"]) for a in analyses]
    message = build_alert_message(alert_results, dashboard_url, mode, threshold) if alert_results else ""
    report_html = generate_dashboard_html(cfg, analyses, message, bool(alert_queue), mode)
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
