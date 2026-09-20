#!/usr/bin/env python3
"""Toggle a monitor with a clap pair and verify its brightness through BetterDisplay."""

from __future__ import annotations

import ctypes
import json
import os
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from ctypes import (
    POINTER,
    byref,
    c_int32,
    c_uint32,
    c_void_p,
)
from typing import Any

import hostwatch

# Keep the onset threshold above desk noise and the release level below clap tails.
# Quieter snaps may need microphone gain adjustment; measure with scope.py first.
THRESH = 0.35                # onset level: a clap must peak at least this loud
RELEASE = 0.08               # re-arm only after a real quiet (must stay << THRESH)
# One pair window for both actions. Panel state, not clap speed, chooses the action.
# Historical count- and tempo-based gestures are documented in PROCESS.md.
WAKE_GAP_RANGE_SECONDS = (
    float(os.environ.get("CLAPWAKE_WAKE_MIN_GAP", "0.12")),
    float(os.environ.get("CLAPWAKE_WAKE_MAX_GAP", "1.20")),
)
# Both actions use the same pair window.
SLEEP_GAP_RANGE_SECONDS = WAKE_GAP_RANGE_SECONDS
if not (
    0.12 <= WAKE_GAP_RANGE_SECONDS[0] < WAKE_GAP_RANGE_SECONDS[1] <= 1.50
):
    raise ValueError(
        "clap pair band must satisfy 0.12 <= min < max <= 1.50"
    )
# One pair window: no dead zone between wake and sleep.
BANDS_ARE_CONTIGUOUS = True
# Band edges are inclusive within this tolerance. It absorbs float drift and the
# POLL/blocksize quantisation, and it is far below human timing precision — no
# one claps to a 5ms boundary, so a hard edge would only create phantom misses.
GAP_EPSILON_SECONDS = 0.005
# Below the pair band an onset is an echo of clap 1, not intent: ignore it but
# keep waiting, so an echo cannot eat the real second clap.
ECHO_GAP_SECONDS = WAKE_GAP_RANGE_SECONDS[0] - GAP_EPSILON_SECONDS
# Past the pair band the two claps are unrelated; the later one starts a pair.
PAIR_EXPIRY_SECONDS = WAKE_GAP_RANGE_SECONDS[1] + GAP_EPSILON_SECONDS
MAX_CONTINUOUS_LOUD_SECONDS = 0.22
MIN_REARM_QUIET_SECONDS = 0.07
# Busy gate: continuous audio (recording) blocks all counting.
ACTIVITY_WINDOW_SECONDS = 1.5
MAX_BUSY_FRACTION = 0.25
MIN_BUSY_SAMPLES = 30
WAKE_COOLDOWN_SECONDS = 2.5
SLEEP_COOLDOWN_SECONDS = 0.4
# Sleep only fires if no real key/mouse event happened this recently. A person
# typing is not clapping; the mic just hears their keyboard through the desk.
# A second pair inside this window is intent (11:43:01 then 11:43:02).
SLEEP_REQUIRES_HID_IDLE_SECONDS = float(
    os.environ.get("CLAPWAKE_SLEEP_HID_IDLE", "3.0")
)
# CGEvent idle and our monotonic stamp can disagree by a beat. Stay under a
# keystroke gap so a real key after our F18 still counts as human.
OWN_HID_MATCH_SLACK_SECONDS = 0.35
ACTION_COOLDOWN_SECONDS = WAKE_COOLDOWN_SECONDS

# Both gestures are two claps. The count is no longer what distinguishes them.
CLAPS_PER_GESTURE = 2

# Deep DisplayPort idle may need a real HID key as well as brightness commands.
# Keep the sparse wake pulses; never move the cursor or request OS display sleep.
BETTERDISPLAY_BIN = "/Applications/BetterDisplay.app/Contents/MacOS/BetterDisplay"
DISPLAY_NAME = "LS32CG51x"
HID_WAKE_BIN = os.path.join(os.path.dirname(__file__), ".venv", "bin", "clapwake-hid")
WAKE_HID_CMD = [HID_WAKE_BIN]
WAKE_CAFFEINATE_SECONDS = 30
WAKE_CAFFEINATE_CMD = ["caffeinate", "-u", "-t", str(WAKE_CAFFEINATE_SECONDS)]
# BetterDisplay software brightness + DDC luminance. hardwareBacklight=off
# reports off on LS32CG51x while luminance stays 100 and the panel stays lit.
# HID still runs on wake for deep DPMS. OS display stays on (no displaysleepnow).
WAKE_DDC_CMD = [
    BETTERDISPLAY_BIN,
    "set",
    f"--name={DISPLAY_NAME}",
    "--brightness=1",
    "--ddc",
    "--vcp=luminance",
    "--value=100",
]
SLEEP_DDC_CMD = [
    BETTERDISPLAY_BIN,
    "set",
    f"--name={DISPLAY_NAME}",
    "--brightness=0",
    "--ddc",
    "--vcp=luminance",
    "--value=0",
]
# Single key-down/up per offset (F18 — no glyph). Retries only for cold panel.
WAKE_KEY_CLICK_OFFSETS_SECONDS = (0.0, 0.8, 2.0, 4.0)
# F18 must match kWakeKeyCode in hidwake.c. Unlike Fn, it is a discrete key.
# The regression test checks both implementations use keycode79.
WAKE_KEY_CODE = 0x4F

WAKE_PULSE_OFFSETS_SECONDS = WAKE_KEY_CLICK_OFFSETS_SECONDS  # back-compat name
WAKE_COMMANDS = [WAKE_CAFFEINATE_CMD, WAKE_DDC_CMD]  # back-compat for tests/docs
# DDC luminance 0 keeps the DisplayPort link and the session unlocked.
# pmset displaysleepnow is clamshell sleep + immediate lock (PROCESS part 12).
SLEEP_COMMANDS = [SLEEP_DDC_CMD]

# Command success is not panel confirmation. Read both channels after an action.
# Retry a bounded number of times and report unverified outcomes.
PANEL_DARK_MAX = 0.05               # software brightness at/below = dark
PANEL_LUM_DARK_MAX = 5.0            # DDC luminance at/below = dark
PANEL_LIT_MIN = 0.95                # software brightness at/above = lit
PANEL_LUM_LIT_MIN = 95.0            # DDC luminance at/above = lit
PANEL_VERIFY_DELAY_SECONDS = 0.7    # DDC needs a beat before a get sees it
PANEL_VERIFY_ATTEMPTS = 4           # extra set fires while unverified

# CoreGraphics / IOKit paths (macOS frameworks; no pyobjc required).
_CG_PATH = "/System/Library/Frameworks/CoreGraphics.framework/CoreGraphics"
_CF_PATH = "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation"
_IOKIT_PATH = "/System/Library/Frameworks/IOKit.framework/IOKit"
_AS_PATH = (
    "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices"
)
_kCGHIDEventTap = 0
_kCGSessionEventTap = 1
# NULL source was the bug: events post but WindowServer does not treat them like
# real keyboard HID, so the panel stays in deep DPMS (hid:true, still black).
_kCGEventSourceStateHIDSystemState = 1
_kCFStringEncodingUTF8 = 0x08000100
_kIOPMUserActiveLocal = 0

# Lazy-loaded framework handles (set once per process).
_fw: dict[str, Any] = {}


def _frameworks() -> dict[str, Any] | None:
    if _fw:
        return _fw
    try:
        cg = ctypes.CDLL(_CG_PATH)
        cf = ctypes.CDLL(_CF_PATH)
        iokit = ctypes.CDLL(_IOKIT_PATH)

        cf.CFStringCreateWithCString.restype = c_void_p
        cf.CFStringCreateWithCString.argtypes = [c_void_p, ctypes.c_char_p, c_uint32]
        cf.CFRelease.argtypes = [c_void_p]

        iokit.IOPMAssertionDeclareUserActivity.restype = c_int32
        iokit.IOPMAssertionDeclareUserActivity.argtypes = [
            c_void_p,
            c_int32,
            POINTER(c_uint32),
        ]

        cg.CGEventSourceCreate.restype = c_void_p
        cg.CGEventSourceCreate.argtypes = [c_int32]
        cg.CGEventPost.argtypes = [c_uint32, c_void_p]
        cg.CGEventCreateKeyboardEvent.restype = c_void_p
        cg.CGEventCreateKeyboardEvent.argtypes = [c_void_p, c_uint32, ctypes.c_bool]

        cg.CGEventSourceSecondsSinceLastEventType.restype = ctypes.c_double
        cg.CGEventSourceSecondsSinceLastEventType.argtypes = [c_int32, c_uint32]
        cg.CGSessionCopyCurrentDictionary.restype = c_void_p
        cf.CFDictionaryGetValue.restype = c_void_p
        cf.CFDictionaryGetValue.argtypes = [c_void_p, c_void_p]

        _fw.update({"cg": cg, "cf": cf, "iokit": iokit})
        return _fw
    except Exception as exc:  # noqa: BLE001
        emit_error(
            "clapwake.action",
            f"framework load: {type(exc).__name__}: {exc}",
            "wake_framework_failed",
            throttle=False,
        )
        return None


def ax_trusted() -> bool | None:
    """True/False if Accessibility trust is readable; None if API missing."""
    try:
        lib = ctypes.CDLL(_AS_PATH)
        lib.AXIsProcessTrusted.restype = ctypes.c_bool
        return bool(lib.AXIsProcessTrusted())
    except Exception:
        return None


def declare_user_activity(reason: str = "clapwake") -> bool:
    """In-process IOPM user-activity (same family as `caffeinate -u`)."""
    fw = _frameworks()
    if fw is None:
        return False
    cf, iokit = fw["cf"], fw["iokit"]
    try:
        name = cf.CFStringCreateWithCString(
            None, reason.encode("utf-8"), _kCFStringEncodingUTF8
        )
        if not name:
            return False
        assertion_id = c_uint32(0)
        try:
            rc = iokit.IOPMAssertionDeclareUserActivity(
                name, _kIOPMUserActiveLocal, byref(assertion_id)
            )
            return rc == 0
        finally:
            cf.CFRelease(name)
    except Exception as exc:  # noqa: BLE001
        emit_error(
            "clapwake.action",
            f"IOPM user activity: {type(exc).__name__}: {exc}",
            "wake_iopm_failed",
            throttle=True,
        )
        return False


def keyboard_click(keycode: int = WAKE_KEY_CODE) -> bool:
    """One key down+up — the hybrid 'keyboard click equivalent' for 2 claps.

    Uses CGEventSourceCreate(kCGEventSourceStateHIDSystemState) so WindowServer
    treats it like a real HID key (Aula path), not a NULL-source synthetic.
    Never moves the mouse. Single inert key (default F18) — no typed character.
    """
    fw = _frameworks()
    if fw is None:
        return False
    cg, cf = fw["cg"], fw["cf"]
    source = cg.CGEventSourceCreate(_kCGEventSourceStateHIDSystemState)
    if not source:
        return False
    try:
        posted = False
        for key_down in (True, False):
            kev = cg.CGEventCreateKeyboardEvent(source, keycode, key_down)
            if not kev:
                continue
            # HID tap only. Injecting here enters below the session, so the
            # event already propagates up to it — posting the same event again
            # at the session tap delivered every key TWICE to the focused app.
            cg.CGEventPost(_kCGHIDEventTap, kev)
            cf.CFRelease(kev)
            posted = True
        return posted
    except Exception as exc:  # noqa: BLE001
        emit_error(
            "clapwake.action",
            f"keyboard_click: {type(exc).__name__}: {exc}",
            "wake_key_failed",
            throttle=True,
        )
        return False
    finally:
        cf.CFRelease(source)


# Back-compat alias used by older notes/tests.
def hid_tickle() -> bool:
    return keyboard_click()


_kCGAnyInputEventType = 0xFFFFFFFF


def _bd_get(*args: str) -> float | None:
    """One BetterDisplay get parsed as a float; None on any failure."""
    try:
        proc = subprocess.run(
            [BETTERDISPLAY_BIN, "get", f"--name={DISPLAY_NAME}", *args],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
    except Exception:  # noqa: BLE001
        return None
    if proc.returncode != 0:
        return None
    try:
        return float((proc.stdout or "").strip())
    except ValueError:
        return None


def panel_levels() -> tuple[float | None, float | None]:
    """(software brightness 0..1, DDC luminance 0..100); None per channel."""
    return _bd_get("--brightness"), _bd_get("--ddc", "--vcp=luminance")


def levels_are_dark(
    brightness: float | None, luminance: float | None
) -> bool:
    """Dark if either channel is near zero; both unreadable biases toward wake."""
    if brightness is not None and brightness <= PANEL_DARK_MAX:
        return True
    if luminance is not None and luminance <= PANEL_LUM_DARK_MAX:
        return True
    return brightness is None and luminance is None


def levels_at_target(
    brightness: float | None, luminance: float | None, dark: bool
) -> bool:
    """True only when BOTH channels read back at the target state."""
    if brightness is None or luminance is None:
        return False
    if dark:
        return (
            brightness <= PANEL_DARK_MAX and luminance <= PANEL_LUM_DARK_MAX
        )
    return brightness >= PANEL_LIT_MIN and luminance >= PANEL_LUM_LIT_MIN


def panel_is_dark() -> bool:
    """True when the panel reads dark (or is unreadable -> wake bias).

    NEVER call from the PortAudio callback: this shells out.
    """
    return levels_are_dark(*panel_levels())


def hid_idle_seconds() -> float:
    """Seconds since the last real human key/mouse event, or -1.0 if unknown.

    NEVER call this from the PortAudio callback thread: it IPCs to WindowServer.
    """
    fw = _frameworks()
    if fw is None:
        return -1.0
    try:
        return float(
            fw["cg"].CGEventSourceSecondsSinceLastEventType(
                _kCGEventSourceStateHIDSystemState, _kCGAnyInputEventType
            )
        )
    except Exception:  # noqa: BLE001
        return -1.0


def screen_is_locked() -> bool:
    """True when the login window owns the screen.

    CGSSessionScreenIsLocked is absent from the session dict while unlocked and
    present while locked, so presence alone is the answer. Returns False when
    the state cannot be read — an unknown lock state must not block a wake.
    """
    fw = _frameworks()
    if fw is None:
        return False
    cg, cf = fw["cg"], fw["cf"]
    session = None
    key = None
    try:
        session = cg.CGSessionCopyCurrentDictionary()
        if not session:
            return False
        key = cf.CFStringCreateWithCString(
            None, b"CGSSessionScreenIsLocked", _kCFStringEncodingUTF8
        )
        return bool(cf.CFDictionaryGetValue(session, key))
    except Exception:  # noqa: BLE001
        return False
    finally:
        if key:
            cf.CFRelease(key)
        if session:
            cf.CFRelease(session)


def hybrid_wake_once() -> dict[str, bool]:
    """2-clap action: keyboard click + IOPM user activity. No cursor motion."""
    return {
        "key": keyboard_click(),
        "iopm": declare_user_activity("clapwake"),
    }

# Pin the listener to a specific mic by NAME (substring match, case-insensitive)
# rather than following the system default. The built-in mic is hardware-disabled
# in clamshell mode (lid closed), so the default silently falling back to it left
# the listener deaf. Name-pinning also survives device-index reshuffling when USB
# devices are plugged/unplugged. Override with the CLAPWAKE_MIC_NAME env var.
PREFERRED_MIC_NAME = os.environ.get("CLAPWAKE_MIC_NAME", "Scarlett")
# Stay in-process and re-poll; do not exit/relaunch on every miss (that produced
# ~10k launchd runs and multi-MB preferred_mic_absent spam).
DEVICE_ABSENT_RETRY_SECONDS = 15.0
ERROR_THROTTLE_SECONDS = 60.0       # same failure_type at most once per minute
DEAD_STREAM_SECONDS = 5.0           # restart stream if the mic delivers no audio
RECONNECT_PAUSE_SECONDS = 2.0       # base pause after a clean dead-stream close
RECONNECT_FAIL_BASE_SECONDS = 2.0   # first open-failure backoff (no 1Hz thrash)
RECONNECT_BACKOFF_MAX_SECONDS = 30.0
PROCESS_RESTART_AFTER_STREAM_FAILURES = 3
LISTEN_HEARTBEAT_SECONDS = 60.0     # prove we're still alive while listening
POLL_SECONDS = 0.005                # main-loop tick: how often settle() is checked
STREAM_BLOCKSIZE = 128              # preferred small buffer for detection lag

# Rate-limit repeated structured errors (keyed by failure_type).
_last_error_emit_at: dict[str, float] = {}


def emit(payload: dict[str, Any], *, stream: Any = sys.stderr) -> None:
    # Wall-clock stamp so wake→sleep gaps in /tmp/clapwake.out are measurable.
    stamped = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **payload}
    print(json.dumps(stamped, sort_keys=True), file=stream, flush=True)


def emit_error(
    component: str,
    root_cause: str,
    failure_type: str,
    *,
    throttle: bool = True,
    stream: Any = sys.stderr,
) -> None:
    """Emit a structured error, optionally throttled by failure_type."""
    if throttle:
        now = time.monotonic()
        last = _last_error_emit_at.get(failure_type, 0.0)
        if now - last < ERROR_THROTTLE_SECONDS:
            return
        _last_error_emit_at[failure_type] = now
    emit(error_payload(component, root_cause, failure_type), stream=stream)


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
        emit_error(
            "clapwake.dependencies",
            f"{type(exc).__name__}: {exc}",
            "dependency_import_failed",
            throttle=False,
        )
        return None


def device_name(device: dict[str, Any]) -> str:
    return str(device.get("name", ""))


def input_channels(device: dict[str, Any]) -> int:
    try:
        return int(device.get("max_input_channels", 0))
    except Exception:
        return 0


def select_preferred_mic(
    sd: Any, *, emit_missing: bool = True
) -> tuple[int, str, int] | None:
    """Return (index, name, max_input_channels) for the pinned mic, or None.

    Deliberately does NOT fall back to any other device: if the pinned mic is
    absent we want to fail loudly, not silently latch onto the built-in mic.
    Re-query every call so USB re-enumeration cannot leave a stale index.
    The listen loop's host watch passes emit_missing=False so a 2s poll cannot
    spam preferred_mic_absent while the stream is still open.
    """
    try:
        devices = list(sd.query_devices())
    except Exception as exc:  # noqa: BLE001
        emit_error(
            "clapwake.device",
            f"{type(exc).__name__}: {exc}",
            "device_query_failed",
        )
        return None

    target = PREFERRED_MIC_NAME.lower()
    available: list[str] = []
    for index, device in enumerate(devices):
        ch = input_channels(device)
        if ch <= 0:
            continue
        name = device_name(device)
        available.append(f"{index}:{name}")
        if target in name.lower():
            return index, name, ch

    if emit_missing:
        emit_error(
            "clapwake.device",
            f"pinned mic {PREFERRED_MIC_NAME!r} not found; available inputs: "
            + (", ".join(available) or "none"),
            "preferred_mic_absent",
        )
    return None


def refresh_device_list(sd: Any) -> bool:
    """Rebuild PortAudio's cached device enumeration. True when it succeeded.

    sd.query_devices() serves a list captured when PortAudio initialized. A mic
    unplugged overnight and replugged in the morning therefore never reappears
    in a long-lived process: the listener stays deaf for hours with the device
    sitting on the USB bus, and only a manual restart fixes it. terminate +
    initialize rebuilds the enumeration in place, so no relaunch is needed.

    Call ONLY while no stream is open (the mic-absent path). Tearing the host
    API down under a live callback is the crash run() warns about.
    """
    try:
        sd._terminate()
        sd._initialize()
        return True
    except Exception as exc:  # noqa: BLE001
        emit_error(
            "clapwake.device",
            f"portaudio re-enumeration: {type(exc).__name__}: {exc}",
            "device_refresh_failed",
        )
        return False


def reconnect_backoff_seconds(fail_streak: int) -> float:
    """Exponential backoff for open failures. fail_streak is 1-based."""
    if fail_streak <= 0:
        return RECONNECT_PAUSE_SECONDS
    exp = min(fail_streak - 1, 5)
    return min(RECONNECT_BACKOFF_MAX_SECONDS, RECONNECT_FAIL_BASE_SECONDS * (2**exp))


def stream_open_configs(max_input_channels: int) -> list[dict[str, Any]]:
    """Ordered open attempts: prefer low-latency mono, then fall back hard.

    Permanent anti-deaf matrix. Never rely on a single (channels, latency, rate,
    blocksize) tuple — Scarlett/CoreAudio rejects combinations after sleep.
    """
    channels: list[int] = [1]
    if max_input_channels >= 2:
        channels.append(2)
    configs: list[dict[str, Any]] = []
    for ch in channels:
        for latency in ("low", "high", None):
            for rate in (None, 44100, 48000):
                for blocksize in (STREAM_BLOCKSIZE, 256, 512):
                    cfg: dict[str, Any] = {
                        "channels": ch,
                        "blocksize": blocksize,
                        "dtype": "float32",
                    }
                    if latency is not None:
                        cfg["latency"] = latency
                    if rate is not None:
                        cfg["samplerate"] = rate
                    configs.append(cfg)
    # Deduplicate while preserving order.
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for cfg in configs:
        key = repr(sorted(cfg.items()))
        if key in seen:
            continue
        seen.add(key)
        out.append(cfg)
    return out


class ClapDetector:
    def __init__(self) -> None:
        # observe_peak runs on the PortAudio callback thread; settle runs on the
        # main loop thread. The lock guards the shared burst state between them.
        self._lock = threading.Lock()
        self.last_clap_at = 0.0
        self.burst_count = 0
        # Hysteresis gate: True once the level has fallen below RELEASE long
        # enough (MIN_REARM_QUIET_SECONDS). Starts armed so the first clap works.
        self._armed = True
        # When continuous quiet (peak < RELEASE) began; None while loud/mid.
        self._quiet_since: float | None = None
        # Once a threshold-level onset starts, keep timing it until the signal
        # falls below RELEASE. This catches a held vocal even if its level
        # briefly dips below THRESH without becoming quiet enough to re-arm.
        self._loud_started_at: float | None = None
        # Monotonic deadline: while now < _cooldown_until, drop onsets.
        self._cooldown_until = 0.0
        # Bump to cancel in-flight cold-wake pulse trains (sleep or newer wake).
        self._wake_generation = 0
        self._wake_gen_lock = threading.Lock()
        # Recent (timestamp, is_active) samples for the busy-environment gate.
        self._activity: deque[tuple[float, bool]] = deque()
        # Last time this process posted HID (wake train). Sleep must not treat
        # that as a human at the keyboard (09:48:59 sleep_suppressed after wake).
        self._own_hid_mono = 0.0
        # Last HID-gated sleep. A second pair inside the idle window confirms.
        self._hid_suppressed_at = 0.0

    def _bump_wake_generation(self) -> int:
        """Invalidate prior cold-wake trains; return the new generation id."""
        with self._wake_gen_lock:
            self._wake_generation += 1
            return self._wake_generation

    def _wake_generation_alive(self, gen: int) -> bool:
        with self._wake_gen_lock:
            return gen == self._wake_generation

    def _record_activity(self, peak: float, now: float) -> None:
        """Track how often recent samples sit above RELEASE (active room sound)."""
        self._activity.append((now, peak >= RELEASE))
        cutoff = now - ACTIVITY_WINDOW_SECONDS
        while self._activity and self._activity[0][0] < cutoff:
            self._activity.popleft()

    def _is_busy(self) -> bool:
        """True when the recent window looks like continuous audio, not claps."""
        n = len(self._activity)
        if n < MIN_BUSY_SAMPLES:
            return False
        active = sum(1 for _, is_active in self._activity if is_active)
        return (active / n) >= MAX_BUSY_FRACTION

    def _clear_burst_disarmed(self) -> None:
        self.burst_count = 0
        self._armed = False
        self._loud_started_at = None

    def _update_quiet_gate(self, peak: float, now: float) -> None:
        """Track continuous quiet and re-arm only after MIN_REARM_QUIET_SECONDS."""
        if peak < RELEASE:
            if self._quiet_since is None:
                self._quiet_since = now
            if now - self._quiet_since >= MIN_REARM_QUIET_SECONDS:
                self._armed = True
                self._loud_started_at = None
            return
        # Mid/loud levels break the quiet streak; leave _armed as-is.
        self._quiet_since = None

    def observe_peak(self, peak: float, now: float | None = None) -> None:
        """Recognize a clap pair, then choose the action from panel brightness.

        - clap 1 → start a pair, do nothing
        - clap 2 in the pair band, panel dark → wake
        - clap 2 in the pair band, panel lit  → sleep
        - onset before the pair band → echo of clap 1; ignore, keep waiting
        - onset after the pair band → too late to pair; it becomes clap 1

        Wake and sleep use the same clap speed. Hysteresis + busy gate still
        apply (recording-safe).
        """
        now = time.monotonic() if now is None else now
        gap = 0.0
        with self._lock:
            self._record_activity(peak, now)

            if now < self._cooldown_until:
                self.burst_count = 0
                self._update_quiet_gate(peak, now)
                return

            # Busy room (singing/speech/music/recording): refuse counting.
            if self._is_busy():
                self._clear_burst_disarmed()
                self._update_quiet_gate(peak, now)
                if self._is_busy():
                    self._armed = False
                return

            if peak < RELEASE:
                self._update_quiet_gate(peak, now)
                return

            self._quiet_since = None

            if peak >= THRESH and self._loud_started_at is None:
                self._loud_started_at = now
            if (
                self._loud_started_at is not None
                and now - self._loud_started_at >= MAX_CONTINUOUS_LOUD_SECONDS
            ):
                self._clear_burst_disarmed()
                return
            if not self._armed or peak < THRESH:
                return

            # This is clap 1 of a pair: start the pair and wait for its partner.
            if self.burst_count == 0:
                self._armed = False
                self.last_clap_at = now
                self.burst_count = 1
                return

            gap = now - self.last_clap_at

            # Too soon to be intent — this is clap 1 ringing. Ignore it WITHOUT
            # touching the pair, so an echo cannot consume the real clap 2.
            if gap < ECHO_GAP_SECONDS:
                return

            # Too late to be a pair. This onset is itself a fresh clap 1.
            if gap > PAIR_EXPIRY_SECONDS:
                self._armed = False
                self.last_clap_at = now
                self.burst_count = 1
                return

            self._armed = False
            self.burst_count = 0
            # Hold a short cooldown on the callback so clap-2's echo cannot
            # start a second pair before the worker reads panel state.
            self._cooldown_until = now + SLEEP_COOLDOWN_SECONDS

        # Same clap speed for on and off. Panel brightness chooses the action.
        # Tests replace _run with a recorder and stub panel_is_dark; keep that
        # path synchronous. Production shells out, so it must not run here.
        if getattr(self._run, "__func__", None) is ClapDetector._run:
            threading.Thread(
                target=self._dispatch_pair,
                args=(now, CLAPS_PER_GESTURE, gap),
                name="clapwake-pair",
                daemon=True,
            ).start()
        else:
            self._dispatch_pair(now, CLAPS_PER_GESTURE, gap)

    def _dispatch_pair(self, t0: float, count: int, gap: float) -> None:
        dark = panel_is_dark()
        action = "wake" if dark else "sleep"
        with self._lock:
            self._cooldown_until = max(
                self._cooldown_until, t0 + SLEEP_COOLDOWN_SECONDS
            )
        commands = WAKE_COMMANDS if action == "wake" else SLEEP_COMMANDS
        self._run(commands, action, count, gap=gap)

    def settle(self, now: float | None = None) -> None:
        """Abandon a lone clap once its partner can no longer arrive.

        Both gestures fire inside observe_peak, so settle no longer commits any
        action. It only expires a half-finished pair.
        """
        now = time.monotonic() if now is None else now
        with self._lock:
            if now < self._cooldown_until:
                return
            if self.burst_count and now - self.last_clap_at > PAIR_EXPIRY_SECONDS:
                self.burst_count = 0

    def _run(
        self,
        commands: list[list[str]],
        action: str,
        count: int,
        gap: float = 0.0,
    ) -> None:
        """Launch wake or gated dimming on daemon workers."""
        t0 = time.monotonic()
        if action == "wake":
            gen = self._bump_wake_generation()
            emit(
                {
                    "component": "clapwake.detector",
                    "event": "wake_triggered",
                    "clap_count": count,
                    "gap_s": round(gap, 3),
                    "t0_mono": round(t0, 3),
                    "mode": "hybrid_key_click",
                    "key_code": WAKE_KEY_CODE,
                    "retry_offsets": list(WAKE_KEY_CLICK_OFFSETS_SECONDS),
                    "wake_gen": gen,
                    "ax_trusted": ax_trusted(),
                },
                stream=sys.stdout,
            )
            threading.Thread(
                target=self._hybrid_wake,
                args=(t0, count, gen),
                name="clapwake-hybrid-wake",
                daemon=True,
            ).start()
            return

        # Check input activity off the audio callback. Only a permitted dim action
        # cancels wake retries or emits sleep_triggered.
        if action == "sleep":
            threading.Thread(
                target=self._sleep_if_user_absent,
                args=(commands, t0, count, gap),
                name="clapwake-sleep-gate",
                daemon=True,
            ).start()
            return

        emit(
            {
                "component": "clapwake.detector",
                "event": f"{action}_triggered",
                "clap_count": count,
                "t0_mono": round(t0, 3),
            },
            stream=sys.stdout,
        )
        threading.Thread(
            target=self._launch_commands,
            args=(commands, action, t0, 0),
            name=f"clapwake-{action}",
            daemon=True,
        ).start()

    def _sleep_if_user_absent(
        self,
        commands: list[list[str]],
        t0: float,
        count: int,
        gap: float,
    ) -> None:
        """Dim when unlocked. First recent-HID pair is dropped; the next confirms.

        Desk-mounted microphones can hear keystrokes as claps. Ignore the wake
        helper's own HID events. A second pair inside the idle window is intent.
        """
        idle = hid_idle_seconds()
        locked = screen_is_locked()
        now = time.monotonic()
        # idle < 0 means the query failed; do not suppress on an unknown answer.
        recent_input = 0.0 <= idle < SLEEP_REQUIRES_HID_IDLE_SECONDS
        hid_confirm = False
        if recent_input and self._own_hid_mono > 0:
            own_age = now - self._own_hid_mono
            # Last HID is not newer than our wake post → it is our F18, not Ben.
            if idle + OWN_HID_MATCH_SLACK_SECONDS >= own_age:
                recent_input = False
        if (
            recent_input
            and self._hid_suppressed_at > 0
            and now - self._hid_suppressed_at <= SLEEP_REQUIRES_HID_IDLE_SECONDS
        ):
            recent_input = False
            hid_confirm = True
        if locked or recent_input:
            if recent_input:
                self._hid_suppressed_at = now
            emit(
                {
                    "component": "clapwake.detector",
                    "event": "sleep_suppressed",
                    "reason": "screen_locked" if locked else "recent_hid_input",
                    "gap_s": round(gap, 3),
                    "hid_idle_s": round(idle, 2),
                    "required_idle_s": SLEEP_REQUIRES_HID_IDLE_SECONDS,
                    "screen_locked": locked,
                    "own_hid_s": round(now - self._own_hid_mono, 2)
                    if self._own_hid_mono
                    else None,
                },
                stream=sys.stdout,
            )
            return

        # Gate passed: only now cancel residual wake retries and act.
        self._hid_suppressed_at = 0.0
        cancelled = self._bump_wake_generation()
        emit(
            {
                "component": "clapwake.detector",
                "event": "sleep_triggered",
                "clap_count": count,
                "gap_s": round(gap, 3),
                "t0_mono": round(t0, 3),
                "hid_idle_s": round(idle, 2),
                "hid_confirm": hid_confirm,
                "cancelled_wake_gen": cancelled,
            },
            stream=sys.stdout,
        )
        self._launch_commands(commands, "sleep", t0, 0)
        self._verify_panel(
            action="sleep", dark=True, gen=cancelled, t0=t0, count=count,
            command=SLEEP_DDC_CMD,
        )

    def _hybrid_wake(self, t0: float, count: int, gen: int) -> None:
        """2 claps → keyboard-click equivalent (+ few retries for deep DPMS).

        Primary path is one real HID key down/up (Aula-class). CLI backups run
        on the first tick only; later ticks re-click the key and re-DDC.
        """
        for pulse_i, offset in enumerate(WAKE_KEY_CLICK_OFFSETS_SECONDS):
            if not self._wake_generation_alive(gen):
                emit(
                    {
                        "component": "clapwake.action",
                        "event": "wake_pulse_cancelled",
                        "clap_count": count,
                        "wake_gen": gen,
                        "at_pulse": pulse_i,
                        "dt_ms": int(round((time.monotonic() - t0) * 1000)),
                    },
                    stream=sys.stdout,
                )
                return
            delay = (t0 + offset) - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            if not self._wake_generation_alive(gen):
                emit(
                    {
                        "component": "clapwake.action",
                        "event": "wake_pulse_cancelled",
                        "clap_count": count,
                        "wake_gen": gen,
                        "at_pulse": pulse_i,
                        "dt_ms": int(round((time.monotonic() - t0) * 1000)),
                    },
                    stream=sys.stdout,
                )
                return

            launched_at = time.monotonic()
            native = hybrid_wake_once()
            self._own_hid_mono = launched_at
            # Every pulse: IOHID + BetterDisplay DDC. caffeinate only on pulse 0.
            commands: list[list[str]] = [WAKE_HID_CMD, WAKE_DDC_CMD]
            if pulse_i == 0:
                commands.insert(1, WAKE_CAFFEINATE_CMD)
            self._launch_commands(commands, "wake", t0, pulse_i)
            emit(
                {
                    "component": "clapwake.action",
                    "event": "wake_key_click",
                    "clap_count": count,
                    "pulse": pulse_i,
                    "offset_s": offset,
                    "dt_ms": int(round((launched_at - t0) * 1000)),
                    "wake_gen": gen,
                    "native": native,
                    "commands": [os.path.basename(c[0]) for c in commands],
                },
                stream=sys.stdout,
            )
        # The fixed pulse train covers deep DPMS; this covers the panel. If
        # every DDC set failed or landed late, keep re-firing until the read-
        # back agrees — the 09:36 all-pulses-Failed case.
        self._verify_panel(
            action="wake", dark=False, gen=gen, t0=t0, count=count,
            command=WAKE_DDC_CMD,
        )

    def _verify_panel(
        self,
        *,
        action: str,
        dark: bool,
        gen: int,
        t0: float,
        count: int,
        command: list[str],
    ) -> None:
        """Check both brightness channels and retry a mismatched target.

        Check generation at the start of each round. The final refire has no
        subsequent read-back; exhaustion reports the target as unverified.
        """
        for attempt in range(1, PANEL_VERIFY_ATTEMPTS + 1):
            if not self._wake_generation_alive(gen):
                return
            time.sleep(PANEL_VERIFY_DELAY_SECONDS)
            brightness, luminance = panel_levels()
            at_target = levels_at_target(brightness, luminance, dark)
            emit(
                {
                    "component": "clapwake.action",
                    "event": f"{action}_verify",
                    "clap_count": count,
                    "attempt": attempt,
                    "brightness": brightness,
                    "luminance": luminance,
                    "at_target": at_target,
                    "dt_ms": int(round((time.monotonic() - t0) * 1000)),
                },
                stream=sys.stdout,
            )
            if at_target:
                return
            self._launch_commands([command], action, t0, attempt)
        emit_error(
            "clapwake.action",
            f"panel never reached target after {PANEL_VERIFY_ATTEMPTS} checks",
            f"{action}_unverified",
            throttle=False,
        )

    def _launch_commands(
        self,
        commands: list[list[str]],
        action: str,
        t0: float,
        pulse: int,
    ) -> None:
        # Non-blocking launch; reap exit codes so silent CLI failures show in logs.
        for command in commands:
            self._launch_one(command, action, t0, pulse)

    def _launch_one(
        self,
        command: list[str],
        action: str,
        t0: float,
        pulse: int,
    ) -> None:
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
            emit_error(
                "clapwake.action",
                f"{command[0]}: {type(exc).__name__}: {exc}",
                f"{action}_command_failed",
                throttle=False,
            )
            return

        def reap() -> None:
            # caffeinate -u -t N is *supposed* to live for N seconds; killing it
            # early is a false failure and used to spam /tmp/clapwake.err.
            wait_s = 12.0
            basename = os.path.basename(command[0]) if command else ""
            if basename == "caffeinate":
                wait_s = float(WAKE_CAFFEINATE_SECONDS) + 5.0
                if "-t" in command:
                    try:
                        t_idx = command.index("-t")
                        wait_s = float(command[t_idx + 1]) + 5.0
                    except (ValueError, IndexError):
                        pass
            try:
                out, err = proc.communicate(timeout=wait_s)
            except subprocess.TimeoutExpired:
                if basename != "caffeinate":
                    proc.kill()
                    emit_error(
                        "clapwake.action",
                        f"{command[0]}: timed out after {wait_s:.0f}s (pulse={pulse})",
                        f"{action}_command_timeout",
                        throttle=False,
                    )
                return
            if proc.returncode not in (0, None):
                detail = (err or out or "").strip() or f"exit {proc.returncode}"
                emit_error(
                    "clapwake.action",
                    f"{command[0]}: {detail} (pulse={pulse}, dt_ms="
                    f"{int(round((time.monotonic() - t0) * 1000))})",
                    f"{action}_command_failed",
                    throttle=False,
                )

        threading.Thread(
            target=reap,
            name=f"clapwake-reap-{os.path.basename(command[0])}-p{pulse}",
            daemon=True,
        ).start()


def run() -> int:
    """Listen forever with self-healing capture — never stay deaf.

    Permanent guarantees:
    - Pin Scarlett by name; re-query index every reconnect (USB renumber-safe).
    - Open via a config matrix (mono/stereo, latency, rate, blocksize).
    - On open failure: exponential backoff (no 1Hz thrash), then a clean process
      restart. Never call PortAudio's private terminate API under a live callback.
    - While the mic is absent, re-enumerate PortAudio each retry so an overnight
      unplug/replug is picked up in-process (no launchd relaunch storm).
    - A stream is dead only when callbacks stop, not when the input is silent.
    - Heartbeat while listening so ops can see liveness in /tmp/clapwake.out.
    - Never exit on mic churn (launchd KeepAlive thrash is a non-goal).
    """
    modules = import_audio_modules()
    if modules is None:
        return 1

    np, sd = modules
    detector = ClapDetector()
    stopped = threading.Event()

    def stop(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)

    emit(
        {
            "component": "clapwake.stream",
            "event": "service_start",
            "preferred_mic": PREFERRED_MIC_NAME,
            "mode": "hybrid_key_click",
            "key_code": WAKE_KEY_CODE,
            "retry_offsets": list(WAKE_KEY_CLICK_OFFSETS_SECONDS),
            "gesture": "tempo_pair",
            "wake_band": list(WAKE_GAP_RANGE_SECONDS),
            "sleep_band": list(SLEEP_GAP_RANGE_SECONDS),
            "ax_trusted": ax_trusted(),
            "device_absent_retry": DEVICE_ABSENT_RETRY_SECONDS,
            "self_heal": True,
        },
        stream=sys.stdout,
    )

    reconnect_fail_streak = 0
    callback_stall_streak = 0

    while not stopped.is_set():
        selected = select_preferred_mic(sd)
        if selected is None:
            # The mic is absent from PortAudio's *cached* enumeration, which is
            # frozen at process start. After a nightly unplug the replugged mic
            # would otherwise stay invisible for the life of this process. No
            # stream is open here, so rebuild the list and re-check next tick.
            refreshed = refresh_device_list(sd)
            emit_error(
                "clapwake.device",
                f"pinned mic {PREFERRED_MIC_NAME!r} absent; portaudio "
                f"re-enumerated={refreshed}; retry in "
                f"{DEVICE_ABSENT_RETRY_SECONDS:.0f}s",
                "device_enumeration_refreshed",
            )
            stopped.wait(DEVICE_ABSENT_RETRY_SECONDS)
            continue

        device_index, device_label, max_ch = selected
        # Callback arrival is liveness. Signal level is diagnostic only: a quiet
        # Scarlett can legitimately deliver exact-zero buffers for minutes.
        callback_seen = {"at": time.monotonic()}
        signal_seen = {"at": time.monotonic()}
        stream_failed = False
        opened = False
        host_reason: str | None = None
        listen_started = 0.0
        last_heartbeat = 0.0

        def callback(
            indata: Any, _frames: int, _time_info: Any, status: Any
        ) -> None:
            if status:
                emit_error(
                    "clapwake.stream",
                    str(status),
                    "audio_stream_status",
                )
            try:
                callback_seen["at"] = time.monotonic()
                peak = float(np.max(np.abs(indata)))
                if peak > 0.0:
                    signal_seen["at"] = callback_seen["at"]
                detector.observe_peak(peak)
            except Exception as exc:  # noqa: BLE001
                emit_error(
                    "clapwake.detector",
                    f"{type(exc).__name__}: {exc}",
                    "sample_processing_failed",
                    throttle=False,
                )

        last_open_error: Exception | None = None
        used_cfg: dict[str, Any] | None = None
        for cfg in stream_open_configs(max_ch):
            kwargs = {
                "device": device_index,
                "callback": callback,
                **cfg,
            }
            try:
                with sd.InputStream(**kwargs):
                    opened = True
                    used_cfg = dict(cfg)
                    reconnect_fail_streak = 0
                    listen_started = time.monotonic()
                    last_heartbeat = listen_started
                    callback_seen["at"] = listen_started
                    signal_seen["at"] = listen_started
                    watch = hostwatch.HostWatch(device_index)
                    last_host_check = listen_started
                    emit(
                        {
                            "component": "clapwake.stream",
                            "event": "listening",
                            "device_index": device_index,
                            "device_name": device_label,
                            "threshold": THRESH,
                            "gesture": "tempo_pair",
                            "wake_band": list(WAKE_GAP_RANGE_SECONDS),
                            "sleep_band": list(SLEEP_GAP_RANGE_SECONDS),
                            "poll_seconds": POLL_SECONDS,
                            "stream_blocksize": cfg.get("blocksize"),
                            "stream_latency": cfg.get("latency", "default"),
                            "stream_channels": cfg.get("channels"),
                            "samplerate": cfg.get("samplerate", "device_default"),
                            "sleep_cooldown": SLEEP_COOLDOWN_SECONDS,
                            "mode": "hybrid_key_click",
                            "retry_offsets": list(WAKE_KEY_CLICK_OFFSETS_SECONDS),
                            "ax_trusted": ax_trusted(),
                            "self_heal": True,
                        },
                        stream=sys.stdout,
                    )
                    while not stopped.wait(POLL_SECONDS):
                        now = time.monotonic()
                        detector.settle(now)
                        if now - last_heartbeat >= LISTEN_HEARTBEAT_SECONDS:
                            last_heartbeat = now
                            callback_stall_streak = 0
                            emit(
                                {
                                    "component": "clapwake.stream",
                                    "event": "listening_heartbeat",
                                    "device_name": device_label,
                                    "uptime_s": round(now - listen_started, 1),
                                    "secs_since_callback": round(
                                        now - callback_seen["at"], 3
                                    ),
                                    "secs_since_signal": round(
                                        now - signal_seen["at"], 3
                                    ),
                                },
                                stream=sys.stdout,
                            )
                        if now - callback_seen["at"] > DEAD_STREAM_SECONDS:
                            emit_error(
                                "clapwake.stream",
                                f"no input callbacks from {device_label!r} for "
                                f"{DEAD_STREAM_SECONDS:.0f}s",
                                "audio_stream_dead",
                            )
                            break
                        if now - last_host_check >= hostwatch.HOST_WATCH_SECONDS:
                            last_host_check = now
                            host_reason = watch.poll(
                                select_preferred_mic(sd, emit_missing=False)
                            )
                            if host_reason:
                                emit(
                                    {
                                        "component": "clapwake.stream",
                                        "event": "host_reconnect",
                                        "reason": host_reason,
                                        "device_index": device_index,
                                        "device_name": device_label,
                                    },
                                    stream=sys.stdout,
                                )
                                break
                break  # left with-block (dead stream, stop, or clean close)
            except Exception as exc:  # noqa: BLE001
                last_open_error = exc
                continue

        if not opened:
            stream_failed = True
            reconnect_fail_streak += 1
            emit_error(
                "clapwake.stream",
                (
                    f"{type(last_open_error).__name__}: {last_open_error}"
                    if last_open_error
                    else "all open configs failed"
                )
                + f" (configs_tried={len(stream_open_configs(max_ch))})",
                "audio_stream_failed",
            )
            if reconnect_fail_streak >= PROCESS_RESTART_AFTER_STREAM_FAILURES:
                emit(
                    {
                        "component": "clapwake.stream",
                        "event": "service_restart_requested",
                        "failure_type": "audio_host_wedged",
                        "root_cause": "input stream failed repeatedly; restart at process boundary",
                        "fail_streak": reconnect_fail_streak,
                    },
                    stream=sys.stdout,
                )
                return 75

        stop_reason = (
            "stream_failed"
            if stream_failed
            else (host_reason or "dead_or_signal")
        )
        emit(
            {
                "component": "clapwake.stream",
                "event": "stopped",
                "device_name": device_label,
                "reason": stop_reason,
                "fail_streak": reconnect_fail_streak,
                "last_cfg": used_cfg,
            },
            stream=sys.stdout,
        )
        if stopped.is_set():
            break

        if host_reason == "power_source_changed":
            # AC unplug/replug resets USB audio. Process-boundary restart is
            # the recovery that actually unwedged CoreAudio on 2026-08-28.
            emit(
                {
                    "component": "clapwake.stream",
                    "event": "service_restart_requested",
                    "failure_type": "power_source_changed",
                    "root_cause": "AC/battery flipped; restart at process boundary",
                    "fail_streak": 0,
                },
                stream=sys.stdout,
            )
            return 75

        if host_reason:
            # Mic pulled or USB index churn: re-pin in-process. Not a stall.
            callback_stall_streak = 0
            pause = RECONNECT_PAUSE_SECONDS
        elif stream_failed:
            pause = reconnect_backoff_seconds(reconnect_fail_streak)
        else:
            # Clean dead-stream: re-pin quickly. Repeated callback loss is
            # handled at the process boundary, never through private C APIs.
            pause = RECONNECT_PAUSE_SECONDS
            callback_stall_streak += 1
            if callback_stall_streak >= PROCESS_RESTART_AFTER_STREAM_FAILURES:
                emit(
                    {
                        "component": "clapwake.stream",
                        "event": "service_restart_requested",
                        "failure_type": "audio_callbacks_stalled",
                        "root_cause": "input callbacks stopped repeatedly; restart at process boundary",
                        "fail_streak": callback_stall_streak,
                    },
                    stream=sys.stdout,
                )
                return 75
        emit(
            {
                "component": "clapwake.stream",
                "event": "reconnect_wait",
                "pause_s": pause,
                "fail_streak": reconnect_fail_streak,
                "stream_failed": stream_failed,
            },
            stream=sys.stdout,
        )
        stopped.wait(pause)

    emit({"component": "clapwake.stream", "event": "service_stop"}, stream=sys.stdout)
    return 0


def fire_hybrid_wake(*, source: str = "cli") -> None:
    """Run the same wake pulse train used by a clap pair.

    Blocks until the pulse train has been submitted. Used by `--wake` and by
    the Chrome Remote Desktop session watcher so those paths cannot drift from
    the clap fire path.
    """
    det = ClapDetector()
    t0 = time.monotonic()
    gen = det._bump_wake_generation()
    emit(
        {
            "component": "clapwake.detector",
            "event": "wake_triggered",
            "source": source,
            "t0_mono": round(t0, 3),
            "mode": "hybrid_key_click",
            "key_code": WAKE_KEY_CODE,
            "retry_offsets": list(WAKE_KEY_CLICK_OFFSETS_SECONDS),
            "wake_gen": gen,
            "ax_trusted": ax_trusted(),
        },
        stream=sys.stdout,
    )
    det._hybrid_wake(t0, 0, gen)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--wake":
        fire_hybrid_wake(source="cli")
        raise SystemExit(0)
    raise SystemExit(run())
