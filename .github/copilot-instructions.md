Project: porcini-tracker, a Python 3.10+ GitHub Actions app that scores daily porcini
mushroom favourability from Open-Meteo weather, stores data in JSON files, sends alerts
and generates porcini_report.html. Northern hemisphere (Germany) only.

Rules for every task:
- Make only the changes in your task. Do not touch files outside the listed scope.
- Run `python -m unittest discover -v` before finishing. Add or update tests for every
  behaviour you change. Do not delete tests to make them pass; if a test encodes a
  behaviour I asked you to change, update it and say so in the PR.
- Never edit or commit porcini_db.json, alert_state.json, harvest_log.json,
  porcini_report.html or index.html by hand. Never print or commit secrets.
- Keep ISO yyyy-MM-dd storage; do not change date formats (see dates.py).
- If an instruction conflicts with the code you find, or a file I mention doesn't exist,
  stop and explain in the PR description instead of guessing.
- PR description: what changed, what you verified, anything you could not verify.
- One PR per task, branch named after the task.
