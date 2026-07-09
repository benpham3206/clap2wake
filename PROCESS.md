# Clap-to-Wake: Process Retrospective

A high-level account of how this system was built. Each claim is one sentence.

## What I did

- Delegated the initial scaffold (listener script, launchd plist, dependency install) to a Codex subagent to conserve the primary model's budget.
- Took over directly once the subagent hit sandbox limits, installing deps into a venv and pointing the LaunchAgent at `~/clapwake/.venv/bin/python3`.
- Loaded the listener as a persistent macOS LaunchAgent (`com.you.clapwake`) wrapped in `caffeinate -s` so the system stays awake while the display sleeps.
- Switched device selection from a hardcoded built-in-mic search to the system default input at the user's request.
- Wired the wake action to fire two mechanisms — `caffeinate -u` (OS idle-sleep reversal) and BetterDisplay's DDC `hardwareBacklight=on` (physical panel relight) — after discovering the display is an external Samsung.
- Traced a display-not-sleeping bug to a separate `caffeinate -dims` holding a display-sleep-prevention assertion for the Hermes gateway, and fixed it by dropping the `-d` flag.
- Mapped the USB topology to explain why the light bar and dongles couldn't be woken directly.
- Rebuilt the detector from a fixed double-clap trigger into a burst counter (2 claps = wake, 3 claps = sleep) after verifying no reliable display-state signal exists.

## Why

- Codex delegation was explicitly requested to preserve the limited primary-model budget.
- Both wake mechanisms fire together because each covers a different failure layer (OS display sleep vs. physical panel DPMS standby).
- Distinct clap counts replaced a state-based toggle because a toggle requires knowing whether the screen is currently on, which turned out to be undetectable here.

## What worked

- The `caffeinate -s` wrapper cleanly ties the listener's lifecycle to a system-awake assertion via a single launchd job.
- Launching wake commands with non-blocking `subprocess.Popen` (not `run`) prevents the audio callback from stalling for the caffeinate hold duration.
- The BetterDisplay CLI, invoked directly as the app binary with a timeout guard, reliably drives DDC power on the Samsung.
- A deterministic offline test with a fake clock and stubbed commands verified all burst-count cases without needing real claps.
- Reading live `pmset -g assertions` output turned "I think the caffeinates conflict" into a precise, evidence-backed diagnosis.

## What didn't work

- `system_profiler SPUSBDataType` returned empty on Apple Silicon, forcing a fallback to `ioreg` for USB enumeration.
- `CGDisplayIsAsleep` (and `CGDisplayIsActive`/`IsOnline`, DDC backlight, and `IODisplayWrangler`) all failed to reflect DPMS display sleep on the external monitor, killing the state-based toggle idea.
- Invoking the BetterDisplay binary without a timeout hung and spawned stray GUI instances that had to be cleaned up.
- The built-in mic picked up nothing usable, so the user moved the system default input to the Scarlett interface.

## What I learned

- On Apple Silicon, an external display that is DPMS-asleep still reports as online/active/awake to every standard API, so "screen off" is effectively unobservable in software.
- `caffeinate` assertions are reference-counted and stack without conflict, but the specific flags (`-d` vs `-s`/`-i`) determine whether the display is allowed to sleep at all.
- A physically powered-off external monitor drops its USB hub, so anything downstream (dongles, light bar) dies and cannot be revived in software.
- USB peripherals have no software "power on" — they are powered whenever the host and upstream hub are awake, which reframes "wake my USB inputs" as a non-problem in the display-sleep case.
- Verifying an assumption (does this API detect sleep?) before building on it is cheaper than debugging a feature built on a false premise.

## Mistakes I made

- I initially built device selection around the built-in mic, which the user's setup couldn't use.
- I probed the BetterDisplay binary without a timeout guard first, spawning duplicate app instances I then had to kill.
- I reached for BetterDisplay's DDC wake path before confirming that plain `caffeinate -u` actually failed on this monitor, adding complexity that may not have been strictly necessary.
- I assumed `CGDisplayIsAsleep` would work and only caught its failure because I ran a self-recovering test — had I trusted it, the toggle would have shipped broken.

## Lessons to apply next time

- Confirm the hardware environment (which mic, which display, internal vs. external) before hardcoding device assumptions.
- Wrap any unfamiliar CLI in a timeout guard on the very first call, especially GUI-app binaries that may block or fork.
- Test a detection/state API in both directions (on AND off) before designing a feature that depends on it flipping.
- Prefer distinct, unambiguous input gestures over inferred state when the state itself can't be reliably queried.
- Inspect live system state (`pmset`, `ioreg`) to ground diagnoses in evidence rather than reasoning from assumptions about how the OS behaves.

---

# Part 2: Debugging & Hardening

A second pass covering the tuning, false-trigger debugging, and mic-reliability work that came after the system first worked. Each claim is one sentence.

## What I did

- Cut wake latency by switching the detector from "wait for the burst to settle, then act" to "fire the instant a burst reaches a count," recovering the ~0.6s I had added for the 3-clap gesture.
- Diagnosed an unwanted display-sleep-then-lock by reading the event log, which showed real `sleep_triggered` / `clap_count: 3` entries.
- Traced the deaf-listener symptom to a live audio capture that returned pure digital zeros from the built-in mic.
- Confirmed via Apple's own docs that the built-in mic is hardware-disconnected in clamshell mode, not software-muted.
- Pinned the listener to the Scarlett by device name and added a dead-stream watchdog that exits so launchd relaunches and re-pins.

## What worked

- The append-only event log (`wake_triggered` / `sleep_triggered` with clap counts) was the single most useful debugging artifact — it turned "I think it's misfiring" into proof.
- A short standalone audio capture printing peak amplitude instantly separated "mic is dead/disabled" (exact `0.0000`) from "mic is just quiet" (small nonzero floor).
- Pinning by device name rather than index made selection immune to the index reshuffling that happens when USB devices come and go.
- Failing loudly when the pinned mic is absent (and letting launchd relaunch to poll) is strictly better than silently falling back to an unusable device.

## What didn't work

- The "follow system default" selection I added at the user's request turned out to be fragile: when the Scarlett disconnected, the default silently fell back to the hardware-dead built-in mic and the listener went deaf with no error.
- Device selection happening once at process startup meant the listener stayed bound to whatever mic it grabbed at boot, so a Scarlett replug didn't help until a manual restart.
- The immediate-fire latency win reintroduced a false-sleep risk, because a reverberant echo of the second wake-clap can register as a phantom third clap and sleep the just-woken screen.

## Mistakes I made

- I confidently told the user the false-sleep was "the built-in mic hearing your typing," which was flatly wrong because the built-in mic is hardware-disabled in clamshell — I reasoned from an assumption about which mic was live instead of measuring it first.
- I let the running listener sit bound to a dead mic for nearly two hours without noticing, because nothing in the design surfaced "I am receiving only silence."
- I designed the detector's device selection as a one-time startup snapshot with no handling for the mic changing underneath it, which is the root of the whole deaf-listener episode.
- I treated "the built-in mic doesn't pick anything up" as low-signal early on, when it was actually the clue that would have pointed at clamshell hardware muting much sooner.

## Lessons to apply next time

- When something "isn't triggering," measure the actual input signal before theorizing about causes — one 3-second capture would have pre-empted a whole wrong explanation.
- Treat exact digital zeros as a distinct diagnostic state (device removed/disabled), not just "quiet," because a live analog input always has a small noise floor.
- Check whether a limitation is software-policy or hardware/firmware before proposing to work around it — the clamshell mic disconnect is unfixable in software by design.
- For a long-running background service, pin to a named resource and fail loudly rather than following a "default" that can silently move to an unusable target.
- Design for resource churn from the start: hot-pluggable devices need a watchdog and a re-selection path, because a startup-only binding will drift out of sync.
- When new evidence contradicts something you already told the user, correct it explicitly rather than quietly moving on.
- Keep separate problems separate: the deaf-mic bug and the false-sleep bug are independent, and fixing one does not address the other.
