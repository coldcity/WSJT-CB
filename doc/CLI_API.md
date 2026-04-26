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
set halt <on|off>
set odd <on|off>
```
`set halt off` enables TX (`autoButton`), `set halt on` halts it. `set odd on` enables odd time slot TX (the "Tx first" checkbox); `set odd off` disables it.

```
status
```
Print selected frequency, current decode, and decode count for the last period.

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

**TX events** are emitted asynchronously:

```
[14:15:30 1200Hz Even] TX: CQ 1AT106 JO22
TX STOP
>
```

The TX line mirrors the spectrum timestamp format and shows the message being transmitted. `TX STOP` is followed by a new prompt.

**Spectrum bar:** `[HH:MM:SS NHz Odd|Even] |<48 chars>|` — 48 cells, one per 50 Hz slot across 0–2400 Hz.

**Marker line:** `▲ NHz, Odd|Even` — `▲` is column-aligned under the selected frequency in the bar above.

| Character | Meaning |
|---|---|
| ` ` (space) | Noise floor (< +3 dB above median) |
| `.` | Weak signal (+3 to +10 dB) |
| `+` | Moderate signal (+10 to +20 dB) |
| `█` | Strong signal (> +20 dB) |

Left edge of the bar = `nfa` Hz, right edge = `nfb` Hz (the active decode window).

**Decode line:** `[FREQ] HHMM snr dt mode  message`

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
answer
```
Reply to the decode at the selected frequency. Triggers the same pathway as clicking a decode in the UI — WSJT-CB AutoSeq takes over and manages the full QSO exchange automatically. Returns an error if no decode exists at the selected frequency.

```
cq
```
Transmit a CQ at the selected audio frequency offset. Does **not** require a decode at that frequency — useful for choosing a clear slot by scanning the spectrum bar.

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
# [1234] 1415 -12  0.3  FT8  26AT715 1AT106 +03
# [ 987] 1415  -6  0.1  FT8  CQ 30AT084 JO22
# [2100] 1415  +2  0.0  FT8  <30AT084> 1XZ001 RR73

select 987      # pick the CQ from 30AT084 by its audio frequency
answer          # AutoSeq begins; WSJT-CB handles the exchange

# --- monitor subsequent bursts for RR73 / 73 to confirm QSO complete ---

# --- alternatively, call CQ on a clear frequency (no decode needed) ---
select 1350     # choose a clear slot spotted in the spectrum bar
cq              # transmit CQ at 1350 Hz
```

---

## Error responses

All errors begin with `ERR:`. Successful responses begin with `OK:` or are informational lines. Connection is never closed on error — just send the corrected command.

---

## 11m Callsign Format

WSJT-CB accepts 11m callsigns in the form `N{1,3}L{1,2}N{1,3}` (digits, 1–2 letters, digits), with a special case allowing a 4-digit unit number when the country prefix is a single digit (e.g. `1AT1000`). Standard amateur callsigns are also fully supported. See `README.md` for the full validation table.
