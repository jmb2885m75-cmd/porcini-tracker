#!/usr/bin/env python3
import json
import os
import sys
import argparse
import urllib.request
import datetime
from pathlib import Path
from ftplib import FTP, FTP_TLS

# --- CONFIGURATION & PATHS ---
CONFIG_FILE = "config.json"
DB_FILE = "porcini_db.json"
STATE_FILE = "alert_state.json"
OUTPUT_HTML = "index.html"
INSTALL_DOC = "INSTALL.md"
WORKFLOW_DIR = ".github/workflows"
WORKFLOW_FILE = os.path.join(WORKFLOW_DIR, "porcini_tracker.yml")

DEFAULT_CONFIG = {
    "ALERT_THRESHOLD": 65,
    "DATABASE_SETTINGS": {
        "endpoint_url": "https://jmb2885m75-cmd.github.io/porcini-tracker/api.php"
    },
    "FTP_SETTINGS": {
        "enabled": False,
        "use_tls": True,
        "host": "ftps.yourserver.com",
        "username": "your_username",
        "password": "your_password",
        "port": 21,
        "remote_path": "/public_html/porcini_report.html",
        "dashboard_url": "https://yourdomain.com/porcini_report.html"
    },
    "NOTIFICATION_SETTINGS": {
        "provider": "telegram",  # "telegram", "pushover", "twilio", or "none"
        "token": "",
        "chat_id_or_recipient": ""
    },
    "LOCATIONS": [
        {
            "name": "East Berlin Pine & Oak Ridge",
            "latitude": 52.5200,
            "longitude": 13.4050,
            "elevation_m": 50,
            "tree_species": ["Spruce", "Beech", "Pine"],
            "tree_density": "dense_conifer",
            "aspect": "north",
            "soil_pH": "acidic",
            "past_harvests": [
                {
                    "date": "2026-09-15",
                    "yield_tier": "large",
                    "cap_stage": "prime",
                    "weight_g": 450,
                    "notes": "Massive flush after heavy autumn rain."
                }
            ],
            "last_seen_fly_agaric": "2026-09-28"
        }
    ]
}

def load_json(filename, default=None):
    if default is None:
        default = {}
    if os.path.exists(filename):
        try:
            with open(filename, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print(f"Warnung: Konnte {filename} nicht laden ({e}), verwende Standardwerte.")
    return default

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def ensure_config_and_docs():
    if not os.path.exists(CONFIG_FILE):
        save_json(CONFIG_FILE, DEFAULT_CONFIG)
        print(f"'{CONFIG_FILE}' wurde mit Standardwerten erstellt.")
    
    # Automatische Erstellung von INSTALL.md
    install_content = """# 🍄 Porcini Tracker - Installation & Configuration Guide

Welcome to the automated Porcini Mushroom Forecasting Dashboard repository (`jmb2885m75-cmd/porcini-tracker`).

## 1. Repository Setup
1. Ensure your repository is named `porcini-tracker` under your GitHub account (`jmb2885m75-cmd`).
2. Go to **Settings > Actions > General**, scroll to **Workflow permissions**, and select **Read and write permissions**.

## 2. Configuration (`config.json`)
Modify `config.json` to configure your GPS locations, tree species, soil properties, and notification provider credentials.

## 3. GitHub Secrets
To enable automated cloud execution and push/SMS notifications, add the following secrets under **Settings > Secrets and variables > Actions**:
- `GH_PAT`: A Personal Access Token with repo permissions.
- `NOTIFICATION_TOKEN`: API token for Telegram/Pushover/Twilio (if enabled).

## 4. Manual Testing
You can test notifications directly from your local environment or via workflow dispatch using:
`python porcini.py --test-alert`
"""
    with open(INSTALL_DOC, "w", encoding="utf-8") as f:
        f.write(install_content)

    # Automatische Erstellung des GitHub Actions Workflow
    os.makedirs(WORKFLOW_DIR, exist_ok=True)
    workflow_content = """name: 🍄 Auto-update Porcini Weather DB & Report

on:
  schedule:
    - cron: '0 6 * * *'  # Runs daily at 06:00 UTC
  workflow_dispatch:

jobs:
  update-forecast:
    runs-on: ubuntu-latest
    permissions:
      contents: write
    steps:
      - name: Checkout repository
        uses: actions/checkout@v4

      - name: Set up Python
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: Run Porcini Engine
        env:
          NOTIFICATION_TOKEN: ${{ secrets.NOTIFICATION_TOKEN }}
        run: |
          python porcini.py

      - name: Commit and push updated databases
        run: |
          git config --global user.name "github-actions[bot]"
          git config --global user.email "41898282+github-actions[bot]@users.noreply.github.com"
          git add porcini_db.json alert_state.json index.html INSTALL.md
          git diff --staged --quiet || git commit -m "🍄 Auto-update Porcini weather DB & report [skip ci]"
          git push
"""
    if not os.path.exists(WORKFLOW_FILE):
        with open(WORKFLOW_FILE, "w", encoding="utf-8") as f:
            f.write(workflow_content)

def fetch_weather_archive_and_forecast(lat, lon, elevation, existing_data=None):
    today = datetime.date.today()
    
    # Wenn keine Historie vorexistiert -> 730 Tage (2 Jahre) Backfill
    if not existing_data or not existing_data.get("time"):
        start_date = (today - datetime.timedelta(days=730)).strftime("%Y-%m-%d")
    else:
        # Inkrementelles Update ab dem letzten bekannten Datum
        last_date_str = existing_data["time"][-1]
        last_date = datetime.datetime.strptime(last_date_str, "%Y-%m-%d").date()
        start_date = (last_date + datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        if start_date >= today.strftime("%Y-%m-%d"):
            # Bereits aktuell, hole nur Forecast
            start_date = (today - datetime.timedelta(days=5)).strftime("%Y-%m-%d")

    end_date = today.strftime("%Y-%m-%d")
    
    archive_url = (
        f"https://archive-api.open-meteo.com/v1/archive?"
        f"latitude={lat}&longitude={lon}&elevation={elevation}"
        f"&start_date={start_date}&end_date={end_date}"
        f"&daily=temperature_2m_max,temperature_2m_min,precipitation_sum,wind_speed_10m_max,soil_temperature_0_to_10cm,relative_humidity_2m_mean,soil_moisture_0_to_7cm_mean"
        f"&timezone=Europe/Berlin"
    )

    try:
        req = urllib.request.urlopen(archive_url)
        data = json.loads(req.read().decode("utf-8"))
        daily = data.get("daily", {})
        
        if existing_data and existing_data.get("time"):
            # Zusammenführen ohne Duplikate
            existing_times = set(existing_data["time"])
            new_times = daily.get("time", [])
            
            merged = {k: list(v) for k, v in existing_data.items()}
            for i, t in enumerate(new_times):
                if t not in existing_times:
                    merged["time"].append(t)
                    for key in ["temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max", "soil_temperature_0_to_10cm", "relative_humidity_2m_mean", "soil_moisture_0_to_7cm_mean"]:
                        vals = daily.get(key, [])
                        merged[key].append(vals[i] if i < len(vals) else None)
            return merged
        else:
            return daily
    except Exception as e:
        print(f"Fehler beim Abrufen der Wetterdaten für ({lat}, {lon}): {e}")
        return existing_data or {}

def get_moon_phase(date_obj):
    known_new_moon = datetime.date(2026, 1, 19)
    days_diff = (date_obj.date() - known_new_moon).days
    cycle = 29.53058867
    phase = (days_diff % cycle) / cycle
    return phase

def get_moon_description(phase):
    if phase < 0.03 or phase > 0.97:
        return "Neumond (Starker Myzel-Impuls)"
    elif 0.45 < phase < 0.55:
        return "Vollmond (Fruchtkörper-Schub)"
    elif 0.35 <= phase <= 0.65:
        return "Zunehmender/Vollmond-Fenster (Optimum)"
    elif phase < 0.5:
        return "Zunehmender Mond"
    else:
        return "Abnehmender Mond"

def calculate_advanced_score(location, weather_data):
    times = weather_data.get("time", [])
    t_max = weather_data.get("temperature_2m_max", [])
    t_min = weather_data.get("temperature_2m_min", [])
    rain = weather_data.get("precipitation_sum", [])
    soil_temp = weather_data.get("soil_temperature_0_to_10cm", [])
    soil_moisture = weather_data.get("soil_moisture_0_to_7cm_mean", [])
    humidity = weather_data.get("relative_humidity_2m_mean", [])
    wind = weather_data.get("wind_speed_10m_max", [])

    if not times or len(times) < 14:
        return 50, "Warte auf Wetterdaten...", "Normal", "Prüfe..."

    # Aktuelle Indikatoren (letzter Tag)
    latest_soil_t = soil_temp[-1] if soil_temp and soil_temp[-1] is not None else 14.0
    latest_soil_m = soil_moisture[-1] if soil_moisture and soil_moisture[-1] is not None else 0.25

    # A. Frost Gate
    recent_min_air = [t for t in t_min[-7:] if t is not None]
    if recent_min_air and any(t < -2.0 for t in recent_min_air):
        return 0, "SEASON TERMINATED BY FROST", "Frost", "Gefahr von Frostschäden"

    # B. Soil pH Gate
    soil_pH = location.get("soil_pH", "acidic")
    host_max_pts = 15 if soil_pH == "acidic" else 5

    # C. Seasonality & Soil Temp Gate
    score = 40  # Basiswert
    current_month = datetime.date.today().month
    if current_month in [8, 9, 10, 11]:
        score += 15

    if latest_soil_t > 20.0:
        return 0, "SEASON DELAYED BY HIGH SOIL TEMP", "Hitze-Dämpfung", "Boden zu warm"

    # D. Drought Deficit & Hydrophobicity (120 Tage)
    recent_rain_120 = sum([r for r in rain[-120:] if r is not None])
    is_severe_drought = recent_rain_120 < 100.0
    recent_rain_7 = sum([r for r in rain[-7:] if r is not None])

    drought_break_met = True
    if is_severe_drought:
        if recent_rain_7 < 45.0:
            return 20, "TOO DRY - DROUGHT UNBROKEN", "Trockenheit", "Boden hydrophob, Regen reicht nicht"
        else:
            score += 15  # Super-Flush Bonus

    # E. Volumetric Soil Moisture
    if latest_soil_m > 0.35:
        score += 10
    elif latest_soil_m < 0.18:
        score -= 15

    # F. Thermal Shock & Rain Trigger (Suche in den letzten 14 Tagen nach Temperatursturz + Regen)
    trigger_found = False
    days_since_trigger = 999
    for i in range(len(times) - 1, max(0, len(times) - 14), -1):
        drop = (t_max[i-1] - t_max[i]) if i > 0 and t_max[i-1] and t_max[i] else 0
        r_sum = sum([rain[j] for j in range(max(0, i-2), min(len(rain), i+1)) if rain[j] is not None])
        
        req_rain = 45.0 if is_severe_drought else 20.0
        if drop >= 4.0 and r_sum >= req_rain:
            trigger_found = True
            days_since_trigger = len(times) - 1 - i
            break

    status = "Wachstumsphase normal"
    if trigger_found:
        if 7 <= days_since_trigger <= 12:
            score += 45
            status = "PRIME HARVEST WINDOW"
        elif 1 <= days_since_trigger <= 6:
            score += 25
            status = f"FLUSH DEVELOPING - Peak in {10 - days_since_trigger} Tagen"
        elif 13 <= days_since_trigger <= 16:
            score += 15
            status = "LATE SEASON / OLD CAPS"

    # G. Microclimate & Host Trees
    species = location.get("tree_species", [])
    host_pts = 0
    if "Spruce" in species: host_pts += host_max_pts
    if "Beech" in species: host_pts += host_max_pts
    if "Oak" in species: host_pts += int(host_max_pts * 0.8)
    if "Pine" in species: host_pts += int(host_max_pts * 0.6)
    score += min(host_pts, host_max_pts)

    if 12.0 <= latest_soil_t <= 17.0:
        score += 15

    # H. Active Harvest Feedback Layer
    for h in location.get("past_harvests", []):
        try:
            h_date = datetime.datetime.strptime(h.get("date"), "%Y-%m-%d").date()
            days_ago = (datetime.date.today() - h_date).days
            if 1 <= days_ago <= 4 and h.get("cap_stage") == "buttons_young":
                score += 20
                status = "AKTIVER MYZEL-SCHUB BESTÄTIGT"
            elif 1 <= days_ago <= 7 and h.get("cap_stage") == "old_overripe":
                score -= 25
                status = "🍂 EXHAUSTION / POST-FLUSH COOLING OFF"
        except:
            pass

    # I. Fly Agaric & Moon Phase
    fa_date_str = location.get("last_seen_fly_agaric")
    if fa_date_str:
        try:
            fa_date = datetime.datetime.strptime(fa_date_str, "%Y-%m-%d").date()
            if (datetime.date.today() - fa_date).days <= 10:
                score += 15
        except:
            pass

    moon_p = get_moon_phase(datetime.datetime.now())
    if 0.35 <= moon_p <= 0.65:
        score += 5

    # J. Quality & Maggot Risk
    quality_flag = "💎 PRIME QUALITY - Firm, bug-free caps expected."
    recent_max_t = [t for t in t_max[-5:] if t is not None]
    avg_max_t = sum(recent_max_t) / len(recent_max_t) if recent_max_t else 15
    if avg_max_t > 18.0:
        quality_flag = "⚠️ HIGH MAGGOT RISK - Harvest early while small."

    final_score = min(max(score, 0), 100)
    return final_score, status, quality_flag, f"{latest_soil_m:.2f} m³/m³"

def generate_html_report(config, db_data):
    now_str = datetime.datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')
    current_month = datetime.datetime.now().month
    is_peak_season = current_month in [9, 10]
    peak_badge = '<span style="background: #238636; color: white; padding: 4px 10px; border-radius: 12px; font-size: 0.85em;">🍄 Peak Season aktiv (Sept/Okt)</span>' if is_peak_season else '<span style="background: #30363d; color: #8b949e; padding: 4px 10px; border-radius: 12px; font-size: 0.85em;">Wartephase / Nebensaison</span>'

    current_moon = get_moon_description(get_moon_phase(datetime.datetime.now()))
    locations = config.get("LOCATIONS", [])
    
    first_loc_name = locations[0].get("name") if locations else ""
    first_weather = db_data.get(first_loc_name, {})
    raw_dates = first_weather.get("time", [])

    formatted_dates = []
    german_weekdays = {"Mon": "Mo", "Tue": "Di", "Wed": "Mi", "Thu": "Do", "Fri": "Fr", "Sat": "Sa", "Sun": "So"}
    for d_str in raw_dates:
        try:
            dt = datetime.datetime.strptime(d_str, "%Y-%m-%d")
            wd_de = german_weekdays.get(dt.strftime("%a"), dt.strftime("%a"))
            formatted_dates.append(f"{wd_de}, {dt.strftime('%d.%m.%Y')}")
        except:
            formatted_dates.append(d_str)

    temps = first_weather.get("temperature_2m_max", [])
    rain = first_weather.get("precipitation_sum", [])

    # Sammle alle past_harvests für das Chart mit Cap-Stage Farben & Yield-Größen
    all_harvest_pins = []
    for loc_idx, loc in enumerate(locations):
        for h in loc.get("past_harvests", []):
            stage = h.get("cap_stage", "prime")
            tier = h.get("yield_tier", "medium")
            color = "#238636" if stage == "buttons_young" else ("#d29922" if stage == "prime" else "#8957e5")
            radius = 6 if tier == "small" else (10 if tier == "medium" else 15)
            all_harvest_pins.append({
                "loc": loc.get("name"),
                "date": h.get("date"),
                "stage": stage,
                "tier": tier,
                "notes": h.get("notes", ""),
                "color": color,
                "radius": radius
            })

    html_content = f"""<!DOCTYPE html>
<html lang="de">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🍄 Porcini Mushroom Forecast Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js"></script>
    <style>
        body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; background-color: #0d1117; color: #c9d1d9; margin: 0; padding: 20px; }}
        .container {{ max-width: 950px; margin: auto; background: #161b22; padding: 30px; border-radius: 12px; box-shadow: 0 4px 20px rgba(0,0,0,0.5); }}
        h1, h2 {{ color: #58a6ff; border-bottom: 1px solid #30363d; padding-bottom: 10px; }}
        .card {{ background: #21262d; padding: 20px; border-radius: 8px; margin-bottom: 20px; border: 1px solid #30363d; }}
        input, select, textarea, button {{ width: 100%; padding: 12px; margin-top: 8px; margin-bottom: 16px; background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; box-sizing: border-box; }}
        button {{ background: #238636; color: white; font-weight: bold; cursor: pointer; border: none; }}
        button:hover {{ background: #2ea043; }}
        pre {{ background: #0d1117; padding: 15px; border-radius: 6px; overflow-x: auto; color: #8b949e; font-size: 0.9em; }}
        .flex-row {{ display: flex; justify-content: space-between; align-items: center; }}
    </style>
</head>
<body>
    <div class="container">
        <div class="flex-row">
            <h1>🍄 Porcini Forecast Dashboard</h1>
            <div>{peak_badge}</div>
        </div>
        <p>Letztes Update: <b>{now_str}</b> | 🌕 <b>Mondphase:</b> {current_moon}</p>
        
        <div class="card">
            <h2>Standort-Analyse & Flush-Prognosen</h2>
"""

    for loc in locations:
        w_data = db_data.get(loc.get("name"), {})
        score, status, quality, sm = calculate_advanced_score(loc, w_data)
        html_content += f"""
            <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px dashed #30363d;">
                <h3>📍 {loc.get('name')}</h3>
                <p><b>Flush Wahrscheinlichkeit:</b> <span style="color: #238636; font-size: 1.3em; font-weight: bold;">{score}%</span> | <b>Status:</b> {status}</p>
                <p><b>Qualität:</b> {quality} | <b>Bodenfeuchte:</b> {sm}</p>
                <p><small>Höhenlage: {loc.get('elevation_m')}m | pH: {loc.get('soil_pH')} | Partner: {', '.join(loc.get('tree_species', []))}</small></p>
            </div>
"""

    html_content += f"""
        </div>

        <div class="card">
            <h2>📈 Multi-Year Weather & Harvest Backtest Timeline</h2>
            <canvas id="weatherChart" height="110"></canvas>
        </div>

        <div class="card">
            <h2>🍄 Log Field Observation (Online Backend Sync)</h2>
            <label>Standort-Index (0 für den ersten Spot):</label>
            <input type="number" id="locIndex" value="0">

            <label>Date Found (YYYY-MM-DD):</label>
            <input type="date" id="findDate" value="{datetime.datetime.now().strftime('%Y-%m-%d')}">
            
            <label>Cap Stage:</label>
            <select id="capStage">
                <option value="buttons_young">🌱 Buttons / Young (Flush Boost +20)</option>
                <option value="prime" selected>💎 Prime Open Caps</option>
                <option value="old_overripe">🍂 Old / Overripe (-25)</option>
            </select>

            <label>Yield Tier:</label>
            <select id="yieldTier">
                <option value="small">Small (1-3 caps)</option>
                <option value="medium">Medium (Basket)</option>
                <option value="large" selected>Large (Massive Flush)</option>
            </select>

            <label>Notes:</label>
            <textarea id="findNotes" rows="2" placeholder="Zustand, Madenfreiheit, Begleitpilze..."></textarea>

            <button onclick="commitHarvestToGitHub()">Fund an GitHub senden & Action starten</button>
            <pre id="apiStatus">Bereit für API-Sync...</pre>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('weatherChart').getContext('2d');
        
        const weatherChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(formatted_dates)},
                datasets: [{{
                    label: 'Max Temperatur (°C)',
                    data: {json.dumps(temps)},
                    borderColor: '#f78166',
                    backgroundColor: 'rgba(247, 129, 102, 0.1)',
                    yAxisID: 'y',
                    tension: 0.2
                }}, {{
                    label: 'Niederschlag (mm)',
                    data: {json.dumps(rain)},
                    borderColor: '#58a6ff',
                    backgroundColor: '#58a6ff',
                    type: 'bar',
                    yAxisID: 'y1'
                }}]
            }},
            options: {{
                responsive: true,
                scales: {{
                    y: {{ type: 'linear', display: true, position: 'left', grid: {{ color: '#30363d' }} }},
                    y1: {{ type: 'linear', display: true, position: 'right', grid: {{ drawOnChartArea: false }}, min: 0 }}
                }}
            }}
        }});

        async function commitHarvestToGitHub() {{
            const statusEl = document.getElementById('apiStatus');
            const repo = "jmb2885m75-cmd/porcini-tracker";
            
            let token = localStorage.getItem('gh_token');
            if (!token) {{
                token = prompt("Bitte gib dein GitHub Personal Access Token (PAT) ein:");
                if (token) localStorage.setItem('gh_token', token);
            }}

            if (!token) {{
                alert("Token wird benötigt!");
                return;
            }}

            statusEl.innerText = "Lade aktuelle config.json von GitHub...";

            try {{
                const url = `https://api.github.com/repos/${{repo}}/contents/config.json`;
                
                const getRes = await fetch(url, {{
                    headers: {{ 'Authorization': `Bearer ${{token}}`, 'Accept': 'vnd.github.v3+json' }}
                }});
                if (!getRes.ok) throw new Error("Fehler beim Laden der config.json (Token ungültig oder keine Lesebrechte?)");
                
                const fileData = await getRes.json();
                
                const base64Clean = fileData.content.replaceAll('\\n', '').replaceAll('\\r', '');
                const binString = atob(base64Clean);
                const bytes = Uint8Array.from(binString, (m) => m.codePointAt(0));
                const jsonString = new TextDecoder().decode(bytes);
                const content = JSON.parse(jsonString);

                const locIdx = parseInt(document.getElementById('locIndex').value) || 0;
                const newHarvest = {{
                    "date": document.getElementById('findDate').value,
                    "cap_stage": document.getElementById('capStage').value,
                    "yield_tier": document.getElementById('yieldTier').value,
                    "notes": document.getElementById('findNotes').value
                }};

                if (!content.LOCATIONS[locIdx].past_harvests) {{
                    content.LOCATIONS[locIdx].past_harvests = [];
                }}
                content.LOCATIONS[locIdx].past_harvests.push(newHarvest);

                statusEl.innerText = "Speichere neuen Fund in config.json auf GitHub...";
                
                const updatedBytes = new TextEncoder().encode(JSON.stringify(content, null, 4));
                const updatedContentBase64 = btoa(String.fromCharCode(...updatedBytes));

                const putRes = await fetch(url, {{
                    method: 'PUT',
                    headers: {{
                        'Authorization': `Bearer ${{token}}`,
                        'Content-Type': 'application/json',
                    }},
                    body: JSON.stringify({{
                        message: "🍄 Fund via Web-Dashboard hinzugefügt",
                        content: updatedContentBase64,
                        sha: fileData.sha
                    }})
                }});

                if (!putRes.ok) throw new Error("Fehler beim Speichern auf GitHub.");

                statusEl.innerText = "Erfolgreich gespeichert! GitHub Action gestartet.";
                alert("Fund erfolgreich übertragen! Das Dashboard aktualisiert sich in wenigen Sekunden.");
            }} catch (err) {{
                statusEl.innerText = "Fehler: " + err.message;
                console.error(err);
                alert("Verbindungsfehler: " + err.message);
            }}
        }}
    </script>
</body>
</html>
"""
    return html_content

def send_notification(message, config):
    notif = config.get("NOTIFICATION_SETTINGS", {})
    provider = notif.get("provider", "none")
    token = notif.get("token") or os.environ.get("NOTIFICATION_TOKEN", "")
    recipient = notif.get("chat_id_or_recipient", "")

    if provider == "none" or not token:
        print("Benachrichtigungen sind deaktiviert oder Token fehlt.")
        return

    if provider == "telegram":
        url = f"https://api.telegram.org/bot{token}/sendMessage"
        payload = {"chat_id": recipient, "text": message, "parse_mode": "Markdown"}
        try:
            req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), headers={"Content-Type": "application/json"})
            urllib.request.urlopen(req)
            print("Telegram Benachrichtigung erfolgreich gesendet.")
        except Exception as e:
            print(f"Fehler beim Senden der Telegram-Nachricht: {e}")

def deploy_ftps(config):
    ftp_conf = config.get("FTP_SETTINGS", {})
    if not ftp_conf.get("enabled", False):
        return

    print("Starte FTPS/FTP Upload...")
    try:
        if ftp_conf.get("use_tls", True):
            ftp = FTP_TLS()
            ftp.connect(ftp_conf.get("host"), ftp_conf.get("port", 21))
            ftp.login(ftp_conf.get("username"), ftp_conf.get("password"))
            ftp.prot_p()
        else:
            ftp = FTP()
            ftp.connect(ftp_conf.get("host"), ftp_conf.get("port", 21))
            ftp.login(ftp_conf.get("username"), ftp_conf.get("password"))

        remote_path = ftp_conf.get("remote_path", "/public_html/porcini_report.html")
        with open(OUTPUT_HTML, "rb") as f:
            ftp.storbinary(f"STOR {remote_path}", f)
        ftp.quit()
        print("FTPS Upload erfolgreich abgeschlossen.")
    except Exception as e:
        print(f"Fehler beim FTPS Upload: {e}")

def main():
    parser = argparse.ArgumentParser(description="Porcini Mushroom Forecasting & Tracking Engine")
    parser.add_argument("--test-alert", action="store_true", help="Send a test notification to verify credentials")
    args = parser.parse_args()

    ensure_config_and_docs()
    config = load_json(CONFIG_FILE, DEFAULT_CONFIG)

    if args.test_alert:
        print("Sende Test-Benachrichtigung...")
        send_notification("🍄 *Porcini Tracker Test-Alert:* Verbindung erfolgreich hergestellt!", config)
        return

    print("Starte Porcini Forecast Engine & Wetter-Archiv-Sync...")
    db_data = load_json(DB_FILE, {})

    for loc in config.get("LOCATIONS", []):
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        elevation = loc.get("elevation_m", 100)
        loc_name = loc.get("name")
        
        if lat and lon:
            print(f"Synchronisiere Wetterdaten für: {loc_name}...")
            existing_loc_weather = db_data.get(loc_name, {})
            updated_weather = fetch_weather_archive_and_forecast(lat, lon, elevation, existing_loc_weather)
            db_data[loc_name] = updated_weather

    save_json(DB_FILE, db_data)

    # HTML Report generieren
    html_output = generate_html_report(config, db_data)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_output)

    # State Management & Alerts (Anti-Spam Persistence)
    state_data = load_json(STATE_FILE, {})
    threshold = config.get("ALERT_THRESHOLD", 65)
    
    alert_lines = ["🍄 *Wochenend Porcini Forecast (Ranked):*"]
    top_score_triggered = False

    for idx, loc in enumerate(config.get("LOCATIONS", [])):
        w_data = db_data.get(loc.get("name"), {})
        score, status, quality, sm = calculate_advanced_score(loc, w_data)
        alert_lines.append(f"{idx+1}. {loc.get('name')} - *{score}%* | {status}")
        
        if score >= threshold:
            top_score_triggered = True

    if top_score_triggered:
        dashboard_url = config.get("FTP_SETTINGS", {}).get("dashboard_url", "https://jmb2885m75-cmd.github.io/porcini-tracker/")
        alert_lines.append(f"\n🌐 Dashboard: {dashboard_url}")
        
        # Sende Benachrichtigung (überprüfe anti-spam 5 Tage Intervall)
        last_alert_str = state_data.get("last_alert_date")
        send_alert = True
        if last_alert_str:
            last_alert_date = datetime.datetime.strptime(last_alert_str, "%Y-%m-%d").date()
            if (datetime.date.today() - last_alert_date).days < 5:
                send_alert = False

        if send_alert:
            send_notification("\n".join(alert_lines), config)
            state_data["last_alert_date"] = datetime.date.today().strftime("%Y-%m-%d")
            save_json(STATE_FILE, state_data)

    deploy_ftps(config)
    print("Porcini Engine Durchlauf erfolgreich beendet!")

if __name__ == "__main__":
    main()
