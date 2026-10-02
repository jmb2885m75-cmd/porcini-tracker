#!/usr/bin/env python3
import json
import os
import urllib.request
import datetime

# --- CONFIGURATION & PATHS ---
CONFIG_FILE = "config.json"
DB_FILE = "porcini_db.json"
OUTPUT_HTML = "index.html"  # Wichtig für GitHub Pages im Root-Verzeichnis

def load_json(filename):
    if os.path.exists(filename):
        with open(filename, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}

def save_json(filename, data):
    with open(filename, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=4, ensure_ascii=False)

def fetch_weather_data(lat, lon):
    url = f"https://archive-api.open-meteo.com/v1/archive?latitude={lat}&longitude={lon}&start_date=2026-06-01&end_date=2026-10-02&daily=temperature_2m_max,precipitation_sum&timezone=Europe/Berlin"
    try:
        req = urllib.request.urlopen(url)
        data = json.loads(req.read().decode("utf-8"))
        return data.get("daily", {})
    except Exception as e:
        print(f"Fehler beim Abrufen der Wetterdaten für ({lat}, {lon}): {e}")
        return {}

def get_approximate_moon_phase(date_obj):
    known_new_moon = datetime.date(2026, 1, 19)
    days_diff = (date_obj.date() - known_new_moon).days
    cycle = 29.53058867
    phase = (days_diff % cycle) / cycle
    if phase < 0.03 or phase > 0.97:
        return "Neumond (Starker Myzel-Impuls)"
    elif 0.45 < phase < 0.55:
        return "Vollmond (Fruchtkörper-Schub)"
    elif phase < 0.5:
        return "Zunehmender Mond"
    else:
        return "Abnehmender Mond"

def calculate_flush_score(location, weather_data):
    score = 50  # Basiswert
    
    for harvest in location.get("past_harvests", []):
        if harvest.get("cap_stage") == "buttons_young":
            score += 20
        elif harvest.get("cap_stage") == "old_overripe":
            score -= 25

    species = location.get("tree_species", [])
    if "Spruce" in species or "Beech" in species:
        score += 15

    return min(max(score, 0), 100)

def generate_html_report(config, db_data):
    now_str = datetime.datetime.now().strftime('%d.%m.%Y um %H:%M Uhr')
    current_month = datetime.datetime.now().month
    is_peak_season = current_month in [9, 10]
    peak_badge = '<span style="background: #238636; color: white; padding: 4px 10px; border-radius: 12px; font-size: 0.85em;">🍄 Peak Season aktiv (Sept/Okt)</span>' if is_peak_season else '<span style="background: #30363d; color: #8b949e; padding: 4px 10px; border-radius: 12px; font-size: 0.85em;">Wartephase / Nebensaison</span>'

    current_moon = get_approximate_moon_phase(datetime.datetime.now())

    locations = config.get("LOCATIONS", [])
    first_loc_name = locations[0].get("name") if locations else ""
    first_weather = db_data.get(first_loc_name, {})
    dates = first_weather.get("time", [])
    temps = first_weather.get("temperature_2m_max", [])
    rain = first_weather.get("precipitation_sum", [])

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
        input, select, button {{ width: 100%; padding: 12px; margin-top: 8px; margin-bottom: 16px; background: #0d1117; border: 1px solid #30363d; color: #c9d1d9; border-radius: 6px; }}
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
        <p>Letztes Update: <b>{now_str}</b> | 🌕 <b>Aktuelle Mondphase:</b> {current_moon}</p>
        
        <div class="card">
            <h2>Standort-Analyse & Prognosen</h2>
"""

    for loc in locations:
        score = calculate_flush_score(loc, db_data)
        html_content += f"""
            <div style="margin-bottom: 15px; padding-bottom: 15px; border-bottom: 1px dashed #30363d;">
                <h3>📍 {loc.get('name')}</h3>
                <p><b>Flush Wahrscheinlichkeit:</b> <span style="color: #58a6ff; font-size: 1.2em;">{score}%</span></p>
                <p><b>Höhenlage:</b> {loc.get('elevation_m')}m | <b>Boden-pH:</b> {loc.get('soil_pH')} | <b>Baumpartner:</b> {', '.join(loc.get('tree_species', []))}</p>
            </div>
"""

    html_content += f"""
        </div>

        <div class="card">
            <h2>📈 Wachstums- & Wetterverlauf (Letzte Monate)</h2>
            <canvas id="weatherChart" height="100"></canvas>
        </div>

        <div class="card">
            <h2>🍄 Log Historical / Recent Find (Direkt via GitHub API)</h2>
            <label>Standort-Index (0 für den ersten Spot):</label>
            <input type="number" id="locIndex" value="0">

            <label>Date Found (YYYY-MM-DD):</label>
            <input type="text" id="findDate" value="{datetime.datetime.now().strftime('%Y-%m-%d')}">
            
            <label>Cap Stage:</label>
            <select id="capStage">
                <option value="buttons_young">Buttons / Young (Flush Boost +20)</option>
                <option value="prime_open" selected>Prime Open Caps</option>
                <option value="old_overripe">Old / Overripe (-25)</option>
            </select>

            <label>Yield Tier:</label>
            <select id="yieldTier">
                <option value="small">Small</option>
                <option value="medium">Medium</option>
                <option value="large" selected>Large (Full haul)</option>
            </select>

            <button onclick="commitHarvestToGitHub()">Fund an GitHub senden & Action starten</button>
            <pre id="apiStatus">Bereit für API-Sync...</pre>
        </div>
    </div>

    <script>
        const ctx = document.getElementById('weatherChart').getContext('2d');
        const weatherChart = new Chart(ctx, {{
            type: 'line',
            data: {{
                labels: {json.dumps(dates)},
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
            
            let repo = localStorage.getItem('gh_repo');
            let token = localStorage.getItem('gh_token');

            if (!repo) {{
                repo = prompt("Bitte gib dein GitHub Repository ein (z. B. deinessername/porcini-dashboard):");
                if (repo) localStorage.setItem('gh_repo', repo);
            }}
            if (!token) {{
                token = prompt("Bitte gib dein GitHub Personal Access Token (PAT mit repo-Rechten) ein:");
                if (token) localStorage.setItem('gh_token', token);
            }}

            if (!repo || !token) {{
                alert("Repository und Token werden benötigt!");
                return;
            }}

            statusEl.innerText = "Lade aktuelle config.json von GitHub...";

            try {{
                const url = `https://api.github.com/repos/${{repo}}/contents/config.json`;
                const getRes = await fetch(url, {{
                    headers: {{ 'Authorization': `token ${{token}}`, 'Accept': 'vnd.github.v3+json' }}
                }});
                if (!getRes.ok) throw new Error("Fehler beim Laden der config.json (Token oder Repo ungültig?)");
                
                const fileData = await getRes.json();
                
                // Base64-Zeilenumbrüche bereinigen, damit atob fehlerfrei läuft
                const base64Clean = fileData.content.replace(/\\s/g, '');
                const jsonString = decodeURIComponent(escape(atob(base64Clean)));
                const content = JSON.parse(jsonString);

                const locIdx = parseInt(document.getElementById('locIndex').value) || 0;
                const newHarvest = {{
                    "date": document.getElementById('findDate').value,
                    "cap_stage": document.getElementById('capStage').value,
                    "yield_tier": document.getElementById('yieldTier').value
                }};

                if (!content.LOCATIONS[locIdx].past_harvests) {{
                    content.LOCATIONS[locIdx].past_harvests = [];
                }}
                content.LOCATIONS[locIdx].past_harvests.push(newHarvest);

                statusEl.innerText = "Speichere neuen Fund in config.json auf GitHub...";
                const updatedContentBase64 = btoa(unescape(encodeURIComponent(JSON.stringify(content, null, 4))));

                const putRes = await fetch(url, {{
                    method: 'PUT',
                    headers: {{
                        'Authorization': `token ${{token}}`,
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
                alert("Fund erfolgreich übertragen! Das Dashboard aktualisiert sich gleich.");
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

def main():
    print("Starte vollständigen Porcini Report Generator...")
    config = load_json(CONFIG_FILE)
    db_data = load_json(DB_FILE)
    
    for loc in config.get("LOCATIONS", []):
        lat = loc.get("latitude")
        lon = loc.get("longitude")
        if lat and lon:
            db_data[loc.get("name")] = fetch_weather_data(lat, lon)
            
    save_json(DB_FILE, db_data)
    
    html_output = generate_html_report(config, db_data)
    with open(OUTPUT_HTML, "w", encoding="utf-8") as f:
        f.write(html_output)
        
    print(f"Erfolgreich als '{OUTPUT_HTML}' gespeichert!")

if __name__ == "__main__":
    main()
