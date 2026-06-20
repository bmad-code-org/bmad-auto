"""Game-engine plugin loader + the shared-mode readiness gate."""

from __future__ import annotations

import importlib.util
import json
import os
import time
import types

import pytest

from automator.adapters.mock import MockAdapter
from automator.engine import Engine, _setup_mcp_agent_id
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
    """The sweep is scoped to the worktree path + a 'unity'/'gamedev-mcp-server' exe
    basename, so a path no such process references yields nothing (never crashing)."""
    mod = _load_unity_teardown()
    assert mod._TARGET_BASENAMES == ("unity", "gamedev-mcp-server")
    # this test process references tmp_path but is python, not a target binary
    assert mod._lingering_pids(tmp_path) == []
    assert mod._force_kill_lingering(tmp_path) == 0


def test_unity_ready_grace_explicit_override(monkeypatch):
    mod = _load_unity_ready()
    monkeypatch.setenv("BMAD_AUTO_ENGINE_EDITOR_MODE", "per_worktree")
    monkeypatch.setenv("BMAD_AUTO_ENGINE_READY_GRACE", "30")
    assert mod._grace() == 30.0  # explicit value wins over the per-mode default
    monkeypatch.setenv("BMAD_AUTO_ENGINE_READY_GRACE", "0")
    assert mod._grace() == 0.0  # operator can force no grace even for per_worktree


# --------------------------------- unity_ready client-independent round-trip


def _fake_cli(tmp_path, *, wait_rc=0, tool_rc=0):
    """A fake unity-mcp-cli that logs argv and returns per-subcommand exit codes."""
    script = tmp_path / "fake-unity-mcp-cli"
    log = tmp_path / "calls.log"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        f"sys.exit({wait_rc} if cmd == 'wait-for-ready' "
        f"else ({tool_rc} if cmd == 'run-tool' else 0))\n"
    )
    script.chmod(0o755)
    return script, log


def test_unity_ready_default_is_wait_for_ready_only(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, log = _fake_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_WORKTREE", str(tmp_path))
    monkeypatch.delenv("BMAD_AUTO_UNITY_READY_TOOL", raising=False)
    # default: the Editor hosts its own server, so wait-for-ready alone gates it
    assert mod._ready_ivanmurzak(time.monotonic() + 10) == 0
    calls = log.read_text()
    assert "wait-for-ready" in calls
    assert "run-tool" not in calls  # round-trip is opt-in


def test_unity_ready_optional_tool_roundtrip(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, log = _fake_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_AUTO_UNITY_READY_TOOL", "ping")  # opt in
    assert mod._ready_ivanmurzak(time.monotonic() + 10) == 0
    assert "run-tool ping" in log.read_text()


def test_unity_ready_tool_error_marker_means_not_ready(tmp_path, monkeypatch):
    """A run-tool that exits 0 but prints an error marker (the CLI returns 0 on a
    connection-refused) is treated as not-ready, not a false pass."""
    mod = _load_unity_ready()
    # exit 0 but stdout carries an error marker
    script = tmp_path / "fake-cli"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
        "print('ERROR: Tool with Name not found' if cmd == 'run-tool' else 'ok')\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_AUTO_UNITY_READY_TOOL", "ping")
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)
    assert mod._ready_ivanmurzak(time.monotonic() + 0.2) == 1


def test_unity_ready_not_ready_when_tool_never_answers(tmp_path, monkeypatch):
    mod = _load_unity_ready()
    script, _ = _fake_cli(tmp_path, wait_rc=0, tool_rc=1)  # bridge up, tool failing
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_WORKTREE", str(tmp_path))
    monkeypatch.setenv("BMAD_AUTO_UNITY_READY_TOOL", "ping")  # opt in to exercise the loop
    monkeypatch.setattr(mod.time, "sleep", lambda *_: None)  # don't burn the retry pause
    assert mod._ready_ivanmurzak(time.monotonic() + 0.2) == 1


# ------------------------------------------ unity_setup custom/local-mode launch


def _load_unity_setup():
    path = os.path.join(get_engine("unity").scripts_dir, "unity_setup.py")
    spec = importlib.util.spec_from_file_location("unity_setup_under_test", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


_SETUP_KNOBS = (
    "BMAD_AUTO_UNITY_MCP_LOCAL",
    "BMAD_AUTO_UNITY_MCP_URL",
    "BMAD_AUTO_UNITY_MCP_TOKEN",
    "BMAD_AUTO_UNITY_MCP_TRANSPORT",
    "BMAD_AUTO_UNITY_MCP_AUTH",
    "BMAD_AUTO_UNITY_MCP_START_SERVER",
    "BMAD_AUTO_UNITY_MCP_KEEP_CONNECTED",
    "BMAD_AUTO_UNITY_PATH",
    "BMAD_AUTO_UNITY_LIBRARY_SEED",
    "BMAD_AUTO_UNITY_LIBRARY_SEED_MODE",
    "BMAD_AUTO_UNITY_LIBRARY_CACHE",
    "BMAD_AUTO_REPO_ROOT",
)


def _clear_setup_knobs(monkeypatch):
    for knob in _SETUP_KNOBS:
        monkeypatch.delenv(knob, raising=False)


def test_unity_setup_local_url_from_mcp_json(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    (tmp_path / ".mcp.json").write_text(
        json.dumps(
            {"mcpServers": {"ai-game-developer": {"type": "http", "url": "http://localhost:28536"}}}
        )
    )
    assert mod._local_url(tmp_path) == "http://localhost:28536"
    # explicit override wins over the file
    monkeypatch.setenv("BMAD_AUTO_UNITY_MCP_URL", "http://localhost:9999")
    assert mod._local_url(tmp_path) == "http://localhost:9999"


def test_unity_setup_local_url_missing_returns_none(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    assert mod._local_url(tmp_path) is None  # no .mcp.json, no override


def test_unity_setup_open_command_local_defaults(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    cmd = mod._open_command("ucli", tmp_path, "http://localhost:1234")
    assert cmd[:3] == ["ucli", "open", str(tmp_path)]
    assert cmd[cmd.index("--url") + 1] == "http://localhost:1234"
    assert cmd[cmd.index("--transport") + 1] == "streamableHttp"
    assert cmd[cmd.index("--auth") + 1] == "none"
    assert cmd[cmd.index("--start-server") + 1] == "true"
    assert "--keep-connected" in cmd
    assert "--token" not in cmd  # empty default token is omitted


def test_unity_setup_open_command_cloud_fallback(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    # no derivable URL → bare open (project's persisted/cloud config)
    assert mod._open_command("ucli", tmp_path, None) == ["ucli", "open", str(tmp_path)]
    # explicit opt-out even with a URL available
    monkeypatch.setenv("BMAD_AUTO_UNITY_MCP_LOCAL", "0")
    assert "--url" not in mod._open_command("ucli", tmp_path, "http://localhost:1")


def test_unity_setup_open_command_overrides(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    monkeypatch.setenv("BMAD_AUTO_UNITY_PATH", "/opt/Unity/Editor")
    monkeypatch.setenv("BMAD_AUTO_UNITY_MCP_TOKEN", "secret")
    monkeypatch.setenv("BMAD_AUTO_UNITY_MCP_START_SERVER", "false")
    monkeypatch.setenv("BMAD_AUTO_UNITY_MCP_KEEP_CONNECTED", "false")
    cmd = mod._open_command("ucli", tmp_path, "http://localhost:1")
    assert cmd[cmd.index("--start-server") + 1] == "false"
    assert "--keep-connected" not in cmd
    assert cmd[cmd.index("--token") + 1] == "secret"
    assert cmd[cmd.index("--editor-path") + 1] == "/opt/Unity/Editor"


def test_unity_setup_truthy(monkeypatch):
    mod = _load_unity_setup()
    assert mod._truthy(None, True) is True
    assert mod._truthy("", False) is False
    assert mod._truthy("0", True) is False
    assert mod._truthy("yes", False) is True
    assert mod._truthy("  True ", False) is True


# ------------------------------------------ unity_setup Library priming


def _warm_library(root):
    """A fake warm main Library: a couple of cache files plus the volatile cruft a
    primed copy must strip (per-Editor identity, a lock, a pid)."""
    lib = root / "Library"
    (lib / "Artifacts").mkdir(parents=True)
    (lib / "Artifacts" / "ab").write_text("artifact")
    (lib / "ArtifactDB").write_text("db")
    (lib / "EditorInstance.json").write_text('{"process_id":123}')
    (lib / "ArtifactDB-lock").write_text("lock")
    (lib / "ilpp.pid").write_text("123")
    return lib


def test_unity_setup_prime_reflink_from_warm_library(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    lib = wt / "Library"
    assert not lib.is_symlink()  # a real (primed) directory, not a symlink
    assert (lib / "ArtifactDB").read_text() == "db"  # warm cache copied in
    assert (lib / "Artifacts" / "ab").read_text() == "artifact"
    # volatile per-Editor identity / locks / pids stripped from the copy
    assert not (lib / "EditorInstance.json").exists()
    assert not (lib / "ArtifactDB-lock").exists()
    assert not (lib / "ilpp.pid").exists()


def test_unity_setup_prime_leaves_substantive_library(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    (wt / "Library" / "Artifacts").mkdir(parents=True)
    (wt / "Library" / "existing").write_text("keep")
    mod._prime_library(wt)
    # a genuine Library is never clobbered
    assert (wt / "Library" / "existing").read_text() == "keep"
    assert not (wt / "Library" / "ArtifactDB").exists()  # seed not copied over it


def test_unity_setup_prime_scriptassemblies_only_is_cold(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    (wt / "Library" / "ScriptAssemblies").mkdir(parents=True)  # cold leftover only
    mod._prime_library(wt)
    assert (wt / "Library" / "ArtifactDB").read_text() == "db"  # primed over leftover


def test_unity_setup_prime_symlink_fallback_when_no_seed(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", "")  # disable priming
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_CACHE", str(tmp_path / "cache"))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    assert (wt / "Library").is_symlink()  # fell back to the empty-cache symlink


def test_unity_setup_prime_seed_mode_off_uses_symlink(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", str(seed))
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED_MODE", "off")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_CACHE", str(tmp_path / "cache"))
    wt = tmp_path / "wt"
    wt.mkdir()
    mod._prime_library(wt)
    assert (wt / "Library").is_symlink()  # priming disabled despite a warm seed


def test_unity_setup_prime_drops_stale_symlink(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    seed = _warm_library(tmp_path / "main")
    monkeypatch.setenv("BMAD_AUTO_UNITY_LIBRARY_SEED", str(seed))
    wt = tmp_path / "wt"
    wt.mkdir()
    (wt / "Library").symlink_to(tmp_path / "old-cache")  # stale symlink-mode link
    mod._prime_library(wt)
    assert not (wt / "Library").is_symlink()  # replaced with a primed real dir
    assert (wt / "Library" / "ArtifactDB").read_text() == "db"


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


# ------------------------------------ per_worktree MCP agent routing (the leak fix)


def test_setup_mcp_agent_id_mapping():
    # only claude carries the "-code" suffix; everything else passes through
    assert _setup_mcp_agent_id("claude") == "claude-code"
    assert _setup_mcp_agent_id("codex") == "codex"
    assert _setup_mcp_agent_id("gemini") == "gemini"
    assert _setup_mcp_agent_id("cursor") == "cursor"
    assert _setup_mcp_agent_id("some-custom-profile") == "some-custom-profile"


class _FakeProfile:
    def __init__(self, name):
        self.name = name


class _FakeAdapter:
    def __init__(self, name):
        self.profile = _FakeProfile(name)


def test_engine_agent_ids_dedups_dev_and_review(project):
    # a worktree can host two different CLIs (dev=claude, review=codex) — both must
    # be routed, deduped and order-preserved
    engine, _ = make_engine(project, Policy(notify=QUIET))
    engine.adapters = {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("codex")}
    assert engine._engine_agent_ids() == ["claude-code", "codex"]
    engine.adapters = {"dev": _FakeAdapter("codex"), "review": _FakeAdapter("codex")}
    assert engine._engine_agent_ids() == ["codex"]


def test_engine_agent_ids_empty_for_profileless_adapters(project):
    # MockAdapter has no .profile -> nothing to route (env var omitted)
    engine, _ = make_engine(project, Policy(notify=QUIET))
    assert engine._engine_agent_ids() == []


def _capture_hook_env(engine, monkeypatch):
    import automator.engine as eng_mod

    captured = {}

    def fake_run(cmd, **kw):
        captured["env"] = kw["env"]
        return types.SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(eng_mod.subprocess, "run", fake_run)
    engine._run_engine_hook("true", StoryTask(story_key="1-1-a", epic=1))
    return captured["env"]


def test_run_engine_hook_injects_agents_env(project, monkeypatch):
    engine, _ = make_engine(project, Policy(notify=QUIET))
    engine.adapters = {"dev": _FakeAdapter("claude"), "review": _FakeAdapter("codex")}
    env = _capture_hook_env(engine, monkeypatch)
    assert env["BMAD_AUTO_ENGINE_AGENTS"] == "claude-code,codex"


def test_run_engine_hook_omits_agents_env_when_no_profiles(project, monkeypatch):
    engine, _ = make_engine(project, Policy(notify=QUIET))  # MockAdapter, no profile
    env = _capture_hook_env(engine, monkeypatch)
    assert "BMAD_AUTO_ENGINE_AGENTS" not in env


def test_unity_setup_engine_agent_ids_env_parsing(monkeypatch):
    mod = _load_unity_setup()
    monkeypatch.delenv("BMAD_AUTO_ENGINE_AGENTS", raising=False)
    monkeypatch.delenv("BMAD_AUTO_ENGINE_AGENT", raising=False)
    assert mod._engine_agent_ids() == ["claude-code"]  # default
    monkeypatch.setenv("BMAD_AUTO_ENGINE_AGENT", "codex")  # legacy singular fallback
    assert mod._engine_agent_ids() == ["codex"]
    monkeypatch.setenv("BMAD_AUTO_ENGINE_AGENTS", "claude-code, codex ,claude-code")
    assert mod._engine_agent_ids() == ["claude-code", "codex"]  # strip + dedup, plural wins


def test_verify_agent_isolation(tmp_path):
    mod = _load_unity_setup()
    url = "http://localhost:23723"
    cdir = tmp_path / ".codex"
    cdir.mkdir()
    # config points at the worktree's port -> isolated
    (cdir / "config.toml").write_text(
        '[mcp_servers.ai-game-developer]\nurl = "http://localhost:23723"\n'
    )
    assert mod._verify_agent_isolation("codex", tmp_path, url) is True
    # config still points at a leaked main-project port -> refuse
    (cdir / "config.toml").write_text(
        '[mcp_servers.ai-game-developer]\nurl = "http://localhost:23191"\n'
    )
    assert mod._verify_agent_isolation("codex", tmp_path, url) is False
    # config missing -> can't guarantee isolation -> refuse
    assert mod._verify_agent_isolation("gemini", tmp_path, url) is False
    # agent without a worktree-local config path -> not verifiable here -> pass through
    assert mod._verify_agent_isolation("claude-desktop", tmp_path, url) is True


def _fake_setup_cli(tmp_path):
    """Fake unity-mcp-cli for the setup hook: setup-mcp writes claude-code's
    .mcp.json (path-derived 23723) and codex's .codex/config.toml (honoring --url,
    or FAKE_CODEX_FORCE_URL to simulate a leaked port); other subcommands no-op 0."""
    script = tmp_path / "fake-setup-cli"
    log = tmp_path / "setup-calls.log"
    script.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, os, json\n"
        f"open({str(log)!r}, 'a').write(' '.join(sys.argv[1:]) + chr(10))\n"
        "a = sys.argv[1:]\n"
        "if a[:1] == ['setup-mcp']:\n"
        "    agent, path = a[1], a[2]\n"
        "    url = a[a.index('--url') + 1] if '--url' in a else 'http://localhost:23723'\n"
        "    if agent == 'claude-code':\n"
        "        json.dump({'mcpServers': {'ai-game-developer': {'url': 'http://localhost:23723'}}},\n"
        "                  open(os.path.join(path, '.mcp.json'), 'w'))\n"
        "    elif agent == 'codex':\n"
        "        u = os.environ.get('FAKE_CODEX_FORCE_URL') or url\n"
        "        d = os.path.join(path, '.codex'); os.makedirs(d, exist_ok=True)\n"
        "        open(os.path.join(d, 'config.toml'), 'w').write(\n"
        "            '[mcp_servers.ai-game-developer]\\nurl = \"%s\"\\n' % u)\n"
        "sys.exit(0)\n"
    )
    script.chmod(0o755)
    return script, log


def test_unity_setup_configures_every_agent_at_worktree_port(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    script, log = _fake_setup_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_ENGINE_AGENTS", "claude-code,codex")
    monkeypatch.delenv("FAKE_CODEX_FORCE_URL", raising=False)
    assert mod._setup_ivanmurzak(tmp_path) == 0
    calls = log.read_text()
    assert calls.count("setup-mcp claude-code") == 1  # written once, no duplicate
    assert "setup-mcp codex" in calls
    assert "--url http://localhost:23723" in calls  # codex forced to the worktree url
    # codex's real config now points at the worktree Editor, not a leaked port
    assert "23723" in (tmp_path / ".codex" / "config.toml").read_text()


def test_unity_setup_fails_loud_when_agent_config_leaks(tmp_path, monkeypatch):
    mod = _load_unity_setup()
    _clear_setup_knobs(monkeypatch)
    script, _ = _fake_setup_cli(tmp_path)
    monkeypatch.setenv("UNITY_MCP_CLI", str(script))
    monkeypatch.setenv("BMAD_AUTO_ENGINE_AGENTS", "claude-code,codex")
    # simulate the bug: codex config still pinned to the main project's port
    monkeypatch.setenv("FAKE_CODEX_FORCE_URL", "http://localhost:23191")
    # isolation check rejects -> non-zero so the engine defers the unit (no work)
    assert mod._setup_ivanmurzak(tmp_path) == 1
