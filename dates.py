"""Central date handling: ISO yyyy-MM-dd for storage, 'dd MMMM yyyy' (e.g. 02 October 2026) for display.

Dates are date-only values (no time zone), so no conversion can shift them by a day.
"""
import re
from datetime import date, datetime
from typing import Any

MONTHS = ("January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December")
_MONTH_LOOKUP = {name.lower(): i for i, name in enumerate(MONTHS, 1)}
_ISO_RE = re.compile(r"^(\d{4})-(\d{1,2})-(\d{1,2})$")
_DOTTED_RE = re.compile(r"^(\d{1,2})\.(\d{1,2})\.(\d{4})$")
_LONG_RE = re.compile(r"^(\d{1,2})\.?\s+([A-Za-z]+)\.?\s+(\d{4})$")


def format_display_date(value: Any) -> str:
    """Render a date (or any accepted input string) as 'dd MMMM yyyy', independent of locale."""
    day = value if isinstance(value, date) and not isinstance(value, datetime) else (value.date() if isinstance(value, datetime) else parse_user_date(value))
    return f"{day.day:02d} {MONTHS[day.month - 1]} {day.year}"


def parse_user_date(value: Any) -> date:
    """Parse yyyy-MM-dd, dd.MM.yyyy or 'dd MMMM yyyy' (English month names, case-insensitive). Raises ValueError."""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str):
        raise ValueError(f"invalid date: {value!r}")
    text = value.strip()
    match = _ISO_RE.match(text)
    if match:
        y, m, d = (int(g) for g in match.groups())
    elif (match := _DOTTED_RE.match(text)):
        d, m, y = (int(g) for g in match.groups())
    elif (match := _LONG_RE.match(text)):
        d, y = int(match.group(1)), int(match.group(3))
        m = _MONTH_LOOKUP.get(match.group(2).lower(), 0)
    else:
        raise ValueError(f"unrecognised date format: {value!r} (use yyyy-MM-dd, dd.MM.yyyy or dd MMMM yyyy)")
    return date(y, m, d)  # raises ValueError for impossible dates


def normalize_to_iso(value: Any) -> str:
    """Normalise any accepted input to ISO yyyy-MM-dd."""
    return parse_user_date(value).isoformat()
