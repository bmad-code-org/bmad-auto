"""TUI data layer — pure filesystem observation, no textual involved."""

from __future__ import annotations

import builtins
import importlib
import os
import subprocess
from pathlib import Path

from conftest import install_bmad_config, write_sprint

from automator.journal import Journal, save_state
from automator.model import RunState
from automator.runs import RUNS_DIR, write_pid
from automator.tui import data


def make_run(root: Path, run_id: str, **state_kwargs) -> Path:
    run_dir = root / RUNS_DIR / run_id
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="2026-06-11T10:00:00",
        **state_kwargs,
    )
    save_state(run_dir, state)
    return run_dir


def dead_pid() -> int:
    """Pid guaranteed (modulo astronomically unlikely reuse) to be dead."""
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


# ------------------------------------------------------------ no textual dep


def test_data_imports_without_textual(monkeypatch):
    real_import = builtins.__import__

    def guard(name, *args, **kwargs):
        assert not name.startswith("textual"), "data.py must not import textual"
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard)
    importlib.reload(data)


# ----------------------------------------------------------------- discovery


def test_discover_runs_missing_dir(tmp_path):
    assert data.discover_runs(tmp_path) == []


def test_discover_runs_classification(tmp_path):
    make_run(tmp_path, "20260611-100000-aaaa", finished=True)
    make_run(tmp_path, "20260611-110000-bbbb", paused_reason="escalation")
    alive_dir = make_run(tmp_path, "20260611-120000-cccc")
    write_pid(alive_dir)  # test process pid: alive
    gone_dir = make_run(tmp_path, "20260611-130000-dddd", run_type="sweep")
    (gone_dir / "engine.pid").write_text(str(dead_pid()))

    infos = data.discover_runs(tmp_path)
    assert [i.status for i in infos] == [
        data.FINISHED,
        data.PAUSED,
        data.RUNNING,
        data.INTERRUPTED,
    ]
    assert infos[0].started_at == "2026-06-11T10:00:00"
    assert [i.run_type for i in infos] == ["story", "story", "story", "sweep"]
    # statuses re-classify on a second (cached-header) pass
    assert [i.status for i in data.discover_runs(tmp_path)] == [i.status for i in infos]


def test_discover_runs_legacy_no_pid_is_unknown(tmp_path, monkeypatch):
    make_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(data.shutil, "which", lambda _: None)
    assert data.discover_runs(tmp_path)[0].status == data.UNKNOWN


def test_legacy_run_with_live_tmux_session_is_running(tmp_path, monkeypatch):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    monkeypatch.setattr(data.shutil, "which", lambda _: "/usr/bin/tmux")
    calls = []

    def fake_run(argv, **kwargs):
        calls.append(argv)

        class Proc:
            returncode = 0

        return Proc()

    monkeypatch.setattr(data.subprocess, "run", fake_run)
    assert data.discover_runs(tmp_path)[0].status == data.RUNNING
    assert calls[0][:3] == ["tmux", "has-session", "-t"]
    assert calls[0][3] == f"=bmad-auto-{run_dir.name}"


def test_discover_runs_corrupt_state_is_unknown_not_crash(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "state.json").write_text("{ not json")
    infos = data.discover_runs(tmp_path)
    assert [i.status for i in infos] == [data.UNKNOWN]
    assert infos[0].run_id == "20260611-100000-aaaa"


# --------------------------------------------------------------- RunWatcher


def test_watcher_state_keeps_last_good_parse(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa", current_epic=1)
    watcher = data.RunWatcher(run_dir)
    assert watcher.state().current_epic == 1

    (run_dir / "state.json").write_text("{ mid-write garbage")
    assert watcher.state().current_epic == 1  # last good survives

    state = RunState(
        run_id=run_dir.name,
        project=str(tmp_path),
        started_at="2026-06-11T10:00:00",
        current_epic=2,
    )
    save_state(run_dir, state)
    assert watcher.state().current_epic == 2


def test_watcher_state_none_before_first_write(tmp_path):
    watcher = data.RunWatcher(tmp_path / "nope")
    assert watcher.state() is None
    assert watcher.status() == data.UNKNOWN


def test_watcher_status_interrupted(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "engine.pid").write_text(str(dead_pid()))
    watcher = data.RunWatcher(run_dir)
    assert watcher.status() == data.INTERRUPTED
    assert watcher.liveness() == "dead"


def test_watcher_attention(tmp_path):
    run_dir = make_run(tmp_path, "20260611-100000-aaaa")
    watcher = data.RunWatcher(run_dir)
    assert watcher.attention() == ""
    (run_dir / "ATTENTION").write_text("[ts] gate: epic boundary\n")
    assert watcher.attention() == "[ts] gate: epic boundary\n"
    with (run_dir / "ATTENTION").open("a") as f:
        f.write("[ts] escalation: help\n")
    assert watcher.attention().count("\n") == 2


# -------------------------------------------------------------- JournalTail


def test_journal_tail_withholds_partial_line(tmp_path):
    journal = Journal(tmp_path)
    tail = data.JournalTail(tmp_path)
    assert tail.read_new() == []  # no file yet

    journal.append("run-start", run_id="x")
    path = tmp_path / "journal.jsonl"
    with path.open("a") as f:
        f.write('{"ts": 2, "kind": "story-start"')  # flush mid-line, no newline
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]
    assert tail.read_new() == []  # partial still withheld

    with path.open("a") as f:
        f.write(', "story": "1-1-a"}\n')
    entries = tail.read_new()
    assert [e["kind"] for e in entries] == ["story-start"]
    assert entries[0]["story"] == "1-1-a"


def test_journal_tail_resets_on_truncation(tmp_path):
    journal = Journal(tmp_path)
    for i in range(3):
        journal.append("session-start", task_id=f"t{i}")
    tail = data.JournalTail(tmp_path)
    assert len(tail.read_new()) == 3

    (tmp_path / "journal.jsonl").write_text('{"ts": 9, "kind": "run-start"}\n')
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]


def test_journal_tail_skips_unparseable_lines(tmp_path):
    path = tmp_path / "journal.jsonl"
    path.write_text('not json\n{"ts": 1, "kind": "run-start"}\n')
    tail = data.JournalTail(tmp_path)
    assert [e["kind"] for e in tail.read_new()] == ["run-start"]


# ------------------------------------------------------------------ LogTail


def test_log_tail_first_read_seeks_to_tail(tmp_path):
    path = tmp_path / "task.log"
    path.write_bytes(b"x" * 100_000 + b"THE-END")
    tail = data.LogTail(path, max_bytes=1024)
    first = tail.read_new()
    assert len(first) == 1024
    assert first.endswith("THE-END")

    with path.open("ab") as f:
        f.write(b"more output")
    assert tail.read_new() == "more output"
    assert tail.read_new() == ""


def test_log_tail_missing_file_and_truncation(tmp_path):
    path = tmp_path / "task.log"
    tail = data.LogTail(path)
    assert tail.read_new() == ""
    path.write_text("hello ")
    assert tail.read_new() == "hello "
    path.write_text("anew")  # shrank: rewritten log
    assert tail.read_new() == "anew"


# ------------------------------------------------------------ active task id


def test_active_task_id_from_journal(tmp_path):
    entries = [
        {"kind": "session-start", "task_id": "t1"},
        {"kind": "session-end", "task_id": "t1"},
        {"kind": "session-start", "task_id": "t2"},
    ]
    assert data.active_task_id(tmp_path, entries) == "t2"
    entries.append({"kind": "session-end", "task_id": "t2"})
    assert data.active_task_id(tmp_path, entries) is None  # no logs fallback either


def test_active_task_id_falls_back_to_newest_log(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-old.log").write_text("old")
    (logs / "t-new.log").write_text("new")
    os.utime(logs / "t-old.log", ns=(1, 1))
    assert data.active_task_id(tmp_path, []) == "t-new"


# ---------------------------------------------------------- pending decision


def test_pending_decision_last_entry_only():
    assert data.pending_decision([]) is None
    entries = [
        {"kind": "sweep-start"},
        {"kind": "decision-pending", "dw_id": "DW-3", "question": "drop the cache?"},
    ]
    assert data.pending_decision(entries) == ("DW-3", "drop the cache?")
    # any later entry means the blocking prompt was answered
    entries.append({"kind": "decision-answered", "dw_id": "DW-3", "key": "a"})
    assert data.pending_decision(entries) is None


def test_pending_decision_missing_fields():
    assert data.pending_decision([{"kind": "decision-pending"}]) == ("?", "")


# ------------------------------------------------------------- sprint summary


def test_sprint_summary(project):
    install_bmad_config(project)
    write_sprint(
        project,
        {
            "epic-1": "in-progress",
            "1-1-a": "ready-for-dev",
            "1-2-b": "done",
            "1-3-c": "backlog",
        },
    )
    summary = data.sprint_summary(project.project)
    assert summary.total == 3
    assert summary.actionable == 2
    assert summary.by_status == {"ready-for-dev": 1, "done": 1, "backlog": 1}

    # cached result until the file changes, then re-parsed
    assert data.sprint_summary(project.project) is summary
    write_sprint(project, {"1-1-a": "done"})
    assert data.sprint_summary(project.project).total == 1


def test_sprint_summary_unavailable(tmp_path, project):
    assert data.sprint_summary(tmp_path) is None  # no _bmad config at all
    install_bmad_config(project)  # config but no sprint file
    assert data.sprint_summary(project.project) is None
