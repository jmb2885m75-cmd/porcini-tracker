# Porcini Tracker

Scores daily porcini mushroom favourability from Open-Meteo weather for configured spots in Germany (northern hemisphere only).
It runs on GitHub Actions, stores data in JSON files, sends alerts and generates a self-contained `porcini_report.html` dashboard.

<!-- TODO: add a screenshot of the report dashboard here once available. -->

## Set up in 5 minutes

See [INSTALL.md](INSTALL.md) for setup, configuration, secrets and workflow verification.

## About the score

The 0–100 score is a heuristic favourability index, not a probability. Its weights and cutoffs are not locally calibrated, so do not read it as a chance of finding mushrooms.

## License

MIT, see [LICENSE](LICENSE).
