# Porcini Tracker – Install & Configuration Guide

The forecast engine (`porcini.py`) runs on a schedule by GitHub Actions. It pulls weather from Open-Meteo (no API
key), scores each configured spot per day, stores tracker data in repository JSON files, optionally sends an alert,
and writes `porcini_report.html`. A separate GitHub issue workflow validates harvest submissions.

## What it does (and does not do)

- Keeps a multi-year weather archive per location in `porcini_db.json` (schema version 3) and a per-day score series.
- Sends Telegram / Pushover / Twilio alerts (see below).
- Generates `porcini_report.html`: a self-contained dashboard (no CDN) with a score chart, harvest pins, tooltips and an observation browser.
- The dashboard includes keyboard-accessible “ⓘ” explanations, found/no-find markers, and a scrollable day-by-day timeline with date picking and previous/next controls. Its 0–100 value is a heuristic favourability index, not a calibrated chance or probability. Weather inputs include rain over the 26 complete days before each scored date, mean air temperature over the 20 complete days before it, a conservative soil-temperature signal (default weight 0.25; per-location `soil_temperature_score_weight` and `soil_temperature_optimal_range` overrides), and a local-history 90-day drought comparison when enough prior-year data is available. Rain input saturates at 60 mm; no additional high-rain penalty is assumed without local observations. The windows are informed by a [regional porcini preprint](https://doi.org/10.64898/2025.12.12.693895); score weights and cutoffs are not locally calibrated. Weekday and lunar phase do not affect scores. The weather feed provides 7 forecast days including today.
- Harvest and no-mushrooms-found observations can be submitted as GitHub issues. A no-find observation caps that
  date's score at 20; it does not affect other dates. The intake workflow validates submissions from the
  repository owner or collaborators and stores them in `harvest_log.json`; browser drafts stay local until submitted.
  Existing harvests can also be configured in `past_harvests` in the private `CONFIG_JSON` secret.
- **Observation form:** the issue form uses friendly dropdowns (Found mushrooms / No mushrooms found, Small–Large, Buttons / young – Prime – Old / overripe). Date may be left empty (today) or `today`/`yesterday`; weight accepts `450`, `450 g` or `1.2 kg`. The intake workflow refreshes the location list and checks issues when opened, edited or labeled. Issues titled `Observation: ...` are processed even if the `harvest` label is missing; intake attempts to add that label automatically. Untrusted authors, unavailable config, or invalid fields get an explanatory issue comment; correcting and editing the issue retries intake. The dashboard saves a local draft and provides a link to the prefilled issue; the observation is not shared until you submit that issue on GitHub and intake succeeds.
- **Observation Log:** the dashboard has an Observation Log section listing every config `past_harvests` and accepted `harvest_log.json` entry (newest first) with source, reporter and issue link, per-location counts and filters. Browser-local drafts are intentionally not presented as shared observations. Run the Harvest Intake workflow manually after changing `CONFIG_JSON` to refresh the issue form's location dropdown.
- **Dates:** stored and exchanged as ISO `yyyy-MM-dd` (database, `harvest_log.json`, issue payloads); every
  user-facing date is displayed as `Ddd. dd Mon. yyyy` (e.g. `Sat. 03 Oct. 2026`; May has no period). Accepted input formats (issue form,
  `past_harvests`): `yyyy-MM-dd`, `dd.MM.yyyy`, `dd MMMM yyyy`, and the abbreviated display format with or without
  its matching weekday; they are normalised to ISO
  and impossible dates are rejected. Dates are date-only (no time zone), so they never shift by a day. See `dates.py`.
- The daily score chart includes optional rain and temperature context lines, each scaled to its visible range and
  shown behind the more prominent favourability index. Toggle them from the chart legend; gaps mean weather data
  is unavailable for those dates.
- `index.html` links to the generated report; GitHub Actions updates its alert preview.

## 1. Setup

1. Fork/clone the repository. Python 3.10+ and `pip install -r requirements.txt` are required for local runs. The season window, frost and late-season decay logic assume the northern hemisphere.
2. Configure `LOCATIONS` (name, `latitude`, `longitude`, `elevation_m`, `tree_species`, `tree_density`, `aspect`,
   `soil_pH`, `past_harvests`, `last_seen_fly_agaric`) and `ALERT_THRESHOLD` (default 55) in the JSON configuration.
   `aspect` and `canopy` are accepted configuration fields but currently unused by the scoring model.
   The default scoring season is Aug 15–Dec 10, aligned with the late-season decay ramp; `season_end` can override it.
   Optional `MIN_ALERT_GAP_DAYS` controls the minimum interval between alerts (default 5; zero disables the gap).
   Optional `FRIDAY_POLICY`: `thursday_alerted_only` (default; Friday confirmation only for spots alerted on Thursday) or
   `all_above_threshold` (Friday confirmation for every spot at or above `ALERT_THRESHOLD`).
   Harvest entries: `date`, `yield_tier` (small/medium/large), `cap_stage` (`buttons_young`, `prime`, `old_overripe`), optional `weight_g`, `notes`.
   A no-find report is recorded as `observation_type: no_mushrooms` with just the location and date.

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

Three workflows live in `.github/workflows/`:

- `porcini_tracker.yml` – the scheduled engine. Cron (UTC): daily 08:00 (default mode), Thursday 18:00 (weekend outlook), Friday 06:00 (final go/no-go). Needs `contents: write`, set in the file.
- `update_report.yml` – re-runs the engine when `porcini.py`, `porcini_db.json` or `harvest_log.json` changes, or
  when manually dispatched. Changes to the `CONFIG_JSON` secret do not trigger GitHub Actions automatically; run
  the workflow manually after updating it.
- `harvest_intake.yml` – validates harvest issues submitted by repository owners/collaborators, updates the
  harvest log and dashboard, and supports a manual run to refresh the issue form after configuration changes.

All workflows share one concurrency group and rebase before pushing. Engine runs commit generated tracker data;
harvest intake commits `harvest_log.json` and the rebuilt dashboard.

Check: Actions tab → "Porcini Intelligence Daily Engine" → *Run workflow*; confirm the run is green and a
"🍄 Auto-update Porcini weather DB & report" commit appears. If the push is rejected, enable
Settings → Actions → General → Workflow permissions → *Read and write*. GitHub may start scheduled runs late; the script's
time windows (Thu 17:00–20:59, Fri 05:00–07:59 UTC) absorb that. Manual runs outside the windows use the default mode, or force one with `--mode`.

## 4. Running locally

```bash
pip install -r requirements.txt
python porcini.py                         # auto-detects mode from UTC time
python porcini.py --mode weekend_outlook  # or final_confirmation / default
python porcini.py --test-alert            # send a test notification
python porcini.py --check-db              # archive integrity check (exit code 1 if problems)
python porcini.py --backtest              # compare stored forecast snapshots to later observations
python -m unittest test_porcini test_harvest -v  # offline self-checks
```

The test-alert command exits with an error if the notification provider rejects or cannot deliver the message.

The first run downloads ~2 years of history per location. A corrupt `porcini_db.json` / `alert_state.json` is moved aside as
`*.corrupt-<timestamp>` and rebuilt, and writes are atomic.

## 5. Behaviour reference

- **Alerts** fire only if the best weekend score crosses the threshold upward since the previous run, the status category changes, and ≥ 5 days passed since the last alert. A failed send is retried next run.
- **Friday final confirmation** follows `FRIDAY_POLICY`: by default only spots that received the Thursday outlook alert are confirmed; `all_above_threshold` confirms every spot meeting the threshold.
- **Archive/forecast merge:** archive observations replace forecast values for the same date; forecast-only dates are kept; a forecast never overwrites archive data.
- **Daily scores** are stored in `porcini_db.json` under `daily_scores` (archive days scored once; forecast days rescored each run; everything rescored when the model version or a spot's config changes). Harvest backtests compare only recorded visit days, exclude that day's observation from its score, and treat unvisited days as unknown. These small, potentially biased samples do not calibrate the index.
- **Forecast snapshots** retain score forecasts for upcoming days for the latest 30 runs. `python porcini.py --backtest` matches those snapshots only to later harvest/no-find visits, prints threshold hit rate and false alarms, and shows AUC only when there are at least five matched finds and five no-finds; smaller samples are flagged as insufficient.
- **Forecast uncertainty** is shown on timeline days when at least 20% of the preceding seasonal rain window comes from forecast data; focus or hover for the forecast/archive day counts. Spot cards show estimated days until the alert threshold is crossed only when a crossing appears in the forecast, explicitly as a heuristic.
- **Runoff penalty** uses hourly rain intensity over the three complete days before the scored date and applies only to days fetched after this feature was added.
- The 26-day rain and 20-day air-temperature windows end the day before the scored date. The soil-temperature signal uses that day's `soil_temperature_0_to_7cm_mean` field and defaults to a 25% weight; a location can override its weight (0–1) and optimal range. Seasonally matched 90-day rainfall comparisons use prior-year archive data only and are omitted when fewer than 20 valid comparison windows are available. Aspect/canopy adjustments are not currently applied.
- Size: roughly 365 records per location per year; there is no automatic pruning.

## Submitting observations from the report (GitHub only)

The report's *Submit observation* button calls the GitHub API to dispatch `.github/workflows/submit-observation.yml`.
The workflow validates the input with the same rules as the issue intake (`harvest.py`), appends it to
`harvest_log.json` and pushes the commit; invalid input fails the run with the reason in the log and nothing is committed.
No server is needed. Refresh the report after the run finishes (the next dashboard rebuild shows the entry).

Setup:
1. Settings → Actions → General → Workflow permissions → *Read and write* (the workflow also declares `contents: write`).
2. Create a fine-grained personal access token (github.com/settings/personal-access-tokens) limited to this repository with
   **Actions: Read and write** (needed to dispatch the workflow). **Contents** permission is not needed in the browser;
   the workflow commits with `GITHUB_TOKEN`. Only people with write access can dispatch the workflow.
3. Paste the token into the form's token field. It is saved in the browser's `localStorage`.

Security caveat: anything in `localStorage` is readable by scripts on the same origin, so use a short-lived token scoped to
this one repository with only Actions: Read and write, and clear it on shared devices. *Save as draft* works offline and never uses the token.

`api.py` (`POST /api/submit-observation`) is still available for people who prefer to self-host a server.
