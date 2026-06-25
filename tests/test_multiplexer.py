"""Multiplexer-seam proof.

Drives a full ``GenericAdapter`` start/wait cycle against a stub
``TerminalMultiplexer`` with **no tmux on PATH** and the tmux backend's
subprocess seam booby-trapped, proving the adapter never shells out to tmux
directly — every transport op goes through ``self.mux``. Mirrors MockAdapter's
role for the transport axis.
"""

import json

import pytest

from automator.adapters import tmux_backend
from automator.adapters.base import SessionSpec
from automator.adapters.generic import GenericAdapter
from automator.adapters.multiplexer import TerminalMultiplexer
from automator.adapters.profile import get_profile
from automator.policy import LimitsPolicy, Policy


class StubMux(TerminalMultiplexer):
    """Records the transport ops the adapter performs; never touches a real
    multiplexer. Unused ops raise, so the test also pins exactly which ops the
    adapter relies on."""

    def __init__(self):
        self.calls: list[str] = []
        self._sessions: set[str] = set()
        self._windows: dict[str, list[str]] = {}
        self._next = 0

    # ---- ops the adapter uses (record + minimal real behavior)
    def has_session(self, name):
        self.calls.append("has_session")
        return name in self._sessions

    def new_session(self, name, cwd, cols, lines):
        self.calls.append("new_session")
        self._sessions.add(name)
        self._windows[name] = []

    def set_session_option(self, name, option, value):
        self.calls.append("set_session_option")

    def new_window(self, session, name, cwd, env, command):
        self.calls.append("new_window")
        self._next += 1
        win = f"@stub{self._next}"
        self._windows.setdefault(session, []).append(win)
        return win

    def pipe_pane(self, window_id, log_file):
        self.calls.append("pipe_pane")

    def list_window_ids(self, session):
        self.calls.append("list_window_ids")
        return list(self._windows.get(session, []))

    def send_text(self, window_id, text):
        self.calls.append("send_text")

    def kill_window(self, target):
        self.calls.append("kill_window")
        for wins in self._windows.values():
            if target in wins:
                wins.remove(target)

    def available(self):
        return True

    # ---- ops the adapter must NOT touch
    def kill_session(self, name):
        raise AssertionError("adapter must not call kill_session")

    def list_sessions(self):
        raise AssertionError("adapter must not call list_sessions")

    def session_options(self, option):
        raise AssertionError("adapter must not call session_options")

    def new_parked_window(self, session, name, cwd, argv, return_opt):
        raise AssertionError("adapter must not call new_parked_window")

    def list_windows(self, session, fields):
        raise AssertionError("adapter must not call list_windows")

    def window_alive(self, session, window_id):
        raise AssertionError("adapter must not call window_alive")

    def select_window(self, target):
        raise AssertionError("adapter must not call select_window")

    def set_window_option(self, target, option, value):
        raise AssertionError("adapter must not call set_window_option")

    def unset_window_option(self, target, option):
        raise AssertionError("adapter must not call unset_window_option")

    def show_window_option(self, target, option):
        raise AssertionError("adapter must not call show_window_option")

    def attach_target_argv(self, target):
        raise AssertionError("adapter must not call attach_target_argv")

    def current_pane_id(self):
        raise AssertionError("adapter must not call current_pane_id")

    def current_window_id(self):
        raise AssertionError("adapter must not call current_window_id")

    def current_session(self):
        raise AssertionError("adapter must not call current_session")

    def detach_client(self):
        raise AssertionError("adapter must not call detach_client")

    def switch_client(self, target, last_fallback=False):
        raise AssertionError("adapter must not call switch_client")


@pytest.fixture
def no_tmux(monkeypatch):
    """tmux off PATH, and the backend's subprocess seam booby-trapped: any direct
    shell-out to tmux fails the test loudly."""
    monkeypatch.setenv("PATH", "")

    def boom(*a, **k):
        raise AssertionError("GenericAdapter shelled out to tmux directly")

    monkeypatch.setattr(tmux_backend.subprocess, "run", boom)


def _spec(tmp_path):
    task_id = "1-1-dev-1"
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-1",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(tmp_path / "run"), "BMAD_AUTO_TASK_ID": task_id},
        timeout_s=10.0,
    )


def test_generic_adapter_drives_only_the_mux(tmp_path, no_tmux):
    stub = StubMux()
    adapter = GenericAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        mux=stub,
    )
    spec = _spec(tmp_path)

    handle = adapter.start_session(spec)
    assert handle.native_id == "@stub1"
    # session bootstrap + window launch + log tee all went through the mux
    assert stub.calls == [
        "has_session",
        "new_session",
        "set_session_option",
        "new_window",
        "pipe_pane",
    ]

    # Seed a fresh Stop event + result.json (ts above the launch floor) so the
    # wait observes a normal completion without any real process.
    ts = handle.launched_ns + 1
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{spec.task_id}-Stop.json").write_text(
        json.dumps({"ts": ts, "event": "Stop", "task_id": spec.task_id, "session_id": "s1"}),
        encoding="utf-8",
    )
    (adapter.tasks_dir / spec.task_id / "result.json").write_text(
        json.dumps({"workflow": "auto-dev"}), encoding="utf-8"
    )

    result = adapter.wait_for_completion(handle, spec)
    assert result.status == "completed"
    assert result.result_json == {"workflow": "auto-dev"}
    assert result.session_id == "s1"

    adapter.kill(handle)
    assert "kill_window" in stub.calls
