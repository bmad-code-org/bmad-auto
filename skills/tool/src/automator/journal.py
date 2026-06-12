"""Append-only run journal and atomic run-state persistence."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from .model import RunState

STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"


class Journal:
    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.path = run_dir / JOURNAL_FILE
        run_dir.mkdir(parents=True, exist_ok=True)

    def append(self, kind: str, **fields: Any) -> None:
        entry = {"ts": time.time(), "kind": kind, **fields}
        with self.path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, default=str) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        out = []
        for line in self.path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:
                continue
        return out


def save_state(run_dir: Path, state: RunState) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / STATE_FILE
    tmp = target.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state.to_dict(), indent=2), encoding="utf-8")
    os.replace(tmp, target)


def load_state(run_dir: Path) -> RunState:
    target = run_dir / STATE_FILE
    return RunState.from_dict(json.loads(target.read_text(encoding="utf-8")))
