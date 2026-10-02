#!/usr/bin/env python3
"""
Porcini tracker with multi-year weather archive, scoring, alerts, and dashboard generation.
"""

import argparse
import json
import math
import os
import sys
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import requests

CONFIG_PATH = Path("config.json")
DB_PATH = Path("porcini_db.json")
ALERT_STATE_PATH = Path("alert_state.json")
REPORT_PATH = Path("porcini_report.html")

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
    with path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, ensure_ascii=False)
        fh.write("\n")


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


def fetch_archive_day_range(latitude: float, longitude: float, elevation: int, start: date, end: date) -> List[Dict[str, Any]]:
    payload = fetch_json(
        ARCHIVE_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "start_date": iso_date(start),
            "end_date": iso_date(end),
            "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,wind_speed_10m_max,soil_temperature_0_to_10cm,relative_humidity_2m_mean,soil_moisture_0_to_7cm_mean",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    if not payload or "daily" not in payload:
        return []
    daily = payload["daily"]
    records: List[Dict[str, Any]] = []
    for idx, date_str in enumerate(daily.get("time", [])):
        item = {
            "date": date_str,
            "precipitation_sum": daily.get("precipitation_sum", [0.0])[idx],
            "temperature_2m_max": daily.get("temperature_2m_max", [None])[idx],
            "temperature_2m_min": daily.get("temperature_2m_min", [None])[idx],
            "wind_speed_10m_max": daily.get("wind_speed_10m_max", [None])[idx],
            "soil_temperature_0_to_10cm": daily.get("soil_temperature_0_to_10cm", [None])[idx],
            "relative_humidity_2m_mean": daily.get("relative_humidity_2m_mean", [None])[idx],
            "soil_moisture_0_to_7cm_mean": daily.get("soil_moisture_0_to_7cm_mean", [None])[idx],
        }
        records.append(item)
    return records


def fetch_forecast_day_range(latitude: float, longitude: float, elevation: int) -> List[Dict[str, Any]]:
    payload = fetch_json(
        FORECAST_API_URL,
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "past_days": 7,
            "forecast_days": 7,
            "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min,wind_speed_10m_max,soil_temperature_0_to_10cm,relative_humidity_2m_mean,soil_moisture_0_to_7cm_mean",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    if not payload or "daily" not in payload:
        return []
    daily = payload["daily"]
    records: List[Dict[str, Any]] = []
    for idx, date_str in enumerate(daily.get("time", [])):
        item = {
            "date": date_str,
            "precipitation_sum": daily.get("precipitation_sum", [0.0])[idx],
            "temperature_2m_max": daily.get("temperature_2m_max", [None])[idx],
            "temperature_2m_min": daily.get("temperature_2m_min", [None])[idx],
            "wind_speed_10m_max": daily.get("wind_speed_10m_max", [None])[idx],
            "soil_temperature_0_to_10cm": daily.get("soil_temperature_0_to_10cm", [None])[idx],
            "relative_humidity_2m_mean": daily.get("relative_humidity_2m_mean", [None])[idx],
            "soil_moisture_0_to_7cm_mean": daily.get("soil_moisture_0_to_7cm_mean", [None])[idx],
        }
        records.append(item)
    return records


def load_or_init_db() -> Dict[str, Any]:
    db = load_json(DB_PATH, {"locations": {}})
    if "locations" not in db:
        db["locations"] = {}
    return db


def append_unique_records(records: List[Dict[str, Any]], new_records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    seen = {r["date"] for r in records}
    for item in new_records:
        if item.get("date") and item["date"] not in seen:
            records.append(item)
    records.sort(key=lambda r: r.get("date", ""))
    return records


def ensure_location_history(location_name: str, db: Dict[str, Any], latitude: float, longitude: float, elevation: int) -> List[Dict[str, Any]]:
    loc_data = db["locations"].get(location_name, {})
    records = loc_data.get("daily_records", [])

    today = date.today()
    if not records:
        start = today - timedelta(days=INITIAL_ARCHIVE_DAYS)
        archive_records = fetch_archive_day_range(latitude, longitude, elevation, start, today - timedelta(days=5))
        forecast_records = fetch_forecast_day_range(latitude, longitude, elevation)
        records = append_unique_records([], archive_records)
        records = append_unique_records(records, forecast_records)
        last_sync = iso_date(today)
    else:
        last_date = max(parse_date(r["date"]) for r in records)
        start = last_date + timedelta(days=1)
        end = today - timedelta(days=5)
        if start <= end:
            archive_records = fetch_archive_day_range(latitude, longitude, elevation, start, end)
            records = append_unique_records(records, archive_records)
        forecast_records = fetch_forecast_day_range(latitude, longitude, elevation)
        records = append_unique_records(records, forecast_records)
        last_sync = iso_date(today)

    db["locations"][location_name] = {
        "latitude": latitude,
        "longitude": longitude,
        "elevation_m": elevation,
        "daily_records": records,
        "last_sync_date": last_sync,
    }
    return records


def calculate_score_for_day(location: Dict[str, Any], daily: Dict[str, Any], historical: List[Dict[str, Any]], past_harvests: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    score = 0
    status = "🟡 Monitoring"
    quality = ""

    d = parse_date(daily["date"])
    if not month_day_window(d):
        return 0, "❌ Outside mushroom season", ""

    if daily.get("temperature_2m_min") is not None and daily["temperature_2m_min"] < -2:
        recent = historical[-7:]
        frost_count = sum(
            1 for rec in recent
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

    rainfall_120 = sum(float(r.get("precipitation_sum", 0) or 0) for r in historical[-120:])
    rainfall_7 = sum(float(r.get("precipitation_sum", 0) or 0) for r in historical[-7:])
    if rainfall_120 < 100:
        if rainfall_7 < 45:
            return 20, "TOO DRY - DROUGHT UNBROKEN", ""
        score += 15
        status = "🔥 DROUGHT BROKEN - High potential for massive super-flush!"
    else:
        if rainfall_7 >= 20:
            score += 30

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

    humidity = daily.get("relative_humidity_2m_mean")
    if humidity is not None and humidity < 60:
        score -= 15

    wind = daily.get("wind_speed_10m_max")
    if wind is not None and wind > 30:
        score -= 10

    aspect = str(location.get("aspect", "")).lower()
    if aspect == "north":
        score += 3
    elif aspect == "south":
        score -= 2

    last_seen = location.get("last_seen_fly_agaric")
    if last_seen:
        delta = (date.today() - parse_date(last_seen)).days
        if 0 <= delta <= 10:
            score += 15

    phase = lunar_phase_fraction(d)
    if 0.35 <= phase <= 0.65:
        score += 5

    for harvest in past_harvests:
        harvest_date = harvest.get("date")
        if not harvest_date:
            continue
        delta_days = (date.today() - parse_date(harvest_date)).days
        if harvest.get("cap_stage") == "buttons_young" and 1 <= delta_days <= 4:
            score += 20
        elif harvest.get("cap_stage") == "old_overripe" and 1 <= delta_days <= 7:
            score -= 25
            status = "🍂 EXHAUSTION / POST-FLUSH COOLING OFF"

    positive = sum(1 for h in past_harvests if h.get("yield_tier") in {"medium", "large"})
    if positive >= 2:
        score += 10

    if daily.get("temperature_2m_max") is not None:
        temp_max = float(daily["temperature_2m_max"])
        if temp_max > 18:
            quality = "⚠️ HIGH MAGGOT RISK - Harvest early while small."
        elif 8 <= temp_max <= 15:
            quality = "💎 PRIME QUALITY - Firm, bug-free caps expected."

    if score < 0:
        score = 0
    if score > 100:
        score = 100
    if not status or status == "🟡 Monitoring":
        status = "✅ Viable conditions" if score >= 65 else "⚠️ Watch closely"
    return int(score), status, quality


def find_weekend_best(location: Dict[str, Any], records: List[Dict[str, Any]], harvests: List[Dict[str, Any]]) -> Tuple[int, str, str, str]:
    valid_days = []
    for record in records:
        dt = parse_date(record["date"])
        if dt.weekday() in {4, 5, 6}:
            score, status, flag = calculate_score_for_day(location, record, records, harvests)
            valid_days.append((dt, score, status, flag))
    if not valid_days:
        return 0, "N/A", "", ""
    best = max(valid_days, key=lambda item: item[1])
    best_date, score, status, flag = best
    return score, best_date.strftime("%a %Y-%m-%d"), status, flag


def build_alert_state() -> Dict[str, Any]:
    state = load_json(ALERT_STATE_PATH, {"locations": {}})
    if "locations" not in state:
        state["locations"] = {}
    return state


def should_alert_for_location(location_name: str, current_score: int, threshold: int, state: Dict[str, Any]) -> bool:
    loc_state = state["locations"].get(location_name, {})
    last_score = loc_state.get("last_score", -1)
    last_date = loc_state.get("last_alert_date")
    today = date.today()
    delta = (today - parse_date(last_date)).days if last_date else 999
    crossed = last_score < threshold and current_score >= threshold
    return crossed or delta >= 5


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


def dispatch_notification(cfg: Dict[str, Any], message: str) -> None:
    settings = cfg.get("NOTIFICATION_SETTINGS", {})
    provider = str(settings.get("provider") or "").lower()
    if provider == "telegram":
        send_telegram(settings.get("token", ""), settings.get("chat_id_or_recipient", ""), message)
    elif provider == "pushover":
        send_pushover(settings.get("api_token", ""), settings.get("user_key", ""), message)
    elif provider == "twilio":
        send_twilio(settings.get("account_sid", ""), settings.get("auth_token", ""), settings.get("from_number", ""), settings.get("to_number", ""), message)
    else:
        print("[WARN] No active notification provider configured")


def build_alert_message(results: List[Tuple[str, int, str, str]], dashboard_url: str) -> str:
    ranking = sorted(results, key=lambda item: item[1], reverse=True)[:3]
    lines = ["🍄 Weekend Porcini Forecast (Ranked):"]
    for index, (name, score, best_day, status) in enumerate(ranking, 1):
        lines.append(f"{index}. {name} - {score}% ({best_day}) | {status}")
    lines.append("")
    lines.append(f"🌐 Dashboard: {dashboard_url}")
    return "\n".join(lines)


def generate_dashboard_html(cfg: Dict[str, Any], analysis: List[Dict[str, Any]]) -> str:
    rows = []
    for item in analysis:
        rows.append(
            f"<div class='card'><div class='score'>{item['best_score']}%</div><div class='name'>{item['name']}</div><div class='meta'>Best day: {item['best_day']} | Status: {item['status']}</div><div class='meta'>Moisture: {item['soil_moisture']:.2f} m³/m³</div></div>"
        )
    return f"""<!DOCTYPE html>
<html>
<head>
  <meta charset="utf-8" />
  <title>Porcini Tracker Dashboard</title>
  <style>
    body {{ font-family: Arial, sans-serif; background: #111827; color: #e5e7eb; margin: 0; padding: 24px; }}
    .wrap {{ max-width: 960px; margin: 0 auto; }}
    h1 {{ color: #a7f3d0; }}
    .grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(240px, 1fr)); gap: 16px; }}
    .card {{ background: #1f2937; border: 1px solid #374151; border-radius: 12px; padding: 18px; }}
    .score {{ font-size: 2rem; font-weight: bold; color: #a7f3d0; }}
    .name {{ font-size: 1.1rem; margin-top: 6px; font-weight: bold; }}
    .meta {{ color: #cbd5e1; margin-top: 8px; }}
  </style>
</head>
<body>
  <div class="wrap">
    <h1>🍄 Porcini Tracker Dashboard</h1>
    <div class="grid">{''.join(rows)}</div>
  </div>
</body>
</html>
"""


def main() -> int:
    parser = argparse.ArgumentParser(description="Porcini tracker forecast engine")
    parser.add_argument("--test-alert", action="store_true", help="Send a test alert using current notification config")
    args = parser.parse_args()

    cfg = ensure_notification_settings(load_config())
    db = load_or_init_db()
    alert_state = build_alert_state()

    if args.test_alert:
        dispatch_notification(cfg, "🍄 Porcini test alert: notification system is working.")
        return 0

    threshold = int(cfg.get("ALERT_THRESHOLD", 65))
    dashboard_url = cfg.get("FTP_SETTINGS", {}).get("dashboard_url") or cfg.get("DATABASE_SETTINGS", {}).get("dashboard_url") or "https://example.invalid/porcini_report.html"

    analyses: List[Dict[str, Any]] = []
    alert_queue: List[Tuple[str, int, str, str]] = []

    for location in cfg.get("LOCATIONS", []):
        name = location.get("name")
        latitude = float(location.get("latitude"))
        longitude = float(location.get("longitude"))
        elevation = int(location.get("elevation_m", 0))
        harvests = location.get("past_harvests", [])
        records = ensure_location_history(name, db, latitude, longitude, elevation)

        best_score, best_day, status, quality = find_weekend_best(location, records, harvests)
        moisture = 0.0
        if records:
            moisture = float(records[-1].get("soil_moisture_0_to_7cm_mean") or 0.0)

        analysis = {
            "name": name,
            "best_score": best_score,
            "best_day": best_day,
            "status": status or "Monitoring",
            "soil_moisture": moisture,
            "quality": quality,
        }
        analyses.append(analysis)

        if should_alert_for_location(name, best_score, threshold, alert_state):
            alert_queue.append((name, best_score, best_day, status or "Monitoring"))
            alert_state["locations"][name] = {
                "last_alert_date": iso_date(date.today()),
                "last_score": best_score,
                "last_status": status or "Monitoring",
            }

    save_json(DB_PATH, db)
    save_json(ALERT_STATE_PATH, alert_state)

    html = generate_dashboard_html(cfg, analyses)
    REPORT_PATH.write_text(html, encoding="utf-8")

    print("\n### Porcini Summary")
    for item in analyses:
        print(f"- {item['name']}: {item['best_score']}% on {item['best_day']} | {item['status']} | moisture {item['soil_moisture']:.2f} m³/m³")

    if alert_queue:
        message = build_alert_message(alert_queue, dashboard_url)
        print("\n[INFO] Dispatching alert message")
        dispatch_notification(cfg, message)

    return 0


if __name__ == "__main__":
    sys.exit(main())
