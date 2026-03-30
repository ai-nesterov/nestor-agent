from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict


class ArtifactManager:
    REQUIRED_FILES = (
        "manifest.json",
        "prompt.txt",
        "result.json",
        "stdout.txt",
        "stderr.txt",
        "events.jsonl",
        "patch.diff",
        "changed_files.json",
        "diff_stat.json",
    )

    def __init__(self, drive_root: Path):
        self.drive_root = Path(drive_root)

    def prepare_artifact_dir(self, task_id: str) -> Path:
        artifact_dir = self.drive_root / "executor_runs" / str(task_id)
        artifact_dir.mkdir(parents=True, exist_ok=True)
        for filename in self.REQUIRED_FILES:
            path = artifact_dir / filename
            if not path.exists():
                if filename.endswith(".json"):
                    path.write_text("{}", encoding="utf-8")
                else:
                    path.write_text("", encoding="utf-8")
        return artifact_dir

    def write_manifest(self, artifact_dir: Path, payload: Dict[str, Any]) -> Path:
        path = Path(artifact_dir) / "manifest.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_result(self, artifact_dir: Path, payload: Dict[str, Any]) -> Path:
        path = Path(artifact_dir) / "result.json"
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_json(self, artifact_dir: Path, filename: str, payload: Any) -> Path:
        path = Path(artifact_dir) / filename
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        return path

    def write_text(self, artifact_dir: Path, filename: str, payload: str) -> Path:
        path = Path(artifact_dir) / filename
        path.write_text(str(payload), encoding="utf-8")
        return path
