#!/usr/bin/env python3
"""
Production Porcini Mushroom Intelligence & Forecasting Engine
Evaluates Boletus edulis probability using multi-year weather archives,
forecasts, soil metrics, microclimates, and online harvest feedback.
Includes a built-in local web server for auto-saving harvest logs.
"""

import argparse
import datetime
import json
import os
import pathlib
import sys
import threading
import webbrowser
from http.server import HTTPServer, SimpleHTTPRequestHandler
import urllib.parse
import requests

CONFIG_FILE = "config.json"
DB_FILE = "porcini_db.json"
STATE_FILE = "alert_state.json"
REPORT_FILE = "porcini_report.html"
PORT = 8080

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
        for idx_w, pw in enumerate(past_14d_weather[1:8], start=1):
            prev_pw = past_14d_weather[idx_w-1]
            temp_drop = prev_pw.get("temperature_2m_max", 15) - pw.get("temperature_2m_max", 15)
            if temp_drop >= 4.0 and pw.get("precipitation_sum", 0) >= 5.0:
                trigger_day_index = idx_w
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
# INTERACTIVE WEB DASHBOARD GENERATOR (AUTO-SAVING)
# ---------------------------------------------------------
def generate_html_dashboard(locations_data, weather_db_all):
    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Porcini Intelligence & Growth Forecasting Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        :root {{
            --bg-color: #12181b;
            --card-bg: #1e262c;
            --accent-green: #2ecc71;
            --accent-gold: #f1c40f;
            --accent-orange: #e67e22;
            --accent-blue: #3498db;
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
        .season-badge {{
            display: inline-block;
            background: linear-gradient(135deg, #f1c40f, #e67e22);
            color: #12181b;
            font-weight: bold;
            padding: 6px 14px;
            border-radius: 20px;
            font-size: 0.9rem;
            margin-top: 10px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.2);
        }}
        .container {{
            max-width: 1400px;
            margin: 0 auto;
        }}
        .grid {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(450px, 1fr));
            gap: 20px;
            margin-bottom: 30px;
        }}
        .card {{
            background-color: var(--card-bg);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 20px;
            box-shadow: 0 4px 6px rgba(0,0,0,0.3);
            margin-bottom: 20px;
        }}
        .score-gauge {{
            font-size: 2.2rem;
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
            height: 280px;
            margin-top: 15px;
        }}
        .legend-help {{
            font-size: 0.8rem;
            color: var(--text-muted);
            margin-top: 8px;
            background: rgba(0,0,0,0.2);
            padding: 8px;
            border-radius: 4px;
        }}
        .form-group {{
            margin-bottom: 12px;
        }}
        label {{
            display: block;
            margin-bottom: 4px;
            color: var(--text-muted);
            font-size: 0.85rem;
        }}
        input, select {{
            width: 100%;
            padding: 8px;
            background: #12181b;
            border: 1px solid var(--border-color);
            color: var(--text-main);
            border-radius: 4px;
            box-sizing: border-box;
        }}
        button {{
            background-color: var(--accent-green);
            color: #12181b;
            font-weight: bold;
            padding: 10px 15px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            width: 100%;
        }}
        button:hover {{
            opacity: 0.9;
        }}
        ul.harvest-list {{
            list-style: none;
            padding: 0;
            margin: 10px 0 0 0;
            font-size: 0.85rem;
            color: var(--text-muted);
        }}
        ul.harvest-list li {{
            background: rgba(0,0,0,0.2);
            padding: 6px 10px;
            border-radius: 4px;
            margin-bottom: 5px;
            display: flex;
            justify-content: space-between;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <h1>🍄 Porcini Intelligence & Growth Forecasting Dashboard</h1>
            <p>Multi-Year Weather Archival, Growth Suitability Curves & Auto-Saving Harvest Logger</p>
            <div id="seasonIndicator"></div>
            <small>Last Generated: {datetime.datetime.utcnow().strftime('%d %B %Y - %H:%M:%S')} UTC</small>
        </header>

        <div class="grid">
    """

    for idx, loc in enumerate(locations_data):
        eval_res = loc["evaluation"]
        harvests = loc.get("past_harvests", [])
        
        harvest_items_html = ""
        for h in harvests:
            harvest_items_html += f"<li><span>{h.get('date')} ({h.get('cap_stage')})</span> <strong>{h.get('yield_tier')}</strong></li>"

        html_content += f"""
            <div class="card">
                <h2>{loc['name']}</h2>
                <p><strong>Coordinates:</strong> {loc['latitude']}, {loc['longitude']} ({loc['elevation_m']}m)</p>
                <div style="display: flex; justify-content: space-between; align-items: center;">
                    <div>
                        <div class="score-gauge">{eval_res['score']}%</div>
                        <div class="status-badge">{eval_res['status']}</div>
                    </div>
                    <div style="text-align: right; font-size: 0.9rem; color: var(--text-muted);">
                        <p><strong>Quality:</strong> {eval_res['quality']}</p>
                        <p><strong>Best Window:</strong> {eval_res['best_weekend_day']} ({eval_res['best_weekend_score']}%)</p>
                    </div>
                </div>

                <hr style="border-color: var(--border-color); margin: 20px 0;">

                <h3>🌤️ Micro-Climate & Growth Prediction Timeline</h3>
                <div class="chart-container">
                    <canvas id="weatherChart_{idx}"></canvas>
                </div>
                <div class="legend-help">
                    💡 <strong>How to read:</strong> The green filled curve shows the <strong>Growth Suitability Score (0–100%)</strong>. Spikes above 60% indicate prime flush windows or predicted growth periods driven by recent rain and temperature drops.
                </div>
                
                <hr style="border-color: var(--border-color); margin: 20px 0;">
                
                <h3>🍄 Log Historical / Recent Find</h3>
                <div class="form-group">
                    <label>Date Found:</label>
                    <input type="date" id="date_{idx}" value="{datetime.date.today().strftime('%Y-%m-%d')}">
                </div>
                <div class="form-group">
                    <label>Cap Stage:</label>
                    <select id="stage_{idx}">
                        <option value="buttons_young">Buttons / Young (+20 Boost / Flush Trigger)</option>
                        <option value="prime_open">Prime Open Caps</option>
                        <option value="old_overripe">Old / Overripe (-25 Penalty / End)</option>
                    </select>
                </div>
                <div class="form-group">
                    <label>Yield Tier:</label>
                    <select id="yield_{idx}">
                        <option value="small">Small (Few mushrooms)</option>
                        <option value="medium">Medium (Basket)</option>
                        <option value="large">Large (Full haul)</option>
                    </select>
                </div>
                <button onclick="addHarvest({idx})">Save Harvest & Recalculate Scores</button>
                
                <h4 style="margin-top: 15px; margin-bottom: 5px;">Current Logged Finds:</h4>
                <ul class="harvest-list" id="list_{idx}">
                    {harvest_items_html if harvest_items_html else "<li>No past finds logged yet.</li>"}
                </ul>
            </div>
        """

    html_content += f"""
        </div>

        <div class="card">
            <h2>📈 Multi-Year Backtest & Mycelium Probability Timeline</h2>
            <div class="chart-container" style="height: 350px;">
                <canvas id="porciniChart"></canvas>
            </div>
        </div>
    </div>

    <script>
        const rawLocations = {json.dumps(locations_data)};
        const allWeatherData = {json.dumps(weather_db_all)};

        const currentMonthNum = new Date().getMonth(); 
        const seasonIndicatorEl = document.getElementById('seasonIndicator');
        if (currentMonthNum === 8 || currentMonthNum === 9) {{
            seasonIndicatorEl.innerHTML = '<span class="season-badge">🌟 Peak Porcini Season (September & October Active)</span>';
        }} else {{
            seasonIndicatorEl.innerHTML = '<span style="color: var(--text-muted); font-size: 0.85rem;">Off-Peak / Shoulder Season (Prime months: September & October)</span>';
        }}

        const dateformatter = new Intl.DateTimeFormat('en-GB', {{ 
            weekday: 'short', 
            day: 'numeric', 
            month: 'long' 
        }});

        rawLocations.forEach((loc, idx) => {{
            const wData = allWeatherData || {{}};
            const dailyHistory = loc.evaluation.daily_history || {{}};
            const dates = Object.keys(wData).sort();
            
            const recentDates = dates.slice(-52);
            const formattedLabels = recentDates.map(dStr => {{
                const d = new Date(dStr);
                return isNaN(d) ? dStr : dateformatter.format(d);
            }});

            const temps = recentDates.map(d => wData[d] ? wData[d].temperature_2m_max : 15);
            const rains = recentDates.map(d => wData[d] ? wData[d].precipitation_sum : 0);
            const scores = recentDates.map(d => dailyHistory[d] ? dailyHistory[d].score : 0);

            const ctxW = document.getElementById('weatherChart_' + idx).getContext('2d');
            new Chart(ctxW, {{
                type: 'line',
                data: {{
                    labels: formattedLabels,
                    datasets: [
                        {{
                            label: 'Growth Suitability (%)',
                            data: scores,
                            borderColor: '#2ecc71',
                            backgroundColor: 'rgba(46, 204, 113, 0.15)',
                            fill: true,
                            yAxisID: 'yScore',
                            tension: 0.2,
                            pointRadius: 2
                        }},
                        {{
                            label: 'Max Temp (°C)',
                            data: temps,
                            borderColor: '#e67e22',
                            backgroundColor: 'transparent',
                            yAxisID: 'yTemp',
                            tension: 0.2,
                            pointRadius: 0
                        }},
                        {{
                            label: 'Precipitation (mm)',
                            data: rains,
                            type: 'bar',
                            backgroundColor: 'rgba(52, 152, 219, 0.4)',
                            yAxisID: 'yRain',
                            barPercentage: 0.6
                        }}
                    ]
                }},
                options: {{
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {{
                        yScore: {{
                            type: 'linear',
                            position: 'left',
                            min: 0,
                            max: 100,
                            grid: {{ color: '#2c3e50' }},
                            ticks: {{ color: '#2ecc71', font: {{ size: 10 }} }}
                        }},
                        yTemp: {{
                            type: 'linear',
                            position: 'right',
                            grid: {{ display: false }},
                            ticks: {{ color: '#e67e22', font: {{ size: 10 }} }}
                        }},
                        yRain: {{
                            type: 'linear',
                            position: 'right',
                            display: false,
                            beginAtZero: true,
                            max: 100
                        }},
                        x: {{
                            grid: {{ color: '#2c3e50' }},
                            ticks: {{ color: '#95a5a6', font: {{ size: 10 }}, maxTicksLimit: 6 }}
                        }}
                    }},
                    plugins: {{
                        legend: {{ labels: {{ color: '#ecf0f1', boxWidth: 12, font: {{ size: 11 }} }} }}
                    }}
                }}
            }});
        }});

        function addHarvest(locIdx) {{
            const date = document.getElementById('date_' + locIdx).value;
            const cap_stage = document.getElementById('stage_' + locIdx).value;
            const yield_tier = document.getElementById('yield_' + locIdx).value;

            fetch('/api/add_harvest', {{
                method: 'POST',
                headers: {{ 'Content-Type': 'application/json' }},
                body: JSON.stringify({{ location_index: locIdx, date, cap_stage, yield_tier }})
            }})
            .then(res => res.json())
            .then(data => {{
                if (data.status === 'success') {{
                    alert('Harvest saved! Recalculating scores and refreshing page...');
                    location.reload();
                }} else {{
                    alert('Error saving harvest: ' + data.message);
                }}
            }})
            .catch(err => {{
                alert('Connection error: ' + err);
            }});
        }}

        const firstLocEval = rawLocations[0].evaluation.daily_history;
        const rawLabels = Object.keys(firstLocEval).sort();
        const formattedMainLabels = rawLabels.map(dStr => {{
            const d = new Date(dStr);
            return isNaN(d) ? dStr : dateformatter.format(d);
        }});
        const scoreData = rawLabels.map(d => firstLocEval[d].score);

        const ctx = document.getElementById('porciniChart').getContext('2d');
        const porciniChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: formattedMainLabels,
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
                    y: {{ beginAtZero: true, max: 100, grid: {{ color: '#2c3e50' }}, ticks: {{ color: '#95a5a6' }} }},
                    x: {{ grid: {{ color: '#2c3e50' }}, ticks: {{ color: '#95a5a6' }} }}
                }},
                plugins: {{
                    legend: {{ labels: {{ color: '#ecf0f1' }} }}
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
# LOCAL WEB SERVER HANDLER FOR AUTO-SAVING
# ---------------------------------------------------------
class PorciniServerHandler(SimpleHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "":
            self.path = "/" + REPORT_FILE
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/add_harvest":
            content_length = int(self.headers.get('Content-Length', 0))
            post_data = self.rfile.read(content_length)
            try:
                payload = json.loads(post_data.decode('utf-8'))
                loc_idx = payload.get("location_index")
                new_entry = {
                    "date": payload.get("date"),
                    "cap_stage": payload.get("cap_stage"),
                    "yield_tier": payload.get("yield_tier")
                }

                config = load_json(CONFIG_FILE, {})
                locations = config.get("LOCATIONS", [])
                
                if 0 <= loc_idx < len(locations):
                    if "past_harvests" not in locations[loc_idx]:
                        locations[loc_idx]["past_harvests"] = []
                    locations[loc_idx]["past_harvests"].append(new_entry)
                    save_json(CONFIG_FILE, config)

                    # Re-run evaluation & regenerate HTML immediately
                    weather_db = load_json(DB_FILE, {})
                    for loc in locations:
                        loc["evaluation"] = evaluate_location(loc, weather_db)
                    generate_html_dashboard(locations, weather_db)

                    self.send_response(200)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(json.dumps({"status": "success"}).encode('utf-8'))
                    return
            except Exception as e:
                print(f"Error handling harvest POST: {e}")

            self.send_response(400)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"status": "error", "message": "Invalid request"}).encode('utf-8'))
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        # Suppress noisy server logs for clean terminal output
        pass

def run_server():
    server_address = ('127.0.0.1', PORT)
    httpd = HTTPServer(server_address, PorciniServerHandler)
    url = f"http://127.0.0.1:{PORT}"
    print(f"🚀 Porcini Interactive Server started at {url}")
    print("Press Ctrl+C in your terminal to stop the server.")
    threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    httpd.serve_forever()

# ---------------------------------------------------------
# MAIN EXECUTION
# ---------------------------------------------------------
def main():
    parser = argparse.ArgumentParser(description="Porcini Mushroom Intelligence Engine")
    parser.add_argument("--test-alert", action="store_true", help="Dispatch test notification immediately")
    parser.add_argument("--server", action="store_true", help="Launch interactive local web server with auto-save")
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
        return

    if args.server or len(sys.argv) == 1:
        run_server()
    else:
        print("Porcini engine execution completed successfully.")

if __name__ == "__main__":
    main()
