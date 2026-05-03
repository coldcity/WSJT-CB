"""Optional ANSI styling for the driver console transcript (stdout).

Respects https://no-color.org/ when ``NO_COLOR`` is set. Uses color only for a TTY
unless *force* is true. The WSJT-CB TCP CLI itself remains plain text; styling is
applied only on the Python client's mirrored output.
"""

from __future__ import annotations

import os
import sys

from pyclient.protocol import (
    decode_row_matches_dx_focus,
    is_cli_log_notify_line,
    is_spectrum_bar_line,
    parse_decode_line,
    parse_marker_line,
)


def use_color(*, enabled_config: bool, force: bool | None) -> bool:
    if force is False:
        return False
    if os.environ.get("NO_COLOR", "").strip():
        return False
    if not enabled_config:
        return False
    if force is True:
        return True
    return sys.stdout.isatty()


class Ansi:
    RESET = "\033[0m"
    BOLD = "\033[1m"
    DIM = "\033[2m"
    ITALIC = "\033[3m"  # not all terminals; harmless on others
    # Foreground (16-color)
    BLACK = "\033[30m"
    RED = "\033[31m"
    GREEN = "\033[32m"
    YELLOW = "\033[33m"
    BLUE = "\033[34m"
    MAGENTA = "\033[35m"
    CYAN = "\033[36m"
    WHITE = "\033[37m"
    BRIGHT_RED = "\033[91m"
    BRIGHT_GREEN = "\033[92m"
    BRIGHT_YELLOW = "\033[93m"
    BRIGHT_BLUE = "\033[94m"
    BRIGHT_MAGENTA = "\033[95m"
    BRIGHT_CYAN = "\033[96m"
    UNDERLINE = "\033[4m"


def _hsv_to_rgb(h: float, s: float, v: float) -> tuple[int, int, int]:
    """Convert HSV (hue degrees) to 8-bit RGB."""
    h = h % 360.0
    c = v * s
    x = c * (1.0 - abs((h / 60.0) % 2.0 - 1.0))
    m = v - c
    if h < 60:
        rp, gp, bp = c, x, 0.0
    elif h < 120:
        rp, gp, bp = x, c, 0.0
    elif h < 180:
        rp, gp, bp = 0.0, c, x
    elif h < 240:
        rp, gp, bp = 0.0, x, c
    elif h < 300:
        rp, gp, bp = x, 0.0, c
    else:
        rp, gp, bp = c, 0.0, x
    r = int((rp + m) * 255)
    g = int((gp + m) * 255)
    b_int = int((bp + m) * 255)
    return r, g, b_int


def _rainbow_line(text: str, *, bold: bool = True) -> str:
    """One-line 24-bit rainbow: each non-space glyph gets a hue by position."""
    non_space = sum(1 for c in text if not c.isspace())
    n = max(non_space, 1)
    out: list[str] = []
    idx = 0
    for ch in text:
        if ch.isspace():
            out.append(ch)
            continue
        # Full arc twice for extra saturation across the line
        hue = (idx / n) * 720.0
        idx += 1
        r, g, b_int = _hsv_to_rgb(hue, 0.92, 1.0)
        if bold:
            out.append(f"\033[1;38;2;{r};{g};{b_int}m{ch}")
        else:
            out.append(f"\033[38;2;{r};{g};{b_int}m{ch}")
    out.append(Ansi.RESET)
    return "".join(out)


def _S(enabled: bool, *parts: str) -> str:
    if not enabled:
        return ""
    return "".join(parts)


def _R(enabled: bool) -> str:
    return Ansi.RESET if enabled else ""


def style_server_line(
    line: str,
    *,
    color: bool,
    in_qso: bool,
    dx_focus_upper: str | None = None,
    my_call_upper: str = "",
) -> str:
    """Color one line of mirrored server / burst traffic."""
    raw = line.rstrip("\r\n")
    if not raw.strip():
        return raw
    t = raw

    if t.startswith("OK:"):
        return f"{_S(color, Ansi.BRIGHT_GREEN)}{t}{_R(color)}"
    if t.startswith("ERR:"):
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_RED)}{t}{_R(color)}"

    if is_cli_log_notify_line(t):
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_YELLOW)}{t}{_R(color)}"

    if is_spectrum_bar_line(t):
        return f"{_S(color, Ansi.DIM, Ansi.CYAN)}{t}{_R(color)}"

    _mh, ms = parse_marker_line(t)
    if ms is not None or "\u25b2" in t:
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_YELLOW)}{t}{_R(color)}"

    dl = parse_decode_line(t)
    if dl is not None:
        focus = (
            bool(dx_focus_upper)
            and bool(my_call_upper)
            and not in_qso
            and decode_row_matches_dx_focus(
                dl,
                dx_focus_upper,
                my_call_upper=my_call_upper,
            )
        )
        if "@" in dl.glyphs.strip():
            # @* = callsign hits us; extra emphasis beyond plain green.
            parts: tuple[str, ...] = (Ansi.BOLD, Ansi.UNDERLINE, Ansi.BRIGHT_GREEN)
            if in_qso:
                parts = (Ansi.BOLD, Ansi.ITALIC, Ansi.UNDERLINE, Ansi.BRIGHT_GREEN)
            return f"{_S(color, *parts)}{t}{_R(color)}"
        if "!" in dl.glyphs.strip() and dl.cq:
            if focus:
                return f"{_S(color, Ansi.YELLOW)}{t}{_R(color)}"
            return f"{_S(color, Ansi.DIM, Ansi.YELLOW)}{t}{_R(color)}"
        if focus:
            return t
        return f"{_S(color, Ansi.DIM)}{t}{_R(color)}"

    if "TX QUEUED:" in t:
        if in_qso:
            return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_MAGENTA)}{t}{_R(color)}"
        return f"{_S(color, Ansi.MAGENTA)}{t}{_R(color)}"

    # Async TX lines from server mirror: [HH:MM:SS] TX: ... / TX STOP
    if t.startswith("[") and "] TX STOP" in t:
        return f"{_S(color, Ansi.BRIGHT_BLUE, Ansi.BOLD)}{t}{_R(color)}"
    if t.startswith("[") and "] TX:" in t:
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_RED)}{t}{_R(color)}"

    return t


def _is_log_book_win_comment(line: str) -> bool:
    s = line.strip().lower()
    return s.startswith("comment ") and "we got him" in s


def _print_log_book_celebration(raw: str, *, color: bool) -> None:
    """ADIF / log booked — one flashy rainbow line plus breathing room."""
    if color:
        p = f"{_S(True, Ansi.DIM)}>{_R(True)}"
        cmd = f"{_S(True, Ansi.DIM, Ansi.BRIGHT_CYAN)}{raw}{_R(True)}"
        print(f"{p} {cmd}", flush=True)
    else:
        print(f"> {raw}", flush=True)

    spark = "\u2726"
    banner_plain = f"  {spark}  WE GOT HIM!  •  LOG BOOKED — 73!  {spark}"
    print()
    if color:
        print(_rainbow_line(banner_plain), flush=True)
    else:
        print(banner_plain, flush=True)
    print(flush=True)


def style_outbound_line(
    line: str,
    *,
    color: bool,
) -> str:
    """Color a logical outbound line (without the ``> `` prompt prefix)."""
    raw = line.rstrip("\r\n")
    rest = raw.strip()
    low = rest.lower()

    if low.startswith("comment "):
        if "hold —" in rest or "hold --" in low:
            return f"{_S(color, Ansi.DIM, Ansi.YELLOW)}{raw}{_R(color)}"
        if "we got him" in low:
            return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_GREEN)}{raw}{_R(color)}"
        return f"{_S(color, Ansi.BRIGHT_CYAN)}{raw}{_R(color)}"

    if low.startswith("cq ") or low.startswith("answer "):
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_YELLOW)}{raw}{_R(color)}"

    if low.startswith("stoptx") or low.startswith("bye"):
        return f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_RED)}{raw}{_R(color)}"

    if low.startswith("set "):
        return f"{_S(color, Ansi.DIM)}{raw}{_R(color)}"

    return raw


def print_outbound_transcript(line: str, *, color: bool) -> None:
    """Echo one outbound CLI line (mirrored with ``> `` prefix)."""
    raw = line.rstrip("\r\n")
    if _is_log_book_win_comment(raw):
        _print_log_book_celebration(raw, color=color)
        return
    body = style_outbound_line(raw, color=color)
    if color:
        print(f"{_S(True, Ansi.DIM)}>{_R(True)} {body}", flush=True)
    else:
        print(f"> {raw}", flush=True)


def style_mode_banner(
    prev_name: str,
    mode_name: str,
    *,
    color: bool,
) -> str:
    """Single transition line when strategy mode changes."""
    if prev_name == mode_name:
        return ""
    qso_tag = ""
    if "IN_QSO" in mode_name:
        qso_tag = f"{_S(color, Ansi.BOLD, Ansi.BRIGHT_GREEN)} ● QSO{_R(color)} "
    elif "CALLING" in mode_name:
        qso_tag = f"{_S(color, Ansi.BRIGHT_BLUE)} ▲ CQ{_R(color)} "
    elif "ANSWERING" in mode_name:
        qso_tag = f"{_S(color, Ansi.BRIGHT_YELLOW)} ◆ hunt{_R(color)} "

    bar = "━" * 8
    return (
        f"{_S(color, Ansi.DIM)}{bar}{_R(color)}"
        f"{qso_tag}"
        f"{_S(color, Ansi.BOLD)}{mode_name}{_R(color)}"
        f"{_S(color, Ansi.DIM)}{bar}{_R(color)}"
    )
