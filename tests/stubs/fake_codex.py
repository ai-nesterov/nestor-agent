#!/usr/bin/env python3
import json
import os
import pathlib
import sys


def _arg_value(name: str) -> str:
    if name in sys.argv:
        i = sys.argv.index(name)
        if i + 1 < len(sys.argv):
            return sys.argv[i + 1]
    return ""


out_path = _arg_value("-o")
if out_path:
    out = pathlib.Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    if os.environ.get("FAKE_CODEX_REVIEW"):
        payload = {"review": os.environ["FAKE_CODEX_REVIEW"]}
    elif os.environ.get("FAKE_CODEX_SCHEMA_INVALID") == "1":
        payload = {"summary": "oops"}
    else:
        payload = {
            "summary": os.environ.get("FAKE_CODEX_SUMMARY", "ok"),
            "tests_run": ["pytest -q"],
            "tests_passed": os.environ.get("FAKE_CODEX_TESTS_PASSED", "1") == "1",
            "changed_files": ["main.py"],
            "risk_summary": "low",
            "made_changes": True,
        }
    out.write_text(json.dumps(payload), encoding="utf-8")

if os.environ.get("FAKE_CODEX_TOUCH_FILE"):
    path = pathlib.Path(os.environ["FAKE_CODEX_TOUCH_FILE"])
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("changed by fake codex\n", encoding="utf-8")

if os.environ.get("FAKE_CODEX_TOUCH_PROTECTED") == "1":
    pathlib.Path("BIBLE.md").write_text("mutated\n", encoding="utf-8")

print(json.dumps({"event": "start"}))
print(json.dumps({"event": "end"}))
sys.exit(int(os.environ.get("FAKE_CODEX_EXIT", "0")))
