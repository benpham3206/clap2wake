"""Host events that should bounce the Scarlett stream.

Power-source flips (AC unplug) and USB index churn wedge CoreAudio while
PortAudio still reports a live device. Detect those on the main poll loop
and reconnect; do not wait for the 5s callback watchdog.
"""

from __future__ import annotations

import ctypes
import subprocess
from ctypes import POINTER, c_int, c_uint32, byref
from typing import Callable

HOST_WATCH_SECONDS = 2.0
_NOTIFY_NAME = b"com.apple.system.powersources"


def power_source() -> str | None:
    """Return 'AC Power' or 'Battery Power', or None if unknown."""
    try:
        out = subprocess.check_output(
            ["/usr/bin/pmset", "-g", "ps"],
            text=True,
            timeout=1.5,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    line = (out.splitlines() or [""])[0]
    if "Battery Power" in line:
        return "Battery Power"
    if "AC Power" in line:
        return "AC Power"
    return None


class NotifyFlag:
    """Poll a Darwin notify token. First check is consumed as a baseline."""

    def __init__(self, name: bytes = _NOTIFY_NAME) -> None:
        self._ok = False
        self._token = c_int(-1)
        try:
            lib = ctypes.CDLL("/usr/lib/system/libsystem_notify.dylib")
            lib.notify_register_check.argtypes = [ctypes.c_char_p, POINTER(c_int)]
            lib.notify_register_check.restype = c_uint32
            lib.notify_check.argtypes = [c_int, POINTER(c_int)]
            lib.notify_check.restype = c_uint32
            status = lib.notify_register_check(name, byref(self._token))
            self._lib = lib
            self._ok = status == 0
        except OSError:
            return
        if self._ok:
            self.check()

    def check(self) -> bool:
        if not self._ok:
            return False
        changed = c_int(0)
        self._lib.notify_check(self._token, byref(changed))
        return bool(changed.value)


class HostWatch:
    """Snapshot of power source + device index at listen-start."""

    def __init__(
        self,
        device_index: int,
        *,
        power_source_fn: Callable[[], str | None] = power_source,
        notify: NotifyFlag | None = None,
    ) -> None:
        self.device_index = device_index
        self._power_source_fn = power_source_fn
        self._power = power_source_fn()
        self._notify = notify if notify is not None else NotifyFlag()

    def poll(self, selected: tuple[int, str, int] | None) -> str | None:
        """Return a reconnect reason, or None if the host is unchanged."""
        if selected is None:
            return "mic_absent"
        idx = selected[0]
        if idx != self.device_index:
            return "device_index_changed"
        notify_fired = self._notify.check()
        if notify_fired or self._power is None:
            src = self._power_source_fn()
        else:
            src = self._power
        if src and self._power and src != self._power:
            self._power = src
            return "power_source_changed"
        if src:
            self._power = src
        return None
