"""Read-only filesystem observation layer for the TUI.

Everything the dashboard shows comes from the run-dir artifacts the engine
already writes atomically: state.json (os.replace), journal.jsonl
(append-only), logs/<task-id>.log, ATTENTION. This module never imports
textual — it is plain stdlib + core modules, fully unit-testable, and the
screens own the poll cadence.

All readers are stat-gated: parse results are cached while the file's
(mtime_ns, size) is unchanged. Liveness is the exception — a dying engine
changes no file, so the pid is re-checked on every call.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .. import bmadconfig, sprintstatus
from ..gates import ATTENTION_FILE
from ..journal import JOURNAL_FILE, STATE_FILE, load_state
from ..model import RunState
from ..runs import PID_FILE, list_run_dirs, session_name

LOGS_DIR = "logs"

# Run statuses shown by the dashboard.
RUNNING = "running"
PAUSED = "paused"
FINISHED = "finished"
INTERRUPTED = "interrupted"
UNKNOWN = "unknown"

_StatSig = tuple[int, int]


def _stat_sig(path: Path) -> _StatSig | None:
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


# ------------------------------------------------------------------ liveness


def liveness(run_dir: Path) -> str:
    """'alive' | 'dead' | 'unknown' for the engine that owns run_dir.

    engine.pid is authoritative (written at run/sweep/resume start, never
    deleted). Legacy runs without one fall back to the per-run tmux session —
    but that session only exists while an agent session runs, so its absence
    proves nothing: 'unknown', never falsely dead. Pid checks are local-only;
    runs on other hosts always come back 'unknown'.
    """
    try:
        pid = int((run_dir / PID_FILE).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return _tmux_liveness(run_dir.name)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return "dead"
    except PermissionError:
        return "alive"
    except OSError:
        return "unknown"
    return "alive"


def _tmux_liveness(run_id: str) -> str:
    if not shutil.which("tmux"):
        return "unknown"
    try:
        proc = subprocess.run(
            ["tmux", "has-session", "-t", f"={session_name(run_id)}"],
            capture_output=True,
            timeout=5,
        )
    except (subprocess.SubprocessError, OSError):
        return "unknown"
    return "alive" if proc.returncode == 0 else "unknown"


def _classify(finished: bool, paused: bool, run_dir: Path) -> str:
    if finished:
        return FINISHED
    if paused:
        return PAUSED
    live = liveness(run_dir)
    if live == "alive":
        return RUNNING
    if live == "dead":
        return INTERRUPTED
    return UNKNOWN


# ----------------------------------------------------------- run discovery


@dataclass(frozen=True)
class RunInfo:
    run_id: str
    run_dir: Path
    run_type: str
    started_at: str
    status: str


# state.json path -> (stat sig, (run_type, started_at, finished, paused))
_header_cache: dict[Path, tuple[_StatSig, tuple[str, str, bool, bool]]] = {}


def discover_runs(project: Path) -> list[RunInfo]:
    """One RunInfo per run dir, oldest first; [] when the runs dir is missing.

    Parses only the state.json header fields (cached on stat); a state file
    that fails to parse yields status 'unknown' rather than crashing — it is
    transient, the engine writes atomically.
    """
    out: list[RunInfo] = []
    for run_dir in list_run_dirs(project):
        state_path = run_dir / STATE_FILE
        sig = _stat_sig(state_path)
        cached = _header_cache.get(state_path)
        if sig is not None and cached is not None and cached[0] == sig:
            run_type, started_at, finished, paused = cached[1]
        else:
            try:
                doc = json.loads(state_path.read_text(encoding="utf-8"))
                run_type = str(doc.get("run_type", "story"))
                started_at = str(doc.get("started_at", ""))
                finished = bool(doc.get("finished", False))
                paused = doc.get("paused_reason") is not None
            except (OSError, json.JSONDecodeError):
                out.append(RunInfo(run_dir.name, run_dir, "?", "", UNKNOWN))
                continue
            if sig is not None:
                _header_cache[state_path] = (
                    sig,
                    (run_type, started_at, finished, paused),
                )
        status = _classify(finished, paused, run_dir)
        out.append(RunInfo(run_dir.name, run_dir, run_type, started_at, status))
    return out


# ------------------------------------------------------------- run watching


class RunWatcher:
    """Stat-gated views of one run dir. Never raises on missing or mid-write
    files: state() keeps returning the last good parse."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self._state_sig: _StatSig | None = None
        self._state: RunState | None = None
        self._attention_sig: _StatSig | None = None
        self._attention = ""

    def state(self) -> RunState | None:
        sig = _stat_sig(self.run_dir / STATE_FILE)
        if sig is None or sig == self._state_sig:
            return self._state
        try:
            self._state = load_state(self.run_dir)
            self._state_sig = sig
        except (OSError, json.JSONDecodeError, KeyError, ValueError):
            pass  # keep last good
        return self._state

    def liveness(self) -> str:
        return liveness(self.run_dir)

    def status(self) -> str:
        state = self.state()
        if state is None:
            return UNKNOWN
        return _classify(state.finished, state.paused, self.run_dir)

    def attention(self) -> str:
        path = self.run_dir / ATTENTION_FILE
        sig = _stat_sig(path)
        if sig is not None and sig != self._attention_sig:
            try:
                self._attention = path.read_text(encoding="utf-8")
                self._attention_sig = sig
            except OSError:
                pass
        return self._attention


class JournalTail:
    """Incremental journal.jsonl reader.

    The byte offset only ever advances past complete lines, so a partially
    flushed append is withheld until its newline lands. Truncation
    (size < offset) resets to the start; unparseable lines are skipped.
    """

    def __init__(self, run_dir: Path):
        self.path = run_dir / JOURNAL_FILE
        self._offset = 0

    def read_new(self) -> list[dict[str, Any]]:
        sig = _stat_sig(self.path)
        if sig is None:
            self._offset = 0
            return []
        size = sig[1]
        if size < self._offset:
            self._offset = 0
        if size == self._offset:
            return []
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        complete = chunk.rfind(b"\n") + 1
        if complete == 0:
            return []
        self._offset += complete
        entries: list[dict[str, Any]] = []
        for raw in chunk[:complete].splitlines():
            line = raw.decode("utf-8", errors="replace").strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(entry, dict):
                entries.append(entry)
        return entries


class LogTail:
    """Incremental reader for a pane log. The first read seeks to the last
    max_bytes so multi-MB logs are never slurped whole; later reads return
    only new bytes. ANSI escapes pass through for the UI to render."""

    def __init__(self, path: Path, max_bytes: int = 65536):
        self.path = path
        self.max_bytes = max_bytes
        self._offset: int | None = None  # None until the file first appears

    def read_new(self) -> str:
        sig = _stat_sig(self.path)
        if sig is None:
            self._offset = None
            return ""
        size = sig[1]
        if self._offset is None:
            self._offset = max(0, size - self.max_bytes)
        elif size < self._offset:
            self._offset = 0
        if size == self._offset:
            return ""
        with self.path.open("rb") as f:
            f.seek(self._offset)
            chunk = f.read(size - self._offset)
        self._offset += len(chunk)
        return chunk.decode("utf-8", errors="replace")


def active_task_id(run_dir: Path, journal_entries: list[dict[str, Any]]) -> str | None:
    """Task whose agent session is currently open: the last session-start
    without a later session-end. Falls back to the newest file in logs/ —
    a tail attached mid-session has no start event in view."""
    active: str | None = None
    for entry in journal_entries:
        kind = entry.get("kind")
        if kind == "session-start" and entry.get("task_id") is not None:
            active = str(entry["task_id"])
        elif kind == "session-end" and str(entry.get("task_id")) == active:
            active = None
    if active is not None:
        return active
    try:
        logs = sorted(
            (run_dir / LOGS_DIR).glob("*.log"),
            key=lambda p: p.stat().st_mtime_ns,
        )
    except OSError:
        return None
    return logs[-1].stem if logs else None


def pending_decision(journal_entries: list[dict[str, Any]]) -> tuple[str, str] | None:
    """(dw_id, question) when the journal's last entry is a decision-pending
    announcement. The sweep prompter blocks on terminal input right after
    writing it, so any subsequent entry of any kind means the prompt was
    answered (or the sweep moved on) and the alert must clear."""
    if not journal_entries:
        return None
    last = journal_entries[-1]
    if last.get("kind") != "decision-pending":
        return None
    return str(last.get("dw_id", "?")), str(last.get("question", ""))


# ------------------------------------------------------------ sprint status


@dataclass(frozen=True)
class SprintSummary:
    total: int
    actionable: int
    by_status: dict[str, int]


# project root -> (config.yaml sig, ProjectPaths)
_paths_cache: dict[Path, tuple[_StatSig, bmadconfig.ProjectPaths]] = {}
# sprint-status.yaml path -> (sig or None for missing, summary or None)
_sprint_cache: dict[Path, tuple[_StatSig | None, SprintSummary | None]] = {}


def sprint_summary(project: Path) -> SprintSummary | None:
    """Story counts from sprint-status.yaml, or None when unavailable
    (uninitialized project, missing file, bad YAML). Stat-gated on both
    config.yaml and the sprint file."""
    project = project.resolve()
    config_sig = _stat_sig(project / "_bmad" / "bmm" / "config.yaml")
    cached_paths = _paths_cache.get(project)
    if config_sig is not None and cached_paths is not None and cached_paths[0] == config_sig:
        paths = cached_paths[1]
    else:
        try:
            paths = bmadconfig.load_paths(project)
        except bmadconfig.BmadConfigError:
            return None
        if config_sig is not None:
            _paths_cache[project] = (config_sig, paths)

    sprint_path = paths.sprint_status
    sig = _stat_sig(sprint_path)
    cached = _sprint_cache.get(sprint_path)
    if cached is not None and cached[0] == sig:
        return cached[1]
    summary: SprintSummary | None = None
    if sig is not None:
        try:
            ss = sprintstatus.load(sprint_path)
        except sprintstatus.SprintStatusError:
            summary = None
        else:
            by_status: dict[str, int] = {}
            for story in ss.stories:
                by_status[story.status] = by_status.get(story.status, 0) + 1
            actionable = sum(
                n for s, n in by_status.items() if s in sprintstatus.ACTIONABLE_STATUSES
            )
            summary = SprintSummary(
                total=len(ss.stories), actionable=actionable, by_status=by_status
            )
    _sprint_cache[sprint_path] = (sig, summary)
    return summary
