# WSJT-CB CLI — API Reference

## Starting the CLI

Launch WSJT-CB with the CLI enabled:

```
wsjtcb.exe --cli-port 7374 --cli-pass hunter2
wsjtcb.exe --cli-port 7374 --cli-pass hunter2 --cli-bind 127.0.0.1
```

- `--cli-port` — TCP port to listen on (required to enable the CLI)
- `--cli-pass` — optional password. If set, the server sends a lone **`? `** prompt (no welcome lines yet); the client must then send that password as the **first line** (there is no `auth` command). If `--cli-pass` is omitted, the server sends the welcome banner immediately.
- `--cli-bind` — IP address to bind to (default: `0.0.0.0`); use `127.0.0.1` to restrict to loopback only

Only one client may connect at a time.

### Transcript log

Whenever the CLI server object exists (i.e. `--cli-port` was given), all client input and server output are appended to a **single UTF-8 log file** next to the executable:

`wsjtcb-cli.log` (same directory as `wsjtcb.exe`)

Each line is prefixed with a UTC ISO8601 timestamp and a role tag: `IN` (client line), `OUT` (line or prompt sent to the client), or `SYS` (connect/disconnect, log open/close). Bare `\r\n` sent on the TCP session before spectrum or TX-announcement bursts (leading “newline” on the wire) is **not** given its own transcript line — the burst lines themselves are logged normally. When `--cli-pass` is set, the **first** client line (the password) is logged as `***REDACTED*** (first-line password)`.

Structured **annotations** tagged as **`[OPERATOR]`** also appear as `OUT` (they are surfaced to the operator in **View → CLI Log…**, and may be mirrored on the TCP session — see [Session notes](#session-notes-operator-and-agent)).

**Agent remarks** (`comment` / **`n`**): your text is logged once as **`IN`**; the server does **not** add a duplicate **`OUT`** annotation line, and it does **not** send **`OK:`** on the TCP session—processing ends with only the usual **`> `** prompt refresh ( **`IN`** remains in **`wsjtcb-cli.log`** as for any typed line).

In the main window, use **View → CLI Log…** to open the **CLI session log** window — a standalone dialog (not docked inside the main decode UI). Entry is enabled only when the program was started with `--cli-port`. The read-only transcript fills the upper portion. Under it, **one row** holds the inject field and **Send**. **Below that**, **Show timestamps**, **Send as command**, and on the **right**, **Kick** then **Close** (**Kick** confirms then disconnects; see [Session notes](#session-notes-operator-and-agent)). With **Send as command** **off**, **Send** (or Enter) appends **`[OPERATOR]`** as `OUT` and mirrors it to **Idle** clients. With it **on**, the field is treated like a typed CLI line: logged as **`IN`**, echoed on the TCP session as if the client had sent it, and executed by the same command parser (**Idle** clients only; fails with a dialog if **Unauthed** or disconnected). **Show timestamps** only affects the viewer (`wsjtcb-cli.log` keeps full prefixes). Reopening reloads `wsjtcb-cli.log` from disk.

---

## Protocol

Plain text over TCP. Commands are newline-delimited (`\n`). Each **response line** the server sends ends with `\r\n`. After most interactions the server sends a **`> `** prompt (two characters: greater-than and space) with **no** trailing `\r\n`, so the next server line or your typing continues on the same “line” in many terminals.

---

## Session Lifecycle

**Without `--cli-pass`:** on connect the server immediately sends **`WSJT-CB CLI ready`**, **`Happy DX!`**, **`Type 'help' (h) for one-letter and full commands`**, then a **`> `** prompt.

**With `--cli-pass`:** the server sends only a **`? `** prompt (like the usual **`> `** but with no `\r\n` after it, same as the command prompt) — then the client sends one line: the password (UTF-8, matching the configured value; leading and trailing spaces are trimmed). Constant-time comparison (SHA-256 of UTF-8 bytes), same as before. If it **matches**, the server sends the same three welcome lines and **`> `** as in the no-password case. If it does **not** match, the server sends **`ERR: wrong password`** and **closes** the TCP connection; connect again and retry.

```
→ connect
← ? 
→ hunter2
← WSJT-CB CLI ready
← Happy DX!
← Type 'help' (h) for one-letter and full commands
← > 
```

Wrong password:

```
→ connect
← ? 
→ wrong
← ERR: wrong password
(connection closed)
```

---

## States

| State | Description |
|---|---|
| **Unauthed** | `--cli-pass` is set and the client has not yet sent the **first-line password**. The server has only sent the **`? `** prompt, not the welcome banner. **No** spectrum or decode lines. The only valid client action is to send that password line (or disconnect). |
| **Idle** | Password matched (or no password was configured). Spectrum and decodes are sent at the end of each decode period. All commands are available. |

---

## Single-letter forms

In **Idle** (after the optional first-line password, or with no password):

- **Set (no `set` prefix):** `q` + callsign, `g` + grid, `o` + odd — e.g. `q 1AT106`, `g JO22`, `o on` (same as `set callsign` / `set grid` / `set odd`).
- **Rest:** `x` `stoptx` · `s` `status` · `n` `comment` · `spots` · `f` `select` · `c` `cq` · `a` `answer` · `h` `help` · `b` `bye` (aliases: `quit`, `exit`). **`spots`** has **no** one-letter synonym—you must spell `spots`.

The letter **`a` is `answer`**. **`q` is not quit**; it is **callsign** (set). Type **`help` (h)** for the aligned list the server prints.

---

## Commands

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
`set odd on` enables odd time slot TX (the "Tx first" checkbox); `set odd off` disables it. Use this to choose which half of the FT8 period you **call CQ in** (first vs second). It does not replace the automatic rule used when **replying** — see `answer` below.

```
status
```
Multi-line snapshot: your **callsign** and **grid** (from config, refreshed when the settings dialog closes or after `set callsign` / `set grid`), **dial frequency** in **MHz** on the main waterfall / rig nominal (updated when the dial changes via the same path as rig updates), **TX time slot** (Odd / Even — same meaning as the spectrum marker and `set odd`), **audio offset** in Hz (Tx + Rx selection), the current **decode selection** (or none), and **decode count** for the last completed pass. If the dial is not set (0 Hz internally), the dial line shows **`(not set)`**; MHz is formatted with unnecessary trailing zeros trimmed (e.g. `27.265`).

Example:

```
--------------------------------------------
 status
--------------------------------------------
  Callsign        1AT106
  Grid            JO22
  Dial frequency  27.265 MHz
  TX time slot    Even (Tx second / even FT8 cycle)
  Audio offset    987 Hz  (Tx + Rx)
  Selection       [ 987]   -6  0.1  Germany         CQ 30AT084 JO22
  Decodes (pass)  12
--------------------------------------------
```

---

### Session notes (`[OPERATOR]` and agent `comment`)

Use these when a human and an **automated CLI client** share a session and need short, out-of-band remarks in the **same transcript** as **View → CLI Log…** and **`wsjtcb-cli.log`**.

**Operator → agent (`[OPERATOR]` or CLI inject):** open **View → CLI Log….** Leave **Send as command** unchecked to send a human-only note: **Send** (or Enter) writes **`[OPERATOR] <text>`** as **`OUT`**; embedded newlines become spaces; if the client is **Idle**, it is also sent on the TCP session (`\r\n`, the line with `\r\n`, then **`> `**) like other async server lines. Tick **Send as command** so the inject field behaves as a CLI line: **`IN`** transcript, echoed on TCP, and executed ( **`select 987`**, **`stoptx`**, etc.—same semantics as inbound client text; **Idle** only). Optionally use **Kick** to disconnect after confirmation—the transcript records **SYS operator kicked client** plus peer address/port, and the socket receives **`BYE disconnected by operator`** before disconnect.

**Agent → operator:** from the CLI:

```
comment <text>
```

One-letter synonym: **`n <text>`**. Your line is logged as **`IN`** (same as any other command). The TCP session does **not** include an **`OK:`** acknowledgement for **`comment`** (**`> `** prompt only—see [Transcript log](#transcript-log)); no extra annotated **`OUT`** line is written.

---

### Output format

A burst is emitted automatically at the end of each decode period.

Each burst prints **(1)** a spectrum bar line, **(2)** a marker line under the current **audio offset** (from `select`, or the default **1200 Hz** on connect) showing `▲` in the column for that offset plus `NHz, Odd|Even` for the **TX time slot** (see “Marker line” below), **(3)** decode lines, **(4)** if **Auto TX** is enabled and a **non-empty** message is selected in the main window’s **Tx** macro (and the program is **not** actively transmitting that moment and **not** in **Tune**), one line **`TX QUEUED: <text>`** (same indent as decodes; `<text>` is the selected macro, single-line), then **(5)** a `> ` prompt. There is no separate Hz scale row on the wire—only the bar and the marker.

Decode lines list **FT8** decodes only; the operating **mode** field from the internal wire format is **not** shown (WSJT-CB is FT8-only for this CLI path). Each line includes a **Country** column (16 characters, **DXCC entity** name from the **DE call** in that decode, using the same `CTY.DAT` lookup and short labels as the main window’s territory filter—long names are truncated; unknown / no DE call is shown as an em dash).

*After* `select 987` (so the marker uses 987 Hz; bar line width matches the in-app spectrum width; shortened here for readability):

```
[14:15:30] |   .-:=*#@█  ...+    .  +.  ..     |
            ▲ 987Hz, Odd
                   [1234]  -12  0.3  26AT715 1AT106 +03  U.S.A.
                *![ 987]   -6  0.1  CQ 30AT084 JO22      Germany
                @   [2100]   +2  0.0  30AT084 1XZ001 RR73  France
            TX QUEUED: CQ 1XZ001 JO22
>
```

On a fresh connect (default **1200** Hz, **Even** in this example) before you change selection—spectrum only, no decodes in this pass:

```
[14:15:30] |   .-:=*#@█  ...+    .  +.  ..     |
            ▲ 1200Hz, Even
>
```

**TX start/stop** and **`LOG`** lines are emitted asynchronously when transmission starts or ends, or when a QSO row is successfully written to the main log (**`wsjtcb.log`** / **`LogBook::add`** — i.e. after **Log QSO** or **automatic logging** succeeds). They use the **same UTC** `hh:mm:ss` clock style as the spectrum bar (LOG uses **`QSO off`** UTC time when that value is known). Each event uses the same pattern as a decode burst: the server sends a **bare `\r\n`** first, then one data line ending in `\r\n`, then a **`> `** prompt.

Forms on the wire:

| Event | Line (after the leading `\r\n`) |
|---|---|
| TX start | `[hh:mm:ss] TX: <message>` |
| TX stop | `[hh:mm:ss] TX STOP` |
| QSO logged (log file write succeeded) | `[hh:mm:ss] LOG: <CALL> <grid-or-(no-grid)> @ <MHz> MHz <mode>` |

Example (times are **UTC**):

```
> 
[14:20:12] TX: CQ 1AT106 JO22
> 
[14:20:28] TX STOP
> 
[14:20:31] LOG: 30AT084 IM67 @ 27.265 MHz FT8
> 
```

The **`LOG`** line is sent only after a successful **`LogBook::add`** on the wired path (**Idle** CLI clients see it; **`Unauthed`** or disconnected clients do not). If **`QSO off`** cannot be formatted, the timestamp falls back to the current UTC time; if the dial frequency is **`0`** internally, **`?`** replaces **`<MHz>`**.

**Spectrum bar:** `[hh:mm:ss] |<48 chars>|` — **UTC** time, then 48 columns mapping the active decode window (`nfa`…`nfb` Hz) onto the averaged spectrum (`savg`, linear power from the decoder). Each column uses the strongest bin in that column. Level is first **dB above the median column** (`10·log10(p / p_median)`), then **per-burst normalised** to that row: a robust **percentile window** (roughly 6th–88th %ile of those dB values) is stretched to the 11 print levels, with a **gamma** curve on top so the bulk of the band stays toward spaces and weak symbols while strong points still reach the top of the scale (similar in spirit to the GUI’s log + flatten + stretch, adapted for a fixed-width text bar).

**Marker line:** `▲ NHz, Odd|Even` — `▲` is column-aligned under the selected frequency in the bar above.

Each bar column is one of **11** density steps, **weakest → strongest**: a **space** (quietest), then `.` `-` `:` `=` `+` `*` `#` `%` `@` and finally **`█` (U+2588, full block)**. The non-space symbols match the palette `█@%#*+=-:.` with an extra leading space, and `-` / `:` ordered as `-` then `:` on the way up in strength.

**dB** is `10·log10(p / p_median column)`; then the server maps a **stretched, gamma-shaped** 0…1 value to the 11-point scale (if that step fails numerically, it falls back to a fixed ±dB range like the old behaviour).

Left edge of the bar = `nfa` Hz, right edge = `nfb` Hz (the active decode window).

**Decode line:** eleven leading spaces, then a **fixed-width glyph band** (four characters, padded with spaces on the **right**) followed by **` `** before **`[FFFF]`**. Any subset of **`@`**, **`*`**, **`!`** that applies appears **packed left** in stable order (**`@`**, **`*`**, **`!`**), so you may see combinations like **`@*!`**, **`@*`** or **`!*`** on one line whenever multiple conditions hold. **`@`** — message contains your **callsign** as a whole word; **`*`** — this decode’s transmitting **DX** (same extractor as the **country** column) matches your **`select`**-locked **base partner** (**`*`** can move with frequency); **`!`** — message contains **`CQ`**. Unused slots in the glyph band remain spaces so **`[FFFF]`**, **snr**, **dt**, **message**, and **country** stay vertically aligned burst-to-burst. (These glyphs are unrelated to **`@`** **`*`** in the **spectrum bar** ramp.)

When a DX partner lock is active, each decode burst may **retune Tx/Rx** to their current **`[FREQ]`** if it moved within the allowed window.

Fixed-width columns after the band: `[FFFF]`, `snr`, `dt`, a **message** field **20 characters** wide (left-justified; the decode text is padded with spaces, or **truncated** if it would be longer), two spaces, and **country** (16 characters, left-justified) — the fixed message width makes the **country** column line up. Internal raw lines still include time and mode; the CLI omits the mode token and inserts the country and marks for that decode.

| Field | Description |
|---|---|
| Glyphs | **Four-character** padded band, then **` `**, then **`[FREQ]`**. **`@`** — message mentions **your callsign**. **`*`** — same **DX** as **`select`** lock (**base call**). **`!`** — **`CQ`** in message. Combine when multiple apply; glyphs pack in order **`@`**, **`*`**, **`!`** (e.g. **`@*!`**). Spectrum bar **`@`** / **`*`** are unrelated. |
| `FFFF` | Audio frequency offset in Hz (padded in brackets) — use with `select` |
| `snr` | Signal-to-noise ratio in dB (fixed width) |
| `dt` | Time offset from period start in seconds (fixed width, one decimal) |
| `message` | Decoded message text, padded to **20** display columns (longer text is cut) |
| `country` | DXCC “country” name for the **DE** station (16 characters, left-justified, space-padded) |

---

### Frequency selection and transmit

```
select <FREQ>
```
Select an audio frequency offset in Hz. **Immediately sets both TX and RX audio offset** in the UI. `FREQ` can be:
- The `[FREQ]` of a decode from the last burst — use with `answer` to reply to that station.
- **Any** valid audio frequency in the active decode window — use with `cq` to call CQ on a clear frequency.

The default frequency on connect is **1200 Hz**. On success: **`OK: selected …`** — either the formatted decode line (same layout as in the burst) or **`OK: selected <N> Hz (no decode here — valid for cq)`** when there is no decode at that offset.

```
answer [<FREQ>]
```
Reply to the decode at the selected frequency. With **`answer <FREQ>`** (e.g. `answer 2135`), the CLI selects that audio offset first (same internal effect as `select <FREQ>`), then replies — one step instead of `select` then `answer`. The server does **not** emit **`OK: selected …`** for that implicit select; you only see **`OK: answering …`** on success. This invokes the same **`processMessage` / AutoSeq** path as a **double–click in the main decode list**, including **automatic “Tx first” (odd / even)** from the **decode’s time stamp** (the same `nmod` rule the GUI uses) so you transmit in the **correct half of the T/R period** relative to the station you are answering, not your prior `set odd` choice. (Fox / Hound and other special modes still apply the usual constraints.) The CLI works even if that decode line is not visible in the main window, as long as the parameters match the last burst. Returns an error if no decode exists at the selected frequency (after any optional `select` implied by `answer <FREQ>`). On success: **`OK: answering …`** (formatted decode line matching the burst layout).

```
cq [<FREQ>]
```
Transmit a CQ at the selected audio frequency offset. With **`cq <FREQ>`** (e.g. `cq 2135`), selects that frequency first, then queues CQ. Does **not** require a decode at that frequency — useful for choosing a clear slot by scanning the spectrum bar. On success: **`OK: CQ queued at <N> Hz — will TX at next slot boundary`**.

```
stoptx
```
Same as the main-window **Stop Tx** control: stops any transmission in progress, turns off automatic TX / AutoSeq, clears CQ / reply-in-progress state, and **nothing will transmit again** until you send **`cq`** or **`answer`** (those commands re-arm the same path the UI uses for a new CQ or reply). The server sends **`OK: TX stopped — use cq or answer to start a new sequence`**. (Older discussion of a `set halt on|off` style toggle referred to UI-only behavior; the reliable way to end a sequence is `stoptx` or the Stop Tx button.)

---

### Utility

```
comment <text>
```
Record a short note for the operator in **`wsjtcb-cli.log`** (**`IN`**) — **no** **`OK:`** on the TCP session ( **`> `** only — see [Session notes](#session-notes-operator-and-agent)). Synonym: **`n <text>`**.

```
spots [<n>]
```
Fetch recent **Tx reports** from [PSK Reporter](https://pskreporter.info/)—stations that uploaded decodes identifying **you as sender** on digital modes—similar in spirit to the map URL’s **`txrx=tx`** / “decoded me transmitting”. **`<n>`** is the report limit (**default `20`**, clamped **`1`**–**`200`**). Your **callsign** must be set (Settings / `set callsign`).

The client calls **`GET https://retrieve.pskreporter.info/query`** with **`senderCallsign`**, **`rptlimit`**, **`noactive=1`** (smaller preamble), and **`flowStartSeconds`**: **`-86400`** (24 h lookback, the API maximum) for **manual** **`spots`**; **`-300`** (5 min) for **automatic** ticks so each poll only asks for a short recent slice (see [**PSK Reporter query API**](https://pskreporter.info/pskdev.html)). Responses are fetched **asynchronously**: no immediate **`OK:`** when you type **`spots`**; when the HTTP request finishes the server sends **`OK: Spots for <CALLSIGN>`** (append **` [auto]`** on automatic polls), then **one indented line per row** in fixed columns (left-padded / truncated as needed): **`yyyy-MM-dd hh:mm:ss`** (**19**), **MHz** (**11**, trimmed trailing zeros), **mode** (**10**), RX **callsign** (**13**), **locator** (**12** — long PSK Reporter values are clipped), **SNR** (**5**, **`--`** if missing), **country** (**16**, rightmost column: shortened **DXCC/*CTY*** for that receiver — same abbreviations as the decode list — unknown ⇒ em dash). If the query returns no reports, you still get that **`OK:`** line followed by an indented **`(no rows …)`** note. Then **`> `**. Errors use **`ERR:`** (network, rate limiting / JSON **`message`** from the API, XML parse failures, blocked HTML pages, etc.). Only one **`spots`** request runs at a time; another while busy returns **`ERR: spots query already in progress`**.

Be gentle with public services: **`pskdev.html`** recommends **not querying more often than roughly every five minutes**.

While an **Idle** CLI session is active and your **callsign** is non-empty, WSJT-CB also runs an **automatic** query every **five minutes** (**`rptlimit=10`**) with **`flowStartSeconds=-300`** (5 min of reports per tick)—same endpoint as manual **`spots`**—with a prefixed **`--- Auto: …---`** banner; the **`OK: Spots for …`** line is suffixed with **`[auto]`**. There is **no** retrieve on connect or when the app starts—the **repeating timer** is the only auto path, and it runs only when a client is **connected** and **Idle** (after **`--cli-pass`** if used). **Typing `spots` manually** (when accepted) **restarts that five-minute countdown** from that moment, which helps stay near PSK Reporter’s “retrieve about every five minutes” guidance. The timer is **off** when unauthenticated (**`--cli-pass`**), disconnected, or callsign empty; a tick is skipped if a **`spots`** query is already in-flight (**`auto`** waits for the next interval). If you still hit rate limits, use **`spots`** less often or disconnect the CLI client.

```
help     Print command list (h). One-letter synonyms: see [Single-letter forms](#single-letter-forms).
bye      Close the connection (b). `quit` and `exit` are accepted aliases and send **`BYE`** the same way.
```

`bye`, `quit`, and `exit` respond with **`BYE`**, then the server closes the TCP connection (the operator can also disconnect the client via **View → CLI Log… → Kick**; the client sees **`BYE disconnected by operator`** instead). The duplicate-connection case (`ERR: already connected` if a second client tries to connect) is separate.

---

## Typical Workflow

(If using `--cli-pass`, send the password as the first line, then:)

```
set callsign 1XZ001
set grid JO22

# --- burst arrives automatically (FT8; mode not shown) ---
# [14:15:30] |   .-:=*#@█  ..   + .  ..    |   # bar line (UTC; 48 chars between | |)
#             ▲ 1200Hz, Even         # marker (default 1200 Hz until you select)
#  (message field is 20 columns wide, space-padded; then country, as on the live socket)
#            [1234]  -12  0.3  26AT715 1AT106 +03  U.S.A.
#        !   [ 987]   -6  0.1  CQ 30AT084 JO22      Germany   # ! only; add * when 30AT084 is select partner (shows *! if still CQ)
#        @   [2100]   +2  0.0  30AT084 1XZ001 RR73  France   # @ (add * if 2100 Hz is also your locked partner)

select 987      # pick the CQ from 30AT084 by its audio frequency
answer          # AutoSeq begins; WSJT-CB handles the exchange

# same as the two lines above:
answer 987

comment band looks quiet on 27.265   # or: n band looks quiet ...

# --- monitor subsequent bursts for RR73 / 73 to confirm QSO complete ---

# --- alternatively, call CQ on a clear frequency (no decode needed) ---
select 1350     # choose a clear slot spotted in the spectrum bar
cq              # transmit CQ at 1350 Hz

# same as select 1350 then cq:
cq 1350
```

---

## Error responses

Most failures return a line starting with **`ERR:`** (e.g. **`ERR: unknown command`**). Success often starts with **`OK:`** or a framed **`status`** block. The server does **not** usually drop the connection on error; fix the command and continue. **Exceptions:** **`ERR: wrong password`** after a bad first-line password (**`--cli-pass`**) — the connection **closes**; `bye` / `quit` / `exit` (replies **`BYE`** and closes); and a second simultaneous client (**`ERR: already connected`** then the new connection is rejected).

---

## 11m Callsign Format

WSJT-CB accepts 11m callsigns in the form `N{1,3}L{1,2}N{1,3}` (digits, 1–2 letters, digits), with a special case allowing a 4-digit unit number when the country prefix is a single digit (e.g. `1AT1000`). Standard amateur callsigns are also fully supported. See `README.md` for the full validation table.
