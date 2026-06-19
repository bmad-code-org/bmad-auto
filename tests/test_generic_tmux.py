"""GenericTmuxAdapter tests.

Unit tests need no tmux. The integration tests drive a REAL tmux session but
substitute a tiny shell script for the CLI binary: the script writes
result.json and emits hook-style event files itself (canonical event names,
exactly what each CLI's hook registration produces), exercising spawn / env
propagation / hook-signal waiting / kill end-to-end for any profile.
"""

import shutil
import subprocess
import time

import pytest

from automator.adapters.base import SessionSpec
from automator.adapters.generic_tmux import GenericTmuxAdapter
from automator.adapters.profile import get_profile
from automator.policy import LimitsPolicy, Policy

HAVE_TMUX = shutil.which("tmux") is not None

FAKE_CLI = """#!/bin/bash
# fake CLI: last positional arg is the prompt; env comes from tmux -e
prompt="${@: -1}"
ts=$(date +%s%N)
mkdir -p "$BMAD_AUTO_RUN_DIR/events" "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID"
printf '{"ts": %s, "event": "SessionStart", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts" "$BMAD_AUTO_TASK_ID" > "$BMAD_AUTO_RUN_DIR/events/$ts-$BMAD_AUTO_TASK_ID-SessionStart.json"
echo "{\\"workflow\\": \\"quick-dev\\", \\"prompt\\": \\"$prompt\\"}" \\
    > "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json"
ts2=$(( ts + 1 ))
printf '{"ts": %s, "event": "Stop", "task_id": "%s", "session_id": "fake-1"}' \\
    "$ts2" "$BMAD_AUTO_TASK_ID" > "$BMAD_AUTO_RUN_DIR/events/$ts2-$BMAD_AUTO_TASK_ID-Stop.json"
sleep 60  # stay alive like an idle interactive session
"""


def make_adapter(
    tmp_path, profile_name="claude", binary=None, extra_args=None, **policy_kw
) -> GenericTmuxAdapter:
    run_dir = tmp_path / "run"
    policy = Policy(limits=LimitsPolicy(**policy_kw) if policy_kw else LimitsPolicy())
    profile = get_profile(profile_name)
    return GenericTmuxAdapter(
        run_dir=run_dir,
        policy=policy,
        profile=profile,
        binary=binary,
        extra_args=extra_args,
    )


def test_ensure_session_tags_project(tmp_path, monkeypatch):
    """A freshly created agent session is stamped with its project so a cleanup
    in another project never prunes this run."""
    from automator import runs
    from automator.adapters import generic_tmux

    project = tmp_path
    run_dir = project / ".automator" / "runs" / "RID"  # parents[2] == project
    adapter = GenericTmuxAdapter(
        run_dir=run_dir, policy=Policy(limits=LimitsPolicy()), profile=get_profile("claude")
    )

    calls: list[list[str]] = []

    def fake_run(argv, **kwargs):
        calls.append(list(argv))
        rc = 1 if argv[1] == "has-session" else 0  # session missing -> create it
        return subprocess.CompletedProcess(argv, rc, stdout="", stderr="")

    monkeypatch.setattr(generic_tmux.subprocess, "run", fake_run)
    adapter._ensure_session(project)

    assert [c for c in calls if c[1] == "set-option"] == [
        [
            "tmux",
            "set-option",
            "-t",
            adapter.session_name,
            runs.PROJECT_OPTION,
            runs.project_tag(project),
        ]
    ]


def make_spec(tmp_path, task_id="1-1-a-dev-1", timeout_s=30.0, model="sonnet") -> SessionSpec:
    return SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-auto-dev 1-1-a",
        cwd=tmp_path,
        env={"BMAD_AUTO_MODE": "1", "BMAD_AUTO_TASK_ID": task_id},
        model=model,
        timeout_s=timeout_s,
    )


def test_build_command_claude(tmp_path):
    adapter = make_adapter(tmp_path)
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith("claude '/bmad-auto-dev 1-1-a' --permission-mode bypassPermissions")
    assert cmd.endswith("--model sonnet")


def test_build_command_codex_renders_skill_mention(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="codex")
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith(
        "codex 'Use the $bmad-auto-dev skill now, and use subagents as needed: 1-1-a'"
    )
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd
    assert cmd.endswith("--model sonnet")


def test_build_command_gemini_uses_interactive_flag(tmp_path):
    adapter = make_adapter(tmp_path, profile_name="gemini")
    cmd = adapter.build_command(make_spec(tmp_path))
    assert cmd.startswith("gemini -i '/bmad-auto-dev 1-1-a' --approval-mode=yolo")
    assert cmd.endswith("--model sonnet")


def test_extra_args_replace_profile_bypass(tmp_path):
    adapter = make_adapter(tmp_path, extra_args=("--custom-flag",))
    cmd = adapter.build_command(make_spec(tmp_path))
    assert "--custom-flag" in cmd
    assert "bypassPermissions" not in cmd


def test_read_result_variants(tmp_path):
    adapter = make_adapter(tmp_path)
    task_dir = adapter.tasks_dir / "t1"
    task_dir.mkdir(parents=True)
    assert adapter._read_result("t1") is None  # missing
    (task_dir / "result.json").write_text("{broken")
    assert adapter._read_result("t1") is None  # malformed
    (task_dir / "result.json").write_text('["not a dict"]')
    assert adapter._read_result("t1") is None  # wrong shape
    (task_dir / "result.json").write_text('{"clean": true}')
    assert adapter._read_result("t1") == {"clean": True}


def test_await_result_grace_expires_fast(tmp_path):
    adapter = make_adapter(tmp_path)
    (adapter.tasks_dir / "t1").mkdir(parents=True)
    start = time.monotonic()
    assert adapter._await_result("t1", grace_s=0.2) is None
    assert time.monotonic() - start < 5


def _write_fake_cli(tmp_path):
    fake = tmp_path / "fake-cli"
    fake.write_text(FAKE_CLI)
    fake.chmod(0o755)
    return fake


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
@pytest.mark.parametrize("profile_name", ["claude", "codex", "gemini"])
def test_tmux_end_to_end_with_fake_cli(tmp_path, profile_name):
    """Spawn a real tmux window running a fake CLI that behaves like a
    hook-instrumented session: emits SessionStart + result.json + Stop."""
    fake = _write_fake_cli(tmp_path)
    # extra_args=() drops the bypass flags so the rendered prompt is the last argv
    # entry for every profile (claude/codex positional, gemini behind -i).
    adapter = make_adapter(tmp_path, profile_name=profile_name, binary=str(fake), extra_args=())
    spec_env = {
        "BMAD_AUTO_MODE": "1",
        "BMAD_AUTO_RUN_DIR": str(adapter.run_dir),
        "BMAD_AUTO_TASK_ID": "t-int-1",
    }
    spec = SessionSpec(
        task_id="t-int-1",
        role="dev",
        prompt="/bmad-auto-dev 1-1-a",
        cwd=tmp_path,
        env=spec_env,
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "quick-dev"
    # the fake echoes back the rendered prompt it received
    assert result.result_json["prompt"] == adapter.profile.render_prompt(spec.prompt)
    assert result.session_id == "fake-1"
    # canonical prompt recorded for debugging
    assert (adapter.tasks_dir / "t-int-1" / "prompt.txt").read_text().strip() == spec.prompt


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_reused_task_id_ignores_stale_artifacts(tmp_path):
    """A re-armed run reuses the task_id. A prior cycle's Stop event + result.json
    must NOT replay: start_session clears the stale result, and the launch-time
    floor makes wait_for skip the old Stop so only the fresh session counts."""
    fake = _write_fake_cli(tmp_path)
    adapter = make_adapter(tmp_path, binary=str(fake), extra_args=())
    task_id = "t-reused-1"
    # seed last cycle's leftovers, with an obviously old ts and a stale marker
    task_dir = adapter.tasks_dir / task_id
    task_dir.mkdir(parents=True, exist_ok=True)
    (task_dir / "result.json").write_text('{"workflow": "STALE"}', encoding="utf-8")
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"1-{task_id}-Stop.json").write_text(
        '{"ts": 1, "event": "Stop", "task_id": "' + task_id + '", "session_id": "old"}',
        encoding="utf-8",
    )
    spec = SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-auto-dev 1-1-a",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(adapter.run_dir), "BMAD_AUTO_TASK_ID": task_id},
        timeout_s=30.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)

    assert result.status == "completed"
    assert result.result_json["workflow"] == "quick-dev"  # fresh, not "STALE"
    assert result.session_id == "fake-1"  # fresh session, not "old"


@pytest.mark.skipif(not HAVE_TMUX, reason="tmux not available")
def test_tmux_crash_detected(tmp_path):
    """A session that dies without writing result.json -> crashed. Also the
    SessionEnd-less path (codex profile) relies on this window-death check."""
    fake = tmp_path / "fake-cli"
    fake.write_text("#!/bin/bash\nexit 1\n")
    fake.chmod(0o755)

    adapter = make_adapter(
        tmp_path, profile_name="codex", binary=str(fake), stop_without_result_nudges=0
    )
    spec = SessionSpec(
        task_id="t-crash",
        role="dev",
        prompt="x",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(adapter.run_dir), "BMAD_AUTO_TASK_ID": "t-crash"},
        timeout_s=20.0,
    )
    try:
        result = adapter.run(spec)
    finally:
        subprocess.run(["tmux", "kill-session", "-t", adapter.session_name], capture_output=True)
    assert result.status == "crashed"
    assert result.result_json is None
