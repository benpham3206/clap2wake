#!/usr/bin/env python3
"""Deterministic offline tests for the clap detector.

Drives ClapDetector with an injected fake clock and stubbed action commands, so
every gesture case is verified without real audio, real timers, or touching the
display. Run:  ~/clapwake/.venv/bin/python3 ~/clapwake/test_clapwake.py

Gesture model: a pair of claps. The TEMPO of the pair is the intent.
  fast pair  -> wake      slow pair -> sleep      dead zone -> ignored
There is no 3-clap gesture, so no gesture is a prefix of another and neither
action waits on a disambiguation window.
"""

from __future__ import annotations

import clapwake


class Recorder:
    """Stands in for ClapDetector._run so we capture actions instead of firing
    caffeinate/pmset/BetterDisplay for real."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, int]] = []
        self.gaps: list[float] = []

    def __call__(self, commands, action, count, gap: float = 0.0) -> None:
        self.actions.append((action, count))
        self.gaps.append(gap)


def make_detector() -> tuple[clapwake.ClapDetector, Recorder]:
    det = clapwake.ClapDetector()
    rec = Recorder()
    det._run = rec  # type: ignore[method-assign]
    return det, rec


LOUD = 0.9          # a clap (>= THRESH)
QUIET = 0.0         # silence (< RELEASE) -> re-arms the hysteresis gate
WAKE_MIN, WAKE_MAX = clapwake.WAKE_GAP_RANGE_SECONDS
SLEEP_MIN, SLEEP_MAX = clapwake.SLEEP_GAP_RANGE_SECONDS
REARM = clapwake.MIN_REARM_QUIET_SECONDS
WAKE_GAP = (WAKE_MIN + WAKE_MAX) / 2.0     # a comfortable fast pair
SLEEP_GAP = (SLEEP_MIN + SLEEP_MAX) / 2.0  # a comfortable slow pair
# (no default dead zone: the bands are contiguous)


def clap(det: clapwake.ClapDetector, t: float) -> None:
    """Simulate one clap onset at time t: sustained quiet then a loud sample."""
    det.observe_peak(QUIET, now=t - REARM - 0.01)
    det.observe_peak(QUIET, now=t - 0.001)
    det.observe_peak(LOUD, now=t)


def pair(det: clapwake.ClapDetector, t0: float, gap: float) -> float:
    """Simulate a two-clap gesture with the given gap; returns the 2nd onset."""
    clap(det, t0)
    clap(det, t0 + gap)
    return t0 + gap


def stream_peaks(
    det: clapwake.ClapDetector,
    levels: list[tuple[float, float]],
    step: float = 0.005,
) -> float:
    t = 1.0
    for level, duration in levels:
        end = t + duration
        while t < end - 1e-12:
            det.observe_peak(level, now=t)
            t += step
        det.observe_peak(level, now=end)
        t = end + step
    return t


def check(name: str, got, want) -> None:
    status = "PASS" if got == want else "FAIL"
    print(f"[{status}] {name}: got={got} want={want}")
    assert got == want, name


# --- the two gestures fire immediately, with no disambiguation wait ----------


def test_fast_pair_wakes_immediately() -> None:
    det, rec = make_detector()
    pair(det, 1.0, WAKE_GAP)
    check("fast pair -> wake on clap 2", rec.actions, [("wake", 2)])


def test_slow_pair_sleeps_immediately() -> None:
    det, rec = make_detector()
    pair(det, 1.0, SLEEP_GAP)
    check("slow pair -> sleep on clap 2", rec.actions, [("sleep", 2)])


def test_wake_needs_no_settle() -> None:
    """The old model waited DISAMBIGUATE before waking. This one must not."""
    det, rec = make_detector()
    pair(det, 1.0, WAKE_GAP)
    check("wake fired without any settle() call", rec.actions, [("wake", 2)])


def test_gesture_boundaries_are_inclusive() -> None:
    # The shared edge belongs to wake (see test_boundary_gap_resolves_to_wake),
    # so the first sleep gap is a hair past it.
    first_sleep = SLEEP_MIN + (0.01 if clapwake.BANDS_ARE_CONTIGUOUS else 0.0)
    for gap, want in ((WAKE_MIN, "wake"), (WAKE_MAX, "wake"),
                      (first_sleep, "sleep"), (SLEEP_MAX, "sleep")):
        det, rec = make_detector()
        pair(det, 1.0, gap)
        check(f"gap {gap:.2f}s -> {want}", rec.actions, [(want, 2)])


# --- the regression that started this: a 3rd onset must not flip intent -----


def test_third_onset_cannot_convert_wake_into_sleep() -> None:
    """The Jul-21 bug: an echo after a double clap slept the display."""
    det, rec = make_detector()
    last = pair(det, 1.0, WAKE_GAP)
    clap(det, last + 0.35)   # phantom 3rd, dead centre of the old kill window
    clap(det, last + 0.70)   # and another
    check("phantom 3rd/4th cannot sleep after wake", rec.actions, [("wake", 2)])


def test_third_onset_cannot_convert_sleep_into_wake() -> None:
    """The mirror bug: a missed/extra onset used to turn a sleep into a wake."""
    det, rec = make_detector()
    last = pair(det, 1.0, SLEEP_GAP)
    clap(det, last + 0.30)
    check("phantom 3rd cannot wake after sleep", rec.actions, [("sleep", 2)])


def test_no_gesture_is_a_prefix_of_another() -> None:
    """Structural guarantee: the wake and sleep bands cannot overlap."""
    check("wake band closes at or before sleep opens", WAKE_MAX <= SLEEP_MIN, True)


def test_contiguous_bands_leave_no_gap_unanswered() -> None:
    """Default config is contiguous: every legal pair resolves to an action."""
    check("bands are contiguous", clapwake.BANDS_ARE_CONTIGUOUS, True)
    gap = WAKE_MAX
    while gap <= SLEEP_MAX - 0.01:
        det, rec = make_detector()
        pair(det, 1.0, gap)
        check(f"gap {gap:.2f}s resolves to an action", len(rec.actions), 1)
        gap += 0.05


def test_boundary_gap_resolves_to_wake_not_sleep() -> None:
    """Doubt at the shared edge must land on the harmless action."""
    det, rec = make_detector()
    pair(det, 1.0, WAKE_MAX)
    check("gap exactly at the boundary -> wake", rec.actions, [("wake", 2)])


def test_dead_zone_still_works_when_bands_are_separated() -> None:
    """A configured gap between the bands must still discard the pair."""
    saved = clapwake.SLEEP_GAP_RANGE_SECONDS, clapwake.BANDS_ARE_CONTIGUOUS
    clapwake.SLEEP_GAP_RANGE_SECONDS = (WAKE_MAX + 0.20, SLEEP_MAX)
    clapwake.BANDS_ARE_CONTIGUOUS = False
    try:
        det, rec = make_detector()
        pair(det, 1.0, WAKE_MAX + 0.10)
        check("separated bands -> pair discarded", rec.actions, [])
    finally:
        clapwake.SLEEP_GAP_RANGE_SECONDS, clapwake.BANDS_ARE_CONTIGUOUS = saved


# --- rejection cases ---------------------------------------------------------


def test_pair_past_sleep_max_is_not_a_gesture() -> None:
    det, rec = make_detector()
    pair(det, 1.0, SLEEP_MAX + 0.30)
    det.settle(now=1.0 + SLEEP_MAX * 2 + 0.5)
    check("pair slower than the sleep band -> no action", rec.actions, [])


def test_echo_faster_than_wake_min_is_ignored() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.0 + WAKE_MIN - 0.04)   # immediate echo, too fast to be intent
    check("sub-wake-min echo -> no action", rec.actions, [])


def test_echo_does_not_consume_the_real_second_clap() -> None:
    """An ignored echo must not stop a genuine 2nd clap from landing."""
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.0 + WAKE_MIN - 0.04)   # echo, ignored
    clap(det, 1.0 + WAKE_GAP)          # the real 2nd clap
    check("echo ignored, real pair still wakes", rec.actions, [("wake", 2)])


def test_onset_slower_than_sleep_max_starts_a_new_pair() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    late = 1.0 + SLEEP_MAX + 0.25      # too late to pair with clap 1
    clap(det, late)
    check("stale clap 1 abandoned, no action yet", rec.actions, [])
    clap(det, late + WAKE_GAP)         # pairs with the late clap instead
    check("late onset became a new clap 1", rec.actions, [("wake", 2)])


def test_single_clap_does_nothing() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    det.settle(now=1.0 + SLEEP_MAX + 0.5)
    check("single clap -> no action", rec.actions, [])


def test_reverb_tail_not_counted() -> None:
    det, rec = make_detector()
    clap(det, 1.0)
    clap(det, 1.0 + WAKE_GAP)
    det.observe_peak(0.5, now=1.0 + WAKE_GAP + 0.10)
    det.observe_peak(LOUD, now=1.0 + WAKE_GAP + 0.20)
    check("reverb tail -> wake only", rec.actions, [("wake", 2)])


def test_sustained_loud_signal_clears_burst() -> None:
    det, rec = make_detector()
    t = 1.0
    det.observe_peak(LOUD, now=t)
    # Hold loud past MAX_CONTINUOUS without re-arm quiet.
    for i in range(1, 60):
        det.observe_peak(LOUD, now=t + i * 0.01)
    det.settle(now=t + 1.0)
    check("sustained loud signal -> no action", rec.actions, [])


def test_syllabic_singing_brief_dips_do_not_act() -> None:
    det, rec = make_detector()
    # Loud with brief dips shorter than re-arm — busy/hysteresis should block.
    segments = [(0.6, 0.08), (0.05, 0.02), (0.6, 0.08)] * 8
    end = stream_peaks(det, segments)
    det.settle(now=end + SLEEP_MAX + 0.5)
    check("syllabic singing with brief dips -> no action", rec.actions, [])


def test_singing_with_rearmable_dips_busy_gate_blocks() -> None:
    det, rec = make_detector()
    segments = [(0.5, 0.15), (0.0, 0.08)] * 12
    end = stream_peaks(det, segments)
    det.settle(now=end + SLEEP_MAX + 0.5)
    check("high-duty singing with re-arm dips -> no action", rec.actions, [])


# --- cooldowns ---------------------------------------------------------------


def test_post_wake_cooldown_blocks_phantom_gesture() -> None:
    det, rec = make_detector()
    last = pair(det, 1.0, WAKE_GAP)
    t = last + 0.05
    pair(det, t, SLEEP_GAP)   # a full slow pair inside the wake cooldown
    check("gesture inside wake cooldown ignored", rec.actions, [("wake", 2)])


def test_sleep_works_after_wake_cooldown() -> None:
    det, rec = make_detector()
    last = pair(det, 1.0, WAKE_GAP)
    t = last + clapwake.WAKE_COOLDOWN_SECONDS + 0.05
    pair(det, t, SLEEP_GAP)
    check(
        "real sleep after wake cooldown still fires",
        rec.actions,
        [("wake", 2), ("sleep", 2)],
    )


def test_short_sleep_cooldown_allows_quick_rewake() -> None:
    det, rec = make_detector()
    last = pair(det, 1.0, SLEEP_GAP)
    check("sleep fires", rec.actions, [("sleep", 2)])
    t = last + clapwake.SLEEP_COOLDOWN_SECONDS + 0.05
    pair(det, t, WAKE_GAP)
    check(
        "wake soon after short sleep cooldown",
        rec.actions,
        [("sleep", 2), ("wake", 2)],
    )


def test_gesture_band_constants_are_sane() -> None:
    assert WAKE_MIN >= 0.12          # below this it is an echo, not intent
    assert WAKE_MAX <= 0.65          # a "fast" pair must still feel fast
    assert SLEEP_MIN >= 0.55         # a "slow" pair must feel deliberate
    assert SLEEP_MAX <= 1.50         # beyond this it is two separate claps
    assert WAKE_MAX <= SLEEP_MIN
    check(
        "clapwake exposes both bands",
        (clapwake.WAKE_GAP_RANGE_SECONDS, clapwake.SLEEP_GAP_RANGE_SECONDS),
        ((WAKE_MIN, WAKE_MAX), (SLEEP_MIN, SLEEP_MAX)),
    )


# --- stream plumbing (unchanged by the gesture rework) -----------------------


def test_reconnect_backoff_grows_and_caps() -> None:
    assert clapwake.reconnect_backoff_seconds(0) == clapwake.RECONNECT_PAUSE_SECONDS
    b1 = clapwake.reconnect_backoff_seconds(1)
    b2 = clapwake.reconnect_backoff_seconds(2)
    b3 = clapwake.reconnect_backoff_seconds(3)
    check("backoff grows", b2 > b1 and b3 > b2, True)
    huge = clapwake.reconnect_backoff_seconds(20)
    check("backoff capped", huge == clapwake.RECONNECT_BACKOFF_MAX_SECONDS, True)


def test_stream_open_configs_include_fallbacks() -> None:
    mono = clapwake.stream_open_configs(1)
    stereo = clapwake.stream_open_configs(2)
    check("mono has configs", len(mono) >= 6, True)
    check("stereo tries ch=2", any(c.get("channels") == 2 for c in stereo), True)
    check("has low latency", any(c.get("latency") == "low" for c in mono), True)
    check("has default latency", any("latency" not in c for c in mono), True)


def test_stream_restart_threshold_is_bounded() -> None:
    check(
        "native audio host gets a bounded process restart",
        clapwake.PROCESS_RESTART_AFTER_STREAM_FAILURES,
        3,
    )


def main() -> int:
    tests = [
        test_fast_pair_wakes_immediately,
        test_slow_pair_sleeps_immediately,
        test_wake_needs_no_settle,
        test_gesture_boundaries_are_inclusive,
        test_third_onset_cannot_convert_wake_into_sleep,
        test_third_onset_cannot_convert_sleep_into_wake,
        test_no_gesture_is_a_prefix_of_another,
        test_pair_past_sleep_max_is_not_a_gesture,
        test_contiguous_bands_leave_no_gap_unanswered,
        test_boundary_gap_resolves_to_wake_not_sleep,
        test_dead_zone_still_works_when_bands_are_separated,
        test_echo_faster_than_wake_min_is_ignored,
        test_echo_does_not_consume_the_real_second_clap,
        test_onset_slower_than_sleep_max_starts_a_new_pair,
        test_single_clap_does_nothing,
        test_reverb_tail_not_counted,
        test_sustained_loud_signal_clears_burst,
        test_syllabic_singing_brief_dips_do_not_act,
        test_singing_with_rearmable_dips_busy_gate_blocks,
        test_post_wake_cooldown_blocks_phantom_gesture,
        test_sleep_works_after_wake_cooldown,
        test_short_sleep_cooldown_allows_quick_rewake,
        test_gesture_band_constants_are_sane,
        test_reconnect_backoff_grows_and_caps,
        test_stream_open_configs_include_fallbacks,
        test_stream_restart_threshold_is_bounded,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
