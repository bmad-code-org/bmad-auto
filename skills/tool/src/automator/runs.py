"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import os
import secrets
import time
from pathlib import Path

RUNS_DIR = Path(".automator") / "runs"
PID_FILE = "engine.pid"


def new_run_id() -> str:
    return time.strftime("%Y%m%d-%H%M%S") + "-" + secrets.token_hex(2)


def list_run_dirs(project: Path) -> list[Path]:
    """All run dirs containing a state.json, oldest first (run ids sort
    chronologically)."""
    runs = project / RUNS_DIR
    if not runs.is_dir():
        return []
    return sorted(d for d in runs.iterdir() if (d / "state.json").is_file())


def latest_run_dir(project: Path) -> Path | None:
    candidates = list_run_dirs(project)
    return candidates[-1] if candidates else None


def write_pid(run_dir: Path) -> None:
    """Record the engine process pid. Never deleted: a stale pid that fails
    os.kill(pid, 0) is the signal that a run was interrupted."""
    (run_dir / PID_FILE).write_text(str(os.getpid()), encoding="utf-8")


def session_name(run_id: str) -> str:
    return f"bmad-auto-{run_id}"


def attach_target_argv(target: str) -> list[str]:
    """tmux command to reach a target session/window. Inside tmux, nesting is
    refused, so switch this client instead (tmux switch-client -l comes back)."""
    if os.environ.get("TMUX"):
        return ["tmux", "switch-client", "-t", target]
    return ["tmux", "attach", "-t", target]


def attach_argv(run_id: str) -> list[str]:
    return attach_target_argv(f"={session_name(run_id)}")
