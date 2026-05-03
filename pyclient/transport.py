"""Async TCP framing for WSJT-CB CLI: CRLF lines and inline '> ' / '? ' prompts."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field

from .protocol import Burst, is_spectrum_bar_line, parse_async_line, parse_decode_line, parse_marker_line

log = logging.getLogger(__name__)

_PROMPT = object()


class CliWireBuffer:
    """Splices Qt-style `line\\r\\n` plus bare `> ` / `? ` prompt suffixes (no trailing CRLF)."""

    def __init__(self, reader: asyncio.StreamReader) -> None:
        self.reader = reader
        self.buf = bytearray()

    async def _read_more(self) -> bool:
        """Returns False on clean EOF."""
        data = await self.reader.read(65536)
        if not data:
            return False
        self.buf += data
        return True

    def _consume_inline_prompt(self) -> bool:
        if self.buf.startswith(b"> ") or self.buf.startswith(b"? "):
            del self.buf[:2]
            return True
        return False

    async def next_datum(self) -> str | object:
        while True:
            if not self.buf:
                if not await self._read_more():
                    raise ConnectionError("EOF from server")
            ix = self.buf.find(b"\r\n")
            while ix < 0:
                if self._consume_inline_prompt():
                    return _PROMPT  # type: ignore[return-value]
                if not await self._read_more():
                    if self._consume_inline_prompt():
                        return _PROMPT  # type: ignore[return-value]
                    raise ConnectionError("EOF while awaiting CRLF/prompt")
                ix = self.buf.find(b"\r\n")

            raw = bytes(self.buf[:ix])
            del self.buf[: ix + 2]
            return raw.decode("utf-8", errors="replace")


@dataclass
class CliSession:
    reader: asyncio.StreamReader
    writer: asyncio.StreamWriter
    transcript_outbound: Callable[[str], None] | None = None
    welcome_lines: list[str] = field(default_factory=list)
    buf: CliWireBuffer = field(init=False)

    def __post_init__(self) -> None:
        self.buf = CliWireBuffer(self.reader)

    async def login(self, password: str | None) -> None:
        if password is not None and password.strip():
            p0 = await self.buf.next_datum()
            if p0 is not _PROMPT:
                raise ConnectionError(f"expected '? ' password prompt, got {p0!r}")
            self.writer.write(password.strip().encode("utf-8") + b"\n")
            await self.writer.drain()

        while True:
            d = await self.buf.next_datum()
            if d is _PROMPT:
                break
            assert isinstance(d, str)
            self.welcome_lines.append(d)
            if d.strip().startswith("ERR:"):
                raise ConnectionError(d.strip())

    def send_line(self, text: str, *, log_out: bool = True) -> None:
        one = text.rstrip("\r\n")
        b = text.encode("utf-8")
        if not b.endswith(b"\n"):
            b += b"\n"
        if log_out:
            log.debug(">> %s", one)
        if self.transcript_outbound is not None:
            self.transcript_outbound(one)
        self.writer.write(b)

    async def drain(self) -> None:
        await self.writer.drain()

    async def close(self) -> None:
        self.writer.close()
        try:
            await self.writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def open_session(
    host: str,
    port: int,
    password: str | None = None,
    *,
    connect_timeout: float = 30.0,
    transcript_outbound: Callable[[str], None] | None = None,
) -> CliSession:
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port),
        timeout=connect_timeout,
    )
    s = CliSession(reader=reader, writer=writer, transcript_outbound=transcript_outbound)
    await s.login(password)
    return s


async def absorb_wire_until_prompt(
    session: CliSession,
    *,
    line_sink: Callable[[str], None] | None = None,
) -> None:
    """Drain until the next bare ``> `` / ``? `` prompt, swallowing bursts/TX like *pump_events*."""
    buf = session.buf
    while True:
        d = await buf.next_datum()
        if d is _PROMPT:
            return
        assert isinstance(d, str)
        if is_spectrum_bar_line(d):
            burst = await assemble_burst(d, buf)
            log.debug(
                "absorb skipped spectrum burst (%d decode lines)",
                len(burst.raw_lines),
            )
            continue
        tx = parse_async_line(d)
        if tx:
            log.debug("absorb skipped async TX wire line")
            continue
        if line_sink is not None:
            line_sink(d)


async def send_cli_station_identity(
    session: CliSession,
    callsign: str,
    *,
    line_sink: Callable[[str], None] | None = None,
) -> None:
    """
    Mirror Settings: push ``callsign`` to the running app via ``set callsign``,
    then ``status`` so the CLI reflects dial/slot/grid at connect time.
    """
    call = callsign.upper().strip()
    if not call:
        raise ValueError("callsign required for CLI station identity")

    session.send_line(f"set callsign {call}")
    await session.drain()
    await absorb_wire_until_prompt(session, line_sink=line_sink)

    session.send_line("status")
    await session.drain()
    await absorb_wire_until_prompt(session, line_sink=line_sink)


async def assemble_burst(first_spectrum_line: str, buf: CliWireBuffer) -> Burst:
    b = Burst(spectrum_line=first_spectrum_line, raw_lines=[first_spectrum_line])
    line: str | object = await buf.next_datum()
    if line is _PROMPT:
        return b

    assert isinstance(line, str)
    mh, ms = parse_marker_line(line)
    if ms is not None:
        b.marker_line = line
        b.marker_hz = mh
        b.tx_slot = ms
        b.raw_lines.append(line)
        line = await buf.next_datum()

    while True:
        if line is _PROMPT:
            break
        assert isinstance(line, str)
        if not line.strip():
            line = await buf.next_datum()
            continue
        if line.lstrip().startswith("TX QUEUED:"):
            b.tx_queued_text = line.split(":", 1)[1].strip()
            b.raw_lines.append(line)
            break
        dl = parse_decode_line(line)
        if dl:
            b.decodes.append(dl)
            b.raw_lines.append(line)
            line = await buf.next_datum()
            continue
        # Foreign line wedged between decodes — keep it and exit
        b.raw_lines.append(line)
        break

    return b


async def pump_events(
    session: CliSession,
    queue: asyncio.Queue,
    *,
    text_sink: Callable[[str], None] | None = None,
    prompt_sink: Callable[[], None] | None = None,
) -> None:
    """
    Reads forever from *session* and posts ('burst', Burst) | ('tx', TxStart|TxStop).

    Plain text responses (commands, help, spots, ...) go to text_sink immediately so
    they are not stalled behind bursts on asyncio.Queue — otherwise awaiting a command
    in the main consumer deadlocks forever.

    Bare ``> `` / ``? `` prompt suffixes on the wire invoke *prompt_sink* (if given)
    once per prompt (optional; some clients omit echoing bare prompts).
    """
    buf = session.buf
    while True:
        d = await buf.next_datum()
        if d is _PROMPT:
            if prompt_sink is not None:
                prompt_sink()
            continue
        assert isinstance(d, str)
        if is_spectrum_bar_line(d):
            burst = await assemble_burst(d, buf)
            await queue.put(("burst", burst))
            continue
        tx = parse_async_line(d)
        if tx:
            await queue.put(("tx", tx))
            continue
        if text_sink is not None:
            text_sink(d)
        else:
            await queue.put(("text", d))
