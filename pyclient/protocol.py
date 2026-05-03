"""
Parse WSJT-CB CLI bursts and async lines per doc/CLI_API.md.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Literal

MarkerSlot = Literal["Odd", "Even"]


@dataclass
class DecodeLine:
    """One FT8 decode row from the CLI burst."""

    glyphs: str  # trimmed 4-char mark band e.g. "@*!" or "!  "
    freq_hz: int
    snr: int | None
    dt: float | None
    message: str  # trimmed from fixed 20-char column
    raw_line: str

    @property
    def cq(self) -> bool:
        return "!" in self.glyphs.strip() or "CQ" in self.message.upper()

    @property
    def mentions_me(self) -> bool:
        return "@" in self.glyphs.strip()


@dataclass
class Burst:
    """One decode-period CLI emission: spectrum + marker + decodes (+ optional TX QUEUED)."""

    spectrum_line: str = ""
    marker_line: str = ""
    marker_hz: int | None = None
    tx_slot: MarkerSlot | None = None  # app's TX time slot indicator
    decodes: list[DecodeLine] = field(default_factory=list)
    tx_queued_text: str | None = None
    raw_lines: list[str] = field(default_factory=list)


_MARKER_RE = re.compile(
    r"^\s*\u25b2\s+(\d+)Hz,\s+(Odd|Even)\s*$"  # ▲ U+25B2
)
# Server: decodeIndent (11 spaces) + 4 glyph chars + whitespace + [hz] + ...
_DECODE_LINE_RE = re.compile(
    r"^[ ]{11}(.{4})\s+\[(\s*\d+)\]\s+([+-]?\d+)\s+([+-]?\d+\.\d+)\s{2}(.{20})(?:\s{2}(.*))?$"
)


def parse_marker_line(line: str) -> tuple[int | None, MarkerSlot | None]:
    m = _MARKER_RE.match(line.rstrip("\r"))
    if not m:
        return None, None
    hz = int(m.group(1).strip())
    slot: MarkerSlot = m.group(2)  # type: ignore[assignment]
    return hz, slot


def parse_decode_line(line: str) -> DecodeLine | None:
    s = line.rstrip("\r\n")
    if len(s) < 40:
        return None
    m = _DECODE_LINE_RE.match(s)
    if not m:
        return None
    glyphs_fixed = m.group(1).rstrip()
    hz = int(m.group(2).strip())
    try:
        snr_val = int(m.group(3))
    except ValueError:
        snr_val = None
    try:
        dt_val = float(m.group(4))
    except ValueError:
        dt_val = None
    msg = m.group(5).rstrip()

    return DecodeLine(
        glyphs=glyphs_fixed.strip(),
        freq_hz=hz,
        snr=snr_val,
        dt=dt_val,
        message=msg,
        raw_line=s,
    )


def is_spectrum_bar_line(line: str) -> bool:
    """[HH:MM:SS] | ... 48-char bar ... |"""
    s = line.rstrip("\r\n")
    if not s.startswith("["):
        return False
    return "|" in s and s.count("|") >= 2


_TX_START_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (?:!!! )?TX: (.*)$")
_TX_STOP_RE = re.compile(r"^\[(\d\d:\d\d:\d\d)\] (?:!!! )?TX STOP(?: \((.+)\))?\s*$")
# Successful log write: ``[HH:MM:SS] LOG: …`` (TcpCliServer; legacy ``!!! LOG:`` still accepted).
_CLI_LOG_NOTIFY_RE = re.compile(r"^\[\d\d:\d\d:\d\d\] (?:!!! )?LOG: ")


@dataclass
class TxStart:
    utc: str
    message: str


@dataclass
class TxStop:
    utc: str
    message: str | None = None


def parse_async_line(line: str) -> TxStart | TxStop | None:
    s = line.rstrip("\r\n")
    ms = _TX_START_RE.match(s)
    if ms:
        return TxStart(utc=ms.group(1), message=ms.group(2))
    mp = _TX_STOP_RE.match(s)
    if mp:
        msg = mp.group(2)
        return TxStop(utc=mp.group(1), message=msg.strip() if msg else None)
    return None


def is_cli_log_notify_line(line: str) -> bool:
    """True when the CLI async line reports a successful QSO row write (see doc/CLI_API.md)."""
    return bool(_CLI_LOG_NOTIFY_RE.match(line.rstrip("\r\n")))


def format_tx_wire(ev: TxStart | TxStop) -> str:
    """Rebuild the server's one-line TX event for display (minus leading bare CRLF)."""
    if isinstance(ev, TxStart):
        return f"[{ev.utc}] TX: {ev.message}"
    if ev.message:
        return f"[{ev.utc}] TX STOP ({ev.message})"
    return f"[{ev.utc}] TX STOP"


_CQ_SKIP_WORDS = frozenset(
    {
        "DX",
        "CQDX",
        "QRZ",
        "DE",
        "NA",
        "SA",
        "EU",
        "AF",
        "AS",
        "OC",
        "AN",
        "USA",
    }
)


def cq_caller_station(msg: str) -> str | None:
    """
    Best-effort extract DX call from CQ decodes ('CQ XYZ JO22' -> XYZ).
    Skips directional tokens like CQ DX JA7MIT … -> JA7MIT.
    """
    parts = msg.upper().split()
    if len(parts) < 2 or parts[0] != "CQ":
        return None
    i = 1
    while i < len(parts) and parts[i] in _CQ_SKIP_WORDS:
        i += 1
    if i >= len(parts):
        return None
    call = parts[i]
    if len(call) < 2:
        return None
    return call


def chase_identity(call: str) -> str:
    """
    Normalize for "same ham" CQ chasing: M3ABC/P matches M3ABC; W1AW/4 matches W1AW.
    Uses base token before '/', good enough for CQ spotter decisions.
    """
    u = call.upper().strip()
    i = u.find("/")
    if i < 0:
        return u
    return u[:i].strip() or u


def strip_angle_hash_callsign(token: str) -> str:
    """
    CLI / WSJT bracket form used for hashed or split decodes (e.g. '<SX20RCK>' → SX20RCK).
    """
    u = token.strip().upper()
    if len(u) >= 2 and u.startswith("<") and u.endswith(">"):
        return u[1:-1].strip()
    return u


def normalized_cli_partner_call(raw: str) -> str:
    """Stable upper base call for comparisons (strip brackets, chase_identity)."""
    return chase_identity(strip_angle_hash_callsign(raw.strip())).upper()


def mentions_callsign_words(msg_upper: str, call_upper: str) -> bool:
    if not call_upper:
        return False
    return bool(re.search(rf"\b{re.escape(call_upper)}\b", msg_upper))


def message_mentions_callsign_plain_or_hashed(msg_upper: str, partner_call: str) -> bool:
    """Plain word or bracket-hashed WSJT column form '<CALL>'."""
    cid = normalized_cli_partner_call(partner_call)
    if not cid:
        return False
    if mentions_callsign_words(msg_upper, cid):
        return True
    return f"<{cid}" in msg_upper.upper()


def is_own_cq_message(msg: str, my_call: str) -> bool:
    u = msg.upper().strip()
    if not u.startswith("CQ"):
        return False
    return mentions_callsign_words(u, my_call.upper().strip())


def is_reply_to_my_cq(msg: str, my_call: str) -> bool:
    u = msg.upper().strip()
    if not my_call:
        return False
    if is_own_cq_message(msg, my_call):
        return False
    return mentions_callsign_words(u, my_call.upper().strip())


_LOCATOR_RE = re.compile(r"^[A-R]{2}[0-9]{2}([A-R]{2})?$", re.IGNORECASE)


def is_locator_token(tok: str) -> bool:
    return bool(_LOCATOR_RE.fullmatch(tok.strip()))


def partner_call_from_tx_message(msg: str, my_call: str) -> str | None:
    """
    First non-self token in a TX summary / TX QUEUED line (e.g. 'UA1OMB M3ABC IO85').
    Skips grids, numeric reports, and RR73-style tokens. Strips '<CALL>' hash brackets.

    Special cases: Maidenhead-style ``is_locator_token`` matches some real callsigns such as
    **RP81IL** — tokens from ``<…>`` are never locator-skipped (WSJT hashes the DX there).
    """
    my_id = chase_identity(my_call.upper().strip()).upper()
    _rst_re = re.compile(r"^R?[+-]\d+$", re.IGNORECASE)
    for raw in msg.split():
        tok = raw.strip().upper()
        if len(tok) < 2:
            continue
        if re.fullmatch(r"[+-]?\d+", tok):
            continue
        unwrapped_angle = tok.startswith("<") and tok.endswith(">")
        cand = normalized_cli_partner_call(tok)
        if len(cand) < 2:
            continue
        # FT8 RST-style remnants (avoid mis-reading R-05 as the DX callsign).
        if _rst_re.fullmatch(cand):
            continue
        if not unwrapped_angle and is_locator_token(cand):
            continue
        if cand in {"RR73", "RRR", "73"}:
            continue
        if cand == my_id:
            continue
        return cand
    return None


def burst_mentions_callsign(decodes: list[DecodeLine], call: str) -> bool:
    """Any decode row shows *call* in plain text or hashed '<CALL>' form (narrow column-safe)."""
    if not call:
        return False
    for d in decodes:
        if message_mentions_callsign_plain_or_hashed(d.message.upper(), call):
            return True
    return False


def primary_dx_token_from_non_cq_decode(msg: str) -> str | None:
    """First callsign-like token for a non-CQ decode (e.g. '*' partner busy elsewhere)."""
    parts = msg.split()
    if not parts:
        return None
    if parts[0].upper() == "CQ":
        c = cq_caller_station(msg)
        return chase_identity(c).upper() if c else None
    return chase_identity(parts[0]).upper()


def burst_decode_freqs(decodes: list[DecodeLine]) -> list[int]:
    return [d.freq_hz for d in decodes]


def free_slots_for_burst(
    decodes: list[DecodeLine],
    grid_start: int = 500,
    grid_end: int = 2800,
    grid_step: int = 50,
    guard_hz: int = 50,
) -> set[int]:
    """
    Candidate start frequencies F where no decode lies in [F, F+guard_hz].
    """
    freqs = burst_decode_freqs(decodes)
    good: set[int] = set()
    for f in range(grid_start, grid_end - guard_hz + 1, grid_step):
        lo, hi = f, f + guard_hz
        if any(lo <= hf <= hi for hf in freqs):
            continue
        good.add(f)
    return good
