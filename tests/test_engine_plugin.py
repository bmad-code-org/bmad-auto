"""Game-engine plugin loader + the shared-mode readiness gate."""

from __future__ import annotations

import importlib.util
import os

import pytest

from automator.adapters.mock import MockAdapter
from automator.engine import Engine
from automator.engines import EngineError, get_engine, load_engines
from automator.journal import Journal
from automator.model import Phase, RunState, StoryTask, TokenUsage
from automator.policy import EnginePolicy, NotifyPolicy, Policy, ScmPolicy

QUIET = NotifyPolicy(desktop=False, file=True)

MINIMAL_ENGINE = """
name = "myeng"
ready_cmd = 'python3 "{scripts}/probe.py"'
"""


# ----------------------------------------------------------------- loader


def test_builtin_unity_plugin_loads():
    engines = load_engines()
    assert "unity" in engines
    unity = engines["unity"]
    assert "unity_ready.py" in unity.ready_cmd
    assert set(unity.editor_modes) == {"shared", "per_worktree"}
    # scripts_dir points at the bundled plugin dir (for {scripts} substitution)
    assert unity.scripts_dir.replace("\\", "/").endswith("data/engines/unity")


def test_builtin_unity_plugin_declares_per_worktree_hooks():
    unity = get_engine("unity")
    assert "unity_setup.py" in unity.worktree_setup_cmd
    assert "unity_teardown.py" in unity.worktree_teardown_cmd
    # MCP-generated skills are gitignored; seed them into per_worktree checkouts.
    assert ".claude/skills/*" in unity.seed_globs


def test_render_expands_scripts_placeholder():
    unity = get_engine("unity")
    rendered = unity.render(unity.ready_cmd)
    assert "{scripts}" not in rendered
    assert unity.scripts_dir in rendered


def test_project_plugin_overlay(tmp_path):
    eng_dir = tmp_path / ".automator" / "engines" / "myeng"
    eng_dir.mkdir(parents=True)
    (eng_dir / "engine.toml").write_text(MINIMAL_ENGINE)
    engines = load_engines(tmp_path)
    assert "myeng" in engines and "unity" in engines  # overlay extends built-ins
    assert engines["myeng"].scripts_dir == str(eng_dir)
    # {scripts} resolves to the project-local dir
    assert str(eng_dir) in engines["myeng"].render(engines["myeng"].ready_cmd)


def test_unknown_engine_raises():
    with pytest.raises(EngineError, match="unknown engine plugin"):
        get_engine("godot")


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ('ready_cmd = "x"', "name"),  # no name
        ('name = "e"\neditor_modes = ["batch"]', "editor_modes"),
        ('name = "e"\nseed_files = ["/etc/passwd"]', "seed_files"),
        ('name = "e"\nseed_globs = ["/abs/*"]', "seed_globs"),
    ],
)
def test_invalid_engine_rejected(tmp_path, body, match):
    eng_dir = tmp_path / ".automator" / "engines" / "bad"
    eng_dir.mkdir(parents=True)
    (eng_dir / "engine.toml").write_text(body)
    with pytest.raises(EngineError, match=match):
        load_engines(tmp_path)


# -------------------------------------------------------- readiness gate


def make_engine(project, policy, script=None, run_id="test-run"):
    run_dir = project.project / ".automator" / "runs" / run_id
    adapter = MockAdapter(
        script or [], usage_per_session=TokenUsage(input_tokens=1, output_tokens=1)
    )
    state = RunState(run_id=run_id, project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
    )
    return engine, adapter


def _write_plugin(project, name, ready_cmd):
    eng_dir = project.project / ".automator" / "engines" / name
    eng_dir.mkdir(parents=True)
    (eng_dir / "engine.toml").write_text(f'name = "{name}"\nready_cmd = "{ready_cmd}"\n')


def test_gate_noop_when_engine_disabled(project):
    engine, _ = make_engine(project, Policy(notify=QUIET))
    assert engine._engine is None
    assert engine._engine_ready_gate(StoryTask(story_key="1-1-a", epic=1)) is True


def test_gate_passes_when_ready_cmd_succeeds(project):
    _write_plugin(project, "teng", "true")
    pol = Policy(notify=QUIET, engine=EnginePolicy(name="teng", editor_mode="shared"))
    engine, _ = make_engine(project, pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    assert engine._engine_ready_gate(task) is True
    assert task.phase != Phase.DEFERRED
    assert "engine-ready" in [e["kind"] for e in engine.journal.entries()]


def test_gate_defers_when_ready_cmd_fails(project):
    _write_plugin(project, "teng", "exit 7")
    pol = Policy(notify=QUIET, engine=EnginePolicy(name="teng", editor_mode="shared"))
    engine, _ = make_engine(project, pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    assert engine._engine_ready_gate(task) is False
    assert task.phase == Phase.DEFERRED
    assert "not ready" in task.defer_reason
    assert "engine-not-ready" in [e["kind"] for e in engine.journal.entries()]


def test_failed_gate_skips_session_in_run_story(project):
    _write_plugin(project, "teng", "false")
    pol = Policy(notify=QUIET, engine=EnginePolicy(name="teng", editor_mode="shared"))
    engine, adapter = make_engine(project, pol)
    task = StoryTask(story_key="1-1-a", epic=1)
    engine._run_story(task)  # shared mode: gate fails -> defer, never drives
    assert task.phase == Phase.DEFERRED
    assert adapter.sessions == []  # no dev/review session was ever started


# ------------------------------------------------ unity_ready cold-launch grace


def _load_unity_ready():
    path = os.path.join(get_engine("unity").scripts_dir, "unity_ready.py")
    spec = importlib.util.spec_from_file_location("unity_ready_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unity_ready_grace_auto_per_mode(monkeypatch):
    mod = _load_unity_ready()
    monkeypatch.delenv("BMAD_AUTO_ENGINE_READY_GRACE", raising=False)
    # cold per_worktree Editor waits; warm shared Editor does not
    monkeypatch.setenv("BMAD_AUTO_ENGINE_EDITOR_MODE", "per_worktree")
    assert mod._grace() == 120.0
    monkeypatch.setenv("BMAD_AUTO_ENGINE_EDITOR_MODE", "shared")
    assert mod._grace() == 0.0
    # explicit -1 is the same auto path
    monkeypatch.setenv("BMAD_AUTO_ENGINE_READY_GRACE", "-1")
    monkeypatch.setenv("BMAD_AUTO_ENGINE_EDITOR_MODE", "per_worktree")
    assert mod._grace() == 120.0


def _load_unity_teardown():
    path = os.path.join(get_engine("unity").scripts_dir, "unity_teardown.py")
    spec = importlib.util.spec_from_file_location("unity_teardown_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_unity_teardown_lingering_scan_no_false_match(tmp_path):
    """The Editor sweep is scoped to the worktree path + a 'unity' exe basename, so
    a path no Unity process references yields nothing (and never crashes the scan)."""
    mod = _load_unity_teardown()
    # this test process references tmp_path but is python, not a Unity binary
    assert mod._lingering_editor_pids(tmp_path) == []
    assert mod._force_kill_lingering(tmp_path) == 0


def test_unity_ready_grace_explicit_override(monkeypatch):
    mod = _load_unity_ready()
    monkeypatch.setenv("BMAD_AUTO_ENGINE_EDITOR_MODE", "per_worktree")
    monkeypatch.setenv("BMAD_AUTO_ENGINE_READY_GRACE", "30")
    assert mod._grace() == 30.0  # explicit value wins over the per-mode default
    monkeypatch.setenv("BMAD_AUTO_ENGINE_READY_GRACE", "0")
    assert mod._grace() == 0.0  # operator can force no grace even for per_worktree


def test_unsupported_editor_mode_rejected_at_construction(project):
    # a plugin that only supports per_worktree, asked to run shared
    eng_dir = project.project / ".automator" / "engines" / "wonly"
    eng_dir.mkdir(parents=True)
    (eng_dir / "engine.toml").write_text('name = "wonly"\neditor_modes = ["per_worktree"]\n')
    pol = Policy(
        notify=QUIET,
        engine=EnginePolicy(name="wonly", editor_mode="shared"),
        scm=ScmPolicy(isolation="none"),
    )
    with pytest.raises(EngineError, match="does not support editor_mode"):
        make_engine(project, pol)
