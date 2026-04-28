#!/usr/bin/env python3
"""
Overnight research runner — invoked by launchd at 23:00 local.

Pops the next runnable line from research/overnight_queue.txt, launches it
detached via subprocess.Popen, logs to research/overnight_<ts>.log, and writes
research/overnight_status.json. Falls back to research/horizon_scan.py if the
queue is empty. Skips if a previous run is still alive (pidfile guard).

Queue format: one entry per line. Lines starting with "#" or blank are skipped.
- "research/foo.py"          -> runs as `/usr/bin/python3 research/foo.py`
- "python3 research/foo.py --flag"  -> runs literally via `bash -c`
- any other shell-style command  -> runs literally via `bash -c`
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/Users/jamesmynott_macbook/Documents/Claude/s2tool-projector")
QUEUE = REPO / "research" / "overnight_queue.txt"
STATUS = REPO / "research" / "overnight_status.json"
PIDFILE = REPO / "research" / "overnight.pid"
FINDINGS = REPO / "research" / "findings.jsonl"
DEFAULT_FALLBACK = "research/horizon_scan.py"


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def pop_next_command():
    if not QUEUE.exists():
        return None, None
    lines = QUEUE.read_text().splitlines()
    for i, line in enumerate(lines):
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        return s, i
    return None, None


def remove_line(idx: int) -> None:
    lines = QUEUE.read_text().splitlines()
    del lines[idx]
    QUEUE.write_text("\n".join(lines) + ("\n" if lines else ""))


def queue_remaining() -> int:
    if not QUEUE.exists():
        return 0
    return sum(
        1 for ln in QUEUE.read_text().splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    )


def load_dotenv() -> dict:
    env = os.environ.copy()
    env_path = REPO / ".env"
    if not env_path.exists():
        return env
    for line in env_path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def main() -> int:
    if PIDFILE.exists():
        try:
            old = int(PIDFILE.read_text().strip())
            if is_pid_alive(old):
                print(
                    f"{now_iso()} overnight_runner: previous job pid={old} still alive, skipping",
                    file=sys.stderr,
                )
                return 0
        except (ValueError, FileNotFoundError):
            pass

    cmd_line, idx = pop_next_command()
    source = "queue"
    if cmd_line is None:
        cmd_line = DEFAULT_FALLBACK
        source = "fallback"

    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
    log_path = REPO / "research" / f"overnight_{ts}.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # Spawn a wrapper that runs the inner command, waits, and appends a
    # one-line finding to findings.jsonl on exit (success, failure, or signal).
    # The runner itself returns immediately so launchd doesn't track an hours-long
    # job; the wrapper is the long-running detached child.
    wrapper = REPO / "scripts" / "_overnight_wrapper.py"
    argv = ["/usr/bin/python3", str(wrapper), source, str(log_path), cmd_line]

    log_fp = open(log_path, "w")
    log_fp.write(f"{now_iso()} overnight_runner: starting (source={source})\n")
    log_fp.write(f"{now_iso()} cmd_line: {cmd_line}\n")
    log_fp.write(f"{now_iso()} cwd: {REPO}\n")
    log_fp.flush()

    env = load_dotenv()
    proc = subprocess.Popen(
        argv,
        cwd=str(REPO),
        stdout=log_fp,
        stderr=subprocess.STDOUT,
        env=env,
        start_new_session=True,
    )
    PIDFILE.write_text(str(proc.pid))

    if source == "queue" and idx is not None:
        remove_line(idx)

    STATUS.write_text(
        json.dumps(
            {
                "started_at": now_iso(),
                "script": cmd_line,
                "source": source,
                "log": str(log_path),
                "pid": proc.pid,
                "queue_remaining": queue_remaining(),
            },
            indent=2,
        )
    )

    print(f"{now_iso()} overnight_runner: launched pid={proc.pid} -> {log_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
