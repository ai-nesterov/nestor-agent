#!/usr/bin/env python3
from __future__ import annotations

import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCENARIOS = ROOT / "tests" / "evals" / "scenarios.txt"
OUT = ROOT / "tests" / "evals" / "last_run.json"


def main() -> int:
    tests = [
        line.strip()
        for line in SCENARIOS.read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.strip().startswith("#")
    ]
    cmd = ["pytest", "-q", *tests]
    proc = subprocess.run(cmd, cwd=str(ROOT), text=True, capture_output=True)

    payload = {
        "command": cmd,
        "returncode": proc.returncode,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "scenario_count": len(tests),
    }
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"Executor evals finished with code {proc.returncode}; report: {OUT}")
    if proc.stdout:
        print(proc.stdout)
    if proc.stderr:
        print(proc.stderr)
    return proc.returncode


if __name__ == "__main__":
    raise SystemExit(main())
