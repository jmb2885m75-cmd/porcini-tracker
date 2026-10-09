from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import html
import json
import os
import secrets

from dates import format_display_date, parse_user_date
from harvest import build_observation_log, entry_id, summarize_observations
from notify import DEFAULT_ALERT_THRESHOLD
from scoring import (
    HEAVY_FORECAST_RAIN_SHARE, MAX_FORECAST_SNAPSHOTS, SOURCE_FORECAST,
    forecast_rain_window_mix, iso_date, parse_date, score_color, score_verdict_tag,
)

TEMPLATE_PATH = Path(__file__).with_name("index.template.html")
DASHBOARD_TEMPLATE = TEMPLATE_PATH.read_text(encoding="utf-8")


def valid_date_string(value: Any) -> bool:
    try:
        parse_user_date(value)
        return True
    except (TypeError, ValueError):
        return False

def public_location_alias(index: int) -> str:
    return f"Location {index + 1}"


def alias_public_text(value: str, locations: List[Dict[str, Any]]) -> str:
    for index, location in sorted(
        enumerate(locations), key=lambda pair: len(str(pair[1].get("name", ""))), reverse=True
    ):
        name = str(location.get("name", ""))
        if name:
            value = value.replace(name, public_location_alias(index))
    return value


MODE_DEFAULT = "default"


MODE_OUTLOOK = "weekend_outlook"


MODE_FINAL = "final_confirmation"


MODE_LABELS = {
    MODE_DEFAULT: "Daily run",
    MODE_OUTLOOK: "Thursday weekend outlook",
    MODE_FINAL: "Friday final go/no-go",
}


def store_forecast_snapshot(db: Dict[str, Any], run_date: date, forecast_scores: Dict[str, List[List[Any]]]) -> None:
    snapshots = db.get("forecast_snapshots")
    if not isinstance(snapshots, list):
        snapshots = []
    compact_scores = {}
    for alias, rows in forecast_scores.items():
        compact_scores[alias] = [
            [day, int(score)] for day, score in rows
            if valid_date_string(day) and isinstance(score, (int, float)) and 0 <= score <= 100
        ]
    snapshot = {"run_date": iso_date(run_date), "scores": compact_scores}
    snapshots = [item for item in snapshots if not isinstance(item, dict) or item.get("run_date") != snapshot["run_date"]]
    snapshots.append(snapshot)
    db["forecast_snapshots"] = snapshots[-MAX_FORECAST_SNAPSHOTS:]


def alert_threshold(cfg: Dict[str, Any]) -> int:
    """ALERT_THRESHOLD as an int in 0–100; invalid values fall back to the default."""
    try:
        return max(0, min(100, int(cfg.get("ALERT_THRESHOLD", DEFAULT_ALERT_THRESHOLD))))
    except (TypeError, ValueError):
        return DEFAULT_ALERT_THRESHOLD


def dashboard_payload(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], mode: str) -> Dict[str, Any]:
    def rnd(value: Any) -> Any:
        return round(value, 2) if isinstance(value, float) else value

    threshold = alert_threshold(cfg)
    locations = []
    for index, item in enumerate(analysis):
        records = item.get("records", [])
        scores = item.get("scores", {})
        alias = public_location_alias(index)
        configured = cfg.get("LOCATIONS", [])
        location = configured[index] if index < len(configured) and isinstance(configured[index], dict) else {}
        harvests = [dict(h, location=alias) for h in item.get("harvests", [])]
        locations.append({
            "name": alias,
            "scores": [[d, v["score"], v["status"], v.get("quality", ""),
                        score_verdict_tag(v["score"], v["status"], threshold), score_color(v["score"])]
                       for d, v in sorted(scores.items())],
            # [date, tmax, tmin, rain, wind, soil_temp, rh, soil_moisture, source]
            "records": [[r["date"]] + [rnd(r.get(k)) for k in ("temperature_2m_max", "temperature_2m_min", "precipitation_sum", "wind_speed_10m_max", "soil_temperature_0_to_7cm_mean", "relative_humidity_2m_mean", "soil_moisture_0_to_7cm_mean")] + [r.get("source", "")] for r in records],
            "rain_mix": [
                [r["date"], mix["forecast_days"], mix["archive_days"], mix["window_days"], mix["coverage_days"]]
                for r in records if r.get("source") == SOURCE_FORECAST
                for mix in [forecast_rain_window_mix(records, location, parse_date(r["date"]))]
            ],
            "harvests": harvests,
            "backtest": item.get("backtest", {}),
        })
    repo = os.environ.get("GITHUB_REPOSITORY") or cfg.get("GITHUB_REPOSITORY") or "jmb2885m75-cmd/porcini-tracker"
    return {"mode": mode, "threshold": threshold, "repo": repo, "branch": str(cfg.get("GITHUB_BRANCH", "main")), "locations": locations}


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
        eid = entry_id(e) if str(e.get("origin", "")).startswith("log") else ""
        action = f"<button type='button' class='del-btn' data-id=\"{esc(eid)}\">Delete</button>" if eid else "—"
        rows.append(
            f"<tr data-id=\"{esc(eid)}\" data-location=\"{esc(e.get('location'))}\" data-type=\"{'no_mushrooms' if none else 'harvest'}\">"
            f"<td>{esc(show_date(e['date']))}</td><td>{esc(e.get('location'))}</td>"
            f"<td class='{'type-none' if none else 'type-found'}'>{'❌ No mushrooms' if none else '🍄 Harvest'}</td>"
            f"<td>{esc(_TIER_LABELS.get(e.get('yield_tier'), e.get('yield_tier') or '—'))}</td>"
            f"<td>{esc(_STAGE_LABELS.get(e.get('cap_stage'), e.get('cap_stage') or '—'))}</td>"
            f"<td class='num'>{esc(weight) if weight not in (None, '') else '—'}</td>"
            f"<td class='notes'>{esc(e.get('notes') or '—')}</td><td>{esc(e.get('origin', ''))}</td>"
            f"<td>{esc(e.get('reporter') or '—')}</td><td>{link}</td><td>{action}</td></tr>")
    orphans = sorted({str(e.get("location")) for e in entries if e.get("origin") == "log (location not configured)"})
    orphan_note = (f"<p class=\"meta orphan-note\">⚠️ Observations for locations that are no longer configured are still listed here "
                   f"but have no chart or score: {esc(', '.join(orphans))}. Add the location back to the config to see them in the Daily Score History.</p>") if orphans else ""
    locations = sorted({str(e.get("location")) for e in entries})
    options = "".join(f"<option value=\"{esc(l)}\">{esc(l)}</option>" for l in locations)
    if not entries:
        return ("    <h2 id='observation-log'>📒 Observation Log</h2>\n    <div class='card'><div class='meta'>No observations recorded yet. This overview includes configured past harvests and submitted observations; browser-local drafts are not shared or listed here.</div></div>")
    return f"""    <h2 id="observation-log">📒 Observation Log</h2>
    <div class="card">
      <div class="log-summary">{''.join(chips)}</div>
      {orphan_note}
      <p class="meta">This overview contains configured past harvests and observations accepted into the shared log. Browser-local drafts are not shared or listed here.</p>
      <details><summary>Counts per location</summary><div class="table-wrap"><table class="log-table"><thead><tr><th>Location</th><th class="num">Harvests</th><th class="num">No mushrooms</th><th class="num">Total g</th><th>Last visit</th></tr></thead><tbody>{summary_rows}</tbody></table></div></details>
      <div class="toolbar"><label for="log-loc">Location<select id="log-loc"><option value="">All locations</option>{options}</select></label>
      <label for="log-type">Type<select id="log-type"><option value="">All types</option><option value="harvest">Harvest</option><option value="no_mushrooms">No mushrooms</option></select></label>
      <span id="log-count" class="meta"></span></div>
      <p id="delete-status" class="note" aria-live="polite">Entries from the shared log can be deleted with your GitHub token (Contents: Read and write). You can delete your own entries; repository admins can delete any. Configured past harvests cannot be deleted here.</p>
      <div class="table-wrap"><table class="log-table"><thead><tr><th>Date</th><th>Location</th><th>Type</th><th>Yield</th><th>Cap stage</th><th class="num">Weight g</th><th>Notes</th><th>Source</th><th>Reported by</th><th>Issue</th><th>Action</th></tr></thead><tbody id="log-rows">{''.join(rows)}</tbody></table></div>
    </div>"""


def generate_dashboard_html(cfg: Dict[str, Any], analysis: List[Dict[str, Any]], alert_message: str = "", alert_will_send: bool = False, mode: str = MODE_DEFAULT, harvest_log: Optional[Dict[str, Any]] = None) -> str:
    """Self-contained dashboard (inline CSS/JS/data, no CDN). Static hosting; observations are POSTed to api.py and stored in harvest_log.json."""
    template_content = TEMPLATE_PATH.read_text(encoding="utf-8")
    if "{{ALERT_PREVIEW}}" not in template_content:
        raise ValueError("index.template.html is missing the {{ALERT_PREVIEW}} placeholder for alert preview injection")
    threshold = alert_threshold(cfg)
    cards = []
    for index, item in enumerate(analysis):
        available = item["best_day"] != "N/A"
        verdict = ""
        score = item["best_score"]
        score_display = "N/A"
        if available:
            score_display = (
                f"<span class='score' style='color:{score_color(score)}'>{score}/100</span>"
                f"<span class='verdict-tag'>{html.escape(score_verdict_tag(score, item['status'], threshold))}</span>"
            )
        if mode == MODE_FINAL and available:
            go = score >= threshold
            verdict = f"<div><span class='badge {'go' if go else 'nogo'}'>{'GO' if go else 'NO-GO'}</span></div>"
        quality = f"<div class='meta'>{html.escape(item['quality'])}</div>" if item.get("quality") else ""
        explanation = f"<div class='meta score-explanation'>{html.escape(item['explanation'])}</div>" if item.get("explanation") else ""
        timing_days = item.get("flush_timing_days")
        timing = (
            f"<div class='meta flush-timing'>Estimated days until score crosses ALERT_THRESHOLD: {timing_days} (heuristic)</div>"
            if isinstance(timing_days, int) else ""
        )
        cards.append(
            f"<div class='card'><div class='score-line'>{score_display}</div><div class='name'>{html.escape(public_location_alias(index))}</div>{verdict}"
            f"<div class='meta best-day'>Best day<strong>{html.escape(str(item['best_day']))}</strong></div><div class='meta'>Status: {html.escape(str(item['status']))}</div>{explanation}{quality}{timing}"
            f"<div class='meta'>Moisture: {item['soil_moisture']:.2f} m³/m³</div></div>"
        )
    alert_status = "This message will be sent with this run." if alert_will_send else "Preview only: no alert is triggered by this run."
    alert_section = ""
    if alert_message:
        public_alert = alias_public_text(alert_message, cfg.get("LOCATIONS", []))
        alert_section = f"""
    <h2>📨 Alert Preview</h2>
    <div class="card">
      <div class="meta">{html.escape(alert_status)}</div>
      <pre class="alert">{html.escape(public_alert)}</pre>
      <div class="meta">Fields sent per ranked spot (top 3): rank, location name, best weekend favourability index (0–100), best day, status. The final line is the dashboard link.</div>
    </div>"""
    payload = dashboard_payload(cfg, analysis, mode)
    public_names = {str(item.get("name")): public_location_alias(i) for i, item in enumerate(cfg.get("LOCATIONS", []))}
    public_entries = [
        dict(entry, location=public_names.get(str(entry.get("location")), "Unlisted location"))
        for entry in build_observation_log(cfg.get("LOCATIONS", []), harvest_log)
    ]
    observation_log = render_observation_log(public_entries, payload["repo"])
    data = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    nonce = secrets.token_urlsafe(18)
    return (
        template_content
        .replace("__CSP_NONCE__", nonce)
        .replace("__HEAVY_FORECAST_RAIN_SHARE__", str(HEAVY_FORECAST_RAIN_SHARE))
        .replace("__MODE_LABEL__", html.escape(MODE_LABELS.get(mode, MODE_LABELS[MODE_DEFAULT])))
        .replace("__GENERATED__", (lambda n: f"{format_display_date(n)} {n:%H:%M}")(datetime.now(timezone.utc)))
        .replace("__CARDS__", "".join(cards))
        .replace("{{ALERT_PREVIEW}}", alert_section)
        .replace("__DATA__", data)
        .replace("__OBSERVATION_LOG__", observation_log)
    )
