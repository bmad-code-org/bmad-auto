"""Live psmux end-to-end gates — real psmux + pwsh on a native-Windows host.

Unlike ``test_psmux_backend.py`` (subprocess mocked, runs on every OS), these
drive the *real* ``PsmuxMultiplexer`` against the installed psmux and assert the
two transport behaviors that can only be confirmed on hardware: env injection
reaching a child, and ``kill_session`` actually removing a session. They are
guarded by ``HAVE_PSMUX`` so they skip cleanly (never fail) on Linux/macOS/CI.

Liveness / graceful-stop is deliberately out of scope here — it lives in the
cross-platform ``platform_util``/engine path and is exercised elsewhere; these
gates are transport-only.
"""

from __future__ import annotations

import os
import shutil
import sys

import pytest

from automator.adapters.base import SessionSpec
from automator.adapters.generic import GenericAdapter
from automator.adapters.profile import CLIProfile, HookSpec
from automator.adapters.psmux_backend import PsmuxMultiplexer
from automator.policy import LimitsPolicy, Policy

HAVE_PSMUX = (
    sys.platform == "win32"
    and shutil.which("psmux") is not None
    and shutil.which("tmux") is not None
    and shutil.which("pwsh") is not None
)
_LIVE = pytest.mark.skipif(not HAVE_PSMUX, reason="native Windows + psmux required")

_seq = 0  # deterministic unique session names (no RNG)


def _session_name(tag: str) -> str:
    global _seq
    _seq += 1
    return f"bmadlive_{os.getpid()}_{_seq}_{tag}"


@_LIVE
def test_live_adapter_step_writes_hook_event_via_injected_env(tmp_path):
    mux = PsmuxMultiplexer()
    run_dir = tmp_path / "run"
    task_id = "live-env"
    code = (
        "import json, os, pathlib, time; "
        "run=pathlib.Path(os.environ['BMAD_AUTO_RUN_DIR']); "
        "task=os.environ['BMAD_AUTO_TASK_ID']; "
        "events=run/'events'; events.mkdir(parents=True, exist_ok=True); "
        "result=run/'tasks'/task/'result.json'; result.parent.mkdir(parents=True, exist_ok=True); "
        "result.write_text(json.dumps({'ok': True}), encoding='utf-8'); "
        "ts=time.time_ns(); "
        "(events/(str(ts)+'-'+task+'-Stop.json')).write_text("
        "json.dumps({'ts': ts, 'event': 'Stop', 'task_id': task, 'session_id': 'live'}), "
        "encoding='utf-8')"
    )
    profile = CLIProfile(
        name="fake-live",
        binary=sys.executable,
        hooks=HookSpec(
            dialect="claude-settings-json",
            config_path="settings.json",
            events={"Stop": "Stop"},
        ),
        launch_args=("-c", code),
        bypass_args=(),
    )
    adapter = GenericAdapter(
        run_dir=run_dir,
        policy=Policy(limits=LimitsPolicy()),
        profile=profile,
        mux=mux,
    )
    spec = SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="run",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(run_dir), "BMAD_AUTO_TASK_ID": task_id},
        timeout_s=30.0,
    )
    try:
        handle = adapter.start_session(spec)
        result = adapter.wait_for_completion(handle, spec)
    finally:
        mux.kill_session(adapter.session_name)
    assert result.status == "completed"
    assert result.result_json == {"ok": True}
    assert list((run_dir / "events").glob(f"*-{task_id}-Stop.json"))


@_LIVE
def test_live_kill_session_removes_session(tmp_path):
    """``kill_session`` (plain-name targeting) must actually remove the session —
    ``has_session`` reports it gone afterward."""
    mux = PsmuxMultiplexer()
    name = _session_name("kill")
    mux.new_session(name, tmp_path)
    try:
        assert mux.has_session(name) is True
        mux.kill_session(name)
        assert mux.has_session(name) is False, "kill_session did not remove the session"
    finally:
        mux.kill_session(name)
