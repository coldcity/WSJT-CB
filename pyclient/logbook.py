"""WSJT-X style CSV log — callsigns worked on a given calendar day."""

from __future__ import annotations

import csv
from datetime import date, datetime, timezone
from pathlib import Path

from pyclient.protocol import chase_identity

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore[misc, assignment]


def today_calendar_date(tz_name: str) -> date:
    n = (tz_name or "local").strip()
    if not n or n.upper() in ("LOCAL", "LOCALTIME"):
        return date.today()
    if n.upper() in ("UTC", "GMT", "Z"):
        return datetime.now(timezone.utc).date()
    if ZoneInfo is None:
        raise RuntimeError("'today_timezone' requires zoneinfo (Python 3.9+) for IANA names")
    return datetime.now(ZoneInfo(n)).date()


def worked_station_ids_on_day(path: Path, day: date) -> frozenset[str]:
    """
    CSV columns (0-based): 0 = QSO start date 'YYYY-MM-DD', 4 = other station callsign.
    One row per completed QSO like WSJT-X exported log.
    """
    if not path.is_file():
        return frozenset()
    out: set[str] = set()
    with path.open(newline="", encoding="utf-8", errors="replace") as f:
        for row in csv.reader(f):
            if len(row) < 5:
                continue
            ds = row[0].strip()
            if not ds or ds.lower().startswith("date"):
                continue
            try:
                row_d = date.fromisoformat(ds[:10])
            except ValueError:
                continue
            if row_d != day:
                continue
            call = row[4].strip().upper()
            if not call or call in {"CALL", "CALLSIGN", "STATION"}:
                continue
            out.add(chase_identity(call).upper())
    return frozenset(out)


def worked_station_ids_today(path: Path | None, tz_name: str) -> frozenset[str]:
    if path is None:
        return frozenset()
    return worked_station_ids_on_day(path, today_calendar_date(tz_name))
