import os
import json
import requests
import time
import datetime
from pathlib import Path

# --- Configuration ---
CONFIG_PATH = Path("config.json")


def load_config():
    """Load configuration from JSON file."""
    if not CONFIG_PATH.exists():
        raise FileNotFoundError(f"Config file not found: {CONFIG_PATH}")
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_config(config):
    """Save configuration to JSON file."""
    with CONFIG_PATH.open("w", encoding="utf-8") as f:
        json.dump(config, f, indent=4, ensure_ascii=False)
        f.write("\n")


def ensure_telegram_notification_settings(config):
    """Normalize notification config keys for older and newer naming styles."""
    notifications = config.setdefault("NOTIFICATION_SETTINGS", {})
    provider = str(notifications.get("provider", "")).lower()
    if provider == "telegram":
        if "token" not in notifications and "telegram_token" not in notifications:
            notifications["token"] = ""
        if "chat_id_or_recipient" not in notifications and "telegram_chat_id" not in notifications:
            notifications["chat_id_or_recipient"] = ""
    return config


def send_telegram_message(token, chat_id, message):
    """Send a Telegram message if token and chat_id are configured."""
    if not token or not chat_id:
        return False
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "disable_web_page_preview": True,
    }
    try:
        resp = requests.post(url, data=payload, timeout=20)
        resp.raise_for_status()
        return True
    except Exception as exc:
        print(f"Telegram send failed: {exc}")
        return False


def send_notification(message, config):
    """Send notification using configured provider (Telegram currently supported)."""
    settings = config.get("NOTIFICATION_SETTINGS", {})
    provider = str(settings.get("provider", "")).lower()
    token = settings.get("token") or settings.get("telegram_token") or ""
    chat_id = settings.get("chat_id_or_recipient") or settings.get("telegram_chat_id") or ""

    if provider == "telegram":
        return send_telegram_message(token, chat_id, message)

    print(f"No notification provider configured for '{provider}'")
    return False


def fetch_weather(lat, lon):
    """Fetch the latest weather data from a simple weather endpoint."""
    url = "https://api.open-meteo.com/v1/forecast"
    params = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,precipitation,weather_code,wind_speed_10m",
        "timezone": "auto",
    }
    try:
        response = requests.get(url, params=params, timeout=20)
        response.raise_for_status()
        return response.json()
    except Exception as exc:
        print(f"Fehler beim Abrufen der Wetterdaten für ({lat}, {lon}): {exc}")
        return {}


def calculate_risk_score(location):
    """Compute a simple risk score based on a few fields."""
    score = 0
    if location.get("tree_density") == "dense_conifer":
        score += 15
    if location.get("aspect") == "south":
        score += 10
    if location.get("soil_pH") == "acidic":
        score += 10
    if location.get("past_harvests"):
        score += 10
    return score


def generate_html_report(config, db_data):
    """Generate a simple HTML report."""
    rows = db_data.get("rows", [])
    rows_html = "".join(
        f"""
        <tr>
            <td>{idx + 1}</td>
            <td>{row.get('date', '')}</td>
            <td>{row.get('location', '')}</td>
            <td>{row.get('weather', '')}</td>
            <td>{row.get('soil', '')}</td>
            <td>{row.get('risk', '')}</td>
        </tr>
        """
        for idx, row in enumerate(rows)
    )

    return f"""
    <html>
      <head><title>Porcini Tracker</title></head>
      <body>
      <table>
        <thead>
          <tr>
            <th>#</th>
            <th>Date</th>
            <th>Location</th>
            <th>Weather</th>
            <th>Soil</th>
            <th>Risk</th>
          </tr>
        </thead>
        <tbody>
        {rows_html}
        </tbody>
      </table>
      </body>
    </html>
    """


def main():
    print("🚀 Starte Porcini Forecast Engine & Wetter-Archiv-Sync...")
    config = load_config()
    config = ensure_telegram_notification_settings(config)

    # Use the provided config file; no additional guessing
    locations = config.get("LOCATIONS", [])
    db_rows = []
    for location in locations:
        lat = location.get("latitude")
        lon = location.get("longitude")
        weather = fetch_weather(lat, lon)
        temperature = weather.get("current", {}).get("temperature_2m")
        precipitation = weather.get("current", {}).get("precipitation")
        risk = calculate_risk_score(location)
        row = {
            "date": datetime.date.today().isoformat(),
            "location": location.get("name", "Unknown"),
            "weather": f"{temperature}°C / {precipitation} mm",
            "soil": location.get("soil_pH", "unknown"),
            "risk": risk,
        }
        db_rows.append(row)

    db_data = {"rows": db_rows}
    html_output = generate_html_report(config, db_data)

    # Example notification to show the fix works without crashing
    send_notification("Porcini tracker report generated successfully.", config)

    print("✅ Verarbeitung abgeschlossen.")


if __name__ == "__main__":
    main()
