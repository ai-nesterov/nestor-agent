#!/usr/bin/env python3
import json
import os
import pathlib
import subprocess
import sys


def _arg_value(name: str, default: str = "") -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return default


if os.environ.get("FAKE_CLAUDE_TOUCH_FILE"):
    p = pathlib.Path(os.environ["FAKE_CLAUDE_TOUCH_FILE"])
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("changed by fake claude\n", encoding="utf-8")

if os.environ.get("FAKE_CLAUDE_TOUCH_PROTECTED") == "1":
    pathlib.Path("BIBLE.md").write_text("mutated\n", encoding="utf-8")

if os.environ.get("FAKE_CLAUDE_STAGE_ALL") == "1":
    subprocess.run(["git", "add", "-A"], check=False)

exit_code = int(os.environ.get("FAKE_CLAUDE_EXIT", "0"))

payload = {
    "result": os.environ.get("FAKE_CLAUDE_RESULT", "done"),
    "session_id": os.environ.get("FAKE_CLAUDE_SESSION", "sess-1"),
    "total_cost_usd": float(os.environ.get("FAKE_CLAUDE_COST", "0")),
    "prompt": _arg_value("-p", ""),
}
print(json.dumps(payload))
sys.exit(exit_code)
