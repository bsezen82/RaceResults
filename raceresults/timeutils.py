"""Helpers for parsing the Argeus Timing (G-Live) time/gap string formats.

Observed formats in the wild:
  race time / clock time : "06h00'13,060"  or  "00h28'30"  (H may be 1-2 digits, ms optional)
  gap                     : "+0:49"  "+01:51"  "+03:24:52"  (M:SS, MM:SS or H:MM:SS)
  status strings          : "Withdrawal", "Disqualified" (appear in place of a time)
"""
from __future__ import annotations

import re
from typing import Optional

_TIME_RE = re.compile(r"^(\d+)h(\d+)'(\d+)(?:,(\d+))?$")
_GAP_RE = re.compile(r"^([+-])?(\d+):(\d+)(?::(\d+))?$")

STATUS_TEXTS = {"Withdrawal", "Disqualified"}


def parse_time(value: Optional[str]) -> Optional[float]:
    """Parse "HHhMM'SS,mmm" into total seconds. Returns None if not a time value."""
    if not value:
        return None
    m = _TIME_RE.match(value.strip())
    if not m:
        return None
    hours, minutes, seconds, millis = m.groups()
    total = int(hours) * 3600 + int(minutes) * 60 + int(seconds)
    if millis:
        total += int(millis) / (10 ** len(millis))
    return total


def parse_gap(value: Optional[str]) -> Optional[float]:
    """Parse a gap string like "+01:51" or "+03:24:52" into signed total seconds."""
    if not value:
        return None
    m = _GAP_RE.match(value.strip())
    if not m:
        return None
    sign, a, b, c = m.groups()
    if c is not None:
        hours, minutes, seconds = int(a), int(b), int(c)
    else:
        hours, minutes, seconds = 0, int(a), int(b)
    total = hours * 3600 + minutes * 60 + seconds
    return -total if sign == "-" else total


def status_for(value: Optional[str]) -> Optional[str]:
    """Return a normalized status keyword if `value` is a known non-time status string."""
    if value and value.strip() in STATUS_TEXTS:
        return value.strip().lower()
    return None


def format_seconds(total_seconds: Optional[float]) -> Optional[str]:
    """Format seconds back into "H:MM:SS" for display."""
    if total_seconds is None:
        return None
    total = int(round(total_seconds))
    hours, rem = divmod(total, 3600)
    minutes, seconds = divmod(rem, 60)
    return f"{hours}:{minutes:02d}:{seconds:02d}"


def parse_pace(value: Optional[str]) -> Optional[float]:
    """Parse a comma-decimal pace string ("12,46") into a float."""
    if not value:
        return None
    try:
        return float(value.strip().replace(",", "."))
    except ValueError:
        return None
