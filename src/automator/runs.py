"""Run-directory discovery and helpers shared by the CLI and the TUI."""

from __future__ import annotations

import os
import secrets
import shutil
import signal
import subprocess
import tarfile
import time
from pathlib import Path

from . import verify
from .journal import STATE_FILE, Journal, load_state, save_state
from .model import PAUSE_ESCALATION, Phase

RUNS_DIR = Path(".automator") / "runs"
ARCHIVE_DIR = Path(".automator") / "archive"
PID_FILE = "engine.pid"

# How long stop_run waits for a signalled engine to exit before falling back to
# marking the run stopped itself.
_STOP_WAIT_S = 10.0
_STOP_POLL_S = 0.1
_TMUX_TIMEOUT_S = 5.0


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


# ---------------------------------------------------- run resolution / liveness


def run_dir_for(project: Path, run_id: str) -> Path:
    return project / RUNS_DIR / run_id


def is_run(run_dir: Path) -> bool:
    """A directory is a run iff it holds a state.json."""
    return (run_dir / STATE_FILE).is_file()


def read_pid(run_dir: Path) -> int | None:
    """The recorded engine pid, or None when missing/unparseable."""
    try:
        return int((run_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def engine_alive(run_dir: Path) -> bool:
    """True only when a local engine pid is provably alive (mirrors
    tui.data.liveness, minus the tmux fallback — callers here want a definite
    'is something running' answer, and 'unknown' must not block stop/delete)."""
    pid = read_pid(run_dir)
    if pid is None:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


# ----------------------------------------------------------- stop / delete / archive


def kill_session(run_id: str) -> None:
    """Kill a run's agent tmux session (bmad-auto-<id>); a no-op when it is
    already gone or tmux is unavailable."""
    if not shutil.which("tmux"):
        return
    try:
        subprocess.run(
            ["tmux", "kill-session", "-t", f"={session_name(run_id)}"],
            capture_output=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        pass


CTL_SESSION = "bmad-auto-ctl"
_SESSION_PREFIX = "bmad-auto-"

# tmux user option stamping a session/window with the project it belongs to, so
# a prune in one project never touches another project's live runs. See
# prunable_sessions and tui.launch.
PROJECT_OPTION = "@bmad_project"


def project_tag(project: Path) -> str:
    """Canonical project identity stored in PROJECT_OPTION. The single source of
    normalization: both the tagging (at session/window creation) and the prune
    comparison must route through this so symlinks/relative paths can't make a
    project look foreign to its own sessions."""
    return str(project.resolve())


def tmux_sessions() -> list[str]:
    """All live tmux session names, or [] when tmux is missing, no server is
    running, or the query fails."""
    if not shutil.which("tmux"):
        return []
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", "#{session_name}"],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return []
    if proc.returncode != 0:  # no server / no sessions
        return []
    return [line for line in proc.stdout.splitlines() if line]


def session_project_tags() -> dict[str, str]:
    """Map each live session name to its PROJECT_OPTION value ("" when unset).
    Same missing-tmux/no-server guards as tmux_sessions()."""
    if not shutil.which("tmux"):
        return {}
    try:
        proc = subprocess.run(
            ["tmux", "list-sessions", "-F", f"#{{session_name}}\t#{{{PROJECT_OPTION}}}"],
            capture_output=True,
            text=True,
            timeout=_TMUX_TIMEOUT_S,
        )
    except (subprocess.SubprocessError, OSError):
        return {}
    if proc.returncode != 0:  # no server / no sessions
        return {}
    tags: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        name, _, tag = line.partition("\t")
        if name:
            tags[name] = tag
    return tags


def prunable_sessions(project: Path) -> tuple[list[str], list[str]]:
    """Partition the bmad-auto-<id> agent sessions into (prunable, live) run ids.

    The control session (bmad-auto-ctl) is never a candidate. Pruning is scoped
    to `project` via the PROJECT_OPTION tag set at session creation:

    - tag == this project: ours — prunable unless a provably-alive engine pid is
      running (covers finished/stopped/crashed *and* orphans whose run dir was
      deleted, since engine_alive is False with no pid).
    - tag is another project: skipped — never touched.
    - tag empty (pre-upgrade, untagged session): can't prove ownership, so fall
      back to the run dir — prunable only when the dir exists under this project
      and is dead; skipped when the dir is absent.
    """
    tags = session_project_tags()
    mine = project_tag(project)
    prunable: list[str] = []
    live: list[str] = []
    for name in tmux_sessions():
        if name == CTL_SESSION or not name.startswith(_SESSION_PREFIX):
            continue
        run_id = name[len(_SESSION_PREFIX) :]
        run_dir = run_dir_for(project, run_id)
        tag = tags.get(name, "")
        if tag:
            if tag != mine:
                continue  # another project's session
        elif not is_run(run_dir):
            continue  # untagged and no run dir here — ownership unprovable
        if engine_alive(run_dir):
            live.append(run_id)
        else:
            prunable.append(run_id)
    return prunable, live


def prune_sessions(project: Path, *, dry_run: bool = False) -> list[str]:
    """Kill every prunable bmad-auto-<id> session (see prunable_sessions);
    returns the run ids that were (or, with dry_run, would be) killed."""
    prunable, _ = prunable_sessions(project)
    if not dry_run:
        for run_id in prunable:
            kill_session(run_id)
    return prunable


def stop_run(run_dir: Path) -> bool:
    """Stop a live run. Returns False if it was already finished.

    Prefers the engine's own SIGTERM handler so the engine stays the single
    writer of `stopped` (it marks the run, kills its in-flight agent window, and
    exits). Falls back to an external kill + mark when there is no live engine
    pid, it is a legacy run, or it does not exit in time.
    """
    state = load_state(run_dir)
    if state.finished:
        return False

    pid = read_pid(run_dir)
    if pid is not None:
        try:
            os.kill(pid, signal.SIGTERM)
        except (ProcessLookupError, PermissionError, OSError):
            pid = None  # already gone / not ours — go straight to fallback
    if pid is not None:
        deadline = time.monotonic() + _STOP_WAIT_S
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except OSError:
                break  # exited
            time.sleep(_STOP_POLL_S)
        # the engine clears its agent window itself, but kill the session as a
        # backstop in case it died before tearing it down
        kill_session(run_dir.name)
        if load_state(run_dir).stopped:
            return True

    # Fallback: no live engine (or it never confirmed). Mark it stopped here.
    kill_session(run_dir.name)
    state = load_state(run_dir)
    state.stopped = True
    save_state(run_dir, state)
    Journal(run_dir).append("run-stop", pid=pid, fallback=True)
    return True


def delete_run(run_dir: Path) -> None:
    """Permanently remove a run directory. Callers enforce the live guard."""
    shutil.rmtree(run_dir)


def archive_run(project: Path, run_dir: Path) -> Path:
    """Compress a run dir into .automator/archive/<id>.tar.gz and remove the
    original. The tarball is written to a temp path then os.replace'd into place
    so a partial archive never appears. Callers enforce the live guard."""
    archive_dir = project / ARCHIVE_DIR
    archive_dir.mkdir(parents=True, exist_ok=True)
    dest = archive_dir / f"{run_dir.name}.tar.gz"
    tmp = dest.with_suffix(".tar.gz.tmp")
    with tarfile.open(tmp, "w:gz") as tar:
        tar.add(run_dir, arcname=run_dir.name)
    os.replace(tmp, dest)
    shutil.rmtree(run_dir)
    return dest


# ------------------------------------------------------- reclaim / retention

# Heavy per-run scaffolding trimmed from a concluded run dir while the
# TUI-visible core (state.json, journal.jsonl, logs/, ATTENTION) is preserved,
# so the run still lists and renders in the dashboard. The value mirrors
# workspace.WORKTREE_DIRNAME; kept literal here to avoid an import cycle
# (workspace imports nothing from runs, but runs stays leaf-light on purpose).
_HEAVY_RUN_ENTRIES = ("worktrees",)


def _state_or_none(run_dir: Path):
    """Parsed run state, or None when it cannot be read — never classify (and so
    never reclaim) what you cannot positively read."""
    try:
        return load_state(run_dir)
    except Exception:  # noqa: BLE001 - unreadable/corrupt state ⇒ leave it alone
        return None


def is_finished(run_dir: Path) -> bool:
    """A finished, no-longer-live run. `resume` refuses these (cli checks
    state.finished), so tearing down their worktrees can never strand a resume —
    the safe predicate for the *automatic* reconcile paths."""
    if engine_alive(run_dir):
        return False
    state = _state_or_none(run_dir)
    return bool(state and state.finished)


def reclaimable(run_dir: Path) -> bool:
    """A terminal run (finished or stopped) with no live engine — eligible for
    the *explicit* `clean` command. A stopped run is technically resumable, so
    reclaiming its worktree ends that; `clean` is an opt-in reclaim (guarded by
    --keep / --dry-run). Paused, interrupted (crashed) and running/unknown-host
    runs are never reclaimed: paused/interrupted are actively resumable, and a
    missing pid could mean a foreign-host run, so we require positive local
    termination evidence (finished or stopped)."""
    if engine_alive(run_dir):
        return False
    state = _state_or_none(run_dir)
    return bool(state and (state.finished or state.stopped))


def reconcile_orphan_worktrees(repo: Path, run_dir: Path, *, dry_run: bool = False) -> list[Path]:
    """Force-remove every git worktree whose path lies under ``run_dir``, then
    prune git's admin entries. Reconciles from ``git worktree list`` (on-disk
    truth), NOT from policy — orphans created under a previous isolation=worktree
    config persist after a switch back to isolation=none. Returns the worktree
    paths handled (or that would be, under dry_run). Callers gate on
    ``reclaimable``; the main checkout is never under a run dir, so it is safe."""
    run_res = run_dir.resolve()
    try:
        worktrees = verify.worktree_list(repo)
    except verify.GitError:
        return []
    handled: list[Path] = []
    for wt in worktrees:
        try:
            wt.resolve().relative_to(run_res)
        except (ValueError, OSError):
            continue  # not this run's worktree (incl. the main checkout)
        handled.append(wt)
        if not dry_run:
            try:
                verify.worktree_remove(repo, wt, force=True)
            except verify.GitError:
                shutil.rmtree(wt, ignore_errors=True)
    if handled and not dry_run:
        verify.worktree_prune(repo)
    return handled


def reconcile_stale_worktrees(repo: Path, project: Path, *, dry_run: bool = False) -> list[Path]:
    """Safety net for the automatic paths (run/sweep start): tear down worktrees
    left behind by a *finished* run whose clean-finish GC didn't complete (e.g. a
    crash between merge and teardown). Deliberately finished-ONLY — a stopped run
    is still resumable, so its worktree is left for `resume`/`clean` to handle and
    never stranded out from under the operator."""
    handled: list[Path] = []
    for run_dir in list_run_dirs(project):
        if not is_finished(run_dir):
            continue
        handled += reconcile_orphan_worktrees(repo, run_dir, dry_run=dry_run)
    return handled


def trim_run_dir(run_dir: Path, *, dry_run: bool = False) -> list[Path]:
    """Delete heavy scaffolding (the ``worktrees/`` tree) from a concluded run
    dir, preserving its TUI-visible core so the run still appears in the
    dashboard with full status/journal/logs. Returns the paths removed."""
    removed: list[Path] = []
    for name in _HEAVY_RUN_ENTRIES:
        p = run_dir / name
        if p.exists() or p.is_symlink():
            removed.append(p)
            if not dry_run:
                shutil.rmtree(p, ignore_errors=True)
    return removed


def _run_started_epoch(run_dir: Path) -> float | None:
    """Unix time parsed from the run id's ``YYYYMMDD-HHMMSS`` prefix, or None
    when the name does not carry one (legacy/foreign id)."""
    try:
        return time.mktime(time.strptime(run_dir.name[:15], "%Y%m%d-%H%M%S"))
    except (ValueError, OverflowError):
        return None


def runs_past_retention(
    run_dirs: list[Path], *, keep_n: int, keep_days: int = 0, now: float | None = None
) -> list[Path]:
    """The subset of ``run_dirs`` (oldest-first) beyond the retention window:
    not among the newest ``keep_n``, and — when ``keep_days`` is set — also older
    than ``keep_days`` days. ``keep_n <= 0`` retains nothing by count; an
    unparseable run id is treated as old enough to prune once past ``keep_n``."""
    ordered = list(run_dirs)
    candidates = (
        ordered[:-keep_n]
        if keep_n > 0 and len(ordered) > keep_n
        else ([] if keep_n > 0 else list(ordered))
    )
    if keep_days and keep_days > 0:
        cutoff = (time.time() if now is None else now) - keep_days * 86400
        return [rd for rd in candidates if (_run_started_epoch(rd) or 0.0) < cutoff]
    return candidates


# ----------------------------------------------------------- escalation resolution


class RearmError(Exception):
    """The run/story is not in a re-armable escalation state."""


def rearm_escalation(run_dir: Path, story_key: str | None = None) -> str:
    """Re-arm an escalation-paused story so the next resume re-drives it.

    Flips the escalated task out of its terminal ESCALATED phase back to
    PENDING — which makes `_finish_inflight` reset the tree to the story's
    baseline and re-run it (clean rebuild) against the now-corrected frozen
    spec. Deterministically sets that spec's status to `ready-for-dev` so the
    dev session routes straight to implement. Does NOT clear the pause; the
    caller resumes the run separately.

    Returns the re-armed story key. Raises RearmError when the run is not
    paused at the escalation stage or the target story is not escalated.
    """
    state = load_state(run_dir)
    if state.paused_stage != PAUSE_ESCALATION:
        raise RearmError(
            f"run {run_dir.name} is not paused at an escalation "
            f"(stage: {state.paused_stage or 'none'})"
        )
    key = story_key or state.paused_story_key
    if key is None:
        raise RearmError(f"run {run_dir.name} has no escalated story to resolve")
    task = state.tasks.get(key)
    if task is None:
        raise RearmError(f"run {run_dir.name} has no task for story {key}")
    if task.phase != Phase.ESCALATED:
        raise RearmError(f"story {key} is not escalated (phase: {task.phase})")

    # deliberate reset, not a normal state-machine transition (mirrors
    # engine._finish_inflight): a clean re-attempt against the corrected spec.
    task.phase = Phase.PENDING
    task.attempt = 0
    task.review_cycle = 0
    task.defer_reason = None

    if task.spec_file:
        # route /bmad-auto-dev to re-implement (decision table: ready-for-dev
        # -> step-03); independent of the resolve agent having set it.
        verify.set_frontmatter_status(Path(task.spec_file), "ready-for-dev")

    save_state(run_dir, state)
    Journal(run_dir).append("story-escalation-resolved", story_key=key)
    return key
