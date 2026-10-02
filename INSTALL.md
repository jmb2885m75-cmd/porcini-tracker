# Porcini Tracker – Install & Configuration Guide

The tracker is a single Python script (`porcini.py`) run on a schedule by GitHub Actions. It pulls weather from
Open-Meteo (no API key), scores each configured spot per day, stores everything in JSON files committed to the
repository, optionally sends an alert, and writes `porcini_report.html`.

## What it does (and does not do)

- Keeps a multi-year weather archive per location in `porcini_db.json` (schema version 2) and a per-day score series.
- Sends Telegram / Pushover / Twilio alerts (see below).
- Generates `porcini_report.html`: a self-contained dashboard (no CDN) with a score chart, harvest pins, tooltips and an observation browser.
- **Not implemented:** a backend. Observations logged in the dashboard stay in that browser (localStorage), do not
  change scores and are not synced. To make a harvest count, add it to `past_harvests` in `CONFIG_JSON` and run the
  workflow again.
- `index.html` links to the generated report; GitHub Actions updates its alert preview.

## 1. Setup

1. Fork/clone the repository. Python 3.10+ and `pip install requests` are required for local runs.
2. Configure `LOCATIONS` (name, `latitude`, `longitude`, `elevation_m`, `tree_species`, `tree_density`, `aspect`,
   `soil_pH`, `past_harvests`, `last_seen_fly_agaric`) and `ALERT_THRESHOLD` (default 65) in the JSON configuration.
   Harvest entries: `date`, `yield_tier` (small/medium/large), `cap_stage` (`buttons_young`, `prime`, `old_overripe`), optional `weight_g`, `notes`.

## 2. Secrets

The configuration is not stored in the repository. The workflows read it from the repository Actions secret
named **`CONFIG_JSON`** (Settings → Secrets and variables → Actions). Put the full JSON configuration there,
including notification credentials:

```json
"NOTIFICATION_SETTINGS": { "provider": "telegram", "token": "<bot token>", "chat_id_or_recipient": "<chat id>" }
```

Pushover uses `api_token` + `user_key`; Twilio uses `account_sid`, `auth_token`, `from_number`, `to_number`.
`CONFIG_JSON` is required for workflow runs; there is no committed-config fallback. For local runs, either set
the `CONFIG_JSON` environment variable or create a local `config.json` (ignored by Git).

## 3. Workflow verification

Two workflows live in `.github/workflows/`:

- `porcini_tracker.yml` – the scheduled engine. Cron (UTC): daily 08:00 (default mode), Thursday 18:00 (weekend outlook), Friday 06:00 (final go/no-go). Needs `contents: write`, set in the file.
- `update_report.yml` – re-runs the engine when `porcini.py` or `porcini_db.json` changes, or when manually
  dispatched. Changes to the `CONFIG_JSON` secret do not trigger GitHub Actions automatically; run the workflow
  manually after updating it.

Both share one concurrency group, commit only `porcini_db.json`, `index.html`, `porcini_report.html`, `alert_state.json`, and rebase before pushing.

Check: Actions tab → "Porcini Intelligence Daily Engine" → *Run workflow*; confirm the run is green and a
"🍄 Auto-update Porcini weather DB & report" commit appears. If the push is rejected, enable
Settings → Actions → General → Workflow permissions → *Read and write*. GitHub may start scheduled runs late; the script's
time windows (Thu 17:00–20:59, Fri 05:00–07:59 UTC) absorb that. Manual runs outside the windows use the default mode, or force one with `--mode`.

## 4. Running locally

```bash
pip install requests
python porcini.py                         # auto-detects mode from UTC time
python porcini.py --mode weekend_outlook  # or final_confirmation / default
python porcini.py --test-alert            # send a test notification
python porcini.py --check-db              # archive integrity check (exit code 1 if problems)
python -m unittest test_porcini -v        # offline self-checks
```

The first run downloads ~2 years of history per location. A corrupt `porcini_db.json` / `alert_state.json` is moved aside as
`*.corrupt-<timestamp>` and rebuilt, and writes are atomic.

## 5. Behaviour reference

- **Alerts** fire only if the best weekend score crosses the threshold upward since the previous run, the status category changes, and ≥ 5 days passed since the last alert. A failed send is retried next run.
- **Friday final confirmation** is only sent for spots that received the Thursday outlook alert (it is a GO/NO-GO follow-up, exempt from the 5-day rule).
- **Archive/forecast merge:** archive observations replace forecast values for the same date; forecast-only dates are kept; a forecast never overwrites archive data.
- **Daily scores** are stored in `porcini_db.json` under `daily_scores` (archive days scored once; forecast days rescored each run; everything rescored when the model version or a spot's config changes). A simple harvest backtest is stored under `backtest`.
- **Runoff penalty** uses hourly rain intensity and applies only to days fetched after this feature was added.
- Aspect/canopy adjust the length of the "wet" rain window only; the API's soil-moisture value does not know about aspect.
- Size: roughly 365 records per location per year; there is no automatic pruning.
