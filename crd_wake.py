#!/usr/bin/env python3
"""Wake the Samsung when a Chrome Remote Desktop session starts.

The host process (`remoting_me2me_host`) stays up whenever remote access is
enabled. A live session is the pmset assertion named
"Remoting session is active". Rising edge of that assertion fires the same
hybrid wake train as a clap pair. 2026-08-24 diagnosed the gap (CRD
wakes macOS, not the panel) and never persisted a watcher; this is that
watcher.
"""

from __future__ import annotations

import argparse
import signal
import subprocess
import sys
import threading
import time
from typing import Any

import clapwake

POLL_SECONDS = 1.0
FIRE_COOLDOWN_SECONDS = 15.0
SESSION_MARK = 'NoDisplaySleepAssertion named: "Remoting session is active"'


def remoting_session_active(assertions: str) -> bool:
    return SESSION_MARK in assertions


def should_fire(
    *,
    was_active: bool,
    active: bool,
    now: float,
    last_fire: float,
    cooldown: float = FIRE_COOLDOWN_SECONDS,
) -> bool:
    """True only on a rising edge, and not inside the cooldown window."""
    if not active or was_active:
        return False
    if last_fire > 0.0 and (now - last_fire) < cooldown:
        return False
    return True


def read_assertions() -> str:
    proc = subprocess.run(
        ["/usr/bin/pmset", "-g", "assertions"],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError(
            f"pmset assertions failed rc={proc.returncode}: {proc.stderr.strip()}"
        )
    return proc.stdout


def emit_crd(event: str, **extra: Any) -> None:
    payload: dict[str, Any] = {
        "component": "clapwake.crd",
        "event": event,
        **extra,
    }
    clapwake.emit(payload, stream=sys.stdout)


def fire(*, source: str) -> None:
    emit_crd("crd_session_started", source=source)
    clapwake.fire_hybrid_wake(source="crd")


def run_loop() -> int:
    stopped = threading.Event()

    def handle(_signum: int, _frame: Any) -> None:
        stopped.set()

    signal.signal(signal.SIGTERM, handle)
    signal.signal(signal.SIGINT, handle)
    emit_crd("crd_watch_start", poll_s=POLL_SECONDS, cooldown_s=FIRE_COOLDOWN_SECONDS)
    was_active = False
    last_fire = 0.0
    while not stopped.is_set():
        try:
            active = remoting_session_active(read_assertions())
        except Exception as exc:  # noqa: BLE001
            clapwake.emit_error(
                "clapwake.crd",
                f"{type(exc).__name__}: {exc}",
                "crd_assertions_failed",
            )
            stopped.wait(POLL_SECONDS)
            continue
        now = time.monotonic()
        if should_fire(
            was_active=was_active,
            active=active,
            now=now,
            last_fire=last_fire,
        ):
            fire(source="rising_edge")
            last_fire = time.monotonic()
        was_active = active
        stopped.wait(POLL_SECONDS)
    emit_crd("crd_watch_stop")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--once",
        action="store_true",
        help="If a remoting session is active right now, fire once and exit.",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="Print whether a remoting session is active, then exit.",
    )
    args = parser.parse_args(argv)
    if args.check or args.once:
        active = remoting_session_active(read_assertions())
        emit_crd("crd_session_check", active=active)
        if args.check:
            return 0
        if active:
            fire(source="once")
        return 0
    return run_loop()


if __name__ == "__main__":
    raise SystemExit(main())
