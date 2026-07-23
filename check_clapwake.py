#!/usr/bin/env python3
"""Quick health check: is clapwake loaded, listening, and are logs nominal?

Exit 0 = nominal, 1 = problem. Prints one summary line + short details.

  ~/clapwake/.venv/bin/python3 ~/clapwake/check_clapwake.py
  # or:
  chmod +x ~/clapwake/check_clapwake.py && ~/clapwake/check_clapwake.py
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

LABEL = "com.you.clapwake"
PLIST = Path.home() / "Library/LaunchAgents/com.you.clapwake.plist"
SCRIPT = Path.home() / "clapwake/clapwake.py"
VENV_PY = Path.home() / "clapwake/.venv/bin/python3"
HID_WAKE = Path.home() / "clapwake/.venv/bin/clapwake-hid"
LOG_OUT = Path("/tmp/clapwake.out")
LOG_ERR = Path("/tmp/clapwake.err")

LOG_STALE_SECONDS = 6 * 3600
RECENT_LINES = 120

# Failures that mean the service is not healthy right now.
HARD_FAILURES = {
    "preferred_mic_absent",
    "device_query_failed",
    "audio_stream_dead",
    "audio_stream_failed",
    "dependency_import_failed",
    "listener_resume_failed",
}


def run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, capture_output=True, text=True)


def load_json_lines(path: Path, limit: int = RECENT_LINES) -> list[dict]:
    if not path.is_file():
        return []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    out: list[dict] = []
    for line in raw[-limit:]:
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def parse_launchd_pid() -> int | None:
    """Return PID of com.you.clapwake if running, else None."""
    uid = os.getuid()
    pr = run(["launchctl", "print", f"gui/{uid}/{LABEL}"])
    if pr.returncode == 0:
        for line in (pr.stdout or "").splitlines():
            m = re.search(r"\bpid\s*=\s*(\d+)", line, re.I)
            if m:
                return int(m.group(1))
        # loaded but no pid line → not running
        if "state = running" in (pr.stdout or ""):
            # sometimes pid appears as "PID = N" in other dumps
            pass
        if "state = " in (pr.stdout or "") and "running" not in (pr.stdout or ""):
            return None

    lc = run(["launchctl", "list", LABEL])
    if lc.returncode != 0:
        return None  # not loaded — caller distinguishes

    text = lc.stdout or ""
    # Modern dump: "PID" = 55233;
    m = re.search(r'"PID"\s*=\s*(\d+)', text)
    if m:
        return int(m.group(1))
    m = re.search(r"\bPID\s*=\s*(\d+)", text)
    if m:
        return int(m.group(1))
    # Classic table line: PID Status Label
    for line in text.splitlines():
        parts = line.split()
        if len(parts) >= 3 and parts[-1] == LABEL and parts[0] != "-":
            try:
                return int(parts[0])
            except ValueError:
                return None
    return None


def agent_loaded() -> bool:
    return run(["launchctl", "list", LABEL]).returncode == 0


def main() -> int:
    problems: list[str] = []
    notes: list[str] = []

    if not SCRIPT.is_file():
        problems.append(f"missing {SCRIPT}")
    if not VENV_PY.is_file():
        problems.append(f"missing venv python {VENV_PY}")
    if not HID_WAKE.is_file() or not os.access(HID_WAKE, os.X_OK):
        problems.append(f"missing executable HID wake helper {HID_WAKE}")
    if not PLIST.is_file():
        problems.append(f"missing LaunchAgent {PLIST}")

    if not agent_loaded():
        problems.append(f"LaunchAgent not loaded ({LABEL})")
        pid = None
    else:
        pid = parse_launchd_pid()
        if not pid:
            problems.append("LaunchAgent loaded but not running (no PID)")
        else:
            notes.append(f"pid={pid}")
            ps = run(["ps", "-p", str(pid), "-o", "command="])
            cmd = (ps.stdout or "").strip()
            # Parent may be caffeinate wrapping python clapwake.py
            if "clapwake.py" not in cmd and "caffeinate" not in cmd:
                problems.append(f"pid {pid} unexpected: {cmd[:120]!r}")
            else:
                # If launchd tracks caffeinate, find the python child
                if "clapwake.py" in cmd:
                    notes.append("process=clapwake.py")
                else:
                    tree = run(["pgrep", "-P", str(pid), "-lf"])
                    kids = (tree.stdout or "").strip()
                    if "clapwake.py" in kids:
                        notes.append("process=clapwake.py (under caffeinate)")
                    else:
                        # ps of all matching
                        allp = run(["pgrep", "-lf", "clapwake.py"])
                        if (allp.stdout or "").strip():
                            notes.append("process=clapwake.py (found)")
                        else:
                            problems.append("caffeinate up but clapwake.py not found")

    events = load_json_lines(LOG_OUT)
    err_events = load_json_lines(LOG_ERR)

    # Both logs are append-only across restarts, so errors from a previous run
    # would otherwise be reported forever and mask the state of the current one.
    # Keep only stderr entries at or after the newest service_start.
    started_at = max(
        (
            str(e.get("ts"))
            for e in events
            if e.get("event") == "service_start" and e.get("ts")
        ),
        default="",
    )
    if started_at:
        err_events = [
            e for e in err_events if str(e.get("ts", "")) >= started_at
        ]
        notes.append(f"since_start={started_at}")

    if not LOG_OUT.is_file():
        problems.append(f"missing log {LOG_OUT}")
    else:
        age = time.time() - LOG_OUT.stat().st_mtime
        notes.append(f"log_age={age:.0f}s")
        if pid and age > LOG_STALE_SECONDS:
            problems.append(f"log stale ({age / 3600:.1f}h) while job claims to be up")

    last_listening: dict | None = None
    last_action: dict | None = None
    for ev in events:
        if ev.get("event") == "listening":
            last_listening = ev
        if ev.get("event") in {"wake_triggered", "sleep_triggered"}:
            last_action = ev

    if last_listening is None:
        if events or pid:
            problems.append("no listening event in recent stdout log")
    else:
        dev = str(last_listening.get("device_name", "?"))
        notes.append(f"device={dev!r}")
        wake_band = last_listening.get("wake_band")
        sleep_band = last_listening.get("sleep_band")
        if wake_band and sleep_band:
            notes.append(f"wake_band={wake_band} sleep_band={sleep_band}")
        else:
            problems.append(
                "listener predates the tempo-pair gesture (no wake_band/sleep_band"
                " in its listening event) — restart com.you.clapwake"
            )
        preferred = os.environ.get("CLAPWAKE_MIC_NAME", "Scarlett")
        if preferred.lower() not in dev.lower():
            problems.append(f"listening on unexpected mic: {dev!r} (want {preferred!r})")

        # Index of last listening
        li = max(i for i, e in enumerate(events) if e.get("event") == "listening")
        after = events[li + 1 :]
        if events and events[-1].get("event") == "stopped":
            problems.append("log ends on stopped (not currently listening)")

        recent_hard = [
            str(e.get("failure_type"))
            for e in after
            if e.get("failure_type") in HARD_FAILURES
            or str(e.get("failure_type", "")).endswith("_command_failed")
        ]
        # Ignore known-false caffeinate timeouts in historical logs
        recent_hard = [
            f
            for f in recent_hard
            if f != "wake_command_timeout"  # legacy false positive from -t 20 hold
        ]
        if recent_hard:
            problems.append("failures since last listening: " + ", ".join(dict.fromkeys(recent_hard)))

    if last_action:
        notes.append(
            f"last_action={last_action.get('event')}@{last_action.get('ts', '?')}"
            f" count={last_action.get('clap_count')}"
        )

    # stderr: only hard failures / non-JSON noise count
    if LOG_ERR.is_file() and LOG_ERR.stat().st_size > 0:
        hard_err = [
            e.get("failure_type")
            for e in err_events
            if e.get("failure_type") in HARD_FAILURES
            or (
                str(e.get("failure_type", "")).endswith("_command_failed")
            )
            or (
                str(e.get("failure_type", "")).endswith("_command_timeout")
                and "caffeinate" not in str(e.get("root_cause", "")).lower()
            )
        ]
        # Non-JSON garbage on stderr
        try:
            raw_err_lines = LOG_ERR.read_text(encoding="utf-8", errors="replace").splitlines()[
                -30:
            ]
        except OSError:
            raw_err_lines = []
        junk = [
            ln
            for ln in raw_err_lines
            if ln.strip()
            and not ln.strip().startswith("{")
        ]
        if hard_err:
            problems.append(
                "stderr hard failures: " + ", ".join(str(x) for x in dict.fromkeys(hard_err))
            )
        elif junk:
            problems.append(f"stderr noise: {junk[-1][:160]!r}")
        else:
            # only legacy caffeinate timeout JSON — note, don't fail
            notes.append("stderr=legacy timeouts only (ignored)")

    # Scarlett present?
    if VENV_PY.is_file():
        probe = run(
            [
                str(VENV_PY),
                "-c",
                "import sounddevice as sd\n"
                "print(any('Scarlett' in str(d.get('name','')) "
                "and int(d.get('max_input_channels') or 0)>0 "
                "for d in sd.query_devices()))\n",
            ]
        )
        if probe.returncode == 0:
            if (probe.stdout or "").strip() == "True":
                notes.append("scarlett=present")
            else:
                problems.append("Scarlett input not present in device list")

    if problems:
        print("CLAPWAKE NOT NOMINAL")
        for p in problems:
            print(f"  - {p}")
        if notes:
            print("  notes: " + "; ".join(notes))
        print(
            f"  fix: launchctl kickstart -k gui/$(id -u)/{LABEL}\n"
            f"       # or: launchctl bootstrap gui/$(id -u) {PLIST}"
        )
        return 1

    print("CLAPWAKE NOMINAL")
    print("  " + "; ".join(notes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
