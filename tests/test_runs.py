"""Run-directory helper tests."""

import os
import re
import subprocess
import tarfile

import pytest

from automator import runs
from automator.adapters import tmux_backend
from automator.journal import load_state, save_state
from automator.model import RunState


def _make_run(project, run_id, with_state=True):
    run_dir = project / ".automator" / "runs" / run_id
    run_dir.mkdir(parents=True)
    if with_state:
        (run_dir / "state.json").write_text("{}")
    return run_dir


def _make_state_run(project, run_id, **state_kwargs):
    run_dir = project / ".automator" / "runs" / run_id
    save_state(
        run_dir,
        RunState(
            run_id=run_id,
            project=str(project),
            started_at="2026-06-11T10:00:00",
            **state_kwargs,
        ),
    )
    return run_dir


def _dead_pid() -> int:
    proc = subprocess.Popen(["true"])
    proc.wait()
    return proc.pid


def test_list_run_dirs_sorted_and_filtered(tmp_path):
    _make_run(tmp_path, "20260611-120000-bbbb")
    _make_run(tmp_path, "20260610-090000-aaaa")
    _make_run(tmp_path, "20260612-080000-cccc", with_state=False)  # no state.json
    listed = runs.list_run_dirs(tmp_path)
    assert [d.name for d in listed] == ["20260610-090000-aaaa", "20260611-120000-bbbb"]


def test_list_run_dirs_missing(tmp_path):
    assert runs.list_run_dirs(tmp_path) == []
    assert runs.latest_run_dir(tmp_path) is None


def test_latest_run_dir(tmp_path):
    _make_run(tmp_path, "20260610-090000-aaaa")
    newest = _make_run(tmp_path, "20260611-120000-bbbb")
    assert runs.latest_run_dir(tmp_path) == newest


def test_new_run_id_format():
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{4}", runs.new_run_id())


def test_write_pid(tmp_path):
    runs.write_pid(tmp_path)
    assert (tmp_path / "engine.pid").read_text() == str(os.getpid())


def test_attach_argv_outside_tmux(monkeypatch):
    monkeypatch.delenv("TMUX", raising=False)
    assert runs.attach_argv("r1") == ["tmux", "attach", "-t", "=bmad-auto-r1"]


def test_attach_argv_inside_tmux(monkeypatch):
    monkeypatch.setenv("TMUX", "/tmp/tmux-1000/default,123,0")
    assert runs.attach_argv("r1") == ["tmux", "switch-client", "-t", "=bmad-auto-r1"]


# --------------------------------------------------------- resolution / liveness


def test_run_dir_for_and_is_run(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.run_dir_for(tmp_path, "r1") == run_dir
    assert runs.is_run(run_dir)
    assert not runs.is_run(tmp_path / ".automator" / "runs" / "nope")


def test_short_ref():
    assert runs.short_ref("20260620-143025-a1b2") == "a1b2"


def test_resolve_run_dir_exact_and_partial(tmp_path):
    target = _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260619-101010-c3d4")
    # exact full id
    assert runs.resolve_run_dir(tmp_path, "20260620-143025-a1b2") == target
    # full trailing segment
    assert runs.resolve_run_dir(tmp_path, "a1b2") == target
    # prefix of the trailing segment
    assert runs.resolve_run_dir(tmp_path, "a1") == target
    # a longer tail of the id (endswith)
    assert runs.resolve_run_dir(tmp_path, "025-a1b2") == target


def test_resolve_run_dir_no_match(tmp_path):
    _make_run(tmp_path, "20260620-143025-a1b2")
    with pytest.raises(runs.RunRefError, match="no such run: zzzz"):
        runs.resolve_run_dir(tmp_path, "zzzz")


def test_resolve_run_dir_ambiguous(tmp_path):
    _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260619-101010-a1c9")
    with pytest.raises(runs.RunRefError, match="ambiguous run ref 'a1' matches 2 runs"):
        runs.resolve_run_dir(tmp_path, "a1")


def test_resolve_run_dir_exact_wins_over_ambiguity(tmp_path):
    # An exact id resolves even when another run's id ends with it (which would
    # otherwise be an ambiguous partial match).
    exact = _make_run(tmp_path, "20260620-143025-a1b2")
    _make_run(tmp_path, "20260101-000000-20260620-143025-a1b2")  # ends with the exact id
    assert runs.resolve_run_dir(tmp_path, "20260620-143025-a1b2") == exact


def test_read_pid_missing_and_garbage(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.read_pid(run_dir) is None
    (run_dir / "engine.pid").write_text("not-a-pid")
    assert runs.read_pid(run_dir) is None
    (run_dir / "engine.pid").write_text("4242")
    assert runs.read_pid(run_dir) == 4242


def test_engine_alive(tmp_path):
    run_dir = _make_run(tmp_path, "r1")
    assert runs.engine_alive(run_dir) is False  # no pid file
    runs.write_pid(run_dir)  # this test process: alive
    assert runs.engine_alive(run_dir) is True
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.engine_alive(run_dir) is False


# ---------------------------------------------------------------- stop / delete


def test_stop_run_already_finished(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1", finished=True)
    assert runs.stop_run(run_dir) is False
    assert load_state(run_dir).stopped is False


def test_stop_run_no_pid_falls_back_to_mark(tmp_path, monkeypatch):
    killed = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    run_dir = _make_state_run(tmp_path, "r1")  # no engine.pid -> legacy/dead
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True
    assert killed == ["r1"]
    journal = (run_dir / "journal.jsonl").read_text()
    assert "run-stop" in journal and '"fallback": true' in journal


def test_stop_run_dead_pid_falls_back(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    (run_dir / "engine.pid").write_text(str(_dead_pid()))
    assert runs.stop_run(run_dir) is True
    assert load_state(run_dir).stopped is True


def test_stop_run_signals_live_process(tmp_path, monkeypatch):
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    proc = subprocess.Popen(["sleep", "30"])
    (run_dir / "engine.pid").write_text(str(proc.pid))
    assert runs.stop_run(run_dir) is True
    # the process received SIGTERM and is gone
    assert proc.poll() is not None or proc.wait(timeout=5) is not None
    assert load_state(run_dir).stopped is True


def test_stop_run_respects_engine_written_stopped(tmp_path, monkeypatch):
    """When a live engine exits having already marked the run stopped, stop_run
    trusts it and does not re-journal a fallback entry."""
    monkeypatch.setattr(runs, "kill_session", lambda _rid: None)
    run_dir = _make_state_run(tmp_path, "r1")
    proc = subprocess.Popen(["sleep", "30"])
    (run_dir / "engine.pid").write_text(str(proc.pid))

    real_kill = os.kill

    def fake_kill(pid, sig):
        # emulate the engine handler marking stopped, then dying on SIGTERM
        if pid == proc.pid and sig != 0:
            st = load_state(run_dir)
            st.stopped = True
            save_state(run_dir, st)
        return real_kill(pid, sig)

    monkeypatch.setattr(runs.os, "kill", fake_kill)
    assert runs.stop_run(run_dir) is True
    proc.wait(timeout=5)
    assert load_state(run_dir).stopped is True
    # trusted the engine: no fallback journal entry written
    journal = run_dir / "journal.jsonl"
    assert not journal.exists() or "fallback" not in journal.read_text()


# ---------------------------------------------------------------- prune sessions


def test_tmux_sessions_no_tmux(monkeypatch):
    # tmux_sessions now delegates to the multiplexer backend; patch its seam.
    monkeypatch.setattr(tmux_backend.shutil, "which", lambda _name: None)
    assert runs.tmux_sessions() == []


def test_tmux_sessions_no_server(monkeypatch):
    monkeypatch.setattr(tmux_backend.shutil, "which", lambda _name: "/usr/bin/tmux")
    monkeypatch.setattr(
        tmux_backend.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 1, stdout="", stderr="no server"),
    )
    assert runs.tmux_sessions() == []


def test_prunable_sessions_partitions(tmp_path, monkeypatch):
    mine = runs.project_tag(tmp_path)
    # live run: real run dir with this process's pid, tagged ours
    live = _make_state_run(tmp_path, "live-1")
    runs.write_pid(live)
    # finished run: run dir exists but dead pid, tagged ours
    finished = _make_state_run(tmp_path, "fin-1")
    (finished / "engine.pid").write_text(str(_dead_pid()))
    # orphan tagged ours: session's run dir is gone -> still prunable
    # untagged finished run: ownership proven by the run dir under this project
    untag_fin = _make_state_run(tmp_path, "untag-fin")
    (untag_fin / "engine.pid").write_text(str(_dead_pid()))

    sessions = [
        "bmad-auto-live-1",
        "bmad-auto-fin-1",
        "bmad-auto-orphan-1",
        "bmad-auto-other-1",  # another project's live run
        "bmad-auto-untag-fin",  # pre-upgrade session, no tag
        "bmad-auto-untag-orphan",  # pre-upgrade, no tag, no run dir here
        "bmad-auto-ctl",  # control session: never a candidate
        "unrelated",  # not ours
    ]
    monkeypatch.setattr(runs, "tmux_sessions", lambda: sessions)
    monkeypatch.setattr(
        runs,
        "session_project_tags",
        lambda: {
            "bmad-auto-live-1": mine,
            "bmad-auto-fin-1": mine,
            "bmad-auto-orphan-1": mine,
            "bmad-auto-other-1": "/some/other/project",
            # untag-* and unrelated intentionally absent (no tag)
        },
    )
    prunable, alive = runs.prunable_sessions(tmp_path)
    # other-1 (foreign tag) and untag-orphan (unprovable) are skipped entirely
    assert sorted(prunable) == ["fin-1", "orphan-1", "untag-fin"]
    assert alive == ["live-1"]


def test_prune_sessions_dry_run_kills_nothing(tmp_path, monkeypatch):
    finished = _make_state_run(tmp_path, "fin-1")
    (finished / "engine.pid").write_text(str(_dead_pid()))
    killed: list[str] = []
    monkeypatch.setattr(runs, "kill_session", lambda rid: killed.append(rid))
    monkeypatch.setattr(runs, "tmux_sessions", lambda: ["bmad-auto-fin-1"])
    monkeypatch.setattr(
        runs, "session_project_tags", lambda: {"bmad-auto-fin-1": runs.project_tag(tmp_path)}
    )
    assert runs.prune_sessions(tmp_path, dry_run=True) == ["fin-1"]
    assert killed == []
    assert runs.prune_sessions(tmp_path) == ["fin-1"]
    assert killed == ["fin-1"]


def test_delete_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "r1")
    runs.delete_run(run_dir)
    assert not run_dir.exists()


def test_archive_run(tmp_path):
    run_dir = _make_state_run(tmp_path, "20260611-100000-aaaa")
    (run_dir / "journal.jsonl").write_text('{"kind":"x"}\n')
    dest = runs.archive_run(tmp_path, run_dir)

    assert dest == tmp_path / ".automator" / "archive" / "20260611-100000-aaaa.tar.gz"
    assert dest.is_file()
    assert not run_dir.exists()  # original removed
    assert not dest.with_suffix(".tar.gz.tmp").exists()  # temp cleaned via replace
    with tarfile.open(dest) as tar:
        names = tar.getnames()
    assert "20260611-100000-aaaa/state.json" in names
    assert "20260611-100000-aaaa/journal.jsonl" in names
