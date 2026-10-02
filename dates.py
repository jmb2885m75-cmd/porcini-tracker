"""Central date handling: ISO yyyy-MM-dd for storage and 'Ddd. DD Mon. YYYY' for display.

Dates are date-only values (no time zone), so no conversion can shift them by a day.
"""
import re
from datetime import date, datetime
from typing import Any

MONTHS = ("Jan.", "Feb.", "Mar.", "Apr.", "May", "Jun.", "Jul.", "Aug.", "Sep.", "Oct.", "Nov.", "Dec.")
WEEKDAYS = ("Mon.", "Tue.", "Wed.", "Thu.", "Fri.", "Sat.", "Sun.")
_FULL_MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_MONTH_LOOKUP = {
    name.lower().rstrip("."): i
    for i, name in enumerate(_FULL_MONTHS, 1)
}
_MONTH_LOOKUP.update({name.lower().rstrip("."): i for i, name in enumerate(MONTHS, 1)})
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DOTTED_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_LONG_RE = re.compile(r"^(?:(Mon|Tue|Wed|Thu|Fri|Sat|Sun)\.?\s+)?(\d{1,2})\.?\s+([A-Za-z]+)\.?\s+(\d{4})$", re.IGNORECASE)


def format_display_date(value: Any) -> str:
    """Render a date (or any accepted input string) as 'Ddd. dd Mon. yyyy', independent of locale."""
    day = value if isinstance(value, date) and not isinstance(value, datetime) else (value.date() if isinstance(value, datetime) else parse_user_date(value))
    return f"{WEEKDAYS[day.weekday()]} {day.day:02d} {MONTHS[day.month - 1]} {day.year}"


def parse_user_date(value: Any) -> date:
    """Parse ISO, dotted, full/abbreviated English dates, optionally prefixed by weekday. Raises ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid date: {value!r}")
    text = value.strip()
    weekday = None
    match = _ISO_RE.match(text)
    if match:
        y, m, d = (int(g) for g in match.groups())
    elif (match := _DOTTED_RE.match(text)):
        d, m, y = (int(g) for g in match.groups())
    elif (match := _LONG_RE.match(text)):
        weekday, day_text, month_text, year_text = match.groups()
        d, y = int(day_text), int(year_text)
        m = _MONTH_LOOKUP.get(month_text.lower().rstrip("."), 0)
    else:
        raise ValueError(f"unrecognised date format: {value!r} (use yyyy-MM-dd, dd.MM.yyyy or dd MMMM yyyy)")
    parsed = date(y, m, d)  # raises ValueError for impossible dates
    if weekday and WEEKDAYS[parsed.weekday()][:3].lower() != weekday.lower():
        raise ValueError(f"weekday does not match date: {value!r}")
    return parsed


def normalize_to_iso(value: Any) -> str:
    """Normalise any accepted input to ISO yyyy-MM-dd."""
    return parse_user_date(value).isoformat()
