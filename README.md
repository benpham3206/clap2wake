# clap2wake

Clap a **fast** pair to wake your display. Clap a **slow** pair to sleep it.

A background listener watches a microphone and fires the OS action. Both
gestures are two claps — the **tempo** of the pair is the intent, not the count.

## How it works

- `clapwake.py` — the listener. Runs as a macOS LaunchAgent, listens on a
  pinned mic device, and times the gap between a pair of claps.
- **Tempo decides, and it decides on clap 2:**

  | gap between the two claps | result |
  |---|---|
  | < 0.15 s | echo of clap 1 — ignored, still waiting for the real partner |
  | **0.15 – 0.40 s** | **wake** |
  | 0.40 – 0.60 s | dead zone — ignored, refuses to guess |
  | **0.60 – 1.10 s** | **sleep** |
  | > 1.10 s | too late to pair; that clap starts a new pair |

- **Why tempo and not a 3-clap sleep.** Counting made "2 claps" a *prefix* of
  "3 claps". A prefix code has to wait to learn which gesture it received, so
  every spurious onset flipped wake→sleep and every missed onset flipped
  sleep→wake. Widening the window to catch real triples is exactly what let
  room echoes hijack doubles — the two failure modes rode one scalar and no
  value of it fixed both. Tempo removes the prefix: intent is known the instant
  clap 2 lands, nothing is deferred, and a third onset means nothing at all.
- **No disambiguation delay.** Wake used to wait ~0.90 s for a possible 3rd
  clap. It now fires on clap 2.
- **Hybrid wake action:** IOHIDSystem F18 + BetterDisplay DDC
  (`--hardwareBacklight=on` for `LS32CG51x`) + IOPM + `caffeinate -u` (pulse 0).
  Retries at 0.8 / 2 / 4s re-fire HID and DDC for deep DPMS. F18 is inert on
  the configured layout and does not type into applications.
- Claps/snaps: onset **0.22** / re-arm **0.08** (hysteresis). Override the two
  tempo bands with `CLAPWAKE_WAKE_MIN_GAP` / `CLAPWAKE_WAKE_MAX_GAP` and
  `CLAPWAKE_SLEEP_MIN_GAP` / `CLAPWAKE_SLEEP_MAX_GAP`. The bands must keep a
  **≥ 0.10s dead zone** between them; the listener refuses to start otherwise.
  Continuous audio (recording) blocked by the busy-room gate.
- Mic unplug / CoreAudio wedge is **self-healing** (open-config matrix, backoff,
  clean process-boundary restart). Silence no longer causes stream churn.
  Heartbeats in `/tmp/clapwake.out` report callback and signal age separately.
- **Power boundary:** recording from the Scarlett keeps the listener alive but
  does not prevent display idle. The LaunchAgent wrapper uses `caffeinate -s`
  (system-sleep prevention), while macOS may still enter deep display sleep
  after its `displaysleep` timeout. A 3-clap sleep is normally shallow and
  wakes quickly; a long idle can require DisplayPort/monitor renegotiation.
- **Sleep** remains `pmset displaysleepnow` (no BetterDisplay off path).
- `scope.py` — a live terminal meter for tuning thresholds against your mic
  and room. It pauses the background listener while running (both can't hold
  the mic at once) and resumes it on exit.
- `test_clapwake.py` — offline tests for the detector using a fake clock, no
  real audio or launchd required.

## Requirements

- macOS
- Python 3 with `sounddevice` and `numpy`
- A dedicated input device (tested against a Focusrite Scarlett 2i2); pin it
  by name via the `CLAPWAKE_MIC_NAME` env var
- [BetterDisplay](https://github.com/waydabber/BetterDisplay) if your monitor
  needs a DDC wake command in addition to `caffeinate`/`pmset`

## Setup

```bash
python3 -m venv .venv
.venv/bin/pip install sounddevice numpy
clang -Wall -Wextra -Werror -Wno-deprecated-declarations \
  -framework IOKit -framework CoreFoundation \
  hidwake.c -o .venv/bin/clapwake-hid
```

The HID helper uses Apple's IOHIDSystem post-event path. The first manual run
may request Input Monitoring access:

```bash
.venv/bin/clapwake-hid
```

Load it as a LaunchAgent (see `PROCESS.md` for the full story of how this was
built), pointing `ProgramArguments` at `.venv/bin/python3 clapwake.py`.

## Tuning

```bash
.venv/bin/python3 scope.py
```

Clap and watch the bar. `|` marks the onset threshold, `:` marks the release
level. Every fire prints the **measured gap in ms** — that number is what you
tune the bands against.

Clap your natural fast pair ten times and read the gaps. Set
`CLAPWAKE_WAKE_MAX_GAP` above your slowest, not at the default, and do the same
for your slow pair with `CLAPWAKE_SLEEP_MIN_GAP`. Set them in the LaunchAgent
environment. Values must satisfy `0.12 <= wake_min < wake_max`,
`wake_max + 0.10 <= sleep_min < sleep_max <= 1.50` — the listener refuses to
start if the bands touch, because a dead zone is what makes a sloppy pair do
nothing instead of guessing.

## Tests

```bash
.venv/bin/python3 test_clapwake.py
```
