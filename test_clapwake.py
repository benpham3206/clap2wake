#!/usr/bin/env python3
"""Deterministic offline tests for the clap detector.

Drives ClapDetector with an injected fake clock and stubbed action commands, so
every burst-count case is verified without real audio, real timers, or touching
the display. Run:  ~/clapwake/.venv/bin/python3 ~/clapwake/test_clapwake.py
"""

from __future__ import annotations

import clapwake


class Recorder:
    """Stands in for ClapDetector._run so we capture actions instead of firing
    caffeinate/pmset/BetterDisplay for real."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, int]] = []

    def __call__(self, commands, action, count) -> None:
        self.actions.append((action, count))


def make_detector() -> tuple[clapwake.ClapDetector, Recorder]:
    det = clapwake.ClapDetector()
    rec = Recorder()
    det._run = rec  # type: ignore[method-assign]
    return det, rec


LOUD = 0.9          # a clap (>= THRESH)
QUIET = 0.0         # silence (< RELEASE) -> re-arms the hysteresis gate
GAP = clapwake.MAX_GAP_SECONDS
WAKE_COMMIT = clapwake.WAKE_COMMIT_SECONDS
SLEEP_COMMIT = clapwake.SLEEP_COMMIT_SECONDS


def clap(det: clapwake.ClapDetector, t: float) -> None:
    """Simulate one clap onset at time t: a loud sample preceded by silence so
    the hysteresis gate is armed, matching how real buffers arrive."""
    det.observe_peak(QUIET, now=t - 0.001)   # ensure armed
    det.observe_peak(LOUD, now=t)


def check(name: str, got, want) -> None:
    status = "PASS" if got == want else "FAIL"
    print(f"[{status}] {name}: got={got} want={want}")
    assert got == want, name


def test_two_claps_wake_fast_commit() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.3)
    det.settle(now=1.3 + WAKE_COMMIT + 0.01)
    check("two claps -> single wake after WAKE_COMMIT", rec.actions, [("wake", 2)])


def test_two_claps_no_fire_before_wake_commit() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.3)
    det.settle(now=1.3 + WAKE_COMMIT - 0.05)
    check("nothing fires before wake commit", rec.actions, [])


def test_three_claps_sleep_no_wake_first() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.25)
    # 3rd arrives before wake commit so wake must never fire.
    clap(det, 1.25 + WAKE_COMMIT - 0.05)
    last = 1.25 + WAKE_COMMIT - 0.05
    det.settle(now=last + SLEEP_COMMIT + 0.01)
    check("three claps -> single sleep, no wake", rec.actions, [("sleep", 3)])


def test_three_claps_with_human_spacing_still_sleep() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.25)
    clap(det, 1.53)
    det.settle(now=1.53 + SLEEP_COMMIT + 0.01)
    check("three claps with 280ms final gap -> sleep", rec.actions, [("sleep", 3)])


def test_reverb_tail_not_counted() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.30)
    det.observe_peak(0.5, now=1.40)
    det.observe_peak(LOUD, now=1.50)
    det.settle(now=1.50 + WAKE_COMMIT + 0.01)
    check("reverb tail without silence -> wake, not sleep", rec.actions, [("wake", 2)])


def test_single_clap_does_nothing() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    det.settle(now=1.0 + GAP + 0.01)
    check("single clap -> no action", rec.actions, [])


def test_refractory_rejects_immediate_echo() -> None:
    det, rec = make_detector()
    det.observe_peak(QUIET, now=0.999)
    det.observe_peak(LOUD, now=1.00)
    det.observe_peak(QUIET, now=1.02)
    det.observe_peak(LOUD, now=1.05)
    clap(det, 1.35)
    det.settle(now=1.35 + WAKE_COMMIT + 0.01)
    check("immediate echo inside refractory ignored", rec.actions, [("wake", 2)])


def test_post_wake_cooldown_blocks_phantom_sleep() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.3)
    settle_at = 1.3 + WAKE_COMMIT + 0.01
    det.settle(now=settle_at)
    check("wake fires first", rec.actions, [("wake", 2)])

    t = settle_at + 0.2
    for _ in range(3):
        clap(det, t)
        t += 0.25
    det.settle(now=t + GAP + 0.01)
    check("phantom triple inside wake cooldown does not sleep", rec.actions, [("wake", 2)])


def test_sleep_works_after_wake_cooldown() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.3)
    settle_at = 1.3 + WAKE_COMMIT + 0.01
    det.settle(now=settle_at)
    t = settle_at + clapwake.WAKE_COOLDOWN_SECONDS + 0.05
    for _ in range(3):
        clap(det, t)
        t += 0.25
    det.settle(now=t + SLEEP_COMMIT + 0.01)
    check(
        "real sleep after wake cooldown still fires",
        rec.actions,
        [("wake", 2), ("sleep", 3)],
    )


def test_short_sleep_cooldown_allows_quick_rewake() -> None:
    """After 3-clap sleep, 0.3s mute — then double-clap wake must work soon."""
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.2)
    clap(det, 1.35)
    sleep_at = 1.35 + SLEEP_COMMIT + 0.01
    det.settle(now=sleep_at)
    check("sleep fires", rec.actions, [("sleep", 3)])

    # Inside sleep cooldown: ignored
    t = sleep_at + 0.1
    clap(det, t)
    clap(det, t + 0.15)
    det.settle(now=t + 0.15 + WAKE_COMMIT + 0.01)
    check("double clap inside sleep cooldown ignored", rec.actions, [("sleep", 3)])

    # Just past 0.3s sleep cooldown: wake allowed
    t = sleep_at + clapwake.SLEEP_COOLDOWN_SECONDS + 0.05
    clap(det, t)
    clap(det, t + 0.15)
    det.settle(now=t + 0.15 + WAKE_COMMIT + 0.01)
    check(
        "wake soon after short sleep cooldown",
        rec.actions,
        [("sleep", 3), ("wake", 2)],
    )


def test_wake_faster_than_old_max_gap() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.3)
    assert WAKE_COMMIT <= 0.35
    assert WAKE_COMMIT < GAP
    det.settle(now=1.3 + WAKE_COMMIT + 0.01)
    check("wake before full MAX_GAP", rec.actions, [("wake", 2)])
    assert (WAKE_COMMIT + 0.01) < GAP


def main() -> int:
    tests = [
        test_two_claps_wake_fast_commit,
        test_two_claps_no_fire_before_wake_commit,
        test_three_claps_sleep_no_wake_first,
        test_three_claps_with_human_spacing_still_sleep,
        test_reverb_tail_not_counted,
        test_single_clap_does_nothing,
        test_refractory_rejects_immediate_echo,
        test_post_wake_cooldown_blocks_phantom_sleep,
        test_sleep_works_after_wake_cooldown,
        test_short_sleep_cooldown_allows_quick_rewake,
        test_wake_faster_than_old_max_gap,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
