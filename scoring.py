from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import bisect
import calendar
import hashlib
import json
import math
import re

from dates import format_display_date, parse_user_date
from harvest import merge_harvests
from notify import DEFAULT_ALERT_THRESHOLD


def iso_date(value: date) -> str:
    return value.strftime("%Y-%m-%d")


parse_date = parse_user_date


def valid_date_string(value: Any) -> bool:
    try:
        parse_date(value)
        return True
    except (TypeError, ValueError):
        return False


def _weather_fetch_json(*args: Any, **kwargs: Any) -> Any:
    from porcini import fetch_json
    return fetch_json(*args, **kwargs)


def _weather_api_url(name: str) -> str:
    import porcini
    return getattr(porcini, name)


def status_category(status: Optional[str]) -> str:
    from notify import status_category as classify
    return classify(status)


def month_day_window(day: date, location: Optional[Dict[str, Any]] = None) -> bool:
    from porcini import month_day_window as in_season
    return in_season(day, location)


def public_location_alias(index: int) -> str:
    from report import public_location_alias as alias
    return alias(index)


SOIL_DRY, SOIL_MID, SOIL_WET = 0.20, 0.28, 0.35

MAX_FORECAST_SNAPSHOTS = 30


BACKTEST_MIN_CLASS_SAMPLES = 5


HEAVY_FORECAST_RAIN_SHARE = 0.2


INITIAL_ARCHIVE_DAYS = 3650


SCHEMA_VERSION = 3


NO_FIND_PENALTY = (10, 5)


FLUSH_DEPLETION_TIERS = ((1, 35), (3, 25), (6, 12))


DEFAULT_DEPLETION_RECOVERY_DAYS = 6


SMALL_HARVEST_DEPLETION_RECOVERY_DAYS = 5


DEFAULT_EARLY_HARVEST_PENALTY_FACTOR = 0.5


MODEL_VERSION = 16  # 16: C1–C7 scoring updates (host-tree removal, depletion tiers, drought percentile, rain cap, frost/season, soil temp, utcnow fix)


ARCHIVE_LAG_DAYS = 5


SOURCE_ARCHIVE = "archive"


SOURCE_FORECAST = "forecast"


SOURCE_SAMPLE = "sample"


SOURCE_RANK = {SOURCE_SAMPLE: 0, SOURCE_FORECAST: 1, SOURCE_ARCHIVE: 2}


AFFINITY_LOOKBACK_DAYS = 1461  # "proven spot affinity" counts harvests from the last 4 years


RECENCY_YEARS_BACK = 2  # harvests older than this count for OLD_OBSERVATION_WEIGHT


OLD_OBSERVATION_WEIGHT = 0.5


RECENT_RAIN_WINDOW_DAYS = 7


RECENT_RAIN_WEIGHT = 0.6


BACKGROUND_RAIN_WEIGHT = 0.4


RAINY_DAY_MM = 2.0


RAIN_DISTRIBUTION_SCORE_MAX = 5


DEFAULT_OPTIMAL_TEMP_RANGE = (10.0, 15.0)


TEMPERATURE_SLOPE_PER_DEGREE = 2


HUMIDITY_HIGH = 85


HUMIDITY_LOW = 60


POST_FLUSH_MIN_DAYS = 7


POST_FLUSH_MAX_DAYS = 14


POST_FLUSH_BONUS = 10


POST_FLUSH_FULL_BASE_SCORE = 30  # full bonus from this weather-based score upward


POST_FLUSH_RAMP = 15  # bonus scales linearly from 0 at (full - ramp)


CALIBRATION_HIT_RATE_MIN = 0.70


CALIBRATION_MIN_FIND_DAYS = 3


RUNOFF_MIN_DAY_MM = 15.0


FLUSH_RAIN_WINDOW_DAYS = 26


FLUSH_TEMPERATURE_WINDOW_DAYS = 20


LONG_TERM_RAIN_WINDOW_DAYS = 90


LONG_TERM_RAIN_MIN_YEARS = 5


FLUSH_RAIN_SCORE_MAX = 30


RAIN_SATURATION_MM = 60.0  # rain total at which the rain score saturates


VERY_FAVOURABLE_THRESHOLD = 65


FROST_TMIN_C = -1.5


FROST_LIGHT_TMIN_C = 0.0


FROST_RECENT_DAYS = 3


FROST_REPEAT_WINDOW_DAYS = 14


FROST_REPEAT_PENALTY_PER_NIGHT = 4


FROST_REPEAT_PENALTY_MAX = 20


LATE_SEASON_DECAY_START = (11, 15)


LATE_SEASON_DECAY_END = (12, 1)


LATE_SEASON_DECAY_MAX = 15


SOIL_TEMPERATURE_SCORE_WEIGHT = 0.25


FROST_LIGHT_PENALTY = 4


HEAT_TMAX_C = 25.0


FROST_PENALTY = 10


HEAT_PENALTY = 5


FLUSH_TEMPERATURE_SCORE_MAX = 20


DROUGHT_SCORE_PENALTY_MAX = 10


VERDICT_LOW_TIERS = (
    (15, "🚫 Not worth it"),
    (35, "😐 Unlikely"),
    (50, "🤔 Long shot"),
)


VERDICT_WORTH_LOOK = "👀 Worth a look"


VERDICT_GO = "👍 Favourable conditions"


VERDICT_DEFINITE_GO = "🔥 Very favourable conditions"


VERDICT_SPECIAL_TAGS = {
    "terminated": "❄️ Season over",
    "off_season": "🍂 Off season",
    "delayed": "🔥 Too warm – wait",
    "dry": "💧 Too dry – wait",
}


SCORE_COLOR_MIN = 30


SCORE_COLOR_MAX = 60


DAILY_FIELDS = [
    "precipitation_sum",
    "temperature_2m_max",
    "temperature_2m_min",
    "soil_temperature_0_to_7cm_mean",
    "relative_humidity_2m_mean",
    "soil_moisture_0_to_7cm_mean",
]


RECORD_FIELDS = DAILY_FIELDS + ["precip_peak_2h_mm"]


PEAK_BASIS_HOURLY = "hourly"


PEAK_BASIS_BACKFILL = "hourly_backfill"


PEAK_BASIS_UNAVAILABLE = "unavailable"


BACKFILL_CHUNK_DAYS = 365


BACKFILL_MAX_CHUNKS_PER_RUN = 4  # bounds API calls per run; the rest is picked up by the next run (TODO: one-time manual run if needed)


def peak_precip_by_day(hourly: Dict[str, Any]) -> Dict[str, float]:
    """Return {date: precipitation (mm) in the busiest 2 consecutive hours} from an hourly payload."""
    by_day: Dict[str, List[float]] = {}
    for stamp, value in zip(hourly.get("time", []) or [], hourly.get("precipitation", []) or []):
        by_day.setdefault(str(stamp)[:10], []).append(float(value or 0))
    peaks: Dict[str, float] = {}
    for day, hours in by_day.items():
        if len(hours) < 2:
            peaks[day] = round(sum(hours), 2)
        else:
            peaks[day] = round(max(hours[i] + hours[i + 1] for i in range(len(hours) - 1)), 2)
    return peaks


def parse_open_meteo_payload(payload: Optional[Dict[str, Any]], source: str) -> List[Dict[str, Any]]:
    if not payload or "daily" not in payload:
        return []
    daily = payload["daily"]
    peaks = peak_precip_by_day(payload.get("hourly") or {})
    records: List[Dict[str, Any]] = []
    for idx, date_str in enumerate(daily.get("time", [])):
        item: Dict[str, Any] = {"date": date_str, "source": source}
        for field in DAILY_FIELDS:
            values = daily.get(field) or []
            item[field] = values[idx] if idx < len(values) else None
        item["precip_peak_2h_mm"] = peaks.get(date_str)
        if item["precip_peak_2h_mm"] is not None:
            item["precip_peak_basis"] = PEAK_BASIS_HOURLY
        records.append(item)
    return records


def fetch_archive_day_range(latitude: float, longitude: float, elevation: int, start: date, end: date) -> List[Dict[str, Any]]:
    payload = _weather_fetch_json(
        _weather_api_url("ARCHIVE_API_URL"),
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "start_date": iso_date(start),
            "end_date": iso_date(end),
            "daily": ",".join(DAILY_FIELDS),
            "hourly": "precipitation",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    return parse_open_meteo_payload(payload, SOURCE_ARCHIVE)


def fetch_archive_hourly_precip(latitude: float, longitude: float, elevation: int, start: date, end: date) -> Optional[Dict[str, Any]]:
    """Hourly precipitation only (used for runoff backfill); request failures raise WeatherRequestError."""
    return _weather_fetch_json(
        _weather_api_url("ARCHIVE_API_URL"),
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "start_date": iso_date(start),
            "end_date": iso_date(end),
            "hourly": "precipitation",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )


def needs_runoff_backfill(rec: Dict[str, Any]) -> bool:
    if rec.get("source") != SOURCE_ARCHIVE:
        return False
    if rec.get("precip_peak_2h_mm") is not None or rec.get("precip_peak_basis") is not None:
        return False
    try:
        rec_date = parse_date(rec["date"])
        cutoff = datetime.now(timezone.utc).date() - timedelta(days=400)
        return rec_date >= cutoff
    except (KeyError, TypeError, ValueError):
        return False


def backfill_runoff_data(records: List[Dict[str, Any]], latitude: float, longitude: float, elevation: int,
                         fetch_hourly: Any = None, max_chunks: int = BACKFILL_MAX_CHUNKS_PER_RUN) -> List[str]:
    """Fill precip_peak_2h_mm on older archive records (in place) from hourly precipitation.

    Fails soft: a failed/empty chunk leaves its records untouched (they are retried next run) and never
    raises. Returns the dates that were changed so their scores can be recomputed.
    """
    fetch_hourly = fetch_hourly or fetch_archive_hourly_precip
    pending = sorted((r for r in records if needs_runoff_backfill(r)), key=lambda r: r["date"])
    changed: List[str] = []
    chunks = 0
    while pending and chunks < max_chunks:
        start = parse_date(pending[0]["date"])
        end = start + timedelta(days=BACKFILL_CHUNK_DAYS - 1)
        chunk = [r for r in pending if parse_date(r["date"]) <= end]
        pending = pending[len(chunk):]
        chunks += 1
        try:
            payload = fetch_hourly(latitude, longitude, elevation, start, parse_date(chunk[-1]["date"]))
        except Exception as exc:  # partial API failure must never break the run
            print(f"[WARN] Runoff backfill chunk {iso_date(start)} failed: {exc}")
            continue
        hourly = payload.get("hourly") if isinstance(payload, dict) else None
        if not hourly or not hourly.get("time"):
            print(f"[WARN] Runoff backfill chunk starting {iso_date(start)} returned no hourly data; will retry next run")
            continue
        peaks = peak_precip_by_day(hourly)
        for rec in chunk:
            peak = peaks.get(rec["date"])
            rec["precip_peak_2h_mm"] = peak
            rec["precip_peak_basis"] = PEAK_BASIS_BACKFILL if peak is not None else PEAK_BASIS_UNAVAILABLE
            changed.append(rec["date"])
    if pending:
        print(f"[INFO] {len(pending)} records still await runoff backfill (continues on the next run)")
    return changed


def fetch_forecast_day_range(latitude: float, longitude: float, elevation: int) -> List[Dict[str, Any]]:
    payload = _weather_fetch_json(
        _weather_api_url("FORECAST_API_URL"),
        {
            "latitude": latitude,
            "longitude": longitude,
            "elevation": elevation,
            "past_days": 7,
            "forecast_days": 7,
            "daily": ",".join(DAILY_FIELDS),
            "hourly": "precipitation",
            "temperature_unit": "celsius",
            "wind_speed_unit": "kmh",
            "precipitation_unit": "mm",
            "timezone": "UTC",
        },
    )
    return parse_open_meteo_payload(payload, SOURCE_FORECAST)


class History:
    """Date-indexed view over a sorted list of daily records."""

    def __init__(self, records: List[Dict[str, Any]]):
        self.records = records
        self.dates = [r["date"] for r in records]

    def between(self, start: date, end: date) -> List[Dict[str, Any]]:
        lo = bisect.bisect_left(self.dates, iso_date(start))
        hi = bisect.bisect_right(self.dates, iso_date(end))
        return self.records[lo:hi]

    def window(self, end: date, days: int) -> List[Dict[str, Any]]:
        """The `days` calendar days ending on (and including) `end`."""
        return self.between(end - timedelta(days=days - 1), end)

    def get(self, day: date) -> Optional[Dict[str, Any]]:
        found = self.between(day, day)
        return found[0] if found else None


def _precip(rec: Optional[Dict[str, Any]]) -> float:
    return float((rec or {}).get("precipitation_sum") or 0)


def observed_rainfall_total(hist: History, day: date, days: int) -> Optional[float]:
    """Return the total from the `days` complete days before `day` with at least 80% coverage."""
    records = hist.window(day - timedelta(days=1), days)
    values = [r.get("precipitation_sum") for r in records if r.get("precipitation_sum") is not None]
    if len(values) < math.ceil(days * 0.8):
        return None
    return sum(float(value) for value in values)


def forecast_rain_window_mix(records: List[Dict[str, Any]], location: Dict[str, Any], day: date) -> Dict[str, int]:
    window_days = get_seasonal_params(day, location)["rain_window"]
    window = History(records).window(day - timedelta(days=1), window_days)
    usable = [record for record in window if record.get("precipitation_sum") is not None]
    forecast_days = sum(record.get("source") == SOURCE_FORECAST for record in usable)
    archive_days = sum(record.get("source") == SOURCE_ARCHIVE for record in usable)
    return {
        "forecast_days": forecast_days,
        "archive_days": archive_days,
        "window_days": window_days,
        "coverage_days": len(usable),
    }


def heavily_forecast_dependent(mix: Dict[str, int]) -> bool:
    return mix["window_days"] > 0 and mix["forecast_days"] / mix["window_days"] >= HEAVY_FORECAST_RAIN_SHARE


def mean_air_temperature(hist: History, day: date, days: int) -> Optional[float]:
    """Mean air temperature for the `days` complete days before `day`, with at least 80% coverage."""
    records = hist.window(day - timedelta(days=1), days)
    values = [
        (float(r["temperature_2m_max"]) + float(r["temperature_2m_min"])) / 2
        for r in records
        if r.get("temperature_2m_max") is not None and r.get("temperature_2m_min") is not None
    ]
    if len(values) < math.ceil(days * 0.8):
        return None
    return sum(values) / len(values)


def historical_rainfall_percentile(hist: History, day: date) -> Optional[float]:
    """Compare preceding 90-day rain with one sample per prior year on the same calendar day.

    February 29 uses February 28 in non-leap years. The signal is omitted when fewer than
    LONG_TERM_RAIN_MIN_YEARS valid years are available.
    """
    comparable_totals = []
    for years_back in range(1, 16):
        candidate_year = day.year - years_back
        try:
            candidate_day = date(candidate_year, day.month, day.day)
        except ValueError:
            candidate_day = date(candidate_year, 2, 28)
        if day.month == 2 and day.day == 28:
            leap_candidate = date(candidate_year, 2, 29) if calendar.isleap(candidate_year) else None
            candidate_record = hist.get(candidate_day)
            leap_record = hist.get(leap_candidate) if leap_candidate else None
            candidate_day = leap_candidate if candidate_record is None and leap_record is not None else candidate_day
        if hist.get(candidate_day) is None:
            continue
        total = observed_rainfall_total(hist, candidate_day, LONG_TERM_RAIN_WINDOW_DAYS)
        if total is not None:
            comparable_totals.append(total)
    if len(comparable_totals) < LONG_TERM_RAIN_MIN_YEARS:
        return None

    current_total = observed_rainfall_total(hist, day, LONG_TERM_RAIN_WINDOW_DAYS)
    if current_total is None:
        return None
    below = sum(total < current_total for total in comparable_totals)
    tied = sum(total == current_total for total in comparable_totals)
    return (below + tied / 2) / len(comparable_totals)


def has_runoff(hist: History, day: date) -> bool:
    """Heavy-rain runoff: a day (last 3 complete days) with >= 15 mm where >50% fell in its busiest 2 hours.

    Needs hourly data (precip_peak_2h_mm); records without it (not yet backfilled, see backfill_runoff_data) get no penalty.
    """
    for rec in hist.window(day - timedelta(days=1), 3):
        total, peak = _precip(rec), rec.get("precip_peak_2h_mm")
        if peak is not None and total >= RUNOFF_MIN_DAY_MM and float(peak) > 0.5 * total:
            return True
    return False


def is_northern_hemisphere(location: Optional[Dict[str, Any]] = None) -> bool:
    """Locations without a usable latitude are treated as northern hemisphere."""
    try:
        return float((location or {}).get("latitude", 1.0)) >= 0
    except (TypeError, ValueError):
        return True


def late_season_decay_penalty(day: date, location: Optional[Dict[str, Any]] = None) -> int:
    """Linear 0..LATE_SEASON_DECAY_MAX penalty between LATE_SEASON_DECAY_START and LATE_SEASON_DECAY_END (northern hemisphere)."""
    if not is_northern_hemisphere(location):
        return 0
    start = date(day.year, *LATE_SEASON_DECAY_START)
    end = date(day.year, *LATE_SEASON_DECAY_END)
    if day <= start:
        return 0
    if day >= end:
        return LATE_SEASON_DECAY_MAX
    return round(LATE_SEASON_DECAY_MAX * (day - start).days / (end - start).days)


def hard_freeze_recent(hist: History, day: date) -> bool:
    """True if any of the last FROST_RECENT_DAYS complete days before `day` had Tmin below FROST_TMIN_C."""
    return any(r.get("temperature_2m_min") is not None and float(r["temperature_2m_min"]) < FROST_TMIN_C
               for r in hist.window(day - timedelta(days=1), FROST_RECENT_DAYS))


def severe_freeze_recent(hist: History, day: date, tmin: float = -4.0) -> bool:
    """True if any of the last FROST_RECENT_DAYS complete days before `day` had Tmin at or below tmin."""
    return any(r.get("temperature_2m_min") is not None and float(r["temperature_2m_min"]) <= tmin
               for r in hist.window(day - timedelta(days=1), FROST_RECENT_DAYS))


def frost_penalty(hist: History, day: date) -> int:
    """Points to subtract for freezing nights in the recent window of complete days before `day`.

    Combines a hard-freeze/light-frost penalty for the last FROST_RECENT_DAYS with a progressive penalty for
    the number of sub-zero nights in the last FROST_REPEAT_WINDOW_DAYS. Days without Tmin are ignored.
    """
    def tmins(days: int) -> List[float]:
        return [float(r["temperature_2m_min"]) for r in hist.window(day - timedelta(days=1), days) if r.get("temperature_2m_min") is not None]

    recent = tmins(FROST_RECENT_DAYS)
    penalty = 0
    if any(t < FROST_TMIN_C for t in recent):
        penalty += FROST_PENALTY
    elif any(t < FROST_LIGHT_TMIN_C for t in recent):
        penalty += FROST_LIGHT_PENALTY
    frost_nights = sum(1 for t in tmins(FROST_REPEAT_WINDOW_DAYS) if t < FROST_LIGHT_TMIN_C)
    penalty += min(FROST_REPEAT_PENALTY_MAX, FROST_REPEAT_PENALTY_PER_NIGHT * max(0, frost_nights - 1))
    return penalty


def _temp_range(value: Any) -> Optional[Tuple[float, float]]:
    try:
        low, high = float(value[0]), float(value[1])
    except (TypeError, ValueError, IndexError, KeyError):
        return None
    return (low, high) if low <= high else None


def get_seasonal_params(day: date, location: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Return `season`, `rain_window` (days) and `optimal_temp_range` (°C) for a calendar month.

    Early season (Aug–Sep) is warmer with a shorter rain window, mid season (Oct–Nov) is moderate
    with the standard window, and late season (Dec and other months) is cooler with a longer window.
    A location may override each season through `seasonal_params` ({"early"|"mid"|"late":
    {"rain_window": n, "optimal_temp": [lo, hi]}}); otherwise its `optimal_temp_range` is used.
    Missing or invalid config falls back to the defaults.
    """
    if day.month in (8, 9):
        season, window, temps = "early", 14, (12.0, 17.0)
    elif day.month in (10, 11):
        season, window, temps = "mid", FLUSH_RAIN_WINDOW_DAYS, (8.0, 14.0)
    else:
        season, window, temps = "late", 30, (5.0, 12.0)
    location = location or {}
    location_range = _temp_range(location.get("optimal_temp_range"))
    if location_range:
        temps = location_range
    overrides = location.get("seasonal_params")
    override = overrides.get(season) if isinstance(overrides, dict) else None
    if isinstance(override, dict):
        try:
            if int(override.get("rain_window")) > 0:
                window = int(override["rain_window"])
        except (TypeError, ValueError):
            pass
        temps = _temp_range(override.get("optimal_temp")) or temps
    return {"season": season, "rain_window": window, "optimal_temp_range": temps}


def weight_by_recency(harvest_date: date, reference_date: date, years_back: int = RECENCY_YEARS_BACK) -> float:
    """Weight an observation: 1.0 within `years_back` years of the reference date, 0.5 when older."""
    years_old = (reference_date - harvest_date).days / 365.25
    return OLD_OBSERVATION_WEIGHT if years_old > years_back else 1.0


def temperature_score(average_temperature: float, optimal_range: Tuple[float, float]) -> int:
    """Full points inside the optimal range; points fall off with distance outside it."""
    low, high = optimal_range
    distance = max(low - average_temperature, average_temperature - high, 0)
    return max(0, round(FLUSH_TEMPERATURE_SCORE_MAX - distance * TEMPERATURE_SLOPE_PER_DEGREE))


def soil_temperature_points(hist: History, day: date) -> int:
    """Score the mean soil temperature over the 7 complete days before `day`."""
    records = hist.window(day - timedelta(days=1), 7)
    values = [
        float(r["soil_temperature_0_to_7cm_mean"])
        for r in records
        if r.get("soil_temperature_0_to_7cm_mean") is not None
    ]
    if len(values) < 5:
        return 0
    mean = sum(values) / len(values)
    if 8.0 <= mean <= 15.0:
        return 3
    distance = min(abs(mean - 8.0), abs(mean - 15.0))
    return -min(5, round(distance))


def rainfall_distribution_score(hist: History, day: date, window_days: int = FLUSH_RAIN_WINDOW_DAYS) -> int:
    """Up to 5 bonus points for steady rain: the share of days in the window with more than 2 mm."""
    records = hist.window(day - timedelta(days=1), window_days)
    if sum(_precip(r) for r in records) <= 0:
        return 0
    rainy_days = sum(1 for r in records if _precip(r) > RAINY_DAY_MM)
    return round(RAIN_DISTRIBUTION_SCORE_MAX * min(1.0, rainy_days / window_days * 2))


def days_since_last_flush(past_harvests: List[Dict[str, Any]], day: date) -> Optional[int]:
    """Days between the most recent earlier find (small/medium/large yield) and `day`."""
    deltas = [
        (day - parse_date(h["date"])).days for h in past_harvests
        if h.get("observation_type") in (None, "harvest") and h.get("yield_tier") in {"small", "medium", "large"}
        and valid_date_string(h.get("date")) and parse_date(h["date"]) < day
    ]
    return min(deltas) if deltas else None


def flush_depletion_penalty(harvest_dates: List[date], day: date, recovery_days: int = DEFAULT_DEPLETION_RECOVERY_DAYS) -> int:
    """Negative score adjustment after a harvest: the fruiting bodies are gone and the substrate must recover."""
    earlier = [h for h in harvest_dates if h < day]
    if not earlier:
        return 0
    days_since = (day - max(earlier)).days
    if days_since > recovery_days:
        return 0
    for max_days, penalty in FLUSH_DEPLETION_TIERS:
        if days_since <= max_days:
            return -penalty
    return 0


def depletion_penalty_for_day(location: Dict[str, Any], past_harvests: List[Dict[str, Any]], day: date) -> int:
    """Depletion penalty from the most recent find, softened for small or buttons_young harvests."""
    finds = [
        h for h in past_harvests
        if h.get("observation_type") in (None, "harvest") and h.get("yield_tier") in {"small", "medium", "large"}
        and valid_date_string(h.get("date")) and parse_date(h["date"]) < day
    ]
    if not finds:
        return 0
    latest = max(finds, key=lambda h: parse_date(h["date"]))
    try:
        recovery_days = int(location.get("depletion_recovery_days", DEFAULT_DEPLETION_RECOVERY_DAYS))
    except (TypeError, ValueError):
        recovery_days = DEFAULT_DEPLETION_RECOVERY_DAYS
    if latest.get("cap_stage") == "buttons_young" or latest.get("yield_tier") == "small":
        try:
            small_days = int(location.get("small_harvest_depletion_recovery_days", SMALL_HARVEST_DEPLETION_RECOVERY_DAYS))
        except (TypeError, ValueError):
            small_days = SMALL_HARVEST_DEPLETION_RECOVERY_DAYS
        recovery_days = min(recovery_days, small_days)
    penalty = flush_depletion_penalty([parse_date(latest["date"])], day, recovery_days)
    if penalty and (latest.get("cap_stage") == "buttons_young" or latest.get("yield_tier") == "small"):
        try:
            factor = float(location.get("early_harvest_penalty_factor", DEFAULT_EARLY_HARVEST_PENALTY_FACTOR))
        except (TypeError, ValueError):
            factor = DEFAULT_EARLY_HARVEST_PENALTY_FACTOR
        penalty = round(penalty * max(0.0, min(1.0, factor)))
    return penalty


def rainfall_score(rainfall: float) -> int:
    """Give up to 30 points for recent rain, saturating at RAIN_SATURATION_MM without an uncalibrated wet penalty."""
    scaled = max(0.0, min(FLUSH_RAIN_SCORE_MAX, rainfall * FLUSH_RAIN_SCORE_MAX / RAIN_SATURATION_MM))
    return round(scaled)


def soil_moisture_points(moisture: float) -> int:
    """Continuous soil-moisture adjustment: -10 at/below 0.20, 0 at 0.28, +5 at/above 0.35."""
    if moisture <= SOIL_DRY:
        return -10
    if moisture >= SOIL_WET:
        return 5
    if moisture < SOIL_MID:
        return round(-10 * (SOIL_MID - moisture) / (SOIL_MID - SOIL_DRY))
    return round(5 * (moisture - SOIL_MID) / (SOIL_WET - SOIL_MID))


def calculate_score_for_day(location: Dict[str, Any], daily: Dict[str, Any], historical: Any, past_harvests: List[Dict[str, Any]]) -> Tuple[int, str, str]:
    """Return a 0–100 favourability index, not a calibrated probability."""
    hist = historical if isinstance(historical, History) else History(historical)
    score = 0
    status = "🟡 Monitoring"
    quality = ""

    d = parse_date(daily["date"])
    no_find_today = any(
        h.get("observation_type") == "no_mushrooms" and valid_date_string(h.get("date")) and parse_date(h["date"]) == d
        for h in past_harvests
    )
    if not month_day_window(d, location):
        return 0, "❌ Outside mushroom season", ""

    seasonal = get_seasonal_params(d, location)
    rain_window = seasonal["rain_window"]
    rainfall = observed_rainfall_total(hist, d, rain_window)
    recent_rainfall = observed_rainfall_total(hist, d, RECENT_RAIN_WINDOW_DAYS)
    background_score = rainfall_score(rainfall) if rainfall is not None else None
    # Recent rain is scaled to the background window's units so both parts share one 0-30 scale.
    recent_score = rainfall_score(recent_rainfall * rain_window / RECENT_RAIN_WINDOW_DAYS) if recent_rainfall is not None else None
    if background_score is not None and recent_score is not None:
        recent_score = min(recent_score, background_score + 8)
        score += round(RECENT_RAIN_WEIGHT * recent_score + BACKGROUND_RAIN_WEIGHT * background_score)
    elif background_score is not None or recent_score is not None:
        score += background_score if background_score is not None else recent_score
    if rainfall is not None:
        score += rainfall_distribution_score(hist, d, rain_window)

    long_term_rainfall_percentile = historical_rainfall_percentile(hist, d)
    if long_term_rainfall_percentile is not None and long_term_rainfall_percentile < 0.5:
        score -= round(
            DROUGHT_SCORE_PENALTY_MAX * (0.5 - long_term_rainfall_percentile) / 0.5
        )

    average_temperature = mean_air_temperature(hist, d, FLUSH_TEMPERATURE_WINDOW_DAYS)
    if average_temperature is not None:
        score += temperature_score(average_temperature, seasonal["optimal_temp_range"])
    score += soil_temperature_points(hist, d)

    if has_runoff(hist, d):
        score -= 5

    score -= frost_penalty(hist, d)
    decay = late_season_decay_penalty(d, location)
    score -= decay
    recent_days = hist.window(d - timedelta(days=1), 3)
    if any(r.get("temperature_2m_max") is not None and float(r["temperature_2m_max"]) > HEAT_TMAX_C for r in recent_days):
        score -= HEAT_PENALTY

    humidity = daily.get("relative_humidity_2m_mean")
    if humidity is not None:
        if humidity > HUMIDITY_HIGH:
            score += 5
        elif humidity < HUMIDITY_LOW:
            score -= 3

    soil_moisture = daily.get("soil_moisture_0_to_7cm_mean")
    if soil_moisture is not None:
        score += soil_moisture_points(float(soil_moisture))
        if soil_moisture <= SOIL_DRY:
            status = "💧 Too dry – low soil moisture"
    if decay > 0 and (hard_freeze_recent(hist, d) or severe_freeze_recent(hist, d)):
        status = "❄️ Season terminated by frost"

    if str(location.get("soil_pH") or "").strip().lower() in ("alkaline", "basic", "calcareous"):
        score -= 5

    no_find_penalty = 0
    for harvest in past_harvests:
        harvest_date = harvest.get("date")
        if not valid_date_string(harvest_date):
            continue
        delta_days = (d - parse_date(harvest_date)).days
        if harvest.get("observation_type") == "no_mushrooms":
            if delta_days == 0:
                score = min(score, 20)
                status = "🔎 No mushrooms found (field observation)"
            elif 1 <= delta_days <= len(NO_FIND_PENALTY):
                no_find_penalty = max(no_find_penalty, NO_FIND_PENALTY[delta_days - 1])
                if status == "🟡 Monitoring":
                    status = "🔎 Recent empty visit lowers odds"
            continue
        if harvest.get("cap_stage") == "buttons_young" and 1 <= delta_days <= 4:
            score += 10
        elif harvest.get("cap_stage") == "old_overripe" and 1 <= delta_days <= 7:
            score -= 10
            status = "🍂 Exhaustion / post-flush cooling off"

    score -= no_find_penalty
    depletion = depletion_penalty_for_day(location, past_harvests, d)
    if depletion:
        score += depletion
        if status == "🟡 Monitoring":
            status = "🌱 Flush depleted after harvest"

    # Prior medium/large finds at this site add a modest affinity signal.
    positive = sum(
        weight_by_recency(parse_date(h["date"]), d) for h in past_harvests
        if h.get("yield_tier") in {"medium", "large"} and valid_date_string(h.get("date"))
        and 0 < (d - parse_date(h["date"])).days <= AFFINITY_LOOKBACK_DAYS
    )
    if positive >= 2:
        score += 5

    # Post-flush persistence: a find 7-14 days ago plus still-favourable conditions suggests a continuing flush.
    since_flush = days_since_last_flush(past_harvests, d)
    if (since_flush is not None and POST_FLUSH_MIN_DAYS <= since_flush <= POST_FLUSH_MAX_DAYS
            and depletion == 0 and score > POST_FLUSH_FULL_BASE_SCORE - POST_FLUSH_RAMP and not no_find_today):
        score += round(POST_FLUSH_BONUS * min(1.0, (score - (POST_FLUSH_FULL_BASE_SCORE - POST_FLUSH_RAMP)) / POST_FLUSH_RAMP))
        if status == "🟡 Monitoring":
            status = "🔄 Post-flush conditions persist"

    # Harvest quality / risk is separate from the 20-day flush-favourability temperature signal.
    temps = [float(r["temperature_2m_max"]) for r in hist.window(d - timedelta(days=1), 7) if r.get("temperature_2m_max") is not None]
    if temps:
        avg_max = sum(temps) / len(temps)
        if avg_max > 18:
            quality = "Warmer week: higher maggot risk, pick early"
        elif 8 <= avg_max <= 15:
            quality = "Cool week: firm caps likely"

    if no_find_today:
        score = min(score, 20)
    score = max(0, min(100, score))
    if not status or status == "🟡 Monitoring":
        status = "✅ Viable conditions" if score >= DEFAULT_ALERT_THRESHOLD else "⚠️ Watch closely"
    return int(score), status, quality


def score_breakdown(location: Dict[str, Any], daily: Dict[str, Any], hist: History) -> Dict[str, int]:
    """Diagnostics: the weather-driven terms of calculate_score_for_day for one day (excludes harvest-log adjustments)."""
    d = parse_date(daily["date"])
    seasonal = get_seasonal_params(d, location)
    rain = observed_rainfall_total(hist, d, seasonal["rain_window"])
    recent = observed_rainfall_total(hist, d, RECENT_RAIN_WINDOW_DAYS)
    temp = mean_air_temperature(hist, d, FLUSH_TEMPERATURE_WINDOW_DAYS)
    soil = daily.get("soil_moisture_0_to_7cm_mean")
    background_score = rainfall_score(rain) if rain is not None else None
    recent_score = (
        rainfall_score(recent * seasonal["rain_window"] / RECENT_RAIN_WINDOW_DAYS)
        if recent is not None else None
    )
    if background_score is not None and recent_score is not None:
        recent_score = min(recent_score, background_score + 8)
        rain_points = round(RECENT_RAIN_WEIGHT * recent_score + BACKGROUND_RAIN_WEIGHT * background_score)
    elif background_score is not None or recent_score is not None:
        rain_points = background_score if background_score is not None else recent_score
    else:
        rain_points = 0
    out = {
        "rain": rain_points,
        "rain_distribution": rainfall_distribution_score(hist, d, seasonal["rain_window"]) if rain is not None else 0,
        "temperature": temperature_score(temp, seasonal["optimal_temp_range"]) if temp is not None else 0,
        "soil_temperature": soil_temperature_points(hist, d),
        "soil_moisture": soil_moisture_points(float(soil)) if soil is not None else 0,
        "frost": -frost_penalty(hist, d),
        "late_season_decay": -late_season_decay_penalty(d, location),
    }
    return out


def location_signature(location: Dict[str, Any]) -> str:
    """Fingerprint of the config fields that influence scores; a change recomputes the stored series."""
    keys = ("tree_species", "soil_pH", "past_harvests", "optimal_temp_range", "seasonal_params", "season_start",
            "season_end", "soil_temperature_optimal_range", "soil_temperature_score_weight")
    blob = json.dumps({k: location.get(k) for k in keys}, sort_keys=True, default=str)
    return f"{MODEL_VERSION}:{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


def update_daily_scores(location: Dict[str, Any], records: List[Dict[str, Any]], loc_store: Dict[str, Any]) -> int:
    """Maintain loc_store['daily_scores'] = {date: {score, status, quality, source}} incrementally.

    Archive-sourced days are final and scored once; forecast-sourced days are rescored each run. Everything
    is rescored when the model version or this location's scoring-relevant config changes.
    Returns the number of days (re)scored.
    """
    signature = location_signature(location)
    scores = loc_store.get("daily_scores")
    if not isinstance(scores, dict) or loc_store.get("scores_signature") != signature:
        scores = {}
    hist = History(records)
    harvests = location.get("past_harvests", [])
    force = set(loc_store.get("pending_rescore") or [])
    updated = 0
    result: Dict[str, Any] = {}
    for rec in records:
        d = rec["date"]
        prev = scores.get(d)
        if (d not in force and isinstance(prev, dict) and prev.get("source") == rec["source"]
                and rec["source"] in (SOURCE_ARCHIVE, SOURCE_SAMPLE)):
            result[d] = prev
            continue
        score, status, quality = calculate_score_for_day(location, rec, hist, harvests)
        result[d] = {"score": score, "status": status, "quality": quality, "source": rec["source"]}
        updated += 1
    loc_store["daily_scores"] = result
    loc_store["scores_signature"] = signature
    loc_store["pending_rescore"] = []
    return updated


def backtest_accuracy(location: Dict[str, Any], records: List[Dict[str, Any]],
                      harvests: List[Dict[str, Any]], threshold: int) -> Dict[str, Any]:
    """Compare scores only with dated visits; unvisited days are unknown, not failures.

    Find days are scored with their harvest known; no-find days are recomputed without their own
    observation to avoid evaluating a no-find against a score it already capped. Find days are
    split by whether an earlier harvest (flush depletion) was still suppressing the score.
    """
    hist = History(records)
    outcomes: Dict[str, bool] = {}
    for observation in harvests:
        day = observation.get("date")
        if not valid_date_string(day):
            continue
        observation_type = observation.get("observation_type")
        if observation_type == "no_mushrooms":
            outcomes.setdefault(day, False)
        elif observation_type in (None, "harvest") and observation.get("yield_tier") in {"small", "medium", "large"}:
            outcomes[day] = True

    evaluated = []
    for day, found in outcomes.items():
        daily = hist.get(parse_date(day))
        if daily is None:
            continue
        prior_observations = [
            observation for observation in harvests
            if valid_date_string(observation.get("date")) and (found or observation["date"] != day)
        ]
        score, _, _ = calculate_score_for_day(location, daily, hist, prior_observations)
        post_harvest = depletion_penalty_for_day(location, harvests, parse_date(day)) < 0
        evaluated.append((score, found, post_harvest, day))

    finds = [score for score, found, _, _ in evaluated if found]
    no_finds = [score for score, found, _, _ in evaluated if not found]
    true_false_negatives = [d for score, found, post, d in evaluated if found and not post and score < threshold]
    post_harvest_low_finds = [d for score, found, post, d in evaluated if found and post and score < threshold]
    post_harvest_false_positives = [d for score, found, post, d in evaluated if not found and post and score >= threshold]
    suggested_threshold = None
    if finds:
        ordered = sorted(finds)
        suggested_threshold = (ordered[int(len(ordered) * (1 - CALIBRATION_HIT_RATE_MIN))] // 5) * 5
    finds_above = sum(score >= threshold for score in finds)
    no_finds_above = sum(score >= threshold for score in no_finds)
    return {
        "threshold": threshold,
        "observed_days": len(evaluated),
        "find_days": len(finds),
        "no_find_days": len(no_finds),
        "finds_above_threshold": finds_above,
        "no_finds_above_threshold": no_finds_above,
        "find_hit_rate": round(finds_above / len(finds), 3) if finds else None,
        "no_find_above_threshold_rate": round(no_finds_above / len(no_finds), 3) if no_finds else None,
        "mean_score_on_find_days": round(sum(finds) / len(finds), 1) if finds else None,
        "mean_score_on_no_find_days": round(sum(no_finds) / len(no_finds), 1) if no_finds else None,
        "suggested_threshold": suggested_threshold,
        "true_false_negatives": len(true_false_negatives),
        "genuine_miss_dates": true_false_negatives,
        "post_harvest_low_find_days": len(post_harvest_low_finds),
        "post_harvest_low_find_dates": post_harvest_low_finds,
        "find_breakdown": {
            "above_threshold": finds_above,
            "post_harvest_below_threshold": len(post_harvest_low_finds),
            "genuine_below_threshold": len(true_false_negatives),
        },
        "post_harvest_false_positives": len(post_harvest_false_positives),
        "find_weekday_counts": find_weekday_counts(location, harvests),
    }


def _binary_auc(samples: List[Tuple[int, bool]]) -> Optional[float]:
    positives = [score for score, found in samples if found]
    negatives = [score for score, found in samples if not found]
    if len(positives) < BACKTEST_MIN_CLASS_SAMPLES or len(negatives) < BACKTEST_MIN_CLASS_SAMPLES:
        return None
    wins = sum(1 if positive > negative else 0.5 if positive == negative else 0
               for positive in positives for negative in negatives)
    return wins / (len(positives) * len(negatives))


def forecast_snapshot_backtest(db: Dict[str, Any], cfg: Dict[str, Any],
                               harvest_log: Dict[str, Any], threshold: int) -> Dict[str, Any]:
    snapshots = db.get("forecast_snapshots") if isinstance(db.get("forecast_snapshots"), list) else []
    matched: List[Tuple[int, bool]] = []
    for index, location in enumerate(cfg.get("LOCATIONS", [])):
        name = location.get("name")
        alias = public_location_alias(index)
        outcomes: Dict[str, bool] = {}
        for observation in merge_harvests(location.get("past_harvests", []), harvest_log, name):
            day = observation.get("date")
            if not valid_date_string(day):
                continue
            if observation.get("observation_type") == "no_mushrooms":
                outcomes.setdefault(day, False)
            elif observation.get("observation_type") in (None, "harvest") and observation.get("yield_tier") in {"small", "medium", "large"}:
                outcomes[day] = True
        for day, found in outcomes.items():
            eligible = []
            for snapshot in snapshots:
                run_date = snapshot.get("run_date") if isinstance(snapshot, dict) else None
                score_map = snapshot.get("scores", {}) if isinstance(snapshot, dict) else {}
                scores = score_map.get(alias, []) if isinstance(score_map, dict) else []
                values = {row[0]: row[1] for row in scores if isinstance(row, list) and len(row) == 2}
                if valid_date_string(run_date) and run_date < day and day in values:
                    eligible.append((run_date, values[day]))
            if eligible:
                matched.append((int(max(eligible)[1]), found))
    positives = sum(1 for _, found in matched if found)
    negatives = len(matched) - positives
    hits = sum(1 for score, found in matched if found and score >= threshold)
    false_alarms = sum(1 for score, found in matched if not found and score >= threshold)
    return {
        "samples": len(matched),
        "finds": positives,
        "no_finds": negatives,
        "hits": hits,
        "hit_rate": hits / positives if positives else None,
        "false_alarms": false_alarms,
        "false_alarm_rate": false_alarms / negatives if negatives else None,
        "auc": _binary_auc(matched),
    }


def format_forecast_backtest(metrics: Dict[str, Any]) -> str:
    def rate(value: Optional[float]) -> str:
        return "n/a" if value is None else f"{value:.1%}"

    lines = [
        f"Forecast snapshot backtest: {metrics['samples']} matched observations",
        f"Hit rate: {metrics['hits']}/{metrics['finds']} ({rate(metrics['hit_rate'])})",
        f"False alarms: {metrics['false_alarms']}/{metrics['no_finds']} ({rate(metrics['false_alarm_rate'])})",
    ]
    if metrics["auc"] is None:
        lines.append(
            f"WARNING: sample too small for AUC; need at least {BACKTEST_MIN_CLASS_SAMPLES} finds "
            f"and {BACKTEST_MIN_CLASS_SAMPLES} no-finds."
        )
    else:
        lines.append(f"AUC: {metrics['auc']:.3f}")
    return "\n".join(lines)


WEEKDAY_NAMES = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")


def find_weekday_counts(location: Dict[str, Any], harvests: List[Dict[str, Any]]) -> Dict[str, int]:
    """Count finds per weekday (Mon..Sun); finds clustering on visit days may reflect visiting bias, not weather."""
    counts = {name: 0 for name in WEEKDAY_NAMES}
    for h in harvests:
        if (h.get("observation_type") in (None, "harvest") and h.get("yield_tier") in {"small", "medium", "large"}
                and valid_date_string(h.get("date"))):
            counts[WEEKDAY_NAMES[parse_date(h["date"]).weekday()]] += 1
    return counts


def calibration_messages(name: str, backtest: Dict[str, Any], location: Optional[Dict[str, Any]] = None) -> List[str]:
    """Log lines recommending threshold changes (low find-day hit rate) and noting weekday clustering of finds."""
    messages = []
    hit_rate = backtest.get("find_hit_rate")
    find_days = backtest.get("find_days") or 0
    if hit_rate is not None and find_days >= CALIBRATION_MIN_FIND_DAYS and hit_rate < CALIBRATION_HIT_RATE_MIN:
        message = f"[WARN] {name}: only {hit_rate * 100:.0f}% of {find_days} find-days scored at or above {backtest.get('threshold')}"
        suggested = backtest.get("suggested_threshold")
        if suggested is not None and suggested < backtest.get("threshold", 0):
            message += f"; consider ALERT_THRESHOLD={suggested}"
        messages.append(message)
        post_low = backtest.get("post_harvest_low_find_days") or 0
        genuine = backtest.get("true_false_negatives") or 0
        if post_low and post_low >= genuine:
            messages.append(
                f"[WARN] {name}: {post_low} low find-days followed an earlier harvest and {genuine} did not; "
                "depletion is likely suppressing scores, so adjust depletion before lowering the threshold")
        elif genuine:
            messages.append(
                f"[WARN] {name}: {genuine} low find-days had no recent harvest and {post_low} did; "
                "scores are low across the board, so a lower threshold would help more than depletion changes")
    mean_find = backtest.get("mean_score_on_find_days")
    mean_no_find = backtest.get("mean_score_on_no_find_days")
    if find_days >= CALIBRATION_MIN_FIND_DAYS and mean_find is not None and mean_no_find is not None and mean_find <= mean_no_find:
        messages.append(
            f"[WARN] {name}: mean score on find days ({mean_find}) is not above no-find days ({mean_no_find}); "
            "the score does not differentiate finds from non-finds, so lowering the threshold may only add false alerts")
    counts = backtest.get("find_weekday_counts") or {}
    total = sum(counts.values())
    if total >= CALIBRATION_MIN_FIND_DAYS:
        day, top = max(counts.items(), key=lambda kv: kv[1])
        if top / total >= 0.5:
            note = f"[INFO] {name}: {top}/{total} finds were on {day}; visit-day bias may inflate this pattern"
            preferred = (location or {}).get("preferred_visit_days")
            if isinstance(preferred, list) and WEEKDAY_NAMES.index(day) in preferred:
                note += " (a configured preferred visit day)"
            messages.append(note)
    return messages


def find_weekend_best(location: Dict[str, Any], records: List[Dict[str, Any]], harvests: List[Dict[str, Any]], scores: Optional[Dict[str, Any]] = None, today: Optional[date] = None) -> Tuple[int, str, str, str]:
    """Best Fri/Sat/Sun score within the coming 7 days (today..today+6)."""
    today = today or datetime.now(timezone.utc).date()
    hist = History(records)
    valid_days = []
    for record in records:
        dt = parse_date(record["date"])
        if dt.weekday() in {4, 5, 6} and today <= dt <= today + timedelta(days=6):
            stored = (scores or {}).get(record["date"])
            if stored:
                score, status, flag = stored["score"], stored["status"], stored.get("quality", "")
            else:
                score, status, flag = calculate_score_for_day(location, record, hist, harvests)
            valid_days.append((dt, score, status, flag))
    if not valid_days:
        return 0, "N/A", "", ""
    best_date, score, status, flag = max(valid_days, key=lambda item: item[1])
    return score, format_display_date(best_date), status, flag


def estimate_days_until_threshold(records: List[Dict[str, Any]], scores: Dict[str, Any],
                                  today: date, threshold: int) -> Optional[int]:
    previous = None
    for day, value in sorted(scores.items()):
        if valid_date_string(day) and parse_date(day) <= today:
            previous = int(value.get("score", 0))
    if previous is not None and previous >= threshold:
        return 0
    for record in sorted(records, key=lambda item: item.get("date", "")):
        day = record.get("date")
        if record.get("source") != SOURCE_FORECAST or not valid_date_string(day) or parse_date(day) <= today:
            continue
        current = scores.get(day)
        if not isinstance(current, dict):
            continue
        current_score = int(current.get("score", 0))
        if previous is not None and previous < threshold <= current_score:
            return (parse_date(day) - today).days
        previous = current_score
    return None


def explain_score(location: Dict[str, Any], records: List[Dict[str, Any]], best_day: str, status: str) -> str:
    """Return concise weather context for a scored day."""
    try:
        day = parse_date(best_day)
    except (TypeError, ValueError):
        return "weather unavailable"
    hist = History(records)
    daily = hist.get(day)
    if not daily:
        return "weather unavailable"

    signals = []
    if "no mushrooms found" in status.lower():
        signals.append("no-find observation caps score")
    elif "outside mushroom season" in status.lower():
        signals.append("outside season")
    else:
        rainfall = observed_rainfall_total(hist, day, FLUSH_RAIN_WINDOW_DAYS)
        if rainfall is not None:
            signals.append(f"{rainfall:.0f} mm rain / {FLUSH_RAIN_WINDOW_DAYS} days")

        average_temperature = mean_air_temperature(hist, day, FLUSH_TEMPERATURE_WINDOW_DAYS)
        if average_temperature is not None:
            signals.append(f"{average_temperature:.1f}°C mean / {FLUSH_TEMPERATURE_WINDOW_DAYS} days")

        rainfall_percentile = historical_rainfall_percentile(hist, day)
        if rainfall_percentile is not None:
            signals.append(f"90-day rain at {rainfall_percentile * 100:.0f}th percentile")

        soil_moisture = daily.get("soil_moisture_0_to_7cm_mean")
        if soil_moisture is not None and soil_moisture <= 0.20:
            signals.append(f"soil moisture {float(soil_moisture):.3f} m³/m³")
        elif soil_moisture is not None and soil_moisture > 0.35:
            signals.append("soil moisture high")
        if has_runoff(hist, day):
            signals.append("possible runoff")

    if not signals:
        signals.append("no standout weather signal")
    return "; ".join(signals)


def score_verdict_tag(score: int, status: Optional[str], threshold: int = 55) -> str:
    status_text = (status or "").lower()
    if "no mushrooms found" in status_text:
        return "🔎 No mushrooms found (field observation)"
    category = status_category(status)
    if category in VERDICT_SPECIAL_TAGS:
        return VERDICT_SPECIAL_TAGS[category]
    for upper_bound, label in VERDICT_LOW_TIERS:
        if score < upper_bound:
            return label
    if score >= max(VERY_FAVOURABLE_THRESHOLD, threshold):
        return VERDICT_DEFINITE_GO
    if score >= threshold:
        return VERDICT_GO
    return VERDICT_WORTH_LOOK


def score_color(score: int) -> str:
    hue = round(max(0, min(1, (score - SCORE_COLOR_MIN) / (SCORE_COLOR_MAX - SCORE_COLOR_MIN))) * 120)
    return f"hsl({hue} 80% 58%)"
