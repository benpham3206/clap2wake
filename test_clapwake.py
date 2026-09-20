#!/usr/bin/env python3
"""Deterministic offline tests for the clap detector.

Drives ClapDetector with an injected fake clock and stubbed action commands, so
every gesture case is verified without real audio, real timers, or touching the
display. Run:  ~/clapwake/.venv/bin/python3 ~/clapwake/test_clapwake.py

Gesture model: one pair window. Panel dark -> wake, lit -> dim.
"""

from __future__ import annotations

import time

import clapwake


class Recorder:
    """Stands in for ClapDetector._run so we capture requests instead of firing
    hardware commands for real."""

    def __init__(self) -> None:
        self.actions: list[tuple[str, int]] = []
        self.gaps: list[float] = []

    def __call__(self, commands, action, count, gap: float = 0.0) -> None:
        self.actions.append((action, count))
        self.gaps.append(gap)


def make_detector(*, dark: bool = True) -> tuple[clapwake.ClapDetector, Recorder]:
    det = clapwake.ClapDetector()
    rec = Recorder()
    det._run = rec  # type: ignore[method-assign]
    clapwake.panel_is_dark = lambda: dark  # type: ignore[assignment]
    return det, rec


LOUD = 0.9          # a clap (>= THRESH)
QUIET = 0.0         # silence (< RELEASE) -> re-arms the hysteresis gate
WAKE_MIN, WAKE_MAX = clapwake.WAKE_GAP_RANGE_SECONDS
SLEEP_MIN, SLEEP_MAX = clapwake.SLEEP_GAP_RANGE_SECONDS
REARM = clapwake.MIN_REARM_QUIET_SECONDS
WAKE_GAP = (WAKE_MIN + WAKE_MAX) / 2.0     # inside the shared pair window
SLEEP_GAP = (SLEEP_MIN + SLEEP_MAX) / 2.0  # inside the shared pair window
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
    det, rec = make_detector(dark=True)
    pair(det, 1.0, WAKE_GAP)
    check("pair on a dark panel -> wake", rec.actions, [("wake", 2)])


def test_slow_pair_sleeps_immediately() -> None:
    det, rec = make_detector(dark=False)
    pair(det, 1.0, WAKE_GAP)
    check("same-speed pair on a lit panel -> sleep", rec.actions, [("sleep", 2)])


def test_wake_needs_no_settle() -> None:
    """The old model waited DISAMBIGUATE before waking. This one must not."""
    det, rec = make_detector()
    pair(det, 1.0, WAKE_GAP)
    check("wake fired without any settle() call", rec.actions, [("wake", 2)])


def test_wider_pair_window_toggles_both_states() -> None:
    for gap in (0.12, 0.30, 0.70, 1.20):
        for dark, action in ((True, "wake"), (False, "sleep")):
            det, rec = make_detector(dark=dark)
            pair(det, 1.0, gap)
            check(f"gap {gap:.2f}s -> {action}", rec.actions, [(action, 2)])
    det, rec = make_detector(dark=False)
    pair(det, 1.0, 1.40)
    check("1.40s is outside pair window", rec.actions, [])


def test_gesture_boundaries_are_inclusive() -> None:
    for gap in (WAKE_MIN, WAKE_MAX):
        det, rec = make_detector(dark=True)
        pair(det, 1.0, gap)
        check(f"gap {gap:.2f}s dark -> wake", rec.actions, [("wake", 2)])
        det, rec = make_detector(dark=False)
        pair(det, 1.0, gap)
        check(f"gap {gap:.2f}s lit -> sleep", rec.actions, [("sleep", 2)])


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
    det, rec = make_detector(dark=False)
    last = pair(det, 1.0, WAKE_GAP)
    clap(det, last + 0.30)
    check("phantom 3rd cannot wake after sleep", rec.actions, [("sleep", 2)])


def test_no_gesture_is_a_prefix_of_another() -> None:
    """Wake and sleep share one pair window; neither is a prefix of the other."""
    check("sleep uses the wake pair window", SLEEP_MIN == WAKE_MIN, True)
    check("sleep ends with the wake pair window", SLEEP_MAX == WAKE_MAX, True)


def test_contiguous_bands_leave_no_gap_unanswered() -> None:
    """Every gap inside the pair window resolves to an action."""
    gap = WAKE_MIN
    while gap <= WAKE_MAX + 1e-9:
        det, rec = make_detector(dark=True)
        pair(det, 1.0, gap)
        check(f"gap {gap:.2f}s resolves to an action", len(rec.actions), 1)
        gap += 0.05


def test_boundary_gap_resolves_to_wake_not_sleep() -> None:
    """On a dark panel the pair-window edge still wakes."""
    det, rec = make_detector(dark=True)
    pair(det, 1.0, WAKE_MAX)
    check("gap exactly at the pair ceiling -> wake", rec.actions, [("wake", 2)])


def test_dead_zone_still_works_when_bands_are_separated() -> None:
    """A gap past the pair window is not sleep; it starts a new pair."""
    det, rec = make_detector(dark=False)
    pair(det, 1.0, WAKE_MAX + 0.10)
    check("past pair window -> no action", rec.actions, [])


# --- rejection cases ---------------------------------------------------------


def test_pair_past_sleep_max_is_not_a_gesture() -> None:
    det, rec = make_detector(dark=False)
    pair(det, 1.0, WAKE_MAX + 0.30)
    det.settle(now=1.0 + WAKE_MAX * 2 + 0.5)
    check("pair slower than the pair window -> no action", rec.actions, [])


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
    det, rec = make_detector(dark=True)
    clap(det, 1.0)
    late = 1.0 + WAKE_MAX + 0.25      # too late to pair with clap 1
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
    det, rec = make_detector(dark=True)
    last = pair(det, 1.0, WAKE_GAP)
    t = last + 0.05
    clapwake.panel_is_dark = lambda: False  # type: ignore[assignment]
    pair(det, t, WAKE_GAP)
    check("gesture inside wake cooldown ignored", rec.actions, [("wake", 2)])


def test_sleep_works_after_wake_cooldown() -> None:
    det, rec = make_detector(dark=True)
    last = pair(det, 1.0, WAKE_GAP)
    t = last + clapwake.SLEEP_COOLDOWN_SECONDS + 0.05
    clapwake.panel_is_dark = lambda: False  # type: ignore[assignment]
    pair(det, t, WAKE_GAP)
    check(
        "real sleep after wake cooldown still fires",
        rec.actions,
        [("wake", 2), ("sleep", 2)],
    )


def test_short_sleep_cooldown_allows_quick_rewake() -> None:
    det, rec = make_detector(dark=False)
    last = pair(det, 1.0, WAKE_GAP)
    check("sleep fires", rec.actions, [("sleep", 2)])
    t = last + clapwake.SLEEP_COOLDOWN_SECONDS + 0.05
    clapwake.panel_is_dark = lambda: True  # type: ignore[assignment]
    pair(det, t, WAKE_GAP)
    check(
        "wake soon after short sleep cooldown",
        rec.actions,
        [("sleep", 2), ("wake", 2)],
    )


def test_gesture_band_constants_are_sane() -> None:
    assert WAKE_MIN >= 0.12          # below this it is an echo, not intent
    assert WAKE_MAX <= 1.50          # a pair must still feel like one gesture
    check("wake and sleep share the pair window", SLEEP_MIN == WAKE_MIN, True)
    check("wake and sleep share the pair ceiling", SLEEP_MAX == WAKE_MAX, True)
    check(
        "clapwake exposes both bands",
        (clapwake.WAKE_GAP_RANGE_SECONDS, clapwake.SLEEP_GAP_RANGE_SECONDS),
        ((WAKE_MIN, WAKE_MAX), (SLEEP_MIN, SLEEP_MAX)),
    )


# --- sleep is gated on the login window, not recent HID ----------------------


def run_sleep_gate(
    idle: float,
    locked: bool,
    levels: list[tuple[float, float]] | None = None,
    det: clapwake.ClapDetector | None = None,
) -> list[str]:
    """Drive _sleep_if_user_absent with a faked HID idle time and lock state.

    levels scripts panel_levels() reads for the verify loop; default is one
    dark read so the loop verifies on its first pass and stays offline.
    """
    det = clapwake.ClapDetector() if det is None else det
    launched: list[str] = []
    det._launch_commands = (  # type: ignore[method-assign]
        lambda commands, action, t0, pulse: launched.append(action)
    )
    reads = list(levels) if levels is not None else [(0.0, 0.0)]
    exhausted = reads[-1]
    saved = (
        clapwake.hid_idle_seconds,
        clapwake.screen_is_locked,
        clapwake.panel_levels,
        clapwake.PANEL_VERIFY_DELAY_SECONDS,
    )
    clapwake.hid_idle_seconds = lambda: idle          # type: ignore[assignment]
    clapwake.screen_is_locked = lambda: locked        # type: ignore[assignment]
    clapwake.panel_levels = (                          # type: ignore[assignment]
        lambda: reads.pop(0) if reads else exhausted
    )
    clapwake.PANEL_VERIFY_DELAY_SECONDS = 0.0
    try:
        det._sleep_if_user_absent(clapwake.SLEEP_COMMANDS, 0.0, 2, 0.7)
    finally:
        (
            clapwake.hid_idle_seconds,
            clapwake.screen_is_locked,
            clapwake.panel_levels,
            clapwake.PANEL_VERIFY_DELAY_SECONDS,
        ) = saved
    return launched


def test_first_pair_while_hid_hot_still_sleeps() -> None:
    """11:52:43 — first desk pair was sleep_suppressed; later pairs worked."""
    check("first pair with recent HID -> sleep", run_sleep_gate(0.75, False), ["sleep"])


def test_sleep_blocked_at_the_login_window() -> None:
    check("screen locked -> sleep suppressed", run_sleep_gate(60.0, True), [])


def test_sleep_fires_when_the_desk_is_idle() -> None:
    check("idle + unlocked -> sleep fires", run_sleep_gate(4.0, False), ["sleep"])


def test_sleep_is_ddc_luminance_zero_not_os_display_sleep() -> None:
    """hardwareBacklight=off reports off on this Samsung while luminance stays 100."""
    cmds = clapwake.SLEEP_COMMANDS
    check("sleep is a single command", len(cmds), 1)
    argv = cmds[0]
    joined = " ".join(argv)
    check("sleep uses BetterDisplay", argv[0], clapwake.BETTERDISPLAY_BIN)
    check("sleep names the Samsung", f"--name={clapwake.DISPLAY_NAME}" in argv, True)
    check("sleep zeros software brightness", "--brightness=0" in argv, True)
    check("sleep is DDC luminance", "--ddc" in argv and "--vcp=luminance" in argv, True)
    check("sleep sets luminance 0", "--value=0" in argv, True)
    check("sleep is not pmset displaysleepnow", "displaysleepnow" in joined, False)
    check("sleep is not the no-op hardwareBacklight flag", "--hardwareBacklight=off" in argv, False)
    wake = clapwake.WAKE_DDC_CMD
    check("wake restores brightness", "--brightness=1" in wake, True)
    check("wake restores DDC luminance", "--vcp=luminance" in wake and "--value=100" in wake, True)


def test_unknown_hid_idle_does_not_suppress() -> None:
    """A failed query must not silently disable the sleep gesture forever."""
    check("idle unreadable (-1) -> sleep still fires", run_sleep_gate(-1.0, False), ["sleep"])


def test_sleep_after_own_wake_hid_still_fires() -> None:
    """09:48:59 — sleep after a successful wake was dropped by clapwake-hid."""
    check("sleep after recent wake HID still fires", run_sleep_gate(1.27, False), ["sleep"])


def test_human_hid_after_own_wake_still_sleeps() -> None:
    check("a real key after wake HID still sleeps", run_sleep_gate(0.20, False), ["sleep"])


# --- actions are durable: re-fired until the panel reads back at target -----


def test_sleep_retries_until_panel_reads_dark() -> None:
    """A lit read-back retries dimming; a dark read-back stops the retry loop."""
    launched = run_sleep_gate(
        60.0, False, levels=[(1.0, 100.0), (0.0, 0.0)]
    )
    check("lit read-back re-fires the sleep set", launched, ["sleep", "sleep"])


def test_sleep_gives_up_after_bounded_retries() -> None:
    launched = run_sleep_gate(
        60.0, False, levels=[(1.0, 100.0)] * clapwake.PANEL_VERIFY_ATTEMPTS
    )
    check(
        "unreachable panel -> bounded refires, then unverified",
        launched,
        ["sleep"] * (1 + clapwake.PANEL_VERIFY_ATTEMPTS),
    )


def test_levels_are_dark_reads_either_channel() -> None:
    """Luminance 0 with brightness stuck at 1 is still a dark panel."""
    cases = [
        ((1.0, 100.0), False),
        ((0.0, 100.0), True),
        ((1.0, 0.0), True),
        ((None, None), True),     # unreadable -> wake bias
        ((None, 100.0), False),   # one lit read is still lit
        ((0.0, None), True),
    ]
    for (brightness, luminance), want in cases:
        check(
            f"levels ({brightness}, {luminance}) -> dark={want}",
            clapwake.levels_are_dark(brightness, luminance),
            want,
        )


def test_levels_at_target_needs_both_channels() -> None:
    cases = [
        ((0.0, 0.0), True, True),
        ((0.0, 50.0), True, False),   # luminance not down yet
        ((1.0, 100.0), False, True),
        ((1.0, 50.0), False, False),
        ((None, 0.0), True, False),   # unreadable -> not verified
    ]
    for (brightness, luminance), dark, want in cases:
        check(
            f"levels ({brightness}, {luminance}) dark={dark} -> {want}",
            clapwake.levels_at_target(brightness, luminance, dark),
            want,
        )


def test_wake_is_never_gated() -> None:
    """A stray wake is a no-op; a stray sleep blacks the screen. Only gate sleep."""
    import inspect

    src = inspect.getsource(clapwake.ClapDetector._run)
    gated = src.split('if action == "sleep":')[1]
    check("only the sleep branch reaches the gate", "_sleep_if_user_absent" in gated, True)
    check(
        "wake branch does not consult the gate",
        "_sleep_if_user_absent" in src.split('if action == "wake":')[1].split('if action == "sleep":')[0],
        False,
    )


# --- the two wake paths must press the same key ------------------------------


def test_wake_keycode_matches_hid_helper() -> None:
    """clapwake.py and hidwake.c must post the SAME keycode.

    They drifted once — Python posted 0x3F (kVK_Function) while the C helper
    posted 79 (F18) — and the malformed synthetic Fn press made every wake ring
    the system alert. Nothing else catches this: both paths "work", one beeps.
    """
    import pathlib
    import re

    src = (pathlib.Path(__file__).parent / "hidwake.c").read_text()
    m = re.search(r"kWakeKeyCode\s*=\s*(\d+)", src)
    check("hidwake.c declares a keycode", m is not None, True)
    assert m is not None
    check(
        "clapwake.py and hidwake.c agree on the wake key",
        (clapwake.WAKE_KEY_CODE, int(m.group(1))),
        (79, 79),
    )


def test_wake_key_is_not_a_modifier() -> None:
    """Modifier keycodes cannot be pressed discretely; synthesizing one beeps."""
    modifiers = {0x3B, 0x3C, 0x3D, 0x3E, 0x3F, 0x37, 0x38, 0x39, 0x3A}
    check("wake key is not a modifier", clapwake.WAKE_KEY_CODE in modifiers, False)


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


def test_portaudio_refresh_reveals_a_replugged_mic() -> None:
    """A mic unplugged overnight must come back without a manual restart.

    PortAudio caches its device enumeration at init, so query_devices() keeps
    reporting the pinned mic as absent for the life of the process -- the
    listener sits deaf with the Scarlett plugged in right there. Refreshing the
    host API has to rebuild that list so the next select finds the device.
    """

    class FakeSd:
        """PortAudio's cache: the visible list only changes on _initialize."""

        def __init__(self) -> None:
            self.attached = False   # what is physically on the USB bus
            self.terminated = 0
            self.initialized = 0
            self._visible = self._enumerate()

        def _enumerate(self) -> list[dict]:
            devices = [{"name": "MacBook Pro Microphone", "max_input_channels": 1}]
            if self.attached:
                devices.insert(
                    0, {"name": "Scarlett 2i2 USB", "max_input_channels": 2}
                )
            return devices

        def query_devices(self) -> list[dict]:
            return list(self._visible)

        def _terminate(self) -> None:
            self.terminated += 1

        def _initialize(self) -> None:
            self.initialized += 1
            self._visible = self._enumerate()

    sd = FakeSd()
    check("absent while genuinely unplugged", clapwake.select_preferred_mic(sd), None)

    sd.attached = True  # Ben plugs the Scarlett back in for the day.
    check("stale cache still hides it", clapwake.select_preferred_mic(sd), None)

    check("refresh reports success", clapwake.refresh_device_list(sd), True)
    check("portaudio was rebuilt", (sd.terminated, sd.initialized), (1, 1))

    found = clapwake.select_preferred_mic(sd)
    check("scarlett is visible again", found is not None, True)
    check("and it is the pinned device", found[1], "Scarlett 2i2 USB")


def test_stream_restart_threshold_is_bounded() -> None:
    check(
        "native audio host gets a bounded process restart",
        clapwake.PROCESS_RESTART_AFTER_STREAM_FAILURES,
        3,
    )


class _FakeNotify:
    def __init__(self, fires: list[bool] | None = None) -> None:
        self.fires = list(fires or [])

    def check(self) -> bool:
        if self.fires:
            return bool(self.fires.pop(0))
        return False


def test_hostwatch_reconnects_on_unplug_and_index_churn() -> None:
    import hostwatch

    power = {"src": "AC Power"}
    watch = hostwatch.HostWatch(
        1,
        power_source_fn=lambda: power["src"],
        notify=_FakeNotify([False, True, False]),
    )
    scarlett = (1, "Scarlett 2i2 USB", 2)
    check("steady host is a no-op", watch.poll(scarlett), None)
    check("mic gone -> reconnect", watch.poll(None), "mic_absent")
    check("index churn -> reconnect", watch.poll((0, "Scarlett 2i2 USB", 2)), "device_index_changed")
    power["src"] = "Battery Power"
    check(
        "AC unplug -> process restart reason",
        watch.poll(scarlett),
        "power_source_changed",
    )
    check("same battery after flip is quiet", watch.poll(scarlett), None)


def test_repair_action_starts_and_kicks_a_dead_listener() -> None:
    import check_clapwake as chk

    now = 1_800_000_000.0

    def ev(ts_off: float, event: str, **extra):
        t = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(now + ts_off))
        return {"ts": t, "event": event, **extra}

    check(
        "unloaded agent -> bootstrap (mic may appear later)",
        chk.repair_action(loaded=False, pid=None, now=now, events=[]),
        "bootstrap",
    )
    check(
        "loaded but no pid -> kickstart",
        chk.repair_action(loaded=True, pid=None, now=now, events=[]),
        "kickstart",
    )
    check(
        "fresh start is left alone until the first heartbeat",
        chk.repair_action(
            loaded=True,
            pid=9,
            now=now,
            events=[ev(-10, "service_start"), ev(-10, "listening")],
        ),
        None,
    )
    check(
        "no heartbeat past grace -> kickstart",
        chk.repair_action(
            loaded=True,
            pid=9,
            now=now,
            events=[ev(-120, "service_start"), ev(-120, "listening")],
        ),
        "kickstart",
    )
    check(
        "fresh heartbeat with live callbacks -> none",
        chk.repair_action(
            loaded=True,
            pid=9,
            now=now,
            events=[
                ev(-200, "service_start"),
                ev(-5, "listening_heartbeat", secs_since_callback=0.01),
            ],
        ),
        None,
    )
    loop = [ev(-200, "service_start")]
    loop += [ev(-30 + i, "service_restart_requested") for i in range(5)]
    check(
        "already in a restart loop -> do not pile kickstarts",
        chk.repair_action(loaded=True, pid=9, now=now, events=loop),
        None,
    )


def main() -> int:
    tests = [
        test_fast_pair_wakes_immediately,
        test_slow_pair_sleeps_immediately,
        test_wake_needs_no_settle,
        test_wider_pair_window_toggles_both_states,
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
        test_first_pair_while_hid_hot_still_sleeps,
        test_sleep_blocked_at_the_login_window,
        test_sleep_fires_when_the_desk_is_idle,
        test_sleep_is_ddc_luminance_zero_not_os_display_sleep,
        test_unknown_hid_idle_does_not_suppress,
        test_sleep_after_own_wake_hid_still_fires,
        test_human_hid_after_own_wake_still_sleeps,
        test_sleep_retries_until_panel_reads_dark,
        test_sleep_gives_up_after_bounded_retries,
        test_levels_are_dark_reads_either_channel,
        test_levels_at_target_needs_both_channels,
        test_wake_is_never_gated,
        test_wake_keycode_matches_hid_helper,
        test_wake_key_is_not_a_modifier,
        test_reconnect_backoff_grows_and_caps,
        test_stream_open_configs_include_fallbacks,
        test_portaudio_refresh_reveals_a_replugged_mic,
        test_stream_restart_threshold_is_bounded,
        test_hostwatch_reconnects_on_unplug_and_index_churn,
        test_repair_action_starts_and_kicks_a_dead_listener,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} tests passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
