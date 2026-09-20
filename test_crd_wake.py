#!/usr/bin/env python3
"""Offline tests for the Chrome Remote Desktop session-wake watcher."""

from __future__ import annotations

import crd_wake
import clapwake

FAILED = 0


def check(name: str, got, want) -> None:
    global FAILED
    if got != want:
        FAILED += 1
        print(f"FAIL  {name}: got {got!r} want {want!r}")
        return
    print(f"ok    {name}")


SAMPLE_ACTIVE = """
Assertion status system-wide:
   PreventUserIdleDisplaySleep    1
   pid 1136(remoting_me2me_host): [0x00019637000591b5] 00:31:35 NoDisplaySleepAssertion named: "Remoting session is active"
   pid 946(caffeinate): [0x000000220001823a] PreventUserIdleSystemSleep named: "caffeinate command-line tool"
"""

SAMPLE_IDLE = """
Assertion status system-wide:
   PreventUserIdleDisplaySleep    0
   pid 1136(remoting_me2me_host): [0x0000000100010001] PreventUserIdleSystemSleep named: "Chrome Remote Desktop"
"""


def test_parser_requires_the_session_assertion() -> None:
    check("active session is detected", crd_wake.remoting_session_active(SAMPLE_ACTIVE), True)
    check("host-alive is not a session", crd_wake.remoting_session_active(SAMPLE_IDLE), False)
    check("empty is not a session", crd_wake.remoting_session_active(""), False)
    check(
        "process name alone is not a session",
        crd_wake.remoting_session_active("remoting_me2me_host --host-config"),
        False,
    )


def test_rising_edge_only() -> None:
    now = 100.0
    check(
        "idle stays idle",
        crd_wake.should_fire(was_active=False, active=False, now=now, last_fire=0.0),
        False,
    )
    check(
        "connect fires",
        crd_wake.should_fire(was_active=False, active=True, now=now, last_fire=0.0),
        True,
    )
    check(
        "already-active does not re-fire",
        crd_wake.should_fire(was_active=True, active=True, now=now, last_fire=90.0),
        False,
    )
    check(
        "disconnect does not fire",
        crd_wake.should_fire(was_active=True, active=False, now=now, last_fire=90.0),
        False,
    )
    check(
        "reconnect after cooldown fires",
        crd_wake.should_fire(was_active=False, active=True, now=now, last_fire=80.0),
        True,
    )
    check(
        "reconnect inside cooldown is suppressed",
        crd_wake.should_fire(was_active=False, active=True, now=now, last_fire=92.0),
        False,
    )


def test_fire_hybrid_wake_uses_the_clap_train() -> None:
    called: list[tuple[int, int]] = []
    original = clapwake.ClapDetector._hybrid_wake

    def stub(self, t0, count, gen):  # noqa: ANN001
        called.append((count, gen))

    clapwake.ClapDetector._hybrid_wake = stub  # type: ignore[method-assign]
    try:
        clapwake.fire_hybrid_wake(source="crd")
    finally:
        clapwake.ClapDetector._hybrid_wake = original  # type: ignore[method-assign]
    check("train ran once", len(called), 1)
    check("generation is live", called[0][1] > 0, True)


def main() -> int:
    test_parser_requires_the_session_assertion()
    test_rising_edge_only()
    test_fire_hybrid_wake_uses_the_clap_train()
    if FAILED:
        print(f"\n{FAILED} failed.")
        return 1
    print("\nAll crd_wake tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
