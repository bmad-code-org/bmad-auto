"""Detached launching of bmad-auto commands for the TUI.

The TUI never runs engines in-process: run/sweep/resume are launched in new
windows of a dedicated tmux control session (bmad-auto-ctl) so they survive
TUI exit, and the dashboard observes them through run-dir artifacts exactly
like runs started from a plain shell. Fast read-only commands (validate,
--dry-run) are captured instead, for display in a modal.

No textual imports here — everything is subprocess-level and unit-testable.
"""

from __future__ import annotations

import os
import re
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from .. import runs
from ..journal import Journal

CTL_SESSION = "bmad-auto-ctl"

# control-session windows are named <kind>-<run_id> (see start_detached)
_CTL_WINDOW_RE = re.compile(r"^(?:run|sweep|resume|resolve)-(.+)$")


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


def select_ctl_window_id(window_id: str) -> None:
    """Like select_ctl_window but by stable tmux window id (@N). Immune to the
    by-name first-match ambiguity in ctl_window and to tmux auto-rename."""
    _tmux("select-window", "-t", window_id)


# Per-window tmux user option recording what an interactive attach should do
# with the client once the window's command exits (see RETURN_TRAILER in
# start_detached). Set by set_return_pane at attach time. Value is either a pane
# id (%N) to switch the client to — used when the TUI runs inside tmux and
# switched its own client over — or RETURN_DETACH, used when the TUI runs
# outside tmux and a throwaway client was attached that must detach so the
# suspended TUI resumes.
RETURN_OPTION = "@bmad_return_pane"
RETURN_DETACH = "detach"  # pane ids are %N, so this never collides with one


def current_pane_id() -> str | None:
    """Stable tmux id (%N) of the pane this process runs in, or None when not
    inside tmux / tmux is unavailable. For the TUI process this is its own pane
    — the place an attach should return the client to."""
    proc = _tmux("display-message", "-p", "#{pane_id}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def set_return_pane(window_target: str, pane_id: str) -> None:
    """Record `pane_id` as the return target on a control-session window, so its
    trailing shell switches the client back there when the window's command
    exits. `window_target` is any tmux window spec (e.g. `=bmad-auto-ctl:run-…`
    or a stable `@N` id)."""
    _tmux("set-option", "-w", "-t", window_target, RETURN_OPTION, pane_id)


def current_session() -> str | None:
    """Name of the tmux session this process is running inside, or None when
    not in tmux / tmux is unavailable."""
    proc = _tmux("display-message", "-p", "#{session_name}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def in_ctl_session() -> bool:
    """True when we are running inside a control-session window (i.e. launched
    detached by the TUI), as opposed to a user's own shell."""
    return bool(os.environ.get("TMUX")) and current_session() == CTL_SESSION


def detach_client() -> None:
    """Detach the tmux client viewing the current session, handing the terminal
    back to the user. Processes in the session keep running."""
    _tmux("detach-client")


def return_attached_client() -> bool:
    """Hand an attached client back to its origin *now*, mid-process — the
    RETURN_TRAILER move (see start_detached) executed while the window's command
    keeps running in the background, instead of after it exits.

    Reads the RETURN_OPTION recorded on the current window by set_return_pane:
      - a pane id (%N): switch that client back there (`-l` fallback if it's gone);
      - RETURN_DETACH: detach the client so a blocking `tmux attach` returns;
      - unset/empty: nobody attached with a return target — do nothing.
    The option is then cleared so the post-exit RETURN_TRAILER doesn't fire a
    second time. Returns True iff a client was actually returned."""
    if not tmux_available():
        return False
    win = _current_window_id()
    if win is None:
        return False
    ret = _tmux("show-options", "-wqv", RETURN_OPTION).stdout.strip()
    if not ret:
        return False
    if ret == RETURN_DETACH:
        _tmux("detach-client")
    elif _tmux("switch-client", "-t", ret).returncode != 0:
        _tmux("switch-client", "-l")
    _tmux("set-option", "-wu", "-t", win, RETURN_OPTION)
    return True


def decision_pending(run_dir: Path) -> bool:
    """True when the run's sweep is currently blocked on an interactive decision
    — its journal's last entry is a decision-pending announcement (the prompter
    blocks on input right after writing it, so any later entry means it moved
    on). Mirrors tui.data.pending_decision; kept here so the CLI can decide an
    attach target without importing the textual-laden data module."""
    entries = Journal(run_dir).entries()
    return bool(entries) and entries[-1].get("kind") == "decision-pending"


def attach_plan(project: Path, run_id: str) -> tuple[list[str], str | None] | None:
    """Pick where an interactive attach should land for this run and which window
    (if any) to record a return target on. Shared by the CLI `attach` command and
    mirroring the TUI's action_attach logic: prefer the orchestrator's ctl window
    when a sweep is blocked on a decision or no agent session is live, else the
    live agent session. Returns (tmux argv, return_window) or None when there is
    nothing to attach to."""
    session = runs.session_name(run_id)
    window = ctl_window(run_id)
    agent_live = session_exists(session)
    if window is not None and (
        decision_pending(runs.run_dir_for(project, run_id)) or not agent_live
    ):
        select_ctl_window(window)
        return runs.attach_target_argv(f"={CTL_SESSION}"), f"={CTL_SESSION}:{window}"
    if agent_live:
        return runs.attach_target_argv(f"={session}"), None
    return None


def kill_ctl_window(run_id: str) -> None:
    """Kill the control-session window hosting this run's orchestrator process,
    if any. A no-op when the run was not launched from the TUI or tmux is gone."""
    window = ctl_window(run_id)
    if window is not None:
        _tmux("kill-window", "-t", f"={CTL_SESSION}:{window}")


def _current_window_id() -> str | None:
    """Stable tmux id (@N) of the window this process runs in, or None when not
    inside tmux / tmux is unavailable."""
    proc = _tmux("display-message", "-p", "#{window_id}")
    return proc.stdout.strip() if proc.returncode == 0 else None


def _ctl_window_candidates(project: Path) -> list[tuple[str, str]]:
    """(window_id, window_name) for parked control-session run windows whose run
    is no longer live — the kill candidates for a prune.

    A `<kind>-<run_id>` window parks on a `read` prompt that never closes on its
    own; it is a candidate once its run has finished/stopped/crashed (or its run
    dir is gone). The current window is excluded so a prune triggered from inside
    the ctl session never targets itself; live runs and the session's own shell
    window are excluded too.

    The control session is shared across projects, so pruning is scoped to
    `project` via the per-window PROJECT_OPTION tag (mirrors runs.prunable_sessions):
    a window tagged for another project is left alone; an untagged (pre-upgrade)
    window is only a candidate when its run dir exists under this project.
    """
    if not tmux_available() or not session_exists(CTL_SESSION):
        return []
    current = _current_window_id()
    proc = _tmux(
        "list-windows",
        "-t",
        f"={CTL_SESSION}",
        "-F",
        f"#{{window_id}}\t#{{window_name}}\t#{{{runs.PROJECT_OPTION}}}",
    )
    if proc.returncode != 0:
        return []
    mine = runs.project_tag(project)
    candidates: list[tuple[str, str]] = []
    for line in proc.stdout.splitlines():
        parts = line.split("\t")
        win_id = parts[0] if parts else ""
        name = parts[1] if len(parts) > 1 else ""
        tag = parts[2] if len(parts) > 2 else ""
        if not win_id or win_id == current:
            continue
        m = _CTL_WINDOW_RE.match(name)
        if m is None:
            continue  # not a run window (e.g. the session's initial shell)
        run_dir = runs.run_dir_for(project, m.group(1))
        if tag:
            if tag != mine:
                continue  # another project's window
        elif not runs.is_run(run_dir):
            continue  # untagged and no run dir here — ownership unprovable
        if runs.engine_alive(run_dir):
            continue
        candidates.append((win_id, name))
    return candidates


def prunable_ctl_windows(project: Path) -> list[str]:
    """Names of the control-session windows a prune would close (dry-run view)."""
    return [name for _, name in _ctl_window_candidates(project)]


def prune_ctl_windows(project: Path) -> list[str]:
    """Close parked control-session windows whose run is no longer live; returns
    the names of the windows that were closed (see _ctl_window_candidates)."""
    killed: list[str] = []
    for win_id, name in _ctl_window_candidates(project):
        _tmux("kill-window", "-t", win_id)
        killed.append(name)
    return killed


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


# After the parked `read`, return the user where they came from instead of
# stranding them in the control session, then let the window close on its own
# (sh exits). The action recorded by set_return_pane decides how:
#   - a pane id (%N): the TUI is inside tmux and switched its own client here;
#     switch that client back to the TUI's pane (`-l` is a best-effort fallback
#     when the recorded pane is gone).
#   - "detach": the TUI is outside tmux and a throwaway client attached; detach
#     it so the blocking `tmux attach` returns and the suspended TUI resumes.
#   - unset: nobody attached interactively (a plain detached run) — do nothing
#     and park exactly as before.
RETURN_TRAILER = (
    f"ret=$(tmux show-options -wqv {RETURN_OPTION} 2>/dev/null); "
    f'if [ "$ret" = "{RETURN_DETACH}" ]; then tmux detach-client 2>/dev/null; '
    'elif [ -n "$ret" ]; then '
    'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; '
    "fi"
)


def start_detached(project: Path, argv_tail: list[str], run_id: str, kind: str) -> str | None:
    """Run a bmad-auto command in a new window of the control session.

    The window runs under explicit `sh -c` (the user's login shell may be
    fish); the trailing `read` keeps the exit status inspectable instead of
    tmux closing the window the moment the process exits. After the read it runs
    RETURN_TRAILER, which switches an attached client back to its origin pane.

    Returns the new window's stable tmux id (@N) so callers can target it
    unambiguously (window names collide when several kinds share a run_id).
    """
    if not tmux_available():
        raise LaunchError("tmux not found on PATH")
    _ensure_ctl_session(project)
    inner = shlex.join(cli_argv(*argv_tail))
    shell = (
        f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; '
        f"read -r; {RETURN_TRAILER}"
    )
    proc = _tmux(
        "new-window",
        "-d",
        "-P",
        "-F",
        "#{window_id}",
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
    win_id = proc.stdout.strip() or None
    if win_id:
        # Tag the window with its project so a cleanup in another project never
        # closes it (the ctl session is shared across projects).
        _tmux("set-option", "-w", "-t", win_id, runs.PROJECT_OPTION, runs.project_tag(project))
    return win_id


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


def start_resolve_detached(project: Path, run_id: str) -> str | None:
    """Run `bmad-auto resolve <run_id>` in a ctl-session window. The caller
    attaches to it: the resolve agent is interactive, and the post-session
    confirm + resume happen in that same window. Returns the window id so the
    caller attaches to exactly this window, not a stale same-run_id window."""
    return start_detached(
        project, ["resolve", "--project", str(project), run_id], run_id, "resolve"
    )


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
