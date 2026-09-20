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

---

# Part 3: Cold-wake lag + thrash (2026-07-20)

Full session log. Ben reported: keyboard (Aula F75 Max, 2.4 GHz dongle → USB-C hub)
wakes the display faster than clapwake (Scarlett 2i2 → USB-C); then clarified that
**only deep/idle display sleep** feels slow (~5–7s from 2nd clap to panel), while
3-clap sleep → 2-clap wake feels fine. Wanted hardening + logs inspected.

## 5W1H (compressed)

What: cold-wake pulse train + in-process reconnect + error throttle + launchd
ThrottleInterval + timing logs; Why: cut deep-sleep panel lag and stop launchd
thrash; How: thinnest code change in clapwake.py + plist + docs; Who: Ben
(local LaunchAgent); When: 2026-07-20; Where: `~/clapwake`,
`~/Library/LaunchAgents/com.you.clapwake.plist`, `/tmp/clapwake.{out,err}`;
must not break: 2=wake / 3=sleep, Scarlett name-pin, shallow-sleep feel.

## What I did (chronological)

### 1. Orient + first latency theory

- Read `README.md`, `PROCESS.md` (parts 1–2), full `clapwake.py`.
- Compared keyboard path (kernel HID user-activity) vs clapwake path
  (PortAudio → peak gates → `caffeinate -u` + BetterDisplay DDC).
- First answer: not on par by design (double-clap + userspace synthetic wake).
  That was incomplete once Ben scoped the bug to **deep sleep only**.

### 2. Logs + live system evidence

Pulled and measured:

| Signal | Value |
|---|---|
| LaunchAgent | `com.you.clapwake`, wrapped in `caffeinate -s` |
| `launchctl` runs | **9972** (thrash) |
| last exit code | 1 |
| `/tmp/clapwake.out` (window) | 29 `wake_triggered`, 7 `sleep_triggered`, 20 `listening` |
| `/tmp/clapwake.err` | **7976** `preferred_mic_absent`, 18 `audio_stream_dead`, 6 `audio_stream_failed` |
| Err size | ~2.1 MB spam |
| `pmset` | `displaysleep 5` (minutes); system sleep often prevented by caffeinate/coreaudiod |
| Display | Samsung LS32CG51x, main, online |
| Mic | Scarlett 2i2 USB pinned by name |
| Detection timing | wake/sleep often **same wall-clock second** → detector not the 5–7s |

Key log samples (pre-fix out):

- Same-second triple: wake then sleep (shallow path works).
- Overnight/mic-absent: exit → KeepAlive → relaunch every ~3s, deaf until Scarlett back.

### 3. Diagnosis (mechanism)

- **Shallow sleep** (`pmset displaysleepnow` from 3 claps): panel still close to ready;
  single-shot `caffeinate` + DDC is enough → feels fine.
- **Deep / idle sleep** (OS `displaysleep` after idle): Samsung drops deeper DPMS;
  DisplayPort + DDC often not ready at first BetterDisplay call → one-shot is a no-op;
  panel sits black until something re-asserts (keyboard HID keeps prodding; we didn’t).
- **Not** Scarlett vs hub topology; **not** inter-clap spacing for the 5–7s cold gap
  (that gap is post-`wake_triggered`).
- Separate reliability bug: `DEVICE_ABSENT_RETRY_SECONDS = 3` + process exit +
  KeepAlive = thousands of relaunches and multi-MB stderr.

### 4. Code changes (`clapwake.py`)

Cold wake:

- `WAKE_CAFFEINATE_SECONDS = 30` (`caffeinate -u -t 30` once per wake).
- `WAKE_DDC_CMD` → BetterDisplay `--hardwareBacklight=on` for `LS32CG51x`.
- `WAKE_PULSE_OFFSETS_SECONDS = (0.0, 0.5, 1.5, 3.0, 5.0)`.
- Pulse 0: caffeinate + DDC; pulses 1–4: DDC only (covers ~5s cold window).
- `_run` for wake: emit `wake_triggered` with `t0_mono` + offsets, spawn daemon
  `_cold_wake_pulses`; sleep stays one-shot `pmset displaysleepnow`.
- Each pulse logs `wake_pulse` with `pulse`, `offset_s`, `dt_ms`, `commands`.
- Process launches moved off the audio callback onto worker/reap threads.

Hardening:

- Outer `while not stopped` loop: **in-process** re-select mic / reopen stream;
  no exit on mic absent or dead stream (only SIGINT/SIGTERM ends service).
- `DEVICE_ABSENT_RETRY_SECONDS` 3 → **15**.
- `emit_error()` with **60s throttle** per `failure_type` (stops preferred_mic spam).
- New events: `service_start`, `service_stop`; `stopped` includes `reason` /
  `device_name`; listening includes `wake_pulse_offsets`.
- Constants: `ERROR_THROTTLE_SECONDS`, `RECONNECT_PAUSE_SECONDS`, `DISPLAY_NAME`.

### 5. LaunchAgent + ops

- File: `~/Library/LaunchAgents/com.you.clapwake.plist`
- Added `ThrottleInterval` = 10 (safety net if process ever exits).
- `launchctl bootout` → archived err log → `bootstrap`.
- Archived: `/tmp/clapwake.err.pre-harden-20260720-163054` (~2.1 MB thrash).
- Fresh stderr empty after reload; `runs = 1`, `pid` new, listening on Scarlett.

### 6. Docs

- `README.md`: cold-wake pulse train, log fields, in-process mic reconnect.
- This `PROCESS.md` part 3 (session log).

### 7. Verification

- Offline: `.venv/bin/python3 test_clapwake.py` → **20/20 passed** (detector stubs
  still replace `_run`; pulse train not unit-tested end-to-end).
- Smoke: pulse offsets last ≥ 5s; error throttle suppresses duplicate failure_type.
- Health: `check_clapwake.py` NOMINAL after reload (later NOT NOMINAL only because
  a single mid-train BetterDisplay `Failed` landed in stderr — expected cold DDC miss).
- Ben cold-tested: confirmed feels good.

Post-fix log proof (clean train @ 2026-07-20T16:31:55):

```text
wake_triggered  t0_mono=...  pulse_offsets=[0.0, 0.5, 1.5, 3.0, 5.0]
wake_pulse #0  dt_ms=1     caffeinate + BetterDisplay
wake_pulse #1  dt_ms=650   BetterDisplay
wake_pulse #2  dt_ms=1573  BetterDisplay
wake_pulse #3  dt_ms=3139  BetterDisplay
wake_pulse #4  dt_ms=5026  BetterDisplay
```

Also observed (not fixed this session):

- Mid-train DDC fail: `BetterDisplay: Failed. (pulse=3, dt_ms=3260)` — validates retries.
- Overlapping pulse threads if multiple double-claps before prior train ends.
- Triple-clap sleep does **not** cancel residual wake pulses (can re-light after sleep).

## Files touched

| Path | Change |
|---|---|
| `~/clapwake/clapwake.py` | cold-wake train, reconnect loop, emit_error, timing logs |
| `~/clapwake/README.md` | document cold wake + reconnect + log fields |
| `~/clapwake/PROCESS.md` | this part 3 |
| `~/Library/LaunchAgents/com.you.clapwake.plist` | `ThrottleInterval` 10 |
| `/tmp/clapwake.err` | rotated to `clapwake.err.pre-harden-20260720-163054` |

Not committed (left as local dirty tree for Ben).

## Why (design choices)

- Pulse train over single longer sleep: early pulses win when panel is ready; late
  pulses catch deep DPMS without blocking the audio path.
- DDC-only after pulse 0: caffeinate once is enough for UserIsActive; hammering DDC
  is the missing piece when the link is late.
- In-process reconnect over launchd restart: exit/KeepAlive was the thrash mechanism.
- Throttle errors, don’t silence them: first failure still logs; spam dies.

## What worked

- Log-first diagnosis: same-second `wake_triggered` killed the “detector is slow” theory.
- Pulse train matched the reported 5–7s cold window; Ben verified feel.
- `runs` 9972 → 1 after in-process loop + reload.
- Existing offline detector tests stayed green without rewriting gesture semantics.

## What didn’t / deferred

- BetterDisplay CLI `help` hung (known; already guarded with timeouts in reap path).
- No cancel of in-flight cold-wake on sleep or on newer wake (follow-up if residual
  DDC after triple-clap becomes annoying).
- No Accessibility/synthetic HID path (would need permissions; not required after
  pulse train felt good).
- `check_clapwake.py` treats any recent `wake_command_failed` as hard fail — noisy
  when a mid-train DDC miss is expected; could soft-classify later.

## Lessons

- Compare **same sleep depth** when blaming latency; shallow vs deep display sleep
  are different machines.
- Measure `wake_triggered` vs panel light separately; if log is instant, fix the
  **action/panel** path, not the detector.
- KeepAlive + short exit-on-missing-resource = silent reliability disaster; prefer
  stay-alive poll with throttled errors.
- Keyboard HID is a privileged continuous wake source; userspace DDC is one-shot
  unless you retry across the link-up window.

## Final state (end of session)

- Service: running under `caffeinate -s` + `clapwake.py`, Scarlett pinned.
- Pulse offsets live in `listening` / `service_start` JSON.
- Watch: `tail -f /tmp/clapwake.out` for `wake_triggered` / `wake_pulse`.
- Health: `~/clapwake/.venv/bin/python3 ~/clapwake/check_clapwake.py`
- Optional next inch: cancel cold-wake thread on sleep / superseding wake.

---

# Part 4: Permanent HID wake (2026-07-20 evening)

## Failure that forced this

- User: 2-clap did not wake until a keyboard keypress.
- Log **18:04:49**: full `wake_triggered` + pulses 0–4 completed.
- stderr: BetterDisplay **Failed** on pulse 0 and 1; later pulses returned 0 but
  panel stayed black until real HID.
- `pmset` after keyboard: WindowServer `iohideventsystem.queue.tickle` from
  `AppleHIDKeyboardEventDriver` / 2.4G Dongle — that is the path that works.

## Permanent fix

- In-process **CGEvent** HID tickle (1px mouse + Fn down/up) on every pulse.
- In-process **IOPMAssertionDeclareUserActivity** on every pulse.
- CLI `caffeinate -u` + BetterDisplay still as backup; caffeinate re-asserted
  on later pulses (`-t 5`), not only pulse 0.
- Pulse train extended to **0 … 10s** dense early grid.
- **Generation cancel**: new wake or sleep bumps `_wake_generation` so residual
  DDC/HID cannot fight `displaysleepnow` or stack overlapping trains.
- Logged as `native: {hid, iopm}` on each `wake_pulse`.

---

# Part 5: Third-try idle wake (2026-07-20 18:23)

## Why it took three tries (from logs)

1. **18:08:54 gen=1** — wake started; **~400ms later `sleep_triggered`** (3rd clap)
   cancelled the train at pulse 2. Residual echo/fast triple **aborted wake**.
2. **18:08:57 gen=3** — full 0–10s train, every pulse `hid:true`/`iopm:true`, but
   CGEvent used a **NULL event source** (posts “succeed” without a real
   WindowServer HID tickle). Panel stayed dark.
3. **18:23:37 gen=4** — another full train; BetterDisplay **Failed** on pulses
   3–4; eventually came up (third try). Keyboard still the only path that
   reliably creates `iohideventsystem.queue.tickle`.

## Permanent fix (part 5)

- **CGEventSourceCreate(kCGEventSourceStateHIDSystemState)** for mouse + keys
  (not NULL source).
- **Dense burst 0–3s @ 100ms** native tickles; CLI (caffeinate/DDC) every 0.5s;
  sustain to **15s**.
- Extra HID channels: **cliclick** mouse nudge + **osascript** System Events
  key code 63 (Fn).
- Multi IOPM declare per pulse (×3).
- **POST_WAKE_COUNT_MUTE 0.28s** after 2nd clap so echo cannot count as 3rd and
  cancel the train (intentional triple still works at ≥0.35s gap).
- 21 unit tests green; agent reloaded (`wake_pulse_count=55`, `ax_trusted=true`).

---

# Part 6: Never-deaf stream self-heal (2026-07-20 late)

## Failure

- 22:22–22:31: Scarlett stream died then PortAudio open failed in a tight loop
  (`-9986`, CoreAudio `-10851`). No `listening` → claps never counted.
- Root: exclusive/wedged host after dead stream; single open config; 1s reconnect.

## Permanent fix

- Re-query pinned mic **by name** every reconnect (index churn safe).
- Open **config matrix**: channels 1|2, latency low|high|default, rate
  default|44.1k|48k, blocksize 128|256|512.
- Exponential backoff (2s…30s) on open failure — no 1Hz thrash.
- `sd._terminate()` / `_initialize()` every 3 open failures (and after repeated
  dead streams) to clear wedged CoreAudio.
- `listening_heartbeat` every 60s; `reconnect_wait` logs pause + streak.
- Gap band already widened to 0.25–1.10s (part 5 follow-up).

---

# Part 7: Silent-capture stability and cadence band (2026-07-21)

## Observed failure

- The Scarlett listener restarted every ~5 seconds while quiet because its
  watchdog treated a zero-valued audio buffer as a dead stream.
- The repeated close/reopen cycle eventually crashed Python 3.14 in the
  PortAudio/libffi input callback (`EXC_BAD_ACCESS`). During those cycles no
  claps could be detected.
- The detector also used different lower bounds for ordinary claps and a third
  sleep clap, making fast but deliberate triples unnecessarily unreliable.

## Fix

- Liveness is now **callback arrival**, not signal amplitude. A silent Scarlett
  remains open; heartbeats record both callback age and signal age.
- Removed private `sounddevice`/PortAudio terminate-and-reinitialize calls from
  the live callback process. After three genuine callback/open failures, the
  listener exits cleanly so launchd restarts it at the process boundary.
- Every consecutive clap now uses one configurable fast-to-moderate cadence
  band: default **0.20–0.85s**, set with `CLAPWAKE_MIN_CLAP_GAP` and
  `CLAPWAKE_MAX_CLAP_GAP`. The wake disambiguation window covers the entire
  accepted third-clap range.
- Offline detector coverage: fast double, fast triple, moderate triple,
  out-of-range cadence, reverb, cooldown, and bounded native-host recovery.
  Result: **24/24** tests passed.

## Power-state finding

- The Scarlett is continuously listened to while idle, but that only keeps the
  listener alive. The LaunchAgent's `caffeinate -s` prevents system sleep, not
  display idle.
- **2026-08-13:** `displaysleep 5` was blanking the Samsung during ordinary
  desk use. AC is now `displaysleep 0` (Never); battery is 15 min. Clap sleep
  is still `pmset displaysleepnow`. Restore `pmset -c displaysleep 5` only when
  you need a deep-DPMS wake test.
- A recent 3-clap sleep followed by wake is fast because the monitor/link is
  shallow. A long macOS idle allows deeper Samsung/DisplayPort DPMS, so wake
  latency is link renegotiation after clapwake has already triggered.
- BetterDisplay toggles the Samsung hardware backlight as a direct monitor
  command. It supplements Fn/IOPM user activity but does not create a physical
  USB-HID wake event, and it can fail while the deep link is unavailable.

---

# Part 8: IOHIDSystem wake path (2026-07-21)

## Why the first snap wake still failed

- Detection and dispatch were healthy: the first wake pulse began in 2ms.
- The in-process Fn action used `CGEventPost`; success only proved that the
  synthetic event was accepted, not that it entered WindowServer's HID wake
  queue. BetterDisplay also failed on a retry while the deep link was absent.
- The original GitHub program confirmed that BetterDisplay was only a one-shot
  wake supplement and that normal sleep was `pmset displaysleepnow`.

## Replacement

- Added `hidwake.c`, a narrow helper that opens `IOHIDSystem` and posts an inert
  F18 down/up through `IOHIDPostEvent`. It requires post-event/Input Monitoring
  access but no virtual-device entitlement.
- Verified directly: `pmset -g assertions` recorded
  `iohideventsystem.queue.tickle.nxevent service:IOHIDSystem ... clapwake-hid`.
  That is the lower-level wake queue missing from the CGEvent-only approach.
- Every wake pulse now invokes the IOHID helper first; CGEvent, IOPM,
  `caffeinate -u`, and BetterDisplay remain redundant fallbacks.
- Restored ordinary display sleep (`pmset displaysleepnow`). Backlight-only
  blanking and a permanent display-sleep assertion are explicitly not part of
  this fix.

---

# Part 9: Afternoon deafness after sleep clap (2026-08-28)

## Verdict

- Clapwake did not fire from 13:28 to 16:05 because the Scarlett stream stopped
  delivering input callbacks. The detector was not the failure. The LaunchAgent
  stayed up and self-heal spun for 2.6 h without recovering.

## Timeline (all local, 2026-08-28)

- Boot 08:49. `/tmp/clapwake.out` starts 08:50:46. No 08-27 log on this boot.
- 08:50–13:27 healthy: 277 heartbeats, `secs_since_callback` ≈ 0.00x.
- 10:18:20 `sleep_triggered` (gap 0.679 s) then 10:18:22 `wake_triggered`
  (gap 0.453 s). Display off/on in 1 s. This path still works.
- 13:27:15 `sleep_triggered` (gap 0.775 s). Display off. Heartbeat at 13:27:50
  still live (`uptime_s` 16623.9).
- 13:27:56 display on with no `wake_triggered` — HID keyboard, not clapwake.
- 13:28:05 / 13:28:11 `sleep_suppressed` (`screen_locked`, gaps 0.923 / 1.097 s).
  Those claps were heard. They were slow pairs, so they resolved to sleep and
  the lock gate dropped them.
- 13:28:15 display off again. `coreaudiod` turned off the Scarlett USB-audio
  prevent-sleep assertion in the same second.
- 13:28:20–21 first `audio_stream_dead` / `stopped reason=dead_or_signal`.
- 13:28–16:05 restart loop: 284 `service_restart_requested`, 852 listen/stop
  pairs, 283 PortAudio `PaMacCore (AUHAL) err='35'` (EAGAIN), 2× `err='-50'`.
  Zero `wake_triggered` in that window.
- 16:05:07 `DarkWake to FullWake from Deep Idle` from HID. Device index
  moved 0→1 at 16:04:57.
- 16:05:17 process 29728 `listening` on Scarlett index 1. Heartbeats at
  16:06:17 and 16:07:17 with `secs_since_callback=0.001`. Recovered.

## Writer of the stall

- `coreaudiod` dropped the live Scarlett USB engine on the second display-off
  (`TurnedOff` then `Released` the AppleUSBAudioEngine assertions at 13:28:15
  and 13:28:51). PortAudio still opened a stream on a named device, then got
  no callbacks for 5 s, then AUHAL `err=35` on reopen.
- Chrome WebRTC released the Scarlett at 13:20:29, seven minutes earlier. It
  is not the immediate trigger. Display sleep alone is also not sufficient:
  the 13:27:15 sleep left callbacks alive.
- The 13:27:56 on → 13:28:15 off bounce, with the login window up, is the
  closest event to the HAL wedge. Keyboard HID later turned the panel on
  (14:55–16:04) without unwedging audio. Only the 16:05 FullWake / device
  re-enum restored callbacks.

## Check-script blind spot

- `check_clapwake.py` reports `NOMINAL` if the last stdout event is
  `listening`. During the stall that was true for ~5 s of every 10 s cycle.
  A live diagnosis needs a `listening_heartbeat` with `secs_since_callback < 2`.

## Not a code change this session

- Debug only. Listener is healthy again as of 16:05:17. Clap a fast pair to
  confirm fire. If the 13:28 pattern repeats, the next move is to treat
  `audio_callbacks_stalled` after display-off as a CoreAudio host wedge, not
  a mic-absent poll, and to stop calling `NOMINAL` without a fresh heartbeat.

---

# Part 10: Stay up on unplug, start on mic, restart when down (2026-08-28)

## Why

- Ben unplugged AC and clap-to-sleep did nothing. Yesterday's 2.6 h stall was
  the same class of failure: CoreAudio kept a named Scarlett but stopped
  callbacks, `check_clapwake` still said NOMINAL, and KeepAlive restarted a
  wedged host instead of waiting for a real host change.

## What changed

- `hostwatch.py`: every 2s the listen loop checks Scarlett index and AC vs
  battery. Mic gone or index churn reconnects in-process. AC unplug/replug
  exits 75 so launchd restarts PortAudio at a process boundary.
- `check_clapwake.py --repair`: bootstrap if unloaded, kickstart if loaded
  with no PID or with no `listening_heartbeat` past 75s. A live restart loop
  is left alone. NOMINAL now requires a heartbeat with
  `secs_since_callback < 2`.
- LaunchAgent `com.you.clapwake.watchdog` runs `--repair` every 30s. That is
  the "sleeper is off → start when the Scarlett is back / when the job died"
  path. The listener already waited 15s in-process for a missing mic; the
  watchdog covers the case where the job itself is gone.

## Not changed

- Tempo bands, sleep HID/lock gates, Scarlett name-pin, `caffeinate -s`
  wrapper. On battery that wrapper still prevents idle system sleep so the
  listener can hear a clap with the lid closed and an external panel.

---

# Part 11: Chrome Remote Desktop session wake (2026-08-31)

## Why

- 2026-08-24 diagnosed the black Samsung on Chrome Remote Desktop: the host
  takes `NoDisplaySleepAssertion` named `Remoting session is active` and
  posts HID, so macOS thinks the display is on, while the LS32CG51x stays in
  DPMS until clapwake's DDC poke. That session fired HID+DDC+caffeinate once
  (all rc 0) and left "wire CRD-connect → clapwake" as an open yes. Nothing
  persisted, so remote login still left the panel black.

## What changed

- Session start is the rising edge of `Remoting session is active`, not the
  presence of `remoting_me2me_host` (that process stays up whenever remote
  access is enabled).
- `crd_wake.py` polls `pmset -g assertions` every 1s and calls
  `clapwake.fire_hybrid_wake` on that edge. Same pulse train as a fast clap.
- LaunchAgent `com.you.clapwake.crd` keeps the watcher alive. Logs go to
  `/tmp/clapwake.out` as `clapwake.crd` events.

## Not changed

- Clap hear/classify, sleep gates, `pmset displaysleep`, the clap LaunchAgent,
  the watchdog. No new wake channel.

---

# Part 12: Clamshell lock loop when the panel goes dark (2026-09-20)

## Verdict

- The lock screen is not clapwake classify/fire. Lid closed + the last
  display turning off enters `Clamshell Sleep`. `sysadminctl` screenLock
  delay is immediate, so that DarkWake is a login window. Remote for Mac
  HID then FullWakes into the lock screen and the 10s WindowServer DMGrace
  expires back into Clamshell Sleep.

## Evidence (this boot, 2026-09-19 21:37)

- `AppleClamshellState = Yes`. AC `displaysleep 0`, `sleep 0`, clapwake
  wrapped in `caffeinate -s`. None of that blocks clamshell when macOS
  sees no awake display.
- `pmset -g log`: six Clamshell Sleep entries in 2.5 min (01:16:47,
  01:16:57, 01:17:07, 01:17:42, 01:18:12, 01:19:04). Each is followed by
  `Display is turned off`, then HID FullWake from `Remote for Mac`, then
  `loginwindow` User Activity.
- Clapwake log this boot: zero `sleep_triggered`. Sleep path was not the
  writer tonight.
- 01:11:48 Scarlett stream died; 01:11:56 `preferred_mic_absent`. The
  Samsung is gone from Quartz; the only display is BetterDisplay
  `Generic Display` 1920x1080 (`unkn`/`vnon`). That is the USB-hub-drop
  signature of a powered-off or DPMS-unlinked LS32CG51x, not DDC
  backlight-off.

## Writer

- macOS clamshell policy: lid shut and no awake display → DarkWake
  `Clamshell Sleep`. Immediate lock policy then presents loginwindow on
  the next FullWake. Remote HID is the repeat, not a second bug.

## Desired state (named, not built this pass)

- OS display stays on. Samsung backlight is DDC `hardwareBacklight=off`.
  DisplayPort link and the monitor USB hub stay up. Session stays
  unlocked so Remote for Mac / CRD keep the live session.
- `pmset displaysleepnow`, the monitor power button, and
  `hardwarePowerOff` are the opposite of that state: they drop the
  display, trip clamshell, and lock.

## Not changed

- Tempo bands, `SLEEP_COMMANDS` (`pmset displaysleepnow`), screenLock
  delay, AC `displaysleep 0`. Changing sleep-to-DDC or screenLock is a
  Ben yes: the first stops clap-sleep from locking; the second is a
  security door.

---

# Part 13: Sleep is DDC backlight off (2026-09-20)

## Why

- Part 12 named the lock loop: lid closed + OS display off → Clamshell
  Sleep → immediate screenLock. Ben picked option 1: clap-sleep must
  darken the Samsung without putting the OS display to sleep.

## What changed

- `SLEEP_COMMANDS` is BetterDisplay `--hardwareBacklight=off` for
  `LS32CG51x`, the inverse of the existing wake DDC command.
- Offline test `test_sleep_is_ddc_backlight_off_not_os_display_sleep`
  locks that argv. README sleep bullet matches.

## Not changed

- Tempo bands, sleep HID/lock gates, wake pulse train, screenLock delay,
  AC `displaysleep 0`. No `pmset displaysleepnow` on the sleep path.

---

# Part 14: Sleep DDC is luminance 0 (2026-09-20)

## Verdict

- Hear and classify worked. 09:34:11 and 09:35:29 `sleep_triggered`.
  Fire used `--hardwareBacklight=off`, which this Samsung reports as
  `off` while DDC luminance stays 100 and the panel stays lit.

## What changed

- Sleep is `--ddc --vcp=luminance --value=0`. Wake DDC restores
  `--value=100`. Still no `displaysleepnow`.

## Evidence

- `get -hardwareBacklight` → `off` while `get -ddc -vcp=luminance` →
  `100` and `-brightness` → `1.0`. Direct set of luminance 0/100 and
  brightness 0/1 both round-tripped.

---

# Part 15: Slow pair was the HID gate (2026-09-20)

## Verdict

- 09:42:23 slow pair (gap 0.966s) classified as sleep and was dropped:
  `sleep_suppressed` `recent_hid_input` `hid_idle_s=1.87`. Writer is the
  Aula 2.4G dongle (`AppleHIDKeyboardEventDriver product:2.4G Dongle`).
  Luminance-0 sleep from part 14 never ran on that attempt.

## What changed

- Sleep/wake DDC now also set `--brightness=0` / `--brightness=1`.
  HID idle gate stays 3.0s — lowering it reopens typing-as-sleep when
  the audio thump beats WindowServer HID.

---

# Part 16: Sleep after wake was our own HID (2026-09-20)

## Verdict

- 09:48:52 sleep and 09:48:56 wake worked. 09:48:59 and 09:49:03 slow
  pairs (0.905 / 1.088 s) were `sleep_suppressed` `recent_hid_input`
  because the wake train posts F18 at 0 / 0.8 / 2 / 4 s. That is
  clapwake, not Ben typing.

## What changed

- Sleep gate ignores HID explained by `_own_hid_mono` (wake pulses).
  A real key newer than our post still suppresses.
- Sleep/wake boundary 0.60 → 0.50. Live wake 0.479 stays wake.

---

# Part 17: Same clap speed, toggle on brightness (2026-09-20)

## Why

- Ben asked for the slow pair at the same speed as wake. Two intents
  cannot share one tempo, so sleep is no longer a slower gap. A pair
  in 0.15–0.50 s wakes if BetterDisplay brightness is ~0, else sleeps.

## What changed

- `SLEEP_GAP_RANGE_SECONDS` aliases the wake pair window.
- `_dispatch_pair` reads `panel_is_dark()` off the audio callback.
  Unknown brightness fails toward wake.

---

# Part 18: Actions verify and re-fire until the panel lands (2026-09-20)

## Why

- 09:36 — BetterDisplay returned `Failed.` on all 4 wake pulses; nothing
  retried, so the DDC restore to 100 never ran that round.
- 10:02 — three `sleep_triggered` in a row. Correction: those logs predate
  the toggle implementation — that was the old tempo-based version logging
  repeated sleeps. The logs carry no brightness read-back, so a delayed
  set landing is unproven, not established.

## What changed

- `panel_levels()` reads both channels: software brightness and DDC
  luminance. `panel_is_dark()` is dark if EITHER channel is dark; both
  unreadable still fails toward wake.
- Every action now runs `_verify_panel`: wait 0.7s, read back both
  channels, re-fire the set while not at target (max 4 refires), emit
  `{action}_verify` per check and `{action}_unverified` on exhaustion.
  Generation-cancellable: a newer gesture kills a pending verify.
- Wake keeps its fixed HID pulse train for deep DPMS; the verify loop runs
  after it, re-firing DDC only.

## Evidence

- Live round-trip: `sleep_verify` at_target (0.0, 0.0), `wake_verify`
  at_target (1.0, 100.0). 43 offline tests pass.

---

# Part 19: Second pair confirms HID-gated sleep (2026-09-20)

## Verdict

- 11:43:01 and 11:43:02 pairs were heard (`gap_s` 0.392 / 0.383) then
  `sleep_suppressed` `recent_hid_input` (idle 1.2s / 2.35s). Hear was
  live. Restarting the process would not have slept the panel.

## What changed

- First recent-HID pair still drops (password-typing incident).
- A second pair inside the 3s idle window is intent: `sleep_triggered`
  with `hid_confirm: true`.
- Watchdog `com.you.clapwake.watchdog` remains the process restarter.
  Do not kickstart on `sleep_suppressed`.

---

# Part 20: First desk pair must sleep (2026-09-20)

## Verdict

- 11:52:43 first pair was `sleep_suppressed` `recent_hid_input`
  (idle 0.75s). 11:52:45 retry slept with `hid_confirm`. Later toggles
  worked because wake F18 looked like "our" HID. The first clap of a
  session had no such exemption.

## What changed

- Sleep no longer gates on HID idle. A clap pair on a lit panel sleeps.
- Login window still suppresses. THRESH and the busy-room gate stay.
- Removed `_hid_suppressed_at`, `_own_hid_mono`, and `hid_confirm`.
