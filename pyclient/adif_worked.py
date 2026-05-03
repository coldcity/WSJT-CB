"""
Load worked DXCC-style country labels from ADIF for CQ pick weighting.

We match the trimmed **country** column on CLI decode lines (see ``doc/CLI_API.md``)
to ``<COUNTRY:`` fields in ADIF QSO records. Matching is fuzzy (substring / containment)
after normalization because WSJT and loggers vary in spelling and truncation.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

log = logging.getLogger(__name__)

_EOR_SPLIT_RE = re.compile(r"(?i)<eor>")
_FIELD_HEAD_RE = re.compile(r"<([a-z0-9_]+):(\d+)(?::[a-z]*)?>")


def normalize_country_label(raw: str | None) -> str:
    if raw is None:
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    if s in {"—", "-", "--", "\u2014", "n/a", "unknown"}:
        return ""
    return " ".join(s.split())


def _parse_adif_records(text: str) -> list[dict[str, str]]:
    records: list[dict[str, str]] = []
    for chunk in _EOR_SPLIT_RE.split(text):
        chunk = chunk.strip()
        if not chunk:
            continue
        fields: dict[str, str] = {}
        pos = 0
        n = len(chunk)
        while pos < n:
            m = _FIELD_HEAD_RE.match(chunk, pos)
            if not m:
                pos += 1
                continue
            name = m.group(1).lower()
            ln = int(m.group(2))
            start = m.end()
            if start + ln > n:
                break
            fields[name] = chunk[start : start + ln]
            pos = start + ln
        if fields:
            records.append(fields)
    return records


def load_worked_country_labels_from_adif(
    path: Path,
    *,
    encoding: str = "utf-8",
) -> set[str]:
    """
    Return normalized unique country strings from ``COUNTRY`` fields (and ``MY_COUNTRY``
    is ignored — we want worked *DX*, not operator location).
    """
    try:
        text = path.read_text(encoding=encoding, errors="replace")
    except OSError as exc:
        log.warning("worked ADIF unreadable %s: %s", path, exc)
        return set()

    out: set[str] = set()
    for rec in _parse_adif_records(text):
        raw = rec.get("country")
        if not raw:
            continue
        norm = normalize_country_label(raw)
        if len(norm) >= 2:
            out.add(norm)
    return out


def load_worked_country_labels_from_adif_paths(paths: list[Path]) -> frozenset[str]:
    merged: set[str] = set()
    for p in paths:
        if not p.is_file():
            log.warning("worked ADIF path not found (skip): %s", p)
            continue
        merged |= load_worked_country_labels_from_adif(p)
    if paths and merged:
        log.info("CQ weighting: %d distinct country label(s) from ADIF", len(merged))
    return frozenset(merged)


def country_likely_worked(decode_country: str | None, worked_norm: frozenset[str]) -> bool:
    """True if decode territory column matches something in the worked set (fuzzy)."""
    c = normalize_country_label(decode_country)
    if len(c) < 2:
        return False
    for w in worked_norm:
        if len(w) < 2:
            continue
        if c == w or c in w or w in c:
            return True
    return False


def decode_country_matches_preferred(decode_country: str | None, phrases: tuple[str, ...]) -> bool:
    """True when decode **country** column matches any preferred phrase (substring, normalized)."""
    if not phrases:
        return False
    c = normalize_country_label(decode_country)
    if len(c) < 2:
        return False
    for p in phrases:
        pn = normalize_country_label(p)
        if len(pn) < 2:
            continue
        if pn in c or c in pn:
            return True
    return False
