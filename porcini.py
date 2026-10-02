#!/usr/bin/env python3
"""
Production Porcini Mushroom Intelligence & Forecasting Engine
Evaluates Boletus edulis probability using multi-year weather archives,
forecasts, soil metrics, microclimates, and online harvest feedback.
"""

import argparse
import datetime
import ftplib
import json
import math
import os
import pathlib
import sys
import requests

CONFIG_FILE = "config.json"
DB_FILE = "porcini_db.json"
STATE_FILE = "alert_state.json"
REPORT_FILE = "porcini_report.html"

# ---------------------------------------------------------
# UTILS & HELPERS
# ---------------------------------------------------------
def load_json(filepath, default=None):
    path = pathlib.Path(filepath)
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warning: Error reading {filepath}: {e}")
    return default if default is not None else {}

def save_json(filepath, data):
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def get_lunar_phase(date_obj):
    known_new_moon = datetime.date(2000, 1, 6)
    delta = (date_obj - known_new_moon).days
    lunation = 29.53058867
    phase = (delta % lunation) / lunation
    return phase

# ---------------------------------------------------------
# WEATHER DATA FETCHING & STITCHING
# ---------------------------------------------------------
def fetch_location_weather(lat, lon, elevation, db_records):
    today = datetime.date.today()
    
    if not db_records:
        start_date = today - datetime.timedelta(days=730)
    else:
        latest_archived = max(db_records.keys())
        latest_date = datetime.datetime.strptime(latest_archived, "%Y-%m-%d").date()
        start_date = latest_date + datetime.timedelta(days=1)
        
    archive_end_date = today - datetime.timedelta(days=6)
    new_records = {}
    
    if start_date <= archive_end_date:
        archive_url = "https://archive-api.open-meteo.com/v1/archive"
        params = {
            "latitude": lat,
            "longitude": lon,
            "elevation": elevation,
            "start_date": start_date.strftime("%Y-%m-%d"),
            "end_date": archive_end_date.strftime("%Y-%m-%d"),
            "daily": [
                "precipitation_sum",
                "temperature_2m_max",
                "temperature_2m_min",
                "wind_speed_10m_max"
            ],
            "timezone": "UTC"
        }
        try:
            res = requests.get(archive_url, params=params, timeout=30)
            if res.status_code == 200:
                data = res.json().get("daily", {})
                dates = data.get("time", [])
                for i, d in enumerate(dates):
                    new_records[d] = {
                        "precipitation_sum": data.get("precipitation_sum", [])[i] or 0.0,
                        "temperature_2m_max": data.get("temperature_2m_max", [])[i] or 15.0,
                        "temperature_2m_min": data.get("temperature_2m_min", [])[i] or 5.0,
                        "wind_speed_10m_max": data.get("wind_speed_10m_max", [])[i] or 10.0,
                        "soil_temperature_0_to_10cm": 12.0,
                        "relative_humidity_2m_mean": 75.0,
                        "soil_moisture_0_to_7cm_mean": 0.25
                    }
            else:
                print(f"Archive API returned status {res.status_code}: {res.text}")
        except Exception as e:
            print(f"Error fetching archive data for ({lat}, {lon}): {e}")

    forecast_url = "https://api.open-meteo.com/v1/forecast"
    f_params = {
        "latitude": lat,
        "longitude": lon,
        "elevation": elevation,
        "past_days": 7,
        "forecast_days": 7,
        "daily": [
            "precipitation_sum",
            "temperature_2m_max",
            "temperature_2m_min",
            "wind_speed_10m_max",
            "soil_temperature_0_to_10cm",
            "relative_humidity_2m_mean",
            "soil_moisture_0_to_7cm_mean"
        ],
        "timezone": "UTC"
    }
    try:
        res = requests.get(forecast_url, params=f_params, timeout=30)
        if res.status_code == 200:
            data = res.json().get("daily", {})
            dates = data.get("time", [])
            for i, d in enumerate(dates):
                # If date already exists from archive, merge or update with forecast details
                if d not in new_records:
                    new_records[d] = {}
                new_records[d].update({
                    "precipitation_sum": data.get("precipitation_sum", [])[i] or 0.0,
                    "temperature_2m_max": data.get("temperature_2m_max", [])[i] or 15.0,
                    "temperature_2m_min": data.get("temperature_2m_min", [])[i] or 5.0,
                    "wind_speed_10m_max": data.get("wind_speed_10m_max", [])[i] or 10.0,
                    "soil_temperature_0_to_10cm": data.get("soil_temperature_0_to_10cm", [])[i] or 12.0,
                    "relative_humidity_2m_mean": data.get("relative_humidity_2m_mean", [])[i] or 75.0,
                    "soil_moisture_0_to_7cm_mean": data.get("soil_moisture_0_to_7cm_mean", [])[i] or 0.25
                })
    except Exception as e:
        print(f"Error fetching forecast data for ({lat}, {lon}): {e}")

    db_records.update(new_records)
    return db_records

# ---------------------------------------------------------
# ADVANCED SCORING ENGINE
# ---------------------------------------------------------
def evaluate_location(loc, weather_db):
    sorted_dates = sorted(weather_db.keys())
    if not sorted_dates:
        return {"score": 0, "status": "NO DATA", "quality": "UNKNOWN", "best_weekend_score": 0, "best_weekend_day": "N/A", "daily_history": {}}

    today_str = datetime.date.today().strftime("%Y-%m-%d")
    daily_scores = {}
    past_harvests = loc.get("past_harvests", [])

    for d_str in sorted_dates:
        d_obj = datetime.datetime.strptime(d_str, "%Y-%m-%d").date()
        w = weather_db[d_str]
        
        score = 0.0
        status = "DORMANT / INACTIVE"
        quality = "PRIME QUALITY"

        past_14d_keys = [(d_obj - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(15)]
        past_14d_weather = [weather_db[k] for k in past_14d_keys if k in weather_db]

        past_120d_keys = [(d_obj - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(120)]
        rain_120d = sum(weather_db[k].get("precipitation_sum", 0) for k in past_120d_keys if k in weather_db)
        is_severe_drought = rain_120d < 100.0

        past_7d_rain = sum(weather_db[k].get("precipitation_sum", 0) for k in past_14d_keys[:7] if k in weather_db)
        required_rain = 45.0 if is_severe_drought else 20.0

        drought_broken = False
        if is_severe_drought and past_7d_rain >= required_rain:
            drought_broken = True

        if is_severe_drought and past_7d_rain < required_rain and past_7d_rain > 5:
            daily_scores[d_str] = {"score": 20.0, "status": "TOO DRY - DROUGHT UNBROKEN", "quality": "UNKNOWN"}
            continue

        frost_days = sum(1 for pw in past_14d_weather[:7] if pw.get("temperature_2m_min", 5) < -2.0 or pw.get("soil_temperature_0_to_10cm", 12) < 3.0)
        if frost_days >= 2:
            daily_scores[d_str] = {"score": 0.0, "status": "SEASON TERMINATED BY FROST", "quality": "FROST-DAMAGED"}
            continue

        soil_ph = loc.get("soil_pH", "acidic")
        host_max_pts = 15 if soil_ph != "alkaline" else 5

        month = d_obj.month
        if month in [8, 9, 10, 11] or (month == 12 and d_obj.day <= 1):
            soil_temp_7d = sum(pw.get("soil_temperature_0_to_10cm", 12) for pw in past_14d_weather[:7]) / max(1, len(past_14d_weather[:7]))
            if soil_temp_7d > 20.0:
                score += 0
                status = "SEASON DELAYED BY HIGH SOIL TEMP"
            elif 10.0 <= soil_temp_7d <= 18.0:
                score += 15.0

        trigger_day_index = -1
        for idx, pw in enumerate(past_14d_weather[1:8], start=1):
            prev_pw = past_14d_weather[idx-1]
            temp_drop = prev_pw.get("temperature_2m_max", 15) - pw.get("temperature_2m_max", 15)
            if temp_drop >= 4.0 and pw.get("precipitation_sum", 0) >= 5.0:
                trigger_day_index = idx
                break

        if trigger_day_index != -1:
            days_since_trigger = trigger_day_index
            if 7 <= days_since_trigger <= 12:
                score += 45.0
                status = "PRIME HARVEST WINDOW"
                if drought_broken:
                    score += 15.0
                    status = "🔥 DROUGHT BROKEN - SUPER-FLUSH"
            elif 1 <= days_since_trigger <= 6:
                score += 25.0
                status = f"FLUSH DEVELOPING (Peak in {8-days_since_trigger}d)"
            elif 13 <= days_since_trigger <= 16:
                score += 15.0
                status = "LATE SEASON / OLD CAPS"

        trees = loc.get("tree_species", [])
        tree_pts = 0
        if "Spruce" in trees: tree_pts = max(tree_pts, 15)
        if "Beech" in trees: tree_pts = max(tree_pts, host_max_pts)
        if "Oak" in trees: tree_pts = max(tree_pts, 12)
        if "Pine" in trees: tree_pts = max(tree_pts, 10)
        score += min(host_max_pts, tree_pts)

        current_soil_t = w.get("soil_temperature_0_to_10cm", 12)
        if 12.0 <= current_soil_t <= 17.0:
            score += 15.0

        soil_m = w.get("soil_moisture_0_to_7cm_mean", 0.25)
        if soil_m > 0.35:
            score += 10.0
        elif soil_m < 0.18:
            score -= 15.0

        avg_hum = w.get("relative_humidity_2m_mean", 75)
        if avg_hum < 60.0:
            score -= 15.0
            quality = "DRY LEATHER CAPS"

        if w.get("wind_speed_10m_max", 10) > 30.0:
            score -= 10.0

        fly_date_str = loc.get("last_seen_fly_agaric")
        if fly_date_str:
            try:
                fly_date = datetime.datetime.strptime(fly_date_str, "%Y-%m-%d").date()
                if 0 <= (d_obj - fly_date).days <= 10:
                    score += 15.0
            except:
                pass

        phase = get_lunar_phase(d_obj)
        if 0.35 <= phase <= 0.65:
            score += 5.0

        for h in past_harvests:
            try:
                h_date = datetime.datetime.strptime(h.get("date"), "%Y-%m-%d").date()
                diff_days = (d_obj - h_date).days
                if 1 <= diff_days <= 4 and h.get("cap_stage") == "buttons_young":
                    score += 20.0
                    status = "CONFIRMED ACTIVE FLUSH"
                elif 1 <= diff_days <= 7 and h.get("cap_stage") == "old_overripe":
                    score -= 25.0
                    status = "🍂 EXHAUSTION / POST-FLUSH COOLING OFF"
            except:
                pass

        medium_large_count = sum(1 for h in past_harvests if h.get("yield_tier") in ["medium", "large"])
        if medium_large_count >= 2:
            score += 10.0

        max_t = w.get("temperature_2m_max", 15)
        if max_t > 18.0:
            quality = "⚠️ HIGH MAGGOT RISK"
        elif 8.0 <= max_t <= 15.0:
            quality = "💎 PRIME QUALITY"

        score = max(0.0, min(100.0, score))
        daily_scores[d_str] = {"score": round(score, 1), "status": status, "quality": quality}

    target_date = datetime.datetime.strptime(today_str, "%Y-%m-%d").date() if today_str in daily_scores else datetime.date.today()
    
    weekend_scores = {}
    best_wknd_score = 0
    best_wknd_day = "N/A"
    
    for i in range(10):
        check_d = target_date + datetime.timedelta(days=i)
        check_str = check_d.strftime("%Y-%m-%d")
        if check_str in daily_scores and check_d.weekday() in [4, 5, 6]:
            s = daily_scores[check_str]["score"]
            weekend_scores[check_str] = s
            if s > best_wknd_score:
                best_wknd_score = s
                best_wknd_day = check_d.strftime("%A (%Y-%m-%d)")

    current_eval = daily_scores.get(today_str, {"score": 0, "status": "DORMANT", "quality": "UNKNOWN"})
    
    return {
        "score": current_eval["score"],
        "status": current_eval["status"],
        "quality": current_eval["quality"],
        "best_weekend_score": best_wknd_score,
        "best_weekend_day": best_wknd_day,
        "daily_history": daily_scores
    }

# ---------------------------------------------------------
# INTERACTIVE WEB DASHBOARD GENERATOR (`porcini_report.html`)
# ---------------------------------------------------------
def generate_html_dashboard(locations_data, weather_db_all):
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Porcini Intelligence & Mycology Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-color: #12181b;
            --card-bg: #1e262c;
            --accent-green: #2ecc71;
            --accent-gold: #f1c40f;
            --accent-orange: #e67e22;
            --text-main: #ecf0f1;
            --text-muted: #95a5a6;
            --border-color: #2c3e50;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif;
            background-color: var(--bg-color);
            color: var(--text-main);
            margin: 0;
            padding: 20px;
        }}
        header {{
            text-align: center;
            margin-bottom: 30px;
        }}
        h1 {{
            color: var(--accent-green);
            margin-bottom: 5px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(320px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
        }}
        .score-gauge {{
            font-size: 2.5rem;
            font-weight: bold;
            color: var(--accent-green);
        }}
        .status-badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 4px;
            font-size: 0.85rem;
            font-weight: bold;
            background-color: rgba(46, 204, 113, 0.2);
            color: var(--accent-green);
            margin-top: 10px;
        }}
        .chart-container {{
            position: relative;
            height: 400px;
            margin-top: 20px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🍄 Porcini Intelligence & Mycology Dashboard</h1>
            <p>Automated Multi-Year Weather Archival & Probability Analysis</p>
            <small>Last Generated: {datetime.datetime.utcnow().strftime('%Y-%m-%d %H:%M:%S')} UTC</small>
        </header>

        <div class="grid">
    """

    for loc in locations_data:
        eval_res = loc["evaluation"]
        html_content += f"""
            <div class="card">
                <h2>{loc['name']}</h2>
                <p><strong>Coordinates:</strong> {loc['latitude']}, {loc['longitude']} ({loc['elevation_m']}m)</p>
                <div class="score-gauge">{eval_res['score']}%</div>
                <div class="status-badge">{eval_res['status']}</div>
                <p style="margin-top: 15px;"><strong>Quality Indicator:</strong> {eval_res['quality']}</p>
                <p><strong>Best Weekend Window:</strong> {eval_res['best_weekend_day']} ({eval_res['best_weekend_score']}%)</p>
            </div>
        """

    html_content += f"""
        </div>

        <div class="card">
            <h2>📈 Multi-Year Backtest & Probability Timeline</h2>
            <div class="chart-container">
                <canvas id="porciniChart"></canvas>
            </div>
        </div>
    </div>

    <script>
        const rawLocations = {json.dumps(locations_data)};
        const firstLocEval = rawLocations[0].evaluation.daily_history;
        const labels = Object.keys(firstLocEval).sort();
        const scoreData = labels.map(d => firstLocEval[d].score);

        const ctx = document.getElementById('porciniChart').getContext('2d');
        const porciniChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: labels,
                datasets: [{{
                    label: 'Suitability Score (%)',
                    data: scoreData,
                    borderColor: '#2ecc71',
                    backgroundColor: 'rgba(46, 204, 113, 0.1)',
                    fill: true,
                    tension: 0.2,
                    pointRadius: 0,
                    pointHoverRadius: 6
                }}]
            }},
            options: {{
                responsive: true,
                maintainAspectRatio: false,
                scales: {{
                    y: {{ beginAtZero: true, max: 100, grid: {{ color: '#2c3e50' }} }},
                    x: {{ grid: {{ color: '#2c3e50' }} }}
                }}
            }}
        }});
    </script>
</body>
</html>
    """
    with open(REPORT_FILE, "w", encoding="utf-8") as f:
        f.write(html_content)

# ---------------------------------------------------------
# NOTIFICATION MODULE
# ---------------------------------------------------------
def send_alert(config, locations, is_test=False):
    notif = config.get("NOTIFICATION_SETTINGS", {})
    provider = notif.get("provider", "telegram").lower()
    threshold = config.get("ALERT_THRESHOLD", 65)

    state = load_json(STATE_FILE, {})
    today_str = datetime.date.today().strftime("%Y-%m-%d")

    msg_lines = ["🍄 Weekend Porcini Forecast (Ranked):"]
    top_score = 0
    
    for i, loc in enumerate(sorted(locations, key=lambda x: x["evaluation"]["best_weekend_score"], reverse=True), 1):
        ev = loc["evaluation"]
        msg_lines.append(f"{i}. {loc['name']} - {ev['score']}% (Best: {ev['best_weekend_day']} | {ev['best_weekend_score']}%) | {ev['status']}")
        if ev['best_weekend_score'] > top_score:
            top_score = ev['best_weekend_score']

    message = "\n".join(msg_lines)

    if not is_test and top_score < threshold:
        print(f"Top weekend score ({top_score}%) is below threshold ({threshold}%). Skipping alert.")
        return

    if provider == "telegram":
        token = notif.get("telegram_token")
        chat_id = notif.get("telegram_chat_id")
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": chat_id, "text": message}
        try:
            res = requests.post(url, json=payload, timeout=15)
            if res.status_code == 200:
                print("Telegram alert dispatched successfully.")
            else:
                print(f"Telegram alert failed: {res.text}")
        except Exception as e:
            print(f"Telegram notification error: {e}")

    state["last_alert_date"] = today_str
    save_json(STATE_FILE, state)

# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Porcini Mushroom Intelligence Engine")
    parser.add_argument("--test-alert", action="store_true", help="Dispatch test notification immediately")
    args = parser.parse_args()

    config = load_json(CONFIG_FILE, {})
    if not config:
        print("Error: config.json not found.")
        sys.exit(1)

    weather_db = load_json(DB_FILE, {})
    locations = config.get("LOCATIONS", [])
    
    for loc in locations:
        weather_db = fetch_location_weather(loc["latitude"], loc["longitude"], loc["elevation_m"], weather_db)
        loc["evaluation"] = evaluate_location(loc, weather_db)

    save_json(DB_FILE, weather_db)
    generate_html_dashboard(locations, weather_db)

    if args.test_alert:
        send_alert(config, locations, is_test=True)
    else:
        send_alert(config, locations, is_test=False)

    print("Porcini engine execution completed successfully.")

if __name__ == "__main__":
    main()
