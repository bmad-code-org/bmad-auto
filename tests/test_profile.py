import pytest

from automator.adapters.profile import (
    ProfileError,
    get_profile,
    load_profiles,
)

MINIMAL_PROFILE = """
name = "mycli"
binary = "mycli"
bypass_args = ["--yes"]

[hooks]
dialect = "claude-settings-json"
config_path = ".mycli/settings.json"
events = { SessionStart = "SessionStart", Stop = "Stop" }
"""


def test_builtin_profiles_load():
    profiles = load_profiles()
    assert {"claude", "codex", "gemini"} <= set(profiles)
    assert profiles["claude"].usage_parser == "claude-jsonl"
    assert profiles["codex"].hooks.dialect == "codex-hooks-json"
    assert "SessionEnd" not in profiles["codex"].hooks.events  # codex has no such hook
    assert profiles["gemini"].hooks.events["AfterAgent"] == "Stop"
    assert profiles["gemini"].launch_args == ("-i",)
    # claude reads .claude/skills; codex and gemini read .agents/skills
    assert profiles["claude"].skill_tree == ".claude/skills"
    assert profiles["codex"].skill_tree == ".agents/skills"
    assert profiles["gemini"].skill_tree == ".agents/skills"
    # each profile carries the gitignored configs a worktree checkout omits
    assert ".mcp.json" in profiles["claude"].seed_files
    assert ".claude/settings.json" in profiles["claude"].seed_files
    assert profiles["codex"].seed_files == (".codex/config.toml",)
    assert profiles["gemini"].seed_files == (".gemini/settings.json",)


def test_seed_files_default_empty_when_unset(tmp_path):
    # MINIMAL_PROFILE omits seed_files -> defaults to ()
    profiles_dir = tmp_path / ".automator" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    assert load_profiles(tmp_path)["mycli"].seed_files == ()


def test_skill_tree_defaults_when_unset():
    # MINIMAL_PROFILE omits skill_tree -> defaults to .claude/skills
    assert get_profile("claude").skill_tree == ".claude/skills"


def test_legacy_alias_resolves():
    assert get_profile("claude-code-tmux").name == "claude"


def test_unknown_profile_raises():
    with pytest.raises(ProfileError, match="unknown CLI profile"):
        get_profile("acme-cli")


def test_render_prompt_passthrough_and_template():
    claude = get_profile("claude")
    assert claude.render_prompt("/bmad-auto-dev 1-1-a") == "/bmad-auto-dev 1-1-a"
    codex = get_profile("codex")
    assert codex.render_prompt("/bmad-auto-dev 1-1-a") == (
        "Use the $bmad-auto-dev skill now, and use subagents as needed: 1-1-a"
    )
    # non-slash prompts pass through {prompt}; {skill}/{args} degrade gracefully
    assert claude.render_prompt("just do it") == "just do it"


def test_user_profile_overlay(tmp_path):
    profiles_dir = tmp_path / ".automator" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "mycli.toml").write_text(MINIMAL_PROFILE)
    # override a built-in by reusing its name
    (profiles_dir / "claude-override.toml").write_text(
        MINIMAL_PROFILE.replace('name = "mycli"', 'name = "claude"')
    )
    profiles = load_profiles(tmp_path)
    assert "mycli" in profiles
    assert profiles["mycli"].bypass_args == ("--yes",)
    assert profiles["claude"].binary == "mycli"  # overridden
    assert "codex" in profiles  # built-ins still present


@pytest.mark.parametrize(
    ("mutation", "match"),
    [
        ('name = "mycli"\nbinary = "mycli"', "missing"),  # no [hooks]
        (
            MINIMAL_PROFILE.replace('dialect = "claude-settings-json"', 'dialect = "nope"'),
            "dialect",
        ),
        (MINIMAL_PROFILE.replace('Stop = "Stop"', 'Stop = "TurnDone"'), "canonical"),
        (
            MINIMAL_PROFILE.replace("[hooks]", 'usage_parser = "magic"\n[hooks]'),
            "usage_parser",
        ),
        (
            MINIMAL_PROFILE.replace(
                'config_path = ".mycli/settings.json"',
                'config_path = "/abs/settings.json"',
            ),
            "relative",
        ),
        (
            MINIMAL_PROFILE.replace(
                "[hooks]",
                'skill_tree = "/abs/skills"\n[hooks]',
            ),
            "skill_tree",
        ),
        (
            MINIMAL_PROFILE.replace(
                "[hooks]",
                'seed_files = ["/etc/passwd"]\n[hooks]',
            ),
            "seed_files",
        ),
    ],
)
def test_invalid_profiles_rejected(tmp_path, mutation, match):
    profiles_dir = tmp_path / ".automator" / "profiles"
    profiles_dir.mkdir(parents=True)
    (profiles_dir / "bad.toml").write_text(mutation)
    with pytest.raises(ProfileError, match=match):
        load_profiles(tmp_path)
