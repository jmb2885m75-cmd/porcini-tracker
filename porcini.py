#!/usr/bin/env python3
"""
Porcini tracker with multi-year weather archive, scoring, alerts, and dashboard generation.
"""

import argparse
import bisect
import calendar
import hashlib
import html
import json
import math
import os
import secrets
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

from dates import format_display_date, parse_user_date
from harvest import HarvestError, build_observation_log, entry_id, load_harvest_log, merge_harvests, summarize_observations

DATA_DIR = Path(os.environ.get("DATA_DIR", "."))
CONFIG_PATH = Path("config.json")
DB_PATH = DATA_DIR / "porcini_db.json"
ALERT_STATE_PATH = DATA_DIR / "alert_state.json"
REPORT_PATH = DATA_DIR / "porcini_report.html"
INDEX_PATH = DATA_DIR / "index.html"

ARCHIVE_API_URL = "https://archive-api.open-meteo.com/v1/archive"
FORECAST_API_URL = "https://api.open-meteo.com/v1/forecast"





class WeatherRequestError(RuntimeError):
    """A weather API request did not complete successfully."""


class WeatherNoDataError(RuntimeError):
    """A successful weather request contained no usable records."""


def build_weather_session() -> requests.Session:
    retry = Retry(
        total=3, connect=3, read=3, status=3, backoff_factor=0.5,
        status_forcelist=(429, 500, 502, 503, 504), allowed_methods=frozenset({"GET"}),
        raise_on_status=False,
    )
    session = requests.Session()
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    return session


WEATHER_SESSION = build_weather_session()


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
    cfg.setdefault("ALERT_THRESHOLD", DEFAULT_ALERT_THRESHOLD)
    return cfg


def fetch_json(url: str, params: Dict[str, Any], timeout: int = 30) -> Optional[Dict[str, Any]]:
    try:
        response = WEATHER_SESSION.get(url, params=params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
    except requests.RequestException as exc:
        raise WeatherRequestError(f"weather API request failed ({type(exc).__name__})") from exc
    except ValueError as exc:
        raise WeatherRequestError("weather API returned invalid JSON") from exc
    return payload if isinstance(payload, dict) else None


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")






def parse_date(value: str) -> date:
    return parse_user_date(value)


def _month_day(value: Any, default: Tuple[int, int]) -> Tuple[int, int]:
    try:
        month, day = (int(part) for part in str(value).split("-"))
        date(2001, month, day)
        return month, day
    except (TypeError, ValueError):
        return default


def month_day_window(dt: date, location: Optional[Dict[str, Any]] = None) -> bool:
    """Season window: Aug 15 – Dec 10 unless the location sets `season_start`/`season_end` as "MM-DD"."""
    location = location or {}
    start = _month_day(location.get("season_start"), (8, 15))
    end = _month_day(location.get("season_end"), (12, 10))
    return start <= (dt.month, dt.day) <= end


# Score penalty applied 1..N days after a visit that found nothing (flush not started yet)
# Flush depletion after a harvest: (max days since harvest, penalty)
# Merge policy for the same date (see merge_records): archive > forecast > sample.
# A newer forecast replaces an older forecast; sample-only dates remain available.


# Friday final-confirmation targeting (config key FRIDAY_POLICY):
#   thursday_alerted_only - only spots that received the Thursday outlook alert (the default / original behaviour)
#   all_above_threshold   - every spot whose best weekend score is at or above ALERT_THRESHOLD

# Frost handling (porcini mycelium stops fruiting after hard/repeated freezes; caps freeze and rot):
# a hard-freeze night (Tmin < FROST_TMIN_C) within the last FROST_RECENT_DAYS complete days costs FROST_PENALTY,
# a lighter sub-zero night (Tmin < FROST_LIGHT_TMIN_C) costs FROST_LIGHT_PENALTY, and each further sub-zero
# night in the last FROST_REPEAT_WINDOW_DAYS adds FROST_REPEAT_PENALTY_PER_NIGHT (capped), so one cold snap in a
# mild autumn is damped only a little while repeated freezes suppress the score.
# Late-season decay (northern hemisphere only): a penalty ramping linearly from 0 on LATE_SEASON_DECAY_START to
# LATE_SEASON_DECAY_MAX on LATE_SEASON_DECAY_END. A hard freeze in the recent window during the decay ramp ends the season.

# precip_peak_2h_mm is derived from the hourly precipitation series (busiest 2 consecutive hours of the day).

# precip_peak_basis says how precip_peak_2h_mm was obtained, so approximated vs observed data is explicit:
#   "hourly"          - fully observed: derived from the hourly series fetched together with the daily record
#   "hourly_backfill" - derived later from a separate hourly archive request (same data, filled in afterwards)
#   "unavailable"     - the hourly request succeeded but had no data for this day; no runoff penalty can apply
#   (missing)         - not yet backfilled (older records, or a backfill request failed); retried on the next run
# Nothing is ever estimated from daily totals: a missing peak simply means "no runoff penalty".
















def load_json_safe(path: Path, default: Any) -> Any:
    """Load JSON; if the file is unreadable/corrupt, move it aside (never overwrite it) and return default."""
    if not path.exists():
        return default
    try:
        with path.open("r", encoding="utf-8") as fh:
            return json.load(fh)
    except (ValueError, OSError) as exc:
        backup = path.with_name(f"{path.name}.corrupt-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}")
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
    for location in db["locations"].values():
        if isinstance(location, dict):
            for key in ("latitude", "longitude", "elevation_m"):
                location.pop(key, None)
    db["schema_version"] = SCHEMA_VERSION
    return db


LEGACY_KEY = "legacy_quarantine"
KNOWN_TOP_LEVEL_KEYS = {"locations", "schema_version", "meta", "forecast_snapshots", LEGACY_KEY}


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
    """Stitch sample, archive, and forecast data into one series, one record per date.

    Policy (explicit, per date):
      * archive observations replace forecast placeholders for the same date;
      * a forecast never replaces an archive record;
      * forecasts and archive observations replace sample data for the same date;
      * a newer forecast replaces an older forecast (forecasts get revised);
      * an archive record with no observation (archive lag returns nulls) is ignored;
      * dates covered only by forecast or sample data are preserved.
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
    archive_ok = True
    if start <= end:
        archive_records = fetch_archive_day_range(latitude, longitude, elevation, start, end)
        archive_ok = bool(archive_records)
        records = merge_records(records, archive_records)
        if not archive_ok:
            print(f"[WARN] {location_name}: archive API returned no data for {iso_date(start)}..{iso_date(end)}; not advancing last_sync_date")
    forecast_records = fetch_forecast_day_range(latitude, longitude, elevation)
    if not any(parse_date(record["date"]) >= today for record in forecast_records):
        raise WeatherNoDataError(
            f"{location_name}: forecast API returned no usable current or future daily data; "
            "refusing to save an empty forecast"
        )
    records = merge_records(records, forecast_records)
    if not records:
        raise WeatherNoDataError(f"{location_name}: weather API returned no usable records; refusing to save an empty forecast")
    backfilled = backfill_runoff_data(records, latitude, longitude, elevation)
    # Runoff looks at the last 3 days, so each backfilled day can change the next two days' scores too.
    pending = set(loc_data.get("pending_rescore") or [])
    for d in backfilled:
        pending.update(iso_date(parse_date(d) + timedelta(days=i)) for i in range(3))
    loc_data["pending_rescore"] = sorted(pending)

    if archive_ok:
        loc_data["last_sync_date"] = iso_date(today)
    loc_data.update({
        "daily_records": records,
        "record_count": len(records),
        "merge_policy": "archive replaces forecast for the same date; forecast-only dates are kept; newer forecast replaces older forecast",
    })
    for key in ("latitude", "longitude", "elevation_m"):
        loc_data.pop(key, None)
    db["locations"][location_name] = loc_data
    return records
















































































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











































def resolve_dashboard_url() -> str:
    return "https://jmb2885m75-cmd.github.io/porcini-tracker/"



def main() -> int:
    parser = argparse.ArgumentParser(description="Porcini tracker forecast engine")
    parser.add_argument("--test-alert", action="store_true", help="Send a test alert using current notification config")
    parser.add_argument("--check-db", action="store_true", help="Run the archive integrity check and exit (non-zero if problems found)")
    parser.add_argument("--backtest", action="store_true", help="Compare stored forecast snapshots with later field observations")
    parser.add_argument("--mode", choices=["auto", MODE_DEFAULT, MODE_OUTLOOK, MODE_FINAL], default="auto", help="Run mode; 'auto' detects it from the current UTC day/time")
    args = parser.parse_args()

    if args.check_db:
        issues = check_db_integrity(load_json_safe(DB_PATH, {}))
        for issue in issues:
            print(f"[DB] {issue}")
        print("[DB] OK" if not issues else f"[DB] {len(issues)} problem(s) found")
        return 1 if issues else 0

    if args.backtest:
        cfg = load_config()
        db = load_json_safe(DB_PATH, {})
        try:
            harvest_log = load_harvest_log()
        except HarvestError as exc:
            print(f"[WARN] Could not read observations for backtest: {exc}")
            harvest_log = {"harvests": []}
        print(format_forecast_backtest(
            forecast_snapshot_backtest(db, cfg, harvest_log, alert_threshold(cfg))
        ))
        return 0

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

    threshold = alert_threshold(cfg)
    minimum_gap_days = configured_alert_gap_days(cfg)
    friday_policy = resolve_friday_policy(cfg)
    dashboard_url = resolve_dashboard_url()

    try:
        harvest_log = load_harvest_log()
    except HarvestError as exc:
        print(f"[WARN] {exc}; continuing with config past_harvests only")
        harvest_log = {"harvests": []}

    analyses: List[Dict[str, Any]] = []
    alert_queue: List[Tuple[str, int, str, str]] = []
    forecast_scores: Dict[str, List[List[Any]]] = {}

    for location_index, location in enumerate(cfg.get("LOCATIONS", [])):
        name = location.get("name")
        storage_name = public_location_alias(location_index)
        latitude = float(location.get("latitude"))
        longitude = float(location.get("longitude"))
        elevation = int(location.get("elevation_m", 0))
        harvests = merge_harvests(location.get("past_harvests", []), harvest_log, name)
        if storage_name not in db["locations"] and name in db["locations"]:
            db["locations"][storage_name] = db["locations"].pop(name)
        try:
            records = ensure_location_history(storage_name, db, latitude, longitude, elevation, today)
        except Exception as exc:
            notify_sync_failure(cfg, f"{storage_name}: {exc}")
            return 1
        loc_store = db["locations"][storage_name]
        update_daily_scores(dict(location, past_harvests=harvests), records, loc_store)
        forecast_scores[storage_name] = [
            [record["date"], loc_store["daily_scores"][record["date"]]["score"]]
            for record in records
            if record.get("source") == SOURCE_FORECAST and record["date"] >= iso_date(today)
            and record["date"] in loc_store["daily_scores"]
        ]
        loc_store["backtest"] = backtest_accuracy(location, records, harvests, threshold)
        for message in calibration_messages(name, loc_store["backtest"], location):
            print(message)

        best_score, best_day, status, quality = find_weekend_best(location, records, harvests, loc_store["daily_scores"], today)
        moisture = 0.0
        if records:
            moisture = float(records[-1].get("soil_moisture_0_to_7cm_mean") or 0.0)

        analyses.append({
            "name": name,
            "best_score": best_score,
            "best_day": best_day,
            "status": (status or "🟡 Monitoring") if best_day != "N/A" else "📡 Weather data unavailable",
            "explanation": explain_score(location, records, best_day, status) if best_day != "N/A" else "",
            "soil_moisture": moisture,
            "quality": quality,
            "records": records,
            "scores": loc_store["daily_scores"],
            "harvests": harvests,
            "backtest": loc_store["backtest"],
            "flush_timing_days": estimate_days_until_threshold(
                records, loc_store["daily_scores"], today, threshold
            ),
        })

        available = best_day != "N/A"
        current_status = (status or "🟡 Monitoring") if available else "📡 Weather data unavailable"
        send = False
        if storage_name not in alert_state["locations"] and name in alert_state["locations"]:
            alert_state["locations"][storage_name] = alert_state["locations"].pop(name)
        loc_state = alert_state["locations"].get(storage_name, {})
        if mode == MODE_FINAL and available:
            send = should_confirm_for_location(storage_name, alert_state, today, friday_policy, best_score, threshold)
            if send:
                loc_state["last_confirmation_date"] = iso_date(today)
        elif mode != MODE_FINAL and available:
            send = should_alert_for_location(
                storage_name, best_score, current_status, threshold, alert_state, today, minimum_gap_days
            )
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
        alert_state["locations"][storage_name] = loc_state

    stale_issues = [
        issue for issue in check_db_integrity(db, today)
        if "more than 3 days old" in issue
        and any(issue.startswith(f"{alias}:") for alias in (
            public_location_alias(i) for i, _ in enumerate(cfg.get("LOCATIONS", []))
        ))
    ]
    if stale_issues:
        notify_sync_failure(cfg, "; ".join(stale_issues))
        return 1

    store_forecast_snapshot(db, today, forecast_scores)
    db["meta"] = {"last_run_utc": now.strftime("%Y-%m-%dT%H:%M:%SZ"), "last_run_mode": mode, "model_version": MODEL_VERSION}
    save_json(DB_PATH, db)

    # Without a queued alert the ranking is only a preview on the dashboard (nothing is sent).
    alert_results = alert_queue or [(a["name"], a["best_score"], a["best_day"], a["status"], a["explanation"]) for a in analyses if a["best_day"] != "N/A"]
    message = build_alert_message(alert_results, dashboard_url, mode, threshold) if alert_results else ""
    report_html = generate_dashboard_html(cfg, analyses, message, bool(alert_queue), mode, harvest_log)
    REPORT_PATH.write_text(report_html, encoding="utf-8")

    print("\n### Porcini Summary")
    for item in analyses:
        print(f"- {item['name']}: index {item['best_score']}/100 on {item['best_day']} | {item['status']} | moisture {item['soil_moisture']:.2f} m³/m³")

    if alert_queue:
        print("\n[INFO] Dispatching alert message")
        if not dispatch_notification(cfg, message):
            print("[WARN] Alert not delivered; alert state not updated so it will be retried")
            alert_state = build_alert_state()

    save_json(ALERT_STATE_PATH, alert_state)
    return 0



# Public re-exports retained for existing callers and tests.
from scoring import (
    SCHEMA_VERSION, NO_FIND_PENALTY, FLUSH_DEPLETION_TIERS, DEFAULT_DEPLETION_RECOVERY_DAYS,
    SMALL_HARVEST_DEPLETION_RECOVERY_DAYS, DEFAULT_EARLY_HARVEST_PENALTY_FACTOR, MODEL_VERSION,
    ARCHIVE_LAG_DAYS, SOURCE_ARCHIVE, SOURCE_FORECAST, SOURCE_SAMPLE, SOURCE_RANK, INITIAL_ARCHIVE_DAYS,
    BACKFILL_CHUNK_DAYS, BACKFILL_MAX_CHUNKS_PER_RUN, MAX_FORECAST_SNAPSHOTS, BACKTEST_MIN_CLASS_SAMPLES,
    HEAVY_FORECAST_RAIN_SHARE, TRIGGER_RAIN_LAG_DAYS, TRIGGER_RAIN_WINDOW_DAYS, TRIGGER_RAIN_WEIGHT,
    RECENT_RAIN_LAG_DAYS, RECENT_RAIN_WINDOW_DAYS, RECENT_RAIN_WEIGHT, EARLY_SEASON_OPTIMAL_TEMP_RANGE,
    RAINY_DAY_MM, RAIN_DISTRIBUTION_SCORE_MAX, DEFAULT_OPTIMAL_TEMP_RANGE, TEMPERATURE_SLOPE_PER_DEGREE,
    HUMIDITY_HIGH, HUMIDITY_LOW, HUMIDITY_HIGH_BONUS, HUMIDITY_LOW_PENALTY, HUMIDITY_LOOKBACK_DAYS,
    SCORE_GATE_DRY_THRESHOLD, SCORE_GATE_DRY_CAP, SCORE_GATE_TEMP_THRESHOLD, SCORE_GATE_TEMP_CAP,
    lagged_rain_points, humidity_adjustment, score_gate_cap, POST_FLUSH_MIN_DAYS, POST_FLUSH_MAX_DAYS, POST_FLUSH_BONUS,
    POST_FLUSH_FULL_BASE_SCORE, POST_FLUSH_RAMP, CALIBRATION_HIT_RATE_MIN, CALIBRATION_MIN_FIND_DAYS,
    RUNOFF_MIN_DAY_MM, FLUSH_RAIN_WINDOW_DAYS, FLUSH_TEMPERATURE_WINDOW_DAYS, LONG_TERM_RAIN_WINDOW_DAYS,
    LONG_TERM_RAIN_MIN_YEARS, FLUSH_RAIN_SCORE_MAX, RAINFALL_TRIGGER_SATURATION_MM, RAINFALL_BACKGROUND_SATURATION_MM, VERY_FAVOURABLE_THRESHOLD,
    FROST_TMIN_C, FROST_LIGHT_TMIN_C, FROST_RECENT_DAYS, FROST_REPEAT_WINDOW_DAYS,
    FROST_REPEAT_PENALTY_PER_NIGHT, FROST_REPEAT_PENALTY_MAX, LATE_SEASON_DECAY_START,
    LATE_SEASON_DECAY_END, LATE_SEASON_DECAY_MAX, SOIL_TEMPERATURE_SCORE_MAX, SOIL_TEMPERATURE_PENALTY_MAX,
    SOIL_TEMPERATURE_SLOPE_PER_DEGREE, DEFAULT_SOIL_TEMPERATURE_RANGE, FROST_LIGHT_PENALTY,
    HEAT_TMAX_C, FROST_PENALTY, HEAT_PENALTY, SOIL_DRY, SOIL_MID, SOIL_WET,
    FLUSH_TEMPERATURE_SCORE_MAX, DROUGHT_SCORE_PENALTY_MAX, VERDICT_LOW_TIERS, VERDICT_WORTH_LOOK,
    VERDICT_GO, VERDICT_DEFINITE_GO, VERDICT_SPECIAL_TAGS, DAILY_FIELDS, RECORD_FIELDS, PEAK_BASIS_HOURLY,
    PEAK_BASIS_BACKFILL, PEAK_BASIS_UNAVAILABLE, WEEKDAY_NAMES, SCORE_COLOR_MIN, SCORE_COLOR_MAX,
    AFFINITY_LOOKBACK_DAYS, RECENCY_YEARS_BACK, OLD_OBSERVATION_WEIGHT, peak_precip_by_day,
    parse_open_meteo_payload, fetch_archive_day_range, fetch_archive_hourly_precip, needs_runoff_backfill,
    backfill_runoff_data, fetch_forecast_day_range, History, _precip, observed_rainfall_total,
    forecast_rain_window_mix, heavily_forecast_dependent, mean_air_temperature, historical_rainfall_percentile,
    has_runoff, is_northern_hemisphere, late_season_decay_penalty, hard_freeze_recent,
    frost_penalty, _temp_range, get_seasonal_params, weight_by_recency, temperature_score,
    soil_temperature_score, rainfall_distribution_score, days_since_last_flush, flush_depletion_penalty,
    depletion_penalty_for_day, rainfall_score, soil_moisture_points, calculate_score_for_day, score_breakdown,
    location_signature, update_daily_scores, backtest_accuracy, compute_auc, _binary_auc, forecast_snapshot_backtest,
    format_forecast_backtest, find_weekday_counts, calibration_messages, find_weekend_best,
    estimate_days_until_threshold, explain_score, score_verdict_tag, score_color,
)
from report import (
    MODE_DEFAULT, MODE_OUTLOOK, MODE_FINAL, MODE_LABELS, _TIER_LABELS, _STAGE_LABELS,
    public_location_alias, alias_public_text, dashboard_payload, render_observation_log,
    generate_dashboard_html, alert_threshold, store_forecast_snapshot,
)
from notify import (
    DEFAULT_ALERT_THRESHOLD, MIN_ALERT_GAP_DAYS, FRIDAY_POLICY_THURSDAY_ONLY, FRIDAY_POLICY_ALL_ABOVE,
    FRIDAY_POLICIES, send_telegram, send_pushover, send_twilio, dispatch_notification, build_alert_message,
    should_alert_for_location, should_confirm_for_location, configured_alert_gap_days, notify_sync_failure,
    resolve_friday_policy, build_alert_state, status_category,
)

if __name__ == "__main__":
    sys.exit(main())
