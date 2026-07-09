# clap2wake

Clap twice to wake your display. Clap three times to put it to sleep.

A background listener watches a microphone for claps and fires the OS action —
no state-based toggle, since external-display sleep state isn't reliably
queryable on macOS.

## How it works

- `clapwake.py` — the listener. Runs as a macOS LaunchAgent, listens on a
  pinned mic device, and counts claps into a burst.
- Bursts fire only after going quiet for a short settle window, so a 2-clap
  wake and a 3-clap sleep can be told apart before anything happens.
- A hysteresis gate (peak must drop back down before the next clap counts)
  stops a single clap's reverb tail from being miscounted as an extra clap.
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
```

Load it as a LaunchAgent (see `PROCESS.md` for the full story of how this was
built), pointing `ProgramArguments` at `.venv/bin/python3 clapwake.py`.

## Tuning

```bash
.venv/bin/python3 scope.py
```

Clap and watch the bar. `|` marks the wake/sleep threshold, `:` marks the
release level. If a double-clap ever shows a 3rd count, raise `RELEASE` or
`REFRACTORY_SECONDS` at the top of `clapwake.py`.

## Tests

```bash
.venv/bin/python3 test_clapwake.py
```
