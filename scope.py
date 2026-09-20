#!/usr/bin/env python3
"""Live mic meter for tuning clapwake.

Shows a rolling peak-amplitude bar, marks the THRESH and RELEASE lines, and
prints when a gesture would fire. It drives the *actual* clapwake.ClapDetector
(with its firing stubbed out), so what you see here is exactly what the listener
does -- no duplicated detection logic to drift out of sync.

Events (listening / wake / sleep / stopped) are appended to the same JSON log
the LaunchAgent uses (`/tmp/clapwake.out`) with source=scope, so you can:

    tail -f /tmp/clapwake.out

Clap or snap twice within the shared pair window. Panel brightness chooses the displayed action. Each detected pair prints its measured gap.

On exit, scope always re-bootstraps com.you.clapwake (not only if pause
succeeded). Leaving the LaunchAgent unloaded is what made the sleep gesture
appear broken after a scope session.

Run:  ~/clapwake/.venv/bin/python3 ~/clapwake/scope.py
Quit: Ctrl-C
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import signal
import subprocess
import sys
import time
from typing import Any

import numpy as np
import sounddevice as sd

import clapwake
from clapwake import RELEASE, THRESH

SAMPLERATE = 44100
BLOCK = 1024  # ~23 ms per block

# Same path as com.you.clapwake StandardOutPath — one tail for both tools.
LOG_PATH = "/tmp/clapwake.out"

# The background listener and this meter both pin to the same mic, so they can't
# run at once -- pause the LaunchAgent while tuning, then restore it on exit.
LISTENER_LABEL = "com.you.clapwake"
LISTENER_PLIST = os.path.expanduser("~/Library/LaunchAgents/com.you.clapwake.plist")


def log_event(payload: dict[str, Any]) -> None:
    """Append a timestamped JSON line to the shared clapwake log (and stderr)."""
    stamped = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "source": "scope",
        **payload,
    }
    line = json.dumps(stamped, sort_keys=True)
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    except OSError as exc:
        print(f"\n[log write failed] {exc}", file=sys.stderr, flush=True)
    # Also mirror to stderr so an interactive session sees events without tail.
    print(line, file=sys.stderr, flush=True)


def _listener_loaded() -> bool:
    return subprocess.run(
        ["launchctl", "list", LISTENER_LABEL],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode == 0


def pause_listener() -> bool:
    """Boot the background clapwake agent out so it releases the mic."""
    if not _listener_loaded():
        print(f"(background listener {LISTENER_LABEL} was not loaded)")
        return False
    result = subprocess.run(
        ["launchctl", "bootout", f"gui/{os.getuid()}/{LISTENER_LABEL}"],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        err = (result.stderr or result.stdout or "").strip()
        print(f"failed to pause listener: {err or result.returncode}", file=sys.stderr)
        return False
    print(f"paused background listener ({LISTENER_LABEL})")
    time.sleep(0.3)  # let it release the audio device before we open our stream
    return True


def ensure_listener() -> bool:
    """Make sure com.you.clapwake is loaded. Safe to call multiple times.

    Always run on scope exit — not only when we successfully paused — so a
    crashed/killed scope session cannot leave clapwake unloaded (the failure
    mode where clap-pair sleep 'stops working').
    """
    if _listener_loaded():
        print(f"background listener already running ({LISTENER_LABEL})")
        return True
    if not os.path.isfile(LISTENER_PLIST):
        print(f"cannot resume: missing {LISTENER_PLIST}", file=sys.stderr)
        return False
    result = subprocess.run(
        ["launchctl", "bootstrap", f"gui/{os.getuid()}", LISTENER_PLIST],
        capture_output=True,
        text=True,
    )
    # Give launchd a beat, then verify by list (bootstrap can 'succeed' and still fail).
    time.sleep(0.4)
    if _listener_loaded():
        print(f"resumed background listener ({LISTENER_LABEL})")
        log_event(
            {
                "component": "clapwake.scope",
                "event": "listener_resumed",
                "label": LISTENER_LABEL,
            }
        )
        return True
    err = (result.stderr or result.stdout or "").strip()
    print(
        f"FAILED to resume {LISTENER_LABEL}: {err or f'exit {result.returncode}'}\n"
        f"  fix: launchctl bootstrap gui/$(id -u) {LISTENER_PLIST}",
        file=sys.stderr,
    )
    log_event(
        {
            "component": "clapwake.scope",
            "event": "listener_resume_failed",
            "root_cause": err or str(result.returncode),
            "failure_type": "listener_resume_failed",
        }
    )
    return False


def find_device() -> int:
    """Pin to the same mic clapwake uses; fall back to the system default so the
    meter still runs when the preferred interface is unplugged."""
    selected = clapwake.select_preferred_mic(sd)
    if selected is not None:
        return selected[0]
    print("(preferred mic absent -- falling back to system default input)")
    return int(sd.default.device[0])


class Meter:
    def __init__(self) -> None:
        # The real detector, with firing redirected into last_fire so we can
        # display it instead of waking/sleeping the display.
        self.detector = clapwake.ClapDetector()
        self.detector._run = self._record  # type: ignore[method-assign]
        self.last_fire = ("", 0, 0.0)  # (action, count, monotonic time)
        self.peak_hold = 0.0
        self.peak_hold_at = 0.0

    def _record(
        self,
        _commands: list[list[str]],
        action: str,
        count: int,
        gap: float = 0.0,
    ) -> None:
        self.last_fire = (action, count, time.monotonic())
        # Same shape as clapwake.ClapDetector._run event lines, plus source=scope.
        log_event(
            {
                "component": "clapwake.detector",
                "event": f"{action}_triggered",
                "clap_count": count,
                "gap_s": round(gap, 3),
            }
        )
        # Break the \r meter line so the fire is visible in the terminal too.
        # The gap is what you are tuning now, so lead with it.
        sys.stdout.write(
            f"\n>> {action.upper()} would fire  —  gap {gap * 1000:.0f}ms"
            f"  [logged → {LOG_PATH}]\n"
        )
        sys.stdout.flush()

    def observe(self, peak: float) -> None:
        now = time.monotonic()
        # Peak-hold decays over ~0.8s so a spike stays visible briefly.
        if peak >= self.peak_hold or now - self.peak_hold_at > 0.8:
            self.peak_hold = peak
            self.peak_hold_at = now
        # Feed the real detector, then let it settle just like the listener's
        # main loop does.
        self.detector.observe_peak(peak, now=now)
        self.detector.settle(now=now)

    def render(self, peak: float) -> str:
        cols = shutil.get_terminal_size((80, 20)).columns
        bar_width = max(20, cols - 44)

        filled = min(bar_width, int(peak * bar_width))
        thresh_col = min(bar_width - 1, int(THRESH * bar_width))
        release_col = min(bar_width - 1, int(RELEASE * bar_width))

        cells = []
        for i in range(bar_width):
            if i == thresh_col:
                cells.append("|")  # THRESH marker
            elif i == release_col:
                cells.append(":")  # RELEASE marker
            elif i < filled:
                cells.append("#" if peak >= THRESH else "=")
            else:
                cells.append(" ")
        bar = "".join(cells)

        now = time.monotonic()
        action, count, fired_at = self.last_fire
        if now - fired_at < 0.9 and action:
            tag = f" {action.upper()}! ({count})"
        else:
            armed = "armed" if self.detector._armed else "wait "
            tag = f" burst={self.detector.burst_count} {armed}"

        return f"\r[{bar}] {peak:5.3f} thr={THRESH:.2f} rel={RELEASE:.2f}{tag}   "


def main() -> int:
    meter = Meter()
    stop = False

    def handle(_s: int, _f: Any) -> None:
        nonlocal stop
        stop = True

    signal.signal(signal.SIGINT, handle)
    signal.signal(signal.SIGTERM, handle)
    # Closing the terminal often delivers SIGHUP; treat it like a clean stop so
    # finally/atexit can re-bootstrap the listener instead of leaving it dead.
    if hasattr(signal, "SIGHUP"):
        signal.signal(signal.SIGHUP, handle)

    # atexit is a backstop when the process dies outside the try/finally path.
    atexit.register(ensure_listener)

    pause_listener()  # best-effort; may already be down
    try:
        device = find_device()
        name = sd.query_devices(device)["name"]
        print(f"Listening on device {device}: {name}")
        print(f"Event log: {LOG_PATH}  (tail -f {LOG_PATH})")
        print(
            "Bar fills with amplitude; '|' = THRESH, ':' = RELEASE. "
            "Clap to test. Ctrl-C to quit.\n"
        )

        log_event(
            {
                "component": "clapwake.stream",
                "event": "listening",
                "device_index": device,
                "device_name": name,
                "threshold": THRESH,
            }
        )

        def callback(indata: Any, _frames: int, _t: Any, status: Any) -> None:
            if status:
                msg = str(status)
                print(f"\n[stream status] {msg}", file=sys.stderr, flush=True)
                log_event(
                    {
                        "component": "clapwake.stream",
                        "root_cause": msg,
                        "failure_type": "audio_stream_status",
                    }
                )
            peak = float(np.max(np.abs(indata)))
            meter.observe(peak)
            sys.stdout.write(meter.render(peak))
            sys.stdout.flush()

        with sd.InputStream(
            device=device,
            channels=1,
            samplerate=SAMPLERATE,
            blocksize=BLOCK,
            dtype="float32",
            callback=callback,
        ):
            while not stop:
                time.sleep(0.05)

        print("\nstopped")
        log_event({"component": "clapwake.stream", "event": "stopped"})
    finally:
        ensure_listener()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
