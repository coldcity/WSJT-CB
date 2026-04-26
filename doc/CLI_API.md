# WSJT-CB CLI — API Reference

## Starting the CLI

Launch WSJT-CB with the CLI enabled:

```
wsjtcb.exe --cli-port 7374 --cli-pass hunter2
wsjtcb.exe --cli-port 7374 --cli-pass hunter2 --cli-bind 127.0.0.1
```

- `--cli-port` — TCP port to listen on (required to enable the CLI)
- `--cli-pass` — optional password; if omitted, no authentication is required
- `--cli-bind` — IP address to bind to (default: `0.0.0.0`); use `127.0.0.1` to restrict to loopback only

Only one client may connect at a time.

### Transcript log

Whenever the CLI server object exists (i.e. `--cli-port` was given), all client input and server output are appended to a **single UTF-8 log file** next to the executable:

`wsjtcb-cli.log` (same directory as `wsjtcb.exe`)

Each line is prefixed with a UTC ISO8601 timestamp and a role tag: `IN` (client line), `OUT` (line or prompt sent to the client), or `SYS` (connect/disconnect, log open/close). Leading `\\n` in `OUT` marks a bare CRLF sent before a block (e.g. spectrum burst). Lines that would expose a password are logged as `auth ***REDACTED***` instead of the real `auth` argument.

---

## Protocol

Plain text over TCP. Commands are newline-delimited (`\n`). Each response line ends with `\r\n`. A `> ` prompt is sent after each response.

---

## Session Lifecycle

```
→ connect
← WSJT-CB CLI ready
← AUTH required — type: auth <password>

→ auth hunter2
← OK: authenticated
← Type 'help' for commands
← >
```

If no `--cli-pass` was set, the auth step is skipped and you land directly in Idle state.

---

## States

| State | Description |
|---|---|
| **Unauthed** | Must send `auth` before anything else |
| **Idle** | Authenticated. Spectrum and decodes stream automatically each period. Use any command. |

---

## Commands

### Authentication

```
auth <password>
```

Must be sent first if a password was configured. Constant-time comparison — safe against timing attacks.

---

### Configuration

```
set callsign <CALL>
```
Set station callsign. Writes to config, regenerates TX message templates immediately. Automatically uppercased.

```
set grid <LOCATOR>
```
Set Maidenhead grid locator (e.g. `JO22`). Regenerates TX messages.

```
set odd <on|off>
```
`set odd on` enables odd time slot TX (the "Tx first" checkbox); `set odd off` disables it.

```
status
```
Multi-line snapshot: your **callsign** and **grid** (from config, refreshed when the settings dialog closes or after `set callsign` / `set grid`), **TX time slot** (Odd / Even — same meaning as the spectrum marker and `set odd`), **audio offset** in Hz (Tx + Rx selection), the current **decode selection** (or none), and **decode count** for the last completed pass.

Example:

```
--------------------------------------------
 status
--------------------------------------------
  Callsign        1AT106
  Grid            JO22
  TX time slot    Even (Tx second / even FT8 cycle)
  Audio offset    987 Hz  (Tx + Rx)
  Selection       [987] 1415  -6  0.1  FT8  CQ 30AT084 JO22
  Decodes (pass)  12
--------------------------------------------
```

---

### Output format

A burst is emitted automatically at the end of each decode period.
The spectrum timestamp always includes the current TX slot (Odd/Even).
When a frequency is selected it also appears in the header:

```
[14:15:30 987Hz Odd] |   ..+▼ ...+    .  +.  ..     |
                      500     1000    1500    2000   2500
           [1234] 1415 -12  0.3  FT8  26AT715 1AT106 +03
           [ 987] 1415  -6  0.1  FT8  CQ 30AT084 JO22
           [2100] 1415  +2  0.0  FT8  <30AT084> 1XZ001 RR73
>
```

With no frequency selected (first connect, before any `select`):

```
[14:15:30 1200Hz Even] |   ..+█ ...+    .  +.  ..     |
>
```

**TX events** are emitted asynchronously (UTC `HH:MM:SS` in brackets, same style as the spectrum header). Each event is prefixed with `\r\n` so it appears on a new line below the interactive `> ` prompt (same idea as the decode burst overwriting the prompt):

```
>
[14:15:30] !!! TX: CQ 1AT106 JO22
>
[14:15:45] !!! TX STOP (CQ 1AT106 JO22)
>
```

A `> ` prompt is sent after the TX start line and again after TX stop so async lines never sit on the same line as the prompt. The stop line repeats the started message in parentheses when known.

**Spectrum bar:** `[HH:MM:SS] |<96 chars>|` — 96 columns mapping the active decode window (`nfa`…`nfb` Hz) onto the averaged spectrum (`savg`, linear power from the decoder). Each column uses the strongest bin mapped into that column; level is **dB above the median column** (`10·log10(p / p_median)`), so the bar matches the GUI waterfall’s contrast much more closely than a raw linear offset.

**Marker line:** `▲ NHz, Odd|Even` — `▲` is column-aligned under the selected frequency in the bar above.

| Character | Meaning |
|---|---|
| ` ` (space) | Below +3 dB above median |
| `.` | +3 to +10 dB above median |
| `+` | +10 to +20 dB above median |
| `█` | More than +20 dB above median |

Left edge of the bar = `nfa` Hz, right edge = `nfb` Hz (the active decode window).

**Decode line:** eleven leading spaces, then `[FREQ] HHMM snr dt mode  message` (indent lines up under the spectrum bar).

| Field | Description |
|---|---|
| `FREQ` | Audio frequency offset in Hz — use with `select` |
| `HHMM` | UTC time of decode |
| `snr` | Signal-to-noise ratio in dB |
| `dt` | Time offset from period start in seconds |
| `mode` | Operating mode (e.g. `FT8`) |
| `message` | Decoded message text |

---

### Frequency selection and transmit

```
select <FREQ>
```
Select an audio frequency offset in Hz. **Immediately sets both TX and RX audio offset** in the UI. `FREQ` can be:
- The `[FREQ]` of a decode from the last burst — use with `answer` to reply to that station.
- **Any** valid audio frequency in the active decode window — use with `cq` to call CQ on a clear frequency.

The default frequency on connect is **1200 Hz**.

```
answer [<FREQ>]
```
Reply to the decode at the selected frequency. With **`answer <FREQ>`** (e.g. `answer 2135`), the CLI selects that audio offset first (same as `select <FREQ>`), then replies — one step instead of `select` then `answer`. Triggers the same pathway as clicking a decode in the UI — WSJT-CB AutoSeq takes over and manages the full QSO exchange automatically. Returns an error if no decode exists at the selected frequency (after any optional `select` implied by `answer <FREQ>`).

```
cq [<FREQ>]
```
Transmit a CQ at the selected audio frequency offset. With **`cq <FREQ>`** (e.g. `cq 2135`), selects that frequency first, then queues CQ. Does **not** require a decode at that frequency — useful for choosing a clear slot by scanning the spectrum bar.

```
stoptx
```
Same as the main-window **Stop Tx** control: stops any transmission in progress, turns off automatic TX / AutoSeq, clears CQ / reply-in-progress state, and **nothing will transmit again** until you send **`cq`** or **`answer`** (those commands re-arm the same path the UI uses for a new CQ or reply). This replaces the old CLI `set halt on|off`, which only toggled the Enable Tx checkbox and did not reliably end an ongoing sequence.

---

### Utility

```
help     Print command list
quit     Close the connection (alias: exit)
```

---

## Typical Workflow

```
auth <password>
set callsign 1XZ001
set grid JO22

# --- burst arrives automatically ---
# [14:15] |  ..+█  ..   + .  ..    |
#           [1234] 1415 -12  0.3  FT8  26AT715 1AT106 +03
#           [ 987] 1415  -6  0.1  FT8  CQ 30AT084 JO22
#           [2100] 1415  +2  0.0  FT8  <30AT084> 1XZ001 RR73

select 987      # pick the CQ from 30AT084 by its audio frequency
answer          # AutoSeq begins; WSJT-CB handles the exchange

# same as the two lines above:
answer 987

# --- monitor subsequent bursts for RR73 / 73 to confirm QSO complete ---

# --- alternatively, call CQ on a clear frequency (no decode needed) ---
select 1350     # choose a clear slot spotted in the spectrum bar
cq              # transmit CQ at 1350 Hz

# same as select 1350 then cq:
cq 1350
```

---

## Error responses

All errors begin with `ERR:`. Successful responses begin with `OK:` or are informational lines. Connection is never closed on error — just send the corrected command.

---

## 11m Callsign Format

WSJT-CB accepts 11m callsigns in the form `N{1,3}L{1,2}N{1,3}` (digits, 1–2 letters, digits), with a special case allowing a 4-digit unit number when the country prefix is a single digit (e.g. `1AT1000`). Standard amateur callsigns are also fully supported. See `README.md` for the full validation table.
