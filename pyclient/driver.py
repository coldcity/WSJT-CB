#!/usr/bin/env python3
"""
Automated WSJT-CB station driver via CLI (TCP).

Decisions and deliberate “no-change” bursts are echoed with ``comment``; “hold …” messages
explain why we stay in the current mode until something changes again.
Python 3.11+ uses stdlib ``tomllib``; on older versions run ``pip install -r pyclient/requirements.txt``.

Usage::

    python3 -m pyclient

Optional: ``python3 -m pyclient --config /path/to/wsjt-driver.toml``

When ``quiet_wire_transcript`` is false, the driver prints server lines as-is (skipping
blank lines and bare ``>``), prefixes each outbound command with ``> ``,
and after login sends ``set callsign`` (from config) plus ``status`` so the app matches
the driver identity and the console shows a station snapshot.
"""

from __future__ import annotations

import argparse
import asyncio
import enum
import logging
import math
import random
import sys
from datetime import date
from pathlib import Path

# Allow `python3 driver.py` from `pyclient/` (cwd does not contain the parent package).
_root = Path(__file__).resolve().parents[1]
_root_s = str(_root)
if _root_s not in sys.path:
    sys.path.insert(0, _root_s)

from pyclient.protocol import (
    Burst,
    DecodeLine,
    TxStart,
    TxStop,
    chase_identity,
    cq_caller_station,
    format_tx_wire,
    free_slots_for_burst,
    is_own_cq_message,
    is_reply_to_my_cq,
    partner_call_from_tx_message,
    primary_dx_token_from_non_cq_decode,
    is_cli_log_notify_line,
)
from pyclient.transport import CliSession, open_session, pump_events, send_cli_station_identity

from pyclient.config import DriverConfig, load_driver_config, resolve_default_config_path


log = logging.getLogger(__name__)

# Softmax-ish bias when picking among CQ rows: ``exp((snr − best_snr) / τ)``. Lower τ = sharper peak on strongest.
_CQ_SNR_WEIGHT_TEMP_DB: float = 10.0


def _suppress_wire_echo_line(line: str) -> bool:
    """True for empty / whitespace-only lines and bare prompt lines (``>``)."""
    t = line.strip()
    return not t or t == ">"


class StrategyMode(enum.Enum):
    ANSWERING_CQ = enum.auto()
    IN_QSO_ANSWERED = enum.auto()  # we answered someone's CQ first
    CALLING_CQ = enum.auto()
    IN_QSO_CALLED = enum.auto()  # someone answered our CQ first


class CQPhase(enum.Enum):
    FIND_SLOT = enum.auto()
    WAIT_REPLY_AFTER_CQ = enum.auto()


def _eligible_cq_candidates(
    decodes: list[DecodeLine],
    my_call: str,
    *,
    skip_station_ids: frozenset[str] | None = None,
) -> list[tuple[str, DecodeLine]]:
    ski = frozenset() if skip_station_ids is None else skip_station_ids
    out: list[tuple[str, DecodeLine]] = []
    for d in decodes:
        if not d.cq:
            continue
        if is_own_cq_message(d.message, my_call):
            continue
        c = cq_caller_station(d.message)
        if c:
            cid = chase_identity(c).upper()
            if cid in ski:
                continue
            out.append((c.upper(), d))
    return out


def _cq_from_station_anywhere(
    decodes: list[DecodeLine], chase_target_display: str, my_call: str
) -> list[DecodeLine]:
    """All CQ decode rows whose caller matches *chase_target* (any audio bin); /P,/MM,... equivalent."""
    r: list[DecodeLine] = []
    tid = chase_identity(chase_target_display)
    for d in decodes:
        if not d.cq:
            continue
        if is_own_cq_message(d.message, my_call):
            continue
        c = cq_caller_station(d.message)
        if c and chase_identity(c) == tid:
            r.append(d)
    return r


def _pick_best_cq_decode(rows: list[DecodeLine]) -> DecodeLine:
    """Prefer strongest SNR when the same station appears on multiple frequencies."""
    if len(rows) == 1:
        return rows[0]

    def _key(d: DecodeLine) -> int:
        return d.snr if d.snr is not None else -999

    return max(rows, key=_key)


def _weighted_random_cq_pick(
    cand: list[tuple[str, DecodeLine]],
    *,
    temperature_db: float = _CQ_SNR_WEIGHT_TEMP_DB,
) -> tuple[str, DecodeLine]:
    """Bias toward stronger (higher SNR) CQ decodes while still sampling stochastically."""
    if len(cand) == 1:
        return cand[0]

    def snr_float(dc: DecodeLine) -> float:
        return float(dc.snr) if dc.snr is not None else -30.0

    snrs = [snr_float(dc) for _sta, dc in cand]
    mx = max(snrs)
    tau = max(temperature_db, 1e-6)
    weights = [math.exp((s - mx) / tau) for s in snrs]
    total = sum(weights)
    r = random.random() * total
    acc = 0.0
    for i, w in enumerate(weights):
        acc += w
        if r <= acc:
            return cand[i]
    return cand[-1]


def _glyph_at_star_to_us(d: DecodeLine) -> bool:
    """
    WSJT-X glyph band: '@' decoded as to us; '*' matched to Rx partner select.
    Enough to confirm our QSO target is transmitting this pass — no callsign parsing.
    """
    g = d.glyphs.strip()
    # Skip CQ-shaped rows; busy-elsewhere is * without @ (_glyph_partner_busy_elsewhere).
    return (
        d.mentions_me
        and "*" in g
        and not d.message.upper().lstrip().startswith("CQ ")
    )


def _partner_cq_again_star_bang(d: DecodeLine, partner_upper: str) -> bool:
    """
    Glyph * + ! CQ row matching *partner_upper*.
    Means that station is still on CQ while select lock follows them —
    visibility for AutoSeq, not necessarily "CQ again after QSO ends".
    """
    g = d.glyphs.strip()
    if "*" not in g or "!" not in g:
        return False
    if not d.cq:
        return False
    c = cq_caller_station(d.message)
    if not c:
        return False
    return chase_identity(c).upper() == chase_identity(partner_upper).upper()


def _glyph_partner_busy_elsewhere(d: DecodeLine) -> bool:
    """
    '*' = same DX as select lock; no '@' → not addressing us;
    no '!' → not CQ — partner is transmitting to someone else.
    """
    g = d.glyphs.strip()
    # Treat leading CQ … as CQ even if glyphs lack '!' due to truncation edge cases.
    return (
        "*" in g
        and "@" not in g
        and "!" not in g
        and not d.message.upper().lstrip().startswith("CQ ")
    )


class StationDriver:
    def __init__(
        self,
        session: CliSession,
        event_q: asyncio.Queue,
        *,
        callsign_upper: str,
        quiet_burst_to_end_qso: int = 4,
        bursts_without_cq_reply: int = 8,
        idle_answer_passes_until_calling: int = 6,
        max_answer_cq_ignore_passes: int = 4,
        max_chase_answer_retries: int = 8,
        echo_server_stdout: bool = True,
        comment_holds: bool = True,
    ) -> None:
        self.session = session
        self.event_q = event_q
        self.echo_server_stdout = echo_server_stdout
        self.my_call_upper = callsign_upper.upper().strip()
        self.mode = StrategyMode.ANSWERING_CQ
        self.target_station_upper: str | None = None
        self._target_last_cq_hz: int | None = None  # chasing: prior CQ bin for logging moves
        self.cq_phase = CQPhase.FIND_SLOT
        self.last_odd_clear: set[int] | None = None
        self.last_even_clear: set[int] | None = None
        self.chosen_cq_freq: int | None = None

        self.bursts_after_cq = 0
        self.max_bursts_wait_reply = bursts_without_cq_reply

        self.consecutive_cq_without_reply = 0
        self.unanswered_cq_goal = 6

        self.quiet_burst_to_end_qso = quiet_burst_to_end_qso
        self.quiet_bursts = 0

        self.tx_running = False
        self._cmd_fut: asyncio.Future[str] | None = None
        # After OK from our ``stoptx``, next ``TX STOP`` line is expected from that command — skip comment there.
        self._expect_tx_wire_stop_after_driver_stoptx: bool = False

        self.no_eligible_cq_burst_streak = 0
        self.idle_answer_passes_until_calling = idle_answer_passes_until_calling
        self.max_answer_cq_ignore_passes = max(1, max_answer_cq_ignore_passes)
        self.max_chase_answer_retries = max(1, max_chase_answer_retries)
        self._chase_answer_fail_streak = 0

        # After `answer` OK: stay ANSWERING_CQ until we see '@* to us' on a non-CQ decode.
        # While TX queued, partner *! CQ-only passes count toward max_answer_cq_ignore_passes (Wait-and-call style).
        self._answer_wait_at_star_before_in_qso: bool = False
        self._answer_cq_ignore_passes: int = 0

        # IN_QSO_*: partner callsign when known (answer OK / reply path / TX QUEUED hint).
        self._qso_partner_upper: str | None = None
        # IN_QSO_ANSWERED: never treat "no TX QUEUED" as QSO-over until AutoSeq has shown
        # a TX QUEUED line at least once (first pass after `answer` often has none yet).
        self._answered_qso_saw_tx_queued: bool = False

        # Server rejects `answer` when ADIF already has this DX on today's local date; cache skips for CQ picking.
        self._answer_worked_skip_day: date | None = None
        self._skipped_answer_worked_today_local: set[str] = set()

        self.comment_holds = comment_holds
        self._hold_sig_last: tuple[str, str] | None = None

    def _ensure_answer_worked_skip_calendar(self) -> None:
        today = date.today()
        if self._answer_worked_skip_day != today:
            self._skipped_answer_worked_today_local.clear()
            self._answer_worked_skip_day = today

    def skip_station_ids_for_cq_pick(
        self, extra: frozenset[str] | None
    ) -> frozenset[str] | None:
        self._ensure_answer_worked_skip_calendar()
        if not self._skipped_answer_worked_today_local and not extra:
            return None
        merged: set[str] = set(self._skipped_answer_worked_today_local)
        if extra:
            merged.update(extra)
        return frozenset(merged)

    def srv_echo(self, line: str) -> None:
        """Mirror interesting server traffic on stdout (noise lines suppressed)."""
        if not self.echo_server_stdout:
            return
        if _suppress_wire_echo_line(line):
            return
        print(line, flush=True)

    async def command(self, line: str) -> str:
        if self._cmd_fut is not None:
            raise RuntimeError("overlapping command")
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._cmd_fut = fut
        self.session.send_line(line)
        await self.session.drain()
        log.debug("cmd wait → %s", line)
        try:
            return await asyncio.wait_for(fut, timeout=120.0)
        except TimeoutError:
            self._cmd_fut = None
            raise

    def on_text_line(self, line: str) -> None:
        self.srv_echo(line)
        if self._cmd_fut is not None and not self._cmd_fut.done():
            if line.startswith("OK:") or line.startswith("ERR:"):
                self._cmd_fut.set_result(line)
                self._cmd_fut = None
                log.debug("cmd ack ← %s", line[:120])
                return
        if is_cli_log_notify_line(line):
            self._schedule_log_booked_comment()
        log.debug("srv(text): %s", line[:200])

    async def comment_mode(self, text: str, *, hold_signature: tuple[str, str] | None = None) -> None:
        """Send a ``comment`` to the CLI (IN log only; server does not return ``OK:``)."""
        try:
            self.session.send_line(f"comment [{self.mode.name}] {text}")
            await self.session.drain()
            log.debug("comment sent [%s]", self.mode.name)
        except Exception as exc:  # noqa: BLE001
            log.warning("comment failed: %s", exc)
        finally:
            if hold_signature is None:
                self._hold_sig_last = None
            else:
                self._hold_sig_last = hold_signature

    async def _comment_hold(self, subkey: str, explanation: str) -> None:
        """Explain no-op continuation; suppressed while (mode, subkey) repeats."""
        if not self.comment_holds:
            return
        sig = (self.mode.name, subkey)
        if sig == self._hold_sig_last:
            return
        await self.comment_mode(f"hold — {explanation}", hold_signature=sig)

    def _clear_answer_early_phase(self) -> None:
        """Reset post-answer 'hunt until @*' state (stay ANSWERING_CQ)."""
        self._answer_wait_at_star_before_in_qso = False
        self._answer_cq_ignore_passes = 0

    def _clear_answer_chase_target(self) -> None:
        """Drop SNR-lock CQ station (chase Hz) and ``answer`` fail streak."""
        self.target_station_upper = None
        self._target_last_cq_hz = None
        self._chase_answer_fail_streak = 0

    async def _register_failed_answer_attempt(self, key: str, explanation: str) -> bool:
        """Count a bad ``answer`` while chasing a CQ; dice out like target-lost CQ if limit exceeded."""
        self._chase_answer_fail_streak += 1
        if self._chase_answer_fail_streak >= self.max_chase_answer_retries:
            self._clear_answer_chase_target()
            await self.comment_mode(
                f"ANSWERING_CQ: {self.max_chase_answer_retries} consecutive failing "
                "`answer` commands on same locked CQ — giving up; rolling dice for CQ mode",
            )
            if random.choice((True, False)):
                await self.enter_calling_cq("dice exit answering (chase answer retry limit)")
            else:
                await self.comment_mode("dice: stay answering CQs (chase answer retry limit)")
            return True
        await self._comment_hold(key, explanation)
        return False

    async def _confirm_answered_into_in_qso(self, b: Burst, note: str) -> None:
        if (b.tx_queued_text or "").strip():
            self._answered_qso_saw_tx_queued = True
        await self.comment_mode(note)
        self._clear_answer_early_phase()
        self.mode = StrategyMode.IN_QSO_ANSWERED
        self.quiet_bursts = 0
        self.no_eligible_cq_burst_streak = 0
        self._clear_answer_chase_target()

    async def stoptx(self) -> None:
        try:
            r = await self.command("stoptx")
            log.debug("%s", r)
            if r.startswith("OK:"):
                self._expect_tx_wire_stop_after_driver_stoptx = True
        except Exception as exc:  # noqa: BLE001
            log.warning("stoptx: %s", exc)

    async def _abort_partner_missing_from_decode_pass(self, b: Burst) -> None:
        """TX QUEUED but DX not visible this pass — stoptx; on answered-CQ side, pick another CQ."""
        was_called_side = self.mode == StrategyMode.IN_QSO_CALLED
        pu_for_skip_raw: str | None = self._qso_partner_upper
        if (
            pu_for_skip_raw is None
            and b.tx_queued_text is not None
        ):
            pu_for_skip_raw = partner_call_from_tx_message(
                b.tx_queued_text,
                self.my_call_upper,
            )
        exclude_station_ids: frozenset[str] | None = (
            frozenset({chase_identity(pu_for_skip_raw).upper()})
            if pu_for_skip_raw
            else None
        )

        await self.comment_mode(
            "TX queued but no @* row and no *! CQ decode for our DX this pass — "
            "stoptx; AutoSeq possibly out of sync",
        )
        await self.stoptx()
        self._clear_answer_early_phase()
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        self._clear_answer_chase_target()
        self.quiet_bursts = 0
        if was_called_side:
            self.mode = StrategyMode.CALLING_CQ
            self.cq_phase = CQPhase.FIND_SLOT
            self.chosen_cq_freq = None
            self.last_odd_clear = None
            self.last_even_clear = None
            self.consecutive_cq_without_reply = 0
            self.bursts_after_cq = 0
            return

        self.mode = StrategyMode.ANSWERING_CQ
        self.no_eligible_cq_burst_streak = 0
        ok, suppress = await self._engage_random_cq_from_burst(
            b,
            "after TX-queued visibility miss — pick CQ: {sta} @ {hz} Hz",
            exclude_station_ids=exclude_station_ids,
        )
        if not ok and not suppress:
            log.info("TX-queued abort: no alternate CQ in same pass (skipped/server/empty)")
            await self._comment_hold(
                "abort_no_cq_same_pass",
                "after visibility abort, no alternate CQ to answer in this decode pass — "
                "stay ANSWERING_CQ for next opportunity",
            )

    async def _ensure_tx_queued_partner_has_decode(self, b: Burst) -> bool:
        """TX QUEUED: require DX visible — @* to us OR *! CQ decode same caller."""
        if not b.tx_queued_text:
            return True
        if any(_glyph_at_star_to_us(d) for d in b.decodes):
            return True
        pu = self._qso_partner_upper
        if not pu:
            pu = partner_call_from_tx_message(b.tx_queued_text, self.my_call_upper)
            if pu:
                self._qso_partner_upper = pu
        if pu and any(_partner_cq_again_star_bang(d, pu) for d in b.decodes):
            return True
        await self._abort_partner_missing_from_decode_pass(b)
        return False

    def _burst_quiet_autoseq(self, burst_tx_queued: str | None) -> bool:
        if burst_tx_queued:
            self.quiet_bursts = 0
            return False
        if self.tx_running:
            self.quiet_bursts = 0
            return False
        self.quiet_bursts += 1
        return self.quiet_bursts >= self.quiet_burst_to_end_qso

    async def _engage_random_cq_from_burst(
        self,
        b,
        picked_comment_fmt: str,
        *,
        exclude_station_ids: frozenset[str] | None = None,
    ) -> tuple[bool, bool]:
        """
        Comment + answer one CQ from this burst, SNR-weighted random choice.

        Returns ``(True, False)`` when ``answer`` returned ``OK:`` (early-hunt until @*).
        Returns ``(False, True)`` when a station stays locked for chase (non-OK or exception),
        including after the chase retry limit triggered its own dice/comment sequence (caller skips
        generic "no CQ picked" holds).
        Returns ``(False, False)`` when there is no eligible CQ row to try.
        """
        cand = _eligible_cq_candidates(
            b.decodes,
            self.my_call_upper,
            skip_station_ids=self.skip_station_ids_for_cq_pick(exclude_station_ids),
        )
        if not cand:
            return False, False
        sta, dc = _weighted_random_cq_pick(cand)
        self.target_station_upper = sta
        self._target_last_cq_hz = None
        self._chase_answer_fail_streak = 0
        await self.comment_mode(picked_comment_fmt.format(sta=sta, hz=dc.freq_hz))
        try:
            r = await self.command(f"answer {dc.freq_hz}")
            if r.startswith("OK:"):
                self.mode = StrategyMode.ANSWERING_CQ
                self._answer_wait_at_star_before_in_qso = True
                self._answer_cq_ignore_passes = 0
                self.quiet_bursts = 0
                self._clear_answer_chase_target()
                self.no_eligible_cq_burst_streak = 0
                self._qso_partner_upper = chase_identity(sta).upper()
                self._answered_qso_saw_tx_queued = False
                log.debug("%s", r[:120])
                return True, False
            if "already worked today" in r:
                self._ensure_answer_worked_skip_calendar()
                self._skipped_answer_worked_today_local.add(chase_identity(sta).upper())
                log.info("answer rejected (already worked today): %s", sta)
                return False, False
            log.warning("%s", r[:200])
            await self._register_failed_answer_attempt(
                "engage_answer_nok",
                "locked CQ target: answer was not OK — retry or chase next decode pass",
            )
            return False, True
        except Exception as exc:  # noqa: BLE001
            log.warning("answer failed %s", exc)
            await self._register_failed_answer_attempt(
                "engage_answer_exc",
                "locked CQ target: answer command failed — retry or chase next decode pass",
            )
            return False, True

    async def _recover_from_partner_snub(self, b) -> None:
        exclude: set[str] = set()
        if self._qso_partner_upper:
            exclude.add(self._qso_partner_upper)
        for d in b.decodes:
            if _glyph_partner_busy_elsewhere(d):
                t = primary_dx_token_from_non_cq_decode(d.message)
                if t:
                    exclude.add(t)
        await self.comment_mode(
            "* select-partner decode, not CQ and not @ us — DX busy elsewhere; "
            "stoptx, pick another CQ now",
        )
        await self.stoptx()
        self._clear_answer_early_phase()
        self.mode = StrategyMode.ANSWERING_CQ
        self._clear_answer_chase_target()
        self.quiet_bursts = 0
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        ok, suppress = await self._engage_random_cq_from_burst(
            b,
            "race lost — rebound answer {sta} @ {hz} Hz",
            exclude_station_ids=frozenset(exclude),
        )
        if not ok and not suppress:
            log.info("Snub rebound: no eligible CQ in same pass")
            await self._comment_hold(
                "snub_no_cq_same_pass",
                "after DX busy-elsewhere rebound, no CQ to answer this pass — "
                "stay ANSWERING_CQ for next decode",
            )

    async def _finalize_answering_idle_streak_if_still_here(self, b) -> None:
        if self.mode != StrategyMode.ANSWERING_CQ:
            return
        if self._answer_wait_at_star_before_in_qso:
            return
        wt = self.skip_station_ids_for_cq_pick(None)
        cand = _eligible_cq_candidates(
            b.decodes,
            self.my_call_upper,
            skip_station_ids=wt,
        )
        if cand:
            self.no_eligible_cq_burst_streak = 0
            return
        self.no_eligible_cq_burst_streak += 1
        if self.no_eligible_cq_burst_streak < self.idle_answer_passes_until_calling:
            await self._comment_hold(
                f"ans_idle_{self.no_eligible_cq_burst_streak}",
                "no workable CQ rows this pass — "
                f"{self.no_eligible_cq_burst_streak}/"
                f"{self.idle_answer_passes_until_calling} toward CQ mode (dice at threshold) "
                "(skip cache or empty CQ list)",
            )
        elif self.no_eligible_cq_burst_streak >= self.idle_answer_passes_until_calling:
            await self.comment_mode(
                f"{self.idle_answer_passes_until_calling} consecutive decode passes "
                "with no eligible CQ decodes — rolling dice for CQ mode vs stay answering",
            )
            if random.choice((True, False)):
                await self.enter_calling_cq(
                    "dice exit answering (idle band — no workable CQ repetitions)",
                )
            else:
                self.no_eligible_cq_burst_streak = 0
                await self.comment_mode(
                    "dice: stay answering CQs (idle streak threshold rolled stay)",
                )

    async def handle_burst(self, b: Burst) -> None:
        parity = b.tx_slot
        try:
            if self.mode == StrategyMode.IN_QSO_ANSWERED:
                if b.tx_queued_text and self._qso_partner_upper is None:
                    ph = partner_call_from_tx_message(b.tx_queued_text, self.my_call_upper)
                    if ph:
                        self._qso_partner_upper = ph
                if (b.tx_queued_text or "").strip():
                    self._answered_qso_saw_tx_queued = True
                if any(_glyph_partner_busy_elsewhere(d) for d in b.decodes):
                    await self._recover_from_partner_snub(b)
                    return
                if not await self._ensure_tx_queued_partner_has_decode(b):
                    return
                queued = bool((b.tx_queued_text or "").strip())
                if queued:
                    await self._comment_hold(
                        "answered_tx_queued",
                        "TX QUEUED and partner decode rule OK — stay IN_QSO_ANSWERED, AutoSeq runs",
                    )
                elif not self._answered_qso_saw_tx_queued:
                    await self._comment_hold(
                        "answered_no_queue_yet",
                        "no TX QUEUED line yet — hold IN_QSO_ANSWERED until AutoSeq shows one once",
                    )

            elif self.mode == StrategyMode.IN_QSO_CALLED:
                if not await self._ensure_tx_queued_partner_has_decode(b):
                    return

            # --- Answer someone's CQ ---
            elif self.mode == StrategyMode.ANSWERING_CQ:
                # Post ``answer``: stay ANSWERING_CQ until non-CQ @* establishes real dialog.
                if self._answer_wait_at_star_before_in_qso:
                    if b.tx_queued_text and self._qso_partner_upper is None:
                        ph = partner_call_from_tx_message(b.tx_queued_text, self.my_call_upper)
                        if ph:
                            self._qso_partner_upper = ph
                    if (b.tx_queued_text or "").strip():
                        self._answered_qso_saw_tx_queued = True
                    if any(_glyph_partner_busy_elsewhere(d) for d in b.decodes):
                        await self._recover_from_partner_snub(b)
                        return
                    if any(_glyph_at_star_to_us(d) for d in b.decodes):
                        await self._confirm_answered_into_in_qso(
                            b,
                            "@* decode (non-CQ) — contact established; "
                            "enter IN_QSO_ANSWERED (AutoSeq drives TX)",
                        )
                        return
                    queued = bool((b.tx_queued_text or "").strip())
                    if not queued:
                        if not self._answered_qso_saw_tx_queued:
                            await self._comment_hold(
                                "early_ans_presync",
                                "ANSWERING_CQ: answered CQ; waiting first TX QUEUED / decodes "
                                "(stay answering until '@* to us')",
                            )
                        return
                    pu = self._qso_partner_upper
                    if not pu and b.tx_queued_text:
                        pu = partner_call_from_tx_message(b.tx_queued_text, self.my_call_upper)
                        if pu:
                            self._qso_partner_upper = pu
                    star_bang = bool(
                        pu and any(_partner_cq_again_star_bang(d, pu) for d in b.decodes),
                    )
                    if star_bang:
                        self._answer_cq_ignore_passes += 1
                        mx = self.max_answer_cq_ignore_passes
                        n = self._answer_cq_ignore_passes
                        if n >= mx:
                            excl = chase_identity(pu).upper()
                            await self.comment_mode(
                                f"partner still CQ (*! glyphs) ×{mx} decode passes "
                                "(no @* to us) — stoptx, hunt another station",
                            )
                            await self.stoptx()
                            self._clear_answer_early_phase()
                            self._qso_partner_upper = None
                            self._clear_answer_chase_target()
                            self._answered_qso_saw_tx_queued = False
                            self.quiet_bursts = 0
                            ok, suppress = await self._engage_random_cq_from_burst(
                                b,
                                "after CQ-ignore limit — pick CQ: {sta} @ {hz} Hz",
                            )
                            if not ok and not suppress:
                                await self._comment_hold(
                                    "ignore_limit_no_cq",
                                    "CQ-ignore abort: no alternate CQ immediately — listening",
                                )
                            return
                        await self.comment_mode(
                            f"Retry ({n+1}/{mx}): DX still CQ on select (*! glyph) "
                            "without @* to us yet — tolerate while AutoSeq progresses",
                        )
                        return
                    await self._abort_partner_missing_from_decode_pass(b)
                    return

                if any(_glyph_at_star_to_us(d) for d in b.decodes):
                    await self.comment_mode(
                        "@* decode — partner on select answering our side; "
                        "enter IN_QSO_ANSWERED (AutoSeq drives TX)",
                    )
                    self.mode = StrategyMode.IN_QSO_ANSWERED
                    self.quiet_bursts = 0
                    self.no_eligible_cq_burst_streak = 0
                    self._clear_answer_chase_target()
                    self._qso_partner_upper = None
                    self._answered_qso_saw_tx_queued = False
                    return
                if self.target_station_upper:
                    cq_t = _cq_from_station_anywhere(
                        b.decodes,
                        self.target_station_upper,
                        self.my_call_upper,
                    )
                    if cq_t:
                        dpick = _pick_best_cq_decode(cq_t)
                        hz = dpick.freq_hz
                        prev = self._target_last_cq_hz
                        if prev is not None and hz != prev:
                            await self.comment_mode(
                                f"chase {self.target_station_upper}: "
                                f"{prev}→{hz} Hz (CQ moved in passband)",
                            )
                        self._target_last_cq_hz = hz
                        try:
                            r = await self.command(f"answer {hz}")
                            if r.startswith("OK:"):
                                chase_partner = self.target_station_upper
                                self.mode = StrategyMode.ANSWERING_CQ
                                self._answer_wait_at_star_before_in_qso = True
                                self._answer_cq_ignore_passes = 0
                                self.quiet_bursts = 0
                                self.no_eligible_cq_burst_streak = 0
                                self._clear_answer_chase_target()
                                self._qso_partner_upper = (
                                    chase_identity(chase_partner).upper() if chase_partner else None
                                )
                                self._answered_qso_saw_tx_queued = False
                                log.debug("%s", r[:120])
                                return
                            if "already worked today" in r:
                                self._ensure_answer_worked_skip_calendar()
                                if self.target_station_upper:
                                    self._skipped_answer_worked_today_local.add(
                                        chase_identity(self.target_station_upper).upper()
                                    )
                                    log.info(
                                        "answer rejected (already worked today): %s",
                                        self.target_station_upper,
                                    )
                                return
                            log.warning("%s", r[:200])
                            if await self._register_failed_answer_attempt(
                                "chase_answer_nok",
                                "ANSWERING_CQ: target still CQ but answer was not OK — "
                                "same mode, retry next pass",
                            ):
                                return
                        except Exception as exc:  # noqa: BLE001
                            log.warning("answer failed %s", exc)
                            if await self._register_failed_answer_attempt(
                                "chase_answer_exc",
                                "ANSWERING_CQ: answer command failed — same mode, retry next pass",
                            ):
                                return
                        return
                    else:
                        self._clear_answer_chase_target()
                        await self.comment_mode(
                            "target no longer CQ; cleared target — rolling dice for CQ mode",
                        )
                        if random.choice((True, False)):
                            await self.enter_calling_cq("dice exit answering (target quit CQ)")
                        else:
                            await self.comment_mode("dice: stay answering CQs (target quit CQ)")
                        return
                else:
                    ok_pick, suppress_pick = await self._engage_random_cq_from_burst(
                        b,
                        "picked CQ from {sta} @ {hz} Hz (new target)",
                    )
                    if not ok_pick and not suppress_pick:
                        await self._comment_hold(
                            "ans_no_pick",
                            "ANSWERING_CQ: no eligible CQ to answer this pass — "
                            "stay in answer mode for next decode",
                        )
                    return

            elif self.mode == StrategyMode.CALLING_CQ and self.cq_phase == CQPhase.FIND_SLOT:
                if parity not in ("Odd", "Even"):
                    await self._comment_hold(
                        "cq_slot_parity",
                        "CALLING_CQ FIND_SLOT: no Odd/Even marker yet — waiting for CLI slot line",
                    )
                    return
                clear = free_slots_for_burst(b.decodes)
                if parity == "Odd":
                    self.last_odd_clear = clear
                else:
                    self.last_even_clear = clear
                if (
                    self.last_odd_clear is not None
                    and self.last_even_clear is not None
                    and self.chosen_cq_freq is None
                ):
                    overlap = self.last_odd_clear & self.last_even_clear
                    if overlap:
                        self.chosen_cq_freq = sorted(overlap)[len(overlap) // 2]
                        await self.comment_mode(
                            f"clear slot candidate {self.chosen_cq_freq} Hz (odd∩even-clear)",
                        )
                        try:
                            r = await self.command(f"cq {self.chosen_cq_freq}")
                            log.debug("%s", r[:200])
                            if r.startswith("OK:"):
                                self.cq_phase = CQPhase.WAIT_REPLY_AFTER_CQ
                                self.bursts_after_cq = 0
                                self.last_odd_clear = None
                                self.last_even_clear = None
                                return
                        except Exception as exc:  # noqa: BLE001
                            log.warning("cq failed %s — retry listening", exc)
                        self.chosen_cq_freq = None

                if self.chosen_cq_freq is None:
                    if self.last_odd_clear is None or self.last_even_clear is None:
                        await self._comment_hold(
                            "cq_slot_maps",
                            "CALLING_CQ FIND_SLOT: collecting odd xor even free-slot map — "
                            "need both before CQ",
                        )
                    else:
                        await self._comment_hold(
                            "cq_slot_no_overlap",
                            "CALLING_CQ FIND_SLOT: odd and even maps have no common clear bins — "
                            "listen another decode pass",
                        )
                return

            # --- After CQ sent: watch for callers ---
            elif self.mode == StrategyMode.CALLING_CQ and self.cq_phase == CQPhase.WAIT_REPLY_AFTER_CQ:
                if any(_glyph_at_star_to_us(d) for d in b.decodes):
                    await self.comment_mode(
                        "@* decode — responder on select; "
                        "enter IN_QSO_CALLED (AutoSeq drives TX)",
                    )
                    self.mode = StrategyMode.IN_QSO_CALLED
                    self.quiet_bursts = 0
                    self.consecutive_cq_without_reply = 0
                    self.cq_phase = CQPhase.FIND_SLOT
                    self.chosen_cq_freq = None
                    self.bursts_after_cq = 0
                    self._qso_partner_upper = None
                    return

                self.bursts_after_cq += 1
                cf = self.chosen_cq_freq
                await self._comment_hold(
                    f"cq_wait_{cf}",
                    f"CALLING_CQ: waiting after our CQ on {cf} Hz (burst {self.bursts_after_cq}/"
                    f"{self.max_bursts_wait_reply} without reply yet)",
                )
                reply = False
                reply_dx: str | None = None
                for d in b.decodes:
                    if not d.mentions_me:
                        continue
                    if is_reply_to_my_cq(d.message, self.my_call_upper):
                        reply = True
                        parts = d.message.split()
                        if parts:
                            reply_dx = chase_identity(parts[0]).upper()
                        break

                if reply:
                    await self.comment_mode("DX replied to my CQ — IN_QSO_CALLED")
                    self.mode = StrategyMode.IN_QSO_CALLED
                    self.quiet_bursts = 0
                    self.consecutive_cq_without_reply = 0
                    self.cq_phase = CQPhase.FIND_SLOT
                    self.chosen_cq_freq = None
                    self._qso_partner_upper = reply_dx
                    return

                if self.bursts_after_cq >= self.max_bursts_wait_reply:
                    self.consecutive_cq_without_reply += 1
                    await self.comment_mode(
                        f"No reply after CQ (waited {self.bursts_after_cq} bursts) "
                        f"— streak {self.consecutive_cq_without_reply}/{self.unanswered_cq_goal}",
                    )
                    self.cq_phase = CQPhase.FIND_SLOT
                    self.chosen_cq_freq = None
                    self.last_odd_clear = None
                    self.last_even_clear = None
                    self.bursts_after_cq = 0

                    if self.consecutive_cq_without_reply >= self.unanswered_cq_goal:
                        await self.maybe_bail_calling_roll_dice()

        finally:
            if self.mode == StrategyMode.ANSWERING_CQ:
                await self._finalize_answering_idle_streak_if_still_here(b)

    async def maybe_bail_calling_roll_dice(self) -> None:
        """After 6 consecutive unanswered CQs, 50/50 back to answering mode."""
        if self.consecutive_cq_without_reply < self.unanswered_cq_goal:
            return
        self.consecutive_cq_without_reply = 0
        await self.comment_mode("6 unanswered CQs — rolling dice answering vs CQ mode")
        if random.choice((True, False)):
            await self.enter_answering("dice switched to answering CQs after 6 dead CQs")
        else:
            await self.comment_mode("dice: stay in calling CQ")

    async def enter_calling_cq(self, reason: str) -> None:
        await self.stoptx()
        await self.comment_mode(reason)
        self.mode = StrategyMode.CALLING_CQ
        self._clear_answer_chase_target()
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        self.cq_phase = CQPhase.FIND_SLOT
        self.chosen_cq_freq = None
        self.last_odd_clear = None
        self.last_even_clear = None
        self.consecutive_cq_without_reply = 0
        self.bursts_after_cq = 0
        self.no_eligible_cq_burst_streak = 0
        self._clear_answer_early_phase()

    async def enter_answering(self, reason: str) -> None:
        await self.stoptx()
        await self.comment_mode(reason)
        self.mode = StrategyMode.ANSWERING_CQ
        self._clear_answer_chase_target()
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        self.cq_phase = CQPhase.FIND_SLOT
        self.consecutive_cq_without_reply = 0
        self.no_eligible_cq_burst_streak = 0
        self._clear_answer_early_phase()

    async def _finish_answered_qso_if_idle(self, b: Burst) -> None:
        """
        We answered first: exit IN_QSO_ANSWERED when this pass has no TX QUEUED *after*
        we already saw TX QUEUED at least once (*! CQ on the DX is ignored here while TX
        is queued — that's them still CQing on select lock, mid-QSO, not necessarily over).
        After idle, optional comment if their *! CQ also appears this pass (back on CQ banner).
        """
        if b.tx_queued_text and self._qso_partner_upper is None:
            ph = partner_call_from_tx_message(b.tx_queued_text, self.my_call_upper)
            if ph:
                self._qso_partner_upper = ph

        queued = (b.tx_queued_text or "").strip()
        no_tx_queued = not queued

        pu = self._qso_partner_upper
        partner_cq_star_bang = bool(
            pu and any(_partner_cq_again_star_bang(d, pu) for d in b.decodes),
        )

        exit_idle = no_tx_queued and self._answered_qso_saw_tx_queued
        if not exit_idle:
            return

        if partner_cq_star_bang:
            await self.comment_mode(
                f"answered-QSO idle (no TX QUEUED); partner {pu} still showing *! CQ — "
                "hunt next station",
            )
        else:
            await self.comment_mode(
                "no TX QUEUED this pass after prior TX QUEUED — answered-QSO idle; hunt next CQ",
            )

        await self.stoptx()
        self.mode = StrategyMode.ANSWERING_CQ
        self._clear_answer_chase_target()
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        self._clear_answer_early_phase()
        self.quiet_bursts = 0
        self.no_eligible_cq_burst_streak = 0

        ok, suppress = await self._engage_random_cq_from_burst(
            b,
            "immediate pick after answered QSO exit: {sta} @ {hz} Hz",
        )
        if not ok and not suppress:
            log.info("answered-QSO exit: no eligible CQ (skip cache or empty band)")
            await self._comment_hold(
                "ans_idle_exit_no_cq",
                "answered-QSO finished but no CQ to pick immediately — stay ANSWERING_CQ "
                "until next eligible decode",
            )

    async def _finish_early_answer_hunt_idle(self, b: Burst) -> None:
        queued = bool((b.tx_queued_text or "").strip())
        if queued or not self._answered_qso_saw_tx_queued:
            return
        await self.comment_mode(
            "ANSWERING_CQ early hunt idle (had TX queued, none this pass); stoptx, pick CQ",
        )
        await self.stoptx()
        self._clear_answer_early_phase()
        self._qso_partner_upper = None
        self._answered_qso_saw_tx_queued = False
        self._clear_answer_chase_target()
        self.quiet_bursts = 0
        self.no_eligible_cq_burst_streak = 0
        ok, suppress = await self._engage_random_cq_from_burst(
            b,
            "after early hunt idle — pick CQ: {sta} @ {hz} Hz",
        )
        if not ok and not suppress:
            log.info("early hunt idle: no CQ pick")

    async def maybe_finish_qso_burst(self, b: Burst) -> None:
        """Exit IN_QSO_* (answered-CQ branch: idle / no queued TX after one was seen; ...)."""
        if self.mode == StrategyMode.IN_QSO_ANSWERED:
            await self._finish_answered_qso_if_idle(b)
        elif self.mode == StrategyMode.ANSWERING_CQ and self._answer_wait_at_star_before_in_qso:
            await self._finish_early_answer_hunt_idle(b)
        elif self.mode == StrategyMode.IN_QSO_CALLED:
            did_quiet_finish = self._burst_quiet_autoseq(b.tx_queued_text)
            if did_quiet_finish:
                await self.comment_mode(f"quiet {self.quiet_bursts} bursts — resume calling CQ cycle")
                self.mode = StrategyMode.CALLING_CQ
                self.quiet_bursts = 0
                self._clear_answer_chase_target()
                self._qso_partner_upper = None
                self.cq_phase = CQPhase.FIND_SLOT
                self.chosen_cq_freq = None
                self.last_odd_clear = None
                self.last_even_clear = None
                self.consecutive_cq_without_reply = 0
                self.bursts_after_cq = 0
            elif (b.tx_queued_text or "").strip() or self.tx_running:
                await self._comment_hold(
                    "called_tx_activity",
                    "IN_QSO_CALLED: transmission active or TX queued — quiet counter cleared, "
                    "stay in QSO",
                )
            else:
                await self._comment_hold(
                    f"called_quiet_{self.quiet_bursts}",
                    f"IN_QSO_CALLED: no TX queued & not transmitting — "
                    f"quiet count {self.quiet_bursts}/{self.quiet_burst_to_end_qso} bursts "
                    "until CQ cycle resume",
                )

    def _schedule_log_booked_comment(self) -> None:
        msg = "WE GOT HIM!"

        async def _go() -> None:
            await self.comment_mode(msg)

        try:
            asyncio.get_running_loop().create_task(_go())
        except RuntimeError:
            log.debug("no event loop — skip %r comment", msg)

    def _schedule_waiting_for_reply_comment(self) -> None:
        msg = "waiting for reply..."

        async def _go() -> None:
            await self.comment_mode(msg)

        try:
            asyncio.get_running_loop().create_task(_go())
        except RuntimeError:
            log.debug("no event loop — skip %r comment", msg)

    def on_tx(self, ev: TxStart | TxStop) -> None:
        if isinstance(ev, TxStart):
            self._expect_tx_wire_stop_after_driver_stoptx = False
            self.tx_running = True
            log.debug("TX start: %s", ev.message[:80])
        else:
            self.tx_running = False
            tail = f" ({ev.message})" if ev.message else ""
            log.debug("TX stop%s", tail)
            if self._expect_tx_wire_stop_after_driver_stoptx:
                self._expect_tx_wire_stop_after_driver_stoptx = False
                return
            self._schedule_waiting_for_reply_comment()


async def run_driver(cfg: DriverConfig) -> None:
    pq: asyncio.Queue = asyncio.Queue(maxsize=500)

    def transcript_out(line: str) -> None:
        print(f"> {line}", flush=True)

    session = await open_session(
        cfg.host,
        cfg.port,
        cfg.password,
        transcript_outbound=None if cfg.quiet_wire_transcript else transcript_out,
    )

    def handshake_echo(line: str) -> None:
        if cfg.quiet_wire_transcript:
            return
        if _suppress_wire_echo_line(line):
            return
        print(line, flush=True)

    await send_cli_station_identity(session, cfg.callsign, line_sink=handshake_echo)

    drv = StationDriver(
        session,
        pq,
        callsign_upper=cfg.callsign.upper().strip(),
        quiet_burst_to_end_qso=cfg.quiet_qso_bursts,
        bursts_without_cq_reply=cfg.wait_reply_bursts,
        idle_answer_passes_until_calling=cfg.idle_answer_passes_call,
        max_answer_cq_ignore_passes=cfg.max_answer_cq_ignore_passes,
        max_chase_answer_retries=cfg.max_chase_answer_retries,
        echo_server_stdout=not cfg.quiet_wire_transcript,
        comment_holds=cfg.comment_holds,
    )

    pump_task = asyncio.create_task(
        pump_events(
            session,
            pq,
            text_sink=drv.on_text_line,
            prompt_sink=None,
        ),
    )

    for w in session.welcome_lines:
        drv.srv_echo(w)

    try:
        while True:
            kind, payload = await pq.get()
            if kind == "burst":
                for ln in payload.raw_lines:
                    drv.srv_echo(ln)
                await drv.handle_burst(payload)
                await drv.maybe_finish_qso_burst(payload)
            elif kind == "tx":
                drv.srv_echo(format_tx_wire(payload))
                drv.on_tx(payload)
            else:
                log.warning("unknown event %s", kind)
    except asyncio.CancelledError:
        raise
    except KeyboardInterrupt:
        log.info("KeyboardInterrupt — bye")
        await drv.stoptx()
        session.send_line("bye")
        await session.drain()
    finally:
        pump_task.cancel()
        try:
            await pump_task
        except asyncio.CancelledError:
            pass
        await session.close()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="WSJT-CB automated CLI station driver (toml-config).")
    p.add_argument(
        "--config",
        dest="config_path",
        metavar="PATH",
        help="wsjt-driver.toml path (otherwise search cwd, ./pyclient/, package dir)",
    )
    return p


def main() -> None:
    parser = build_arg_parser()
    ns = parser.parse_args()
    try:
        cpath = resolve_default_config_path(ns.config_path)
        cfg = load_driver_config(cpath)
    except FileNotFoundError as e:
        print(e, file=sys.stderr)
        sys.exit(2)
    except ValueError as e:
        print("Config:", e, file=sys.stderr)
        sys.exit(2)
    logging.basicConfig(
        level=logging.DEBUG if cfg.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    try:
        asyncio.run(run_driver(cfg))
    except ConnectionError as e:
        print("Connection/authentication failed:", e, file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
