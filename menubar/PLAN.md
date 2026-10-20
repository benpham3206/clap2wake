# clap2wake menu bar — build plan

**Intent:** Give clap2wake a menu bar panel. The panel shows that the listener
works (live mic level, recent events). It holds the settings (keep-awake with
lid closed, listener on/off, sensitivity, clap gap). Must not break: the
audio callback timing, the 45 listener tests, and the running listener.

**Question (one number):** Can one Swift file show the listener's peak at
10 Hz or faster and flip `pmset disablesleep` in one click, while all 45
listener tests stay green?

## Why

With the lid closed and the Samsung backlight off, the Mac has no display.
macOS then forces clamshell sleep every ~10 s, the Scarlett stream dies
(`audio_stream_dead` in `/tmp/clapwake.err`), and Chrome Remote Desktop
wakes it to a lock screen. `sudo pmset -a disablesleep 1` stops this. Ben
needs a one-click switch for it, because he must turn it off before the
Mac goes in a bag.

## Kill criteria (check these first)

Stop and report, without workarounds, if:

1. The live level needs more in the audio callback than one float `max`.
2. The settings need more than load-at-import of `THRESH` and the gap band.
3. The Swift build needs Xcode, a project file, a package, or signing.
4. The sandbox blocks `swiftc`. Report the exact error.

## Decisions (already made — do not reopen)

- **Surface:** SwiftUI `MenuBarExtra` with `.menuBarExtraStyle(.window)`.
  The window is the "widget". A WidgetKit widget is a non-goal: it
  refreshes every few minutes at best, needs Xcode and signing, and its
  sandbox cannot run `pmset`.
- **One Swift file:** `menubar/ClapwakeBar.swift`, built with `swiftc
  -parse-as-library`. macOS 14 or later. AppKit/SwiftUI/Foundation only.
- **Live level:** UDP on `127.0.0.1:47811`, sent by the listener. The app
  binds the port. If nothing listens, the datagrams are dropped.
- **Settings:** JSON file read by `clapwake.py` at import. The app writes it,
  then restarts the listener.
- **Privilege:** try `sudo -n`. If that fails, use an AppleScript admin
  prompt. The app never stores a password.

## Files (yours to write)

| File | Change |
|---|---|
| `menubar/ClapwakeBar.swift` | New. The whole app. |
| `menubar/build.sh` | New. Builds `menubar/build/Clapwake Bar.app`. |
| `com.you.clapwake.bar.plist` | New. LaunchAgent that starts the app at login. Do not install it. |
| `clapwake.py` | Settings loader + level feed. Smallest diff possible. |
| `test_clapwake.py` | Tests for the loader and the feed. |
| `README.md` | One short "Menu bar" section. ASD-STE100 style: short sentences. |

Do not touch: `PROCESS.md`, `scope.py`, `check_clapwake.py`, `hostwatch.py`,
`crd_wake.py`, `hidwake.c`, detector logic, BetterDisplay commands.

## Listener changes (`clapwake.py`)

### Settings loader

- Path: `~/Library/Application Support/clapwake/settings.json`.
  Env var `CLAPWAKE_SETTINGS` overrides the path.
- Keys: `threshold` (float), `max_gap_s` (float). Unknown keys are ignored.
- Precedence: existing env vars (`CLAPWAKE_WAKE_MAX_GAP`, …) > file > default.
- Bounds: `threshold` in [0.20, 0.90] and above `2 * RELEASE`.
  `max_gap_s` in [0.50, 2.50] and above the min gap.
- Missing file: use defaults, no error. Bad JSON or an out-of-bounds value:
  use the default for that key and call `emit_error` once. Never crash.
- Load at import, so `THRESH`, `WAKE_GAP_RANGE_SECONDS`, and the constants
  derived from them (`ECHO_GAP_SECONDS`, `PAIR_EXPIRY_SECONDS`) stay
  consistent. `scope.py` imports `THRESH` and must keep working.
- `test_clapwake.py` must set `CLAPWAKE_SETTINGS` to a path that does not
  exist **before** `import clapwake`, so Ben's real file cannot change
  test results.
- The existing `listening` event already logs `threshold` and `wake_band`.
  The app uses that event to confirm that a change took effect.

### Level feed

- In `callback`: record the running max peak in a shared dict. Only one
  float `max`. No syscall, no allocation beyond what exists.
- In the `while not stopped.wait(POLL_SECONDS)` loop: every 50 ms, send one
  datagram and reset the max. Payload, compact JSON:
  `{"peak": 0.123, "thresh": 0.55, "release": 0.08}`.
- One non-blocking UDP socket, made once per process. Catch and ignore
  `OSError` on send. A failure to send must never log, raise, or slow the
  loop.

## App (`menubar/ClapwakeBar.swift`)

### Look

Clean, quiet, native. System materials and system colors only, so light and
dark mode work without effort. SF Symbols. No custom fonts, no gradients,
no decoration. One accent: the system accent color. About 320 pt wide.
Generous spacing, thin dividers between sections.

### Menu bar icon

SF Symbol `hands.clap` (if missing, use `waveform`). Filled when the
listener runs. Secondary (dimmed) style when it is stopped.

### Panel, top to bottom

1. **Header.** "clap2wake" and a status line with a small dot:
   green "Listening · <device>", grey "Stopped",
   orange "Mic stalled" (an `audio_stream_dead` in the last 30 s).
2. **Live meter.** Horizontal bar, 0 to 1. Fills with the peak. Short decay,
   plus a peak-hold tick that falls after ~1 s. A thin vertical mark at
   `thresh` and a fainter one at `release`, from the datagram. The fill uses
   the accent color when the peak is at or above `thresh`. If no datagram
   arrives for 1.5 s, show "No signal" in secondary text.
   Respect Reduce Motion: no decay animation, show the raw value.
3. **Toggles.**
   - "Keep awake with lid closed" with the caption "Off before you pack the
     Mac." Shows the real `SleepDisabled` value from `pmset -g`, never the
     intent.
   - "Listener."
4. **Settings.** Two segmented controls.
   - Sensitivity: Low / Medium / High = threshold 0.65 / 0.55 / 0.45.
   - Clap gap: 0.8 s / 1.2 s / 1.6 s = `max_gap_s`.
   A change writes the settings file atomically (temp file + rename), then
   restarts the listener. Show "Applying…" until the next `listening` event
   shows the new values, then "Applied". Show an error after 10 s without it.
   A value from the file that matches no preset shows no segment selected.
5. **Recent events.** The last 6 events from `/tmp/clapwake.out` and
   `/tmp/clapwake.err` (JSON lines), newest first. Leave out heartbeats.
   Short labels: `wake_triggered` → "Wake", `sleep_triggered` → "Dim",
   `sleep_suppressed` → "Dim skipped (locked)", `audio_stream_dead` →
   "Mic stalled", `listening` → "Listening", others → the raw name.
   Time as `HH:mm:ss` in monospaced digits.
6. **Footer.** Small borderless buttons: "Health check" (runs
   `.venv/bin/python3 check_clapwake.py`, shows the output in an expanding
   monospaced text area), "Open log" (`open -a Console /tmp/clapwake.err`),
   "Quit".

### Behavior

- Repo path: the parent of the folder that holds the `.app`'s build dir is
  not reliable. Use a constant `~/clapwake`, overridable by the
  `CLAPWAKE_DIR` environment variable.
- Keep-awake set: run `/usr/bin/sudo -n /usr/bin/pmset -a disablesleep N`.
  If it exits non-zero, run `do shell script "/usr/bin/pmset -a disablesleep N"
  with administrator privileges` through `NSAppleScript`. Then read the
  state again. If the user cancels, the toggle returns to the real state.
- Listener on: `launchctl bootstrap gui/<uid>
  ~/Library/LaunchAgents/com.you.clapwake.plist`. Off: `launchctl bootout
  gui/<uid>/com.you.clapwake`. Restart: `launchctl kickstart -k
  gui/<uid>/com.you.clapwake`. Loaded: exit status of `launchctl print
  gui/<uid>/com.you.clapwake`. Running: that output has a `pid = ` line. The
  switch follows "loaded"; the icon and status line follow "running".
- Poll `pmset -g` and `launchctl print` every 5 s and when the panel opens.
  Tail the two logs every 1 s from the last byte offset. If a file shrinks,
  start again from 0.
- Run every process off the main thread. The UI never blocks.
- Every control has an accessibility label.

### Build (`menubar/build.sh`)

- `swiftc -parse-as-library -O -warnings-as-errors -module-cache-path
  menubar/build/ModuleCache` → `menubar/build/Clapwake Bar.app/Contents/MacOS/ClapwakeBar`.
- Write `Info.plist` with `LSUIElement` = true, bundle id
  `com.you.clapwake.bar`, `LSMinimumSystemVersion` 14.0.
- Ad-hoc sign: `codesign -s - --force`.
- `--install` copies the app to `~/Applications/`. Do not run `--install`.

## Rules for this run

- Work only in this worktree. Do not commit. Leave changes in the tree.
- Do not run: `sudo`, any `pmset` set command, `launchctl bootstrap`,
  `bootout`, or `kickstart`, `open` on the app, `build.sh --install`.
  Do not change system state. Reads (`pmset -g`, `launchctl print`) are fine.
- No new Python dependencies. No Swift packages.

## Acceptance checks (run all, paste the last lines)

1. `.venv/bin/python3 test_clapwake.py` → all pass, more than 45 tests.
2. `.venv/bin/python3 test_crd_wake.py` → passes.
3. `bash menubar/build.sh` → exit 0, no warnings.
4. `plutil -lint com.you.clapwake.bar.plist "menubar/build/Clapwake Bar.app/Contents/Info.plist"`.
5. New tests cover: missing file → defaults; bad JSON → defaults;
   out-of-bounds value → default for that key; env var beats file;
   feed send that raises `OSError` does not raise.

## Return

A short report: files changed; any place you differed from this plan and
why; output tails of the four checks; known gaps.
