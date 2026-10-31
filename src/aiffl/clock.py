"""The league's notion of "now". Tests and rehearsals can pin it with AIFFL_NOW=2026-11-08T17:55:00Z."""

import os
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

EASTERN = ZoneInfo("America/New_York")
ISO = "%Y-%m-%dT%H:%M:%SZ"


def now() -> datetime:
    pinned = os.environ.get("AIFFL_NOW")
    if pinned:
        return datetime.strptime(pinned, ISO).replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def now_iso() -> str:
    return now().strftime(ISO)


def parse(iso: str) -> datetime:
    return datetime.strptime(iso, ISO).replace(tzinfo=timezone.utc)


def eastern(dt: datetime | None = None) -> str:
    return (dt or now()).astimezone(EASTERN).strftime("%A %Y-%m-%d %H:%M %Z")


DAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


def last_occurrence(spec: str, at: datetime | None = None) -> datetime:
    """Most recent past time matching a weekly spec like "Wed 03:00" or a daily "09:30" (US Eastern).
    Returned in UTC."""
    at = (at or now()).astimezone(EASTERN)
    parts = spec.split()
    hh, mm = map(int, parts[-1].split(":"))
    candidate = at.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if len(parts) == 2:
        candidate -= timedelta(days=(at.weekday() - DAYS.index(parts[0])) % 7)
    if candidate > at:
        candidate -= timedelta(days=7 if len(parts) == 2 else 1)
    return candidate.astimezone(timezone.utc)


def next_occurrence(spec: str, at: datetime | None = None) -> datetime:
    last = last_occurrence(spec, at)
    nxt = last.astimezone(EASTERN) + timedelta(days=7 if len(spec.split()) == 2 else 1)
    return nxt.astimezone(timezone.utc)
