"""tui.launch builds exact tmux/CLI argv — verified against monkeypatched
subprocess so no real tmux server is touched, plus one real-subprocess
sanity check of the captured path."""

from __future__ import annotations

import shlex
import subprocess
import sys
from pathlib import Path

import pytest

from automator.tui import launch


class FakeRun:
    """Records argv; scripts the returncode of `tmux has-session`."""

    def __init__(self, has_session_rc: int = 1):
        self.calls: list[list[str]] = []
        self.has_session_rc = has_session_rc

    def __call__(self, argv, **kwargs):
        self.calls.append(list(argv))
        rc = self.has_session_rc if argv[1] == "has-session" else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    def by_verb(self, verb: str) -> list[list[str]]:
        return [c for c in self.calls if c[1] == verb]


@pytest.fixture
def fake_run(monkeypatch) -> FakeRun:
    fake = FakeRun()
    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    return fake


def expected_cli(*tail: str) -> str:
    return shlex.join([sys.executable, "-m", "automator.cli", *tail])


def test_start_run_detached_argv(fake_run, tmp_path: Path):
    launch.start_run_detached(tmp_path, "RID", epic=2, story="1-2-x", max_stories=3)

    # control session was missing: has-session, then new-session, then new-window
    assert [c[1] for c in fake_run.calls] == [
        "has-session",
        "new-session",
        "new-window",
    ]
    ns = fake_run.by_verb("new-session")[0]
    assert ns == [
        "tmux",
        "new-session",
        "-d",
        "-s",
        "bmad-auto-ctl",
        "-c",
        str(tmp_path),
    ]

    nw = fake_run.by_verb("new-window")[0]
    assert nw[:2] == ["tmux", "new-window"]
    assert "-d" in nw
    assert nw[nw.index("-t") + 1] == "=bmad-auto-ctl:"
    assert nw[nw.index("-n") + 1] == "run-RID"
    assert nw[nw.index("-c") + 1] == str(tmp_path)
    assert nw[-3:-1] == ["sh", "-c"]
    shell = nw[-1]
    assert (
        expected_cli(
            "run",
            "--project",
            str(tmp_path),
            "--run-id",
            "RID",
            "--epic",
            "2",
            "--story",
            "1-2-x",
            "--max-stories",
            "3",
        )
        in shell
    )
    assert "read -r" in shell  # window stays open showing the exit status


def test_start_run_omits_blank_filters(fake_run, tmp_path: Path):
    launch.start_run_detached(tmp_path, "RID")
    shell = fake_run.by_verb("new-window")[0][-1]
    assert expected_cli("run", "--project", str(tmp_path), "--run-id", "RID") in shell
    for flag in ("--epic", "--story", "--max-stories"):
        assert flag not in shell


def test_start_sweep_detached_flags(fake_run, tmp_path: Path):
    launch.start_sweep_detached(tmp_path, "RID", no_prompt=True, decisions_only=True, max_bundles=2)
    nw = fake_run.by_verb("new-window")[0]
    assert nw[nw.index("-n") + 1] == "sweep-RID"
    shell = nw[-1]
    assert (
        expected_cli(
            "sweep",
            "--project",
            str(tmp_path),
            "--run-id",
            "RID",
            "--no-prompt",
            "--decisions-only",
            "--max-bundles",
            "2",
        )
        in shell
    )


def test_resume_detached_argv(fake_run, tmp_path: Path):
    launch.resume_detached(tmp_path, "RID")
    nw = fake_run.by_verb("new-window")[0]
    assert nw[nw.index("-n") + 1] == "resume-RID"
    assert expected_cli("resume", "--project", str(tmp_path), "RID") in nw[-1]


def test_existing_ctl_session_reused(monkeypatch, tmp_path: Path):
    fake = FakeRun(has_session_rc=0)
    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    launch.resume_detached(tmp_path, "RID")
    assert [c[1] for c in fake.calls] == ["has-session", "new-window"]


def test_launch_without_tmux_raises(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    assert not launch.tmux_available()
    with pytest.raises(launch.LaunchError, match="tmux not found"):
        launch.start_run_detached(tmp_path, "RID")


def test_new_window_failure_raises(monkeypatch, tmp_path: Path):
    def failing_run(argv, **kwargs):
        rc = 1 if argv[1] in ("has-session", "new-window") else 0
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="boom")

    monkeypatch.setattr(launch.subprocess, "run", failing_run)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    with pytest.raises(launch.LaunchError, match="new-window failed: boom"):
        launch.start_run_detached(tmp_path, "RID")


def test_session_exists(monkeypatch):
    fake = FakeRun(has_session_rc=0)
    monkeypatch.setattr(launch.subprocess, "run", fake)
    assert launch.session_exists("bmad-auto-x")
    assert fake.calls[0] == ["tmux", "has-session", "-t", "=bmad-auto-x"]


def test_ctl_window_matches_run_id_suffix(monkeypatch):
    def fake(argv, **kwargs):
        out = "run-AAAA\nsweep-RID\nresume-BBBB\n" if argv[1] == "list-windows" else ""
        return subprocess.CompletedProcess(argv, 0, stdout=out, stderr="")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window("RID") == "sweep-RID"
    assert launch.ctl_window("CCCC") is None


def test_ctl_window_no_session_or_tmux(monkeypatch):
    def fake(argv, **kwargs):
        return subprocess.CompletedProcess(argv, 1, stdout="", stderr="no session")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    monkeypatch.setattr(launch.shutil, "which", lambda name: f"/usr/bin/{name}")
    assert launch.ctl_window("RID") is None
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    assert launch.ctl_window("RID") is None  # no subprocess call attempted


def test_select_ctl_window_argv(fake_run):
    launch.select_ctl_window("sweep-RID")
    assert fake_run.calls == [["tmux", "select-window", "-t", "=bmad-auto-ctl:sweep-RID"]]


def test_run_captured_merges_streams(monkeypatch):
    def fake(argv, **kwargs):
        assert argv[:3] == [sys.executable, "-m", "automator.cli"]
        assert argv[3:] == ["validate", "--project", "/p"]
        assert kwargs.get("capture_output") and kwargs.get("text")
        return subprocess.CompletedProcess(argv, 1, stdout="ok line", stderr="FAIL line\n")

    monkeypatch.setattr(launch.subprocess, "run", fake)
    rc, out = launch.run_captured(["validate", "--project", "/p"])
    assert rc == 1
    assert out == "ok line\nFAIL line\n"


def test_run_captured_real_subprocess():
    """End-to-end: the module really is invocable as `python -m automator.cli`."""
    rc, out = launch.run_captured(["--version"])
    assert rc == 0
    assert "bmad-auto" in out
