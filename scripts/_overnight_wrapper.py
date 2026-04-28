#!/usr/bin/env python3
"""
Overnight wrapper — long-running child of overnight_runner.py.

Spawns the actual research command, captures exit code + duration, appends a
one-line JSON finding to research/findings.jsonl on exit (success, failure,
or signal-kill via atexit + signal handlers).

argv: [script, source, log_path, cmd_line]
"""
from __future__ import annotations

import atexit
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path("/Users/jamesmynott_macbook/Documents/Claude/s2tool-projector")
FINDINGS = REPO / "research" / "findings.jsonl"


def main() -> int:
    if len(sys.argv) != 4:
        print(f"usage: {sys.argv[0]} <source> <log_path> <cmd_line>", file=sys.stderr)
        return 64
    source, log_path, cmd_line = sys.argv[1], sys.argv[2], sys.argv[3]

    if cmd_line.endswith(".py") and not cmd_line.split()[0].startswith("python"):
        argv = ["/usr/bin/python3"] + cmd_line.split()
    else:
        argv = ["/bin/bash", "-c", cmd_line]

    start = time.time()
    exit_code = 255  # mutable closure target so atexit can read final value

    def append_finding():
        finding = {
            "ts": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "script": cmd_line,
            "source": source,
            "exit": exit_code,
            "duration_s": int(time.time() - start),
            "log": log_path,
        }
        try:
            FINDINGS.parent.mkdir(parents=True, exist_ok=True)
            with open(FINDINGS, "a") as f:
                f.write(json.dumps(finding) + "\n")
        except OSError as e:
            print(f"failed to write finding: {e}", file=sys.stderr)

    atexit.register(append_finding)

    # Translate SIGTERM/SIGINT into a clean exit so atexit fires
    def on_signal(signum, _frame):
        nonlocal exit_code
        exit_code = 128 + signum
        sys.exit(exit_code)

    signal.signal(signal.SIGTERM, on_signal)
    signal.signal(signal.SIGINT, on_signal)

    try:
        log_fp = open(log_path, "a")
        proc = subprocess.Popen(argv, cwd=str(REPO), stdout=log_fp, stderr=subprocess.STDOUT)
        exit_code = proc.wait()
        return exit_code
    except Exception as e:
        print(f"wrapper failed to spawn child: {e}", file=sys.stderr)
        exit_code = 254
        return exit_code


if __name__ == "__main__":
    sys.exit(main())
