"""Detached launching of bmad-auto commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated tmux control session (bmad-auto-ctl) so they survive
TUI exit, and the dashboard observes them through run-dir artifacts exactly
like runs started from a plain shell. Fast read-only commands (validate,
--dry-run) are captured instead, for display in a modal.

No textual imports here — everything is subprocess-level and unit-testable.
"""

from __future__ import annotations

import shlex
import shutil
import subprocess
import sys
from pathlib import Path

CTL_SESSION = "bmad-auto-ctl"


class LaunchError(Exception):
    pass


def tmux_available() -> bool:
    return shutil.which("tmux") is not None


def _tmux(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["tmux", *args], capture_output=True, text=True)


def session_exists(session: str) -> bool:
    return _tmux("has-session", "-t", f"={session}").returncode == 0


def ctl_window(run_id: str) -> str | None:
    """Name of the control-session window hosting this run's orchestrator
    process (start_detached names windows <kind>-<run_id>), or None when the
    run was not launched from the TUI or the session is gone."""
    if not tmux_available():
        return None
    proc = _tmux("list-windows", "-t", f"={CTL_SESSION}", "-F", "#{window_name}")
    if proc.returncode != 0:
        return None
    for name in proc.stdout.splitlines():
        if name.endswith(f"-{run_id}"):
            return name
    return None


def select_ctl_window(window: str) -> None:
    """Make `window` the control session's current window, so a plain attach
    to the session lands on it (attach-session itself takes no window)."""
    _tmux("select-window", "-t", f"={CTL_SESSION}:{window}")


def _ensure_ctl_session(project: Path) -> None:
    if session_exists(CTL_SESSION):
        return
    proc = _tmux("new-session", "-d", "-s", CTL_SESSION, "-c", str(project))
    if proc.returncode != 0:
        raise LaunchError(f"tmux new-session failed: {proc.stderr.strip()}")


def cli_argv(*tail: str) -> list[str]:
    """`sys.executable -m automator.cli ...` — immune to PATH/venv drift
    inside tmux windows."""
    return [sys.executable, "-m", "automator.cli", *tail]


def start_detached(project: Path, argv_tail: list[str], run_id: str, kind: str) -> None:
    """Run a bmad-auto command in a new window of the control session.

    The window runs under explicit `sh -c` (the user's login shell may be
    fish); the trailing `read` keeps the exit status inspectable instead of
    tmux closing the window the moment the process exits.
    """
    if not tmux_available():
        raise LaunchError("tmux not found on PATH")
    _ensure_ctl_session(project)
    inner = shlex.join(cli_argv(*argv_tail))
    shell = f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; read -r'
    proc = _tmux(
        "new-window",
        "-d",
        "-t",
        f"={CTL_SESSION}:",
        "-n",
        f"{kind}-{run_id}",
        "-c",
        str(project),
        "sh",
        "-c",
        shell,
    )
    if proc.returncode != 0:
        raise LaunchError(f"tmux new-window failed: {proc.stderr.strip()}")


def start_run_detached(
    project: Path,
    run_id: str,
    *,
    epic: int | None = None,
    story: str | None = None,
    max_stories: int | None = None,
) -> None:
    tail = ["run", "--project", str(project), "--run-id", run_id]
    if epic is not None:
        tail += ["--epic", str(epic)]
    if story:
        tail += ["--story", story]
    if max_stories is not None:
        tail += ["--max-stories", str(max_stories)]
    start_detached(project, tail, run_id, "run")


def start_sweep_detached(
    project: Path,
    run_id: str,
    *,
    no_prompt: bool = False,
    decisions_only: bool = False,
    max_bundles: int | None = None,
) -> None:
    tail = ["sweep", "--project", str(project), "--run-id", run_id]
    if no_prompt:
        tail.append("--no-prompt")
    if decisions_only:
        tail.append("--decisions-only")
    if max_bundles is not None:
        tail += ["--max-bundles", str(max_bundles)]
    start_detached(project, tail, run_id, "sweep")


def resume_detached(project: Path, run_id: str) -> None:
    start_detached(project, ["resume", "--project", str(project), run_id], run_id, "resume")


def run_captured(argv_tail: list[str]) -> tuple[int, str]:
    """Run a fast read-only command (validate, --dry-run) and capture its
    combined output for display."""
    proc = subprocess.run(cli_argv(*argv_tail), capture_output=True, text=True)
    out = proc.stdout
    if proc.stderr:
        if out and not out.endswith("\n"):
            out += "\n"
        out += proc.stderr
    return proc.returncode, out
