#!/usr/bin/env python3
"""Double-clap listener that wakes the display with caffeinate."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Any

THRESH = 0.35                # onset level: a clap must peak at least this loud
RELEASE = 0.15               # re-arm level: the signal must fall back below this
                             # before the next clap is counted (see hysteresis note)
MAX_GAP_SECONDS = 0.55       # max spacing between claps in one burst; also how
                             # long before we abandon a lone clap / noise count
# After the 2nd clap, wait briefly for a possible 3rd. If none arrives, wake.
# This is the irreducible 2-vs-3 clap tradeoff: wake cannot fire earlier than
# this without making a 3-clap sleep begin as a wake. Keep it short for latency.
WAKE_COMMIT_SECONDS = 0.32
# After the 3rd clap, wait this long (not full MAX_GAP) before sleep — we already
# know the gesture, just need a brief quiet to avoid a 4th noise onset.
SLEEP_COMMIT_SECONDS = 0.12
REFRACTORY_SECONDS = 0.12    # ignore immediate echoes of the same clap
# Cooldowns are asymmetric: wake needs a longer mute window so panel/room noise
# cannot immediately 3-clap sleep the just-woken display. Sleep only needs a
# short mute so you can double-clap wake again almost immediately.
WAKE_COOLDOWN_SECONDS = 2.0
SLEEP_COOLDOWN_SECONDS = 0.3
# Back-compat alias used by older notes/tests that meant "post-wake" mute.
ACTION_COOLDOWN_SECONDS = WAKE_COOLDOWN_SECONDS

# Gesture -> action:  2 claps = wake, 3 claps = sleep.
# (No reliable way to detect display on/off state on this external monitor, so we
#  use distinct clap counts instead of a state-based toggle.)
#
# Fast-wake commit (hybrid):
#   - burst==2 and quiet for WAKE_COMMIT_SECONDS  -> wake (does NOT wait MAX_GAP)
#   - burst==3 and quiet for SLEEP_COMMIT_SECONDS -> sleep only (never wake first)
#   - burst==1 abandoned after MAX_GAP
# A 3rd clap that arrives before the wake commit cancels wake and continues as
# a sleep candidate. That preserves the no-flash sleep path for tight triples.
#
# Counting uses hysteresis (see ClapDetector.observe_peak): a new clap is only
# counted after the level has fallen back below RELEASE. A single clap's decaying
# reverb tail therefore can't be miscounted as an extra clap.
WAKE_CLAPS = 2
SLEEP_CLAPS = 3

# External Samsung LS32CG51x is DDC-capable, so relight the panel directly in
# addition to the OS-level idle-sleep reversal. Both fire on a wake gesture.
# caffeinate -u default timeout is 5s; hold longer so the display idle timer
# actually resets instead of the panel sagging right after a synthetic tickle.
BETTERDISPLAY_BIN = "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
WAKE_COMMANDS = [
    ["caffeinate", "-u", "-t", "20"],
    [BETTERDISPLAY_BIN, "set", "--name=LS32CG51x", "--hardwareBacklight=on"],
]
SLEEP_COMMANDS = [
    ["pmset", "displaysleepnow"],
]

# Pin the listener to a specific mic by NAME (substring match, case-insensitive)
# rather than following the system default. The built-in mic is hardware-disabled
# in clamshell mode (lid closed), so the default silently falling back to it left
# the listener deaf. Name-pinning also survives device-index reshuffling when USB
# devices are plugged/unplugged. Override with the CLAPWAKE_MIC_NAME env var.
PREFERRED_MIC_NAME = os.environ.get("CLAPWAKE_MIC_NAME", "Scarlett")
DEVICE_ABSENT_RETRY_SECONDS = 3.0   # pause before exiting when the mic is missing
DEAD_STREAM_SECONDS = 5.0           # restart if the mic delivers no audio this long
POLL_SECONDS = 0.005                # main-loop tick: how often settle() is checked
STREAM_BLOCKSIZE = 128              # small PortAudio buffers reduce clap-detection lag
STREAM_LATENCY = "low"              # ask CoreAudio/PortAudio for low-latency capture


def emit(payload: dict[str, Any], *, stream: Any = sys.stderr) -> None:
    # Wall-clock stamp so wake→sleep gaps in /tmp/clapwake.out are measurable.
    stamped = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
    print(json.dumps(stamped, sort_keys=True), file=stream, flush=True)


def error_payload(component: str, root_cause: str, failure_type: str) -> dict[str, str]:
    return {
        "component": component,
        "root_cause": root_cause,
        "failure_type": failure_type,
    }


def import_audio_modules() -> tuple[Any, Any] | None:
    try:
        import numpy as np
        import sounddevice as sd

        return np, sd
    except Exception as exc:  # noqa: BLE001 - dependency/import failures need structured output.
        emit(
            error_payload(
                "clapwake.dependencies",
                f"{type(exc).__name__}: {exc}",
                "dependency_import_failed",
            )
        )
        return None


def device_name(device: dict[str, Any]) -> str:
    return str(device.get("name", ""))


def input_channels(device: dict[str, Any]) -> int:
    try:
        return int(device.get("max_input_channels", 0))
    except Exception:
        return 0


def select_preferred_mic(sd: Any) -> tuple[int, str] | None:
    """Return (index, name) of the first input device whose name contains
    PREFERRED_MIC_NAME (case-insensitive), or None if it is not present.

    Deliberately does NOT fall back to any other device: if the pinned mic is
    absent we want to fail loudly, not silently latch onto the built-in mic.
    """
    try:
        devices = list(sd.query_devices())
    except Exception as exc:  # noqa: BLE001
        emit(
            error_payload(
                "clapwake.device",
                f"{type(exc).__name__}: {exc}",
                "device_query_failed",
            )
        )
        return None

    target = PREFERRED_MIC_NAME.lower()
    available: list[str] = []
    for index, device in enumerate(devices):
        if input_channels(device) <= 0:
            continue
        name = device_name(device)
        available.append(f"{index}:{name}")
        if target in name.lower():
            return index, name

    emit(
        error_payload(
            "clapwake.device",
            f"pinned mic {PREFERRED_MIC_NAME!r} not found; available inputs: "
            + (", ".join(available) or "none"),
            "preferred_mic_absent",
        )
    )
    return None


class ClapDetector:
    def __init__(self) -> None:
        # observe_peak runs on the PortAudio callback thread; settle runs on the
        # main loop thread. The lock guards the shared burst state between them.
        self._lock = threading.Lock()
        self.last_clap_at = 0.0
        self.burst_count = 0
        # Hysteresis gate: True once the level has fallen below RELEASE, meaning
        # we are ready to count the next distinct clap onset. Starts armed.
        self._armed = True
        # Monotonic deadline: while now < _cooldown_until, drop onsets so a
        # just-fired wake cannot be followed by a phantom sleep burst.
        self._cooldown_until = 0.0

    def observe_peak(self, peak: float, now: float | None = None) -> None:
        """Count clap onsets into the current burst. Does NOT act -- firing is
        deferred to settle() so the final count decides the action.

        Uses hysteresis: a clap is only counted while "armed", and we disarm on
        each counted onset until the level drops back below RELEASE. A single
        clap's decaying tail / reverb therefore cannot re-cross THRESH and be
        miscounted as an extra clap (the bug that turned a 2-clap wake into a
        3-clap sleep). ``now`` is injectable so the detector can be driven by a
        fake clock in tests.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if now < self._cooldown_until:
                # Discard in-flight burst tails during cooldown so they cannot
                # fire the instant the window ends.
                self.burst_count = 0
                self._armed = peak < RELEASE
                return
            # Re-arm once the signal has quieted; a level between RELEASE and
            # THRESH neither counts nor re-arms.
            if peak < RELEASE:
                self._armed = True
                return
            if not self._armed or peak < THRESH:
                return
            if now - self.last_clap_at < REFRACTORY_SECONDS:
                return
            self._armed = False
            self.last_clap_at = now
            self.burst_count += 1

    def settle(self, now: float | None = None) -> None:
        """Commit wake/sleep based on burst count and the right quiet window.

        - 2 claps + WAKE_COMMIT quiet   -> wake (fast path)
        - 3 claps + SLEEP_COMMIT quiet  -> sleep only (no prior wake)
        - 1 clap  + MAX_GAP quiet       -> abandon
        - other counts after MAX_GAP    -> abandon
        """
        now = time.monotonic() if now is None else now
        action: str | None = None
        count = 0
        with self._lock:
            if now < self._cooldown_until:
                return
            if self.burst_count == 0:
                return

            idle = now - self.last_clap_at
            n = self.burst_count

            if n == WAKE_CLAPS:
                # Fast wake: don't wait the full sleep-disambiguation gap.
                if idle < WAKE_COMMIT_SECONDS:
                    return
                count = n
                self.burst_count = 0
                self._cooldown_until = now + WAKE_COOLDOWN_SECONDS
                action = "wake"
            elif n == SLEEP_CLAPS:
                # Fast sleep commit once we already have 3 onsets.
                if idle < SLEEP_COMMIT_SECONDS:
                    return
                count = n
                self.burst_count = 0
                self._cooldown_until = now + SLEEP_COOLDOWN_SECONDS
                action = "sleep"
            else:
                # 1 clap, or 4+ noise: drop once the burst has gone quiet.
                if idle < MAX_GAP_SECONDS:
                    return
                self.burst_count = 0
                return

        if action == "wake":
            self._run(WAKE_COMMANDS, "wake", count)
        elif action == "sleep":
            self._run(SLEEP_COMMANDS, "sleep", count)

    def _run(self, commands: list[list[str]], action: str, count: int) -> None:
        # Non-blocking launch so settle/audio never stall on CLI lifetime. We still
        # reap exit codes on a daemon thread so silent BetterDisplay/pmset failures
        # show up in the log instead of vanishing into DEVNULL.
        def launch(command: list[str]) -> None:
            try:
                proc = subprocess.Popen(
                    command,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    start_new_session=True,
                    text=True,
                )
            except Exception as exc:  # noqa: BLE001
                emit(
                    error_payload(
                        "clapwake.action",
                        f"{command[0]}: {type(exc).__name__}: {exc}",
                        f"{action}_command_failed",
                    )
                )
                return

            def reap() -> None:
                try:
                    out, err = proc.communicate(timeout=12)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    emit(
                        error_payload(
                            "clapwake.action",
                            f"{command[0]}: timed out after 12s",
                            f"{action}_command_timeout",
                        )
                    )
                    return
                if proc.returncode not in (0, None):
                    detail = (err or out or "").strip() or f"exit {proc.returncode}"
                    emit(
                        error_payload(
                            "clapwake.action",
                            f"{command[0]}: {detail}",
                            f"{action}_command_failed",
                        )
                    )

            threading.Thread(target=reap, name=f"clapwake-reap-{command[0]}", daemon=True).start()

        for command in commands:
            launch(command)
        emit(
            {
                "component": "clapwake.detector",
                "event": f"{action}_triggered",
                "clap_count": count,
            },
            stream=sys.stdout,
        )


def run() -> int:
    modules = import_audio_modules()
    if modules is None:
        return 1

    np, sd = modules
    selected = select_preferred_mic(sd)
    if selected is None:
        # Pinned mic absent. Pause, then exit so launchd relaunches us with a
        # fresh device list to re-check -- effectively polling for the mic to
        # come back, while staying loudly deaf instead of grabbing a dead mic.
        time.sleep(DEVICE_ABSENT_RETRY_SECONDS)
        return 1
    device_index, device_label = selected

    detector = ClapDetector()
    stopped = threading.Event()
    # Watchdog: last time the mic delivered any nonzero audio. A live analog
    # interface always has a small noise floor; sustained exact-zero (or the
    # callback going silent) means the device was removed -> restart to re-pin.
    audio_seen = {"at": time.monotonic()}

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    def callback(indata: Any, _frames: int, _time_info: Any, status: Any) -> None:
        if status:
            emit(
                error_payload(
                    "clapwake.stream",
                    str(status),
                    "audio_stream_status",
                )
            )
        try:
            peak = float(np.max(np.abs(indata)))
            if peak > 0.0:
                audio_seen["at"] = time.monotonic()
            detector.observe_peak(peak)
        except Exception as exc:  # noqa: BLE001
            emit(
                error_payload(
                    "clapwake.detector",
                    f"{type(exc).__name__}: {exc}",
                    "sample_processing_failed",
                )
            )

    try:
        with sd.InputStream(
            device=device_index,
            channels=1,
            callback=callback,
            dtype="float32",
            blocksize=STREAM_BLOCKSIZE,
            latency=STREAM_LATENCY,
        ):
            emit(
                {
                    "component": "clapwake.stream",
                    "event": "listening",
                    "device_index": device_index,
                    "device_name": device_label,
                    "threshold": THRESH,
                    "wake_commit": WAKE_COMMIT_SECONDS,
                    "poll_seconds": POLL_SECONDS,
                    "stream_blocksize": STREAM_BLOCKSIZE,
                    "stream_latency": STREAM_LATENCY,
                    "sleep_cooldown": SLEEP_COOLDOWN_SECONDS,
                },
                stream=sys.stdout,
            )
            # Poll fast so a committed burst fires promptly (wake latency is at
            # most one tick past WAKE_COMMIT). Dead-stream watchdog shares loop.
            while not stopped.wait(POLL_SECONDS):
                now = time.monotonic()
                detector.settle(now)
                if now - audio_seen["at"] > DEAD_STREAM_SECONDS:
                    emit(
                        error_payload(
                            "clapwake.stream",
                            f"no audio from {device_label!r} for "
                            f"{DEAD_STREAM_SECONDS:.0f}s -- device removed?",
                            "audio_stream_dead",
                        )
                    )
                    break  # exit -> launchd restarts -> re-pin to the mic
    except Exception as exc:  # noqa: BLE001
        emit(
            error_payload(
                "clapwake.stream",
                f"{type(exc).__name__}: {exc}",
                "audio_stream_failed",
            )
        )
        return 1

    emit({"component": "clapwake.stream", "event": "stopped"}, stream=sys.stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
