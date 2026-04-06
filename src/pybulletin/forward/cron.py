"""Minimal cron expression evaluator for BBS forward scheduling.

Supports the five-field standard cron format::

    <minute> <hour> <dom> <month> <dow>

Each field accepts:
  *       — any value
  n       — exact value
  */n     — every n (step from 0)
  n-m     — range (inclusive)
  n,m,... — list of values

Designed for the common BBS forwarding patterns:
  ``0 */2 * * *``   — every 2 hours on the hour
  ``*/15 * * * *``  — every 15 minutes
  ``0 6,18 * * *``  — 06:00 and 18:00 UTC
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


def matches(expr: str, dt: datetime | None = None) -> bool:
    """Return True if *dt* (default: now UTC) matches the cron *expr*.

    Invalid expressions always return True so forwarding is never silently
    suppressed by a misconfigured schedule.
    """
    if dt is None:
        dt = datetime.now(timezone.utc)
    try:
        return _matches(expr, dt)
    except Exception:
        return True   # fail-open: don't block forwarding on bad cron


def _matches(expr: str, dt: datetime) -> bool:
    parts = expr.strip().split()
    if len(parts) != 5:
        return True

    minute, hour, dom, month, dow = parts
    return (
        _field(minute, dt.minute, 0, 59)
        and _field(hour,   dt.hour,   0, 23)
        and _field(dom,    dt.day,    1, 31)
        and _field(month,  dt.month,  1, 12)
        and _field(dow,    dt.weekday(), 0, 6)   # 0=Monday per Python
    )


def _field(spec: str, value: int, lo: int, hi: int) -> bool:
    if spec == "*":
        return True

    for part in spec.split(","):
        part = part.strip()
        # */n  — step
        if part.startswith("*/"):
            step = int(part[2:])
            if step > 0 and (value - lo) % step == 0:
                return True
        # n-m  — range
        elif "-" in part and not part.startswith("-"):
            a, _, b = part.partition("-")
            if int(a) <= value <= int(b):
                return True
        # n    — exact
        else:
            if int(part) == value:
                return True

    return False


def next_run_minutes(expr: str, from_dt: datetime | None = None) -> int | None:
    """Return minutes until the next matching time (max scan: 1 week).

    Returns None if no match is found within the scan window (should never
    happen for valid expressions).
    """
    if from_dt is None:
        from_dt = datetime.now(timezone.utc)
    from datetime import timedelta
    dt = from_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    for _ in range(60 * 24 * 7):  # scan up to 1 week
        if _matches(expr, dt):
            delta = dt - from_dt
            return int(delta.total_seconds() // 60)
        dt += timedelta(minutes=1)
    return None
