# clap2wake

Clap or snap twice to toggle the monitor. Use the same gesture for both directions.
The microphone must receive each sound above the configured onset threshold.

| Monitor state | Two claps/snaps do this |
|---|---|
| Lit | Set software brightness to 0 and DDC luminance to 0 |
| Dim | Set software brightness to 1 and DDC luminance to 100 |

The default gap between sounds is **0.12–1.20 seconds**, with a 5 ms boundary
tolerance. Faster onsets are treated as echoes. An onset beyond the window starts
a new pair. There is no fast-for-wake or slow-for-sleep split.

## Behavior and safeguards

- BetterDisplay reads software brightness and DDC luminance from `LS32CG51x`.
  Either channel near zero means dim. If both reads fail, the pair requests wake.
- Dimming changes monitor brightness, not macOS display power. It does not call
  `pmset displaysleepnow`, which can trigger clamshell sleep and locking.
- Wake uses F18 HID events, IOPM activity, `caffeinate`, and BetterDisplay.
  Sparse pulses at 0, 0.8, 2, and 4 seconds cover deep DisplayPort idle.
- Read-back checks log `sleep_verify` or `wake_verify`. A mismatch can trigger up
  to four extra brightness commands. Exhaustion logs an `*_unverified` error.
  The final extra command is not followed by another check.
- Dimming requires an unlocked session and three seconds without recent human
  keyboard or mouse input. The listener accounts for its own wake HID events.
  A blocked action logs `sleep_suppressed`. Wake does not use this guard.
- Onset threshold is `0.35`; release level is `0.08`. Quiet rearming, echo
  rejection, and a busy-room gate reduce false triggers from tails and audio.
- Capture stays pinned to the Scarlett by name. The listener retries missing
  devices, reconnects stalled streams, and requests process restarts for repeated
  failures. Heartbeats report callback age, not just process existence.

## Files

- `clapwake.py`: detection, brightness decisions, wake/dim actions, capture recovery.
- `hostwatch.py`: power-source and microphone-index change detection.
- `check_clapwake.py`: health report; `--repair` can bootstrap or restart the listener.
- `scope.py`: microphone meter using the same detector with actions stubbed out.
- `crd_wake.py`: wakes the monitor when a Chrome Remote Desktop session starts.
- `hidwake.c`: the native F18 helper.
- `PROCESS.md`: historical implementation notes, not the current gesture contract.

## Setup

Requires macOS, Python, `numpy`, `sounddevice`, BetterDisplay, and a microphone
available while the lid is closed. The configured device is a Scarlett 2i2.

```bash
python3 -m venv .venv
.venv/bin/pip install sounddevice numpy
clang -Wall -Wextra -Werror -Wno-deprecated-declarations \
  -framework IOKit -framework CoreFoundation \
  hidwake.c -o .venv/bin/clapwake-hid
```

The helper and listener need the relevant macOS microphone, Accessibility, and
Input Monitoring permissions. The monitor name and command paths are defined in
`clapwake.py`.

The listener runs as `com.you.clapwake`, using a user LaunchAgent that invokes
`.venv/bin/python3 clapwake.py`. The local setup wraps it with `caffeinate -s`.
The main listener plist is installed locally, not included in this repository.

The repository includes optional watchdog and Chrome Remote Desktop plists.
Their paths are specific to this machine; adjust them before installing elsewhere.
The watchdog runs `check_clapwake.py --repair` every 30 seconds when installed and
loaded. It is not active merely because its plist exists. The local watchdog was
not loaded when checked on 2026-09-20.

## Tuning and diagnostics

```bash
.venv/bin/python3 scope.py
.venv/bin/python3 check_clapwake.py
```

Scope pauses the background listener and restores it on exit. It shows microphone
peaks and detected pair gaps without changing monitor brightness. Measure claps,
snaps, and desk noise before changing `THRESH`; quieter snaps may not cross it.

LaunchAgent environment overrides:

- `CLAPWAKE_MIC_NAME`: preferred microphone name substring; default `Scarlett`.
- `CLAPWAKE_WAKE_MIN_GAP` / `CLAPWAKE_WAKE_MAX_GAP`: the shared pair window.
  Bounds must satisfy `0.12 <= min < max <= 1.50`.
- `CLAPWAKE_SLEEP_HID_IDLE`: minimum human-input idle time; default `3.0` seconds.

Logs are `/tmp/clapwake.out` and `/tmp/clapwake.err`. A trigger records a request;
read-back records whether both brightness channels reached their target range.
Some diagnostic field names retain the older wake/sleep-band terminology.

## Tests

```bash
.venv/bin/python3 test_clapwake.py
.venv/bin/python3 test_crd_wake.py
```

These are offline tests with simulated peaks and stubbed actions. They do not
replace a physical clap/snap test on the configured microphone and monitor.
