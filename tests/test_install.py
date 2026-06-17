import json

from automator.adapters.profile import get_profile
from automator.install import (
    MODULE_SKILLS,
    install_into,
    merge_hooks,
    provision_worktree,
)


def _registrations(profile, command="python3 /x/.automator/bmad_auto_hook.py {event}"):
    return {
        native: command.format(event=canonical)
        for native, canonical in profile.hooks.events.items()
    }


def test_merge_hooks_adds_all_events():
    profile = get_profile("claude")
    settings, changed = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert set(profile.hooks.events) <= set(settings["hooks"])


def test_merge_hooks_idempotent():
    profile = get_profile("claude")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    again, changed = merge_hooks(settings, _registrations(profile), profile.hooks.dialect)
    assert not changed
    for event in profile.hooks.events:
        assert len(again["hooks"][event]) == 1


def test_merge_hooks_preserves_existing():
    profile = get_profile("claude")
    existing = {
        "hooks": {"Stop": [{"hooks": [{"type": "command", "command": "echo hi"}]}]},
        "permissions": {"allow": ["Bash(ls)"]},
    }
    settings, changed = merge_hooks(existing, _registrations(profile), profile.hooks.dialect)
    assert changed
    assert settings["permissions"] == {"allow": ["Bash(ls)"]}
    commands = [
        handler["command"] for matcher in settings["hooks"]["Stop"] for handler in matcher["hooks"]
    ]
    assert "echo hi" in commands
    assert any("bmad_auto_hook" in c for c in commands)


def test_merge_hooks_gemini_entry_shape():
    profile = get_profile("gemini")
    settings, _ = merge_hooks({}, _registrations(profile), profile.hooks.dialect)
    entry = settings["hooks"]["AfterAgent"][0]
    assert entry["matcher"] == ""
    handler = entry["hooks"][0]
    assert handler["timeout"] == 60_000  # Gemini hook timeouts are milliseconds
    # registered under the native event but relaying the canonical name
    assert handler["command"].endswith("bmad_auto_hook.py Stop")


def test_install_into_full(tmp_path):
    assert install_into(tmp_path) == 0
    assert (tmp_path / ".automator" / "bmad_auto_hook.py").is_file()
    assert (tmp_path / ".automator" / "policy.toml").is_file()
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert "Stop" in settings["hooks"]
    assert ".automator/runs/" in (tmp_path / ".gitignore").read_text()

    # all four skills land in claude's tree, with nested files intact
    skills_dir = tmp_path / ".claude" / "skills"
    for skill in MODULE_SKILLS:
        assert (skills_dir / skill / "SKILL.md").is_file()
    assert (skills_dir / "bmad-auto-review" / "steps" / "step-01-gather-context.md").is_file()

    # second run: idempotent, does not duplicate
    assert install_into(tmp_path) == 0
    settings = json.loads((tmp_path / ".claude" / "settings.json").read_text())
    assert len(settings["hooks"]["Stop"]) == 1
    assert (tmp_path / ".gitignore").read_text().count(".automator/runs/") == 1


def test_install_into_multiple_clis(tmp_path):
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0

    codex_hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert set(codex_hooks["hooks"]) == {"SessionStart", "Stop"}
    cmd = codex_hooks["hooks"]["Stop"][0]["hooks"][0]["command"]
    # absolute path (no $CLAUDE_PROJECT_DIR equivalent in codex/gemini)
    assert str(tmp_path.resolve()) in cmd and cmd.endswith(" Stop")

    gemini_settings = json.loads((tmp_path / ".gemini" / "settings.json").read_text())
    assert set(gemini_settings["hooks"]) == {"SessionStart", "AfterAgent", "SessionEnd"}

    # idempotent across both
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0
    codex_hooks = json.loads((tmp_path / ".codex" / "hooks.json").read_text())
    assert len(codex_hooks["hooks"]["Stop"]) == 1


def test_install_skills_dedupes_agents_tree(tmp_path):
    # codex and gemini share .agents/skills — install once there, not under .claude
    assert install_into(tmp_path, clis=("codex", "gemini")) == 0
    for skill in MODULE_SKILLS:
        assert (tmp_path / ".agents" / "skills" / skill / "SKILL.md").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_install_skills_skip_existing(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-auto-dev" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    # default run must not clobber an existing skill dir
    assert install_into(tmp_path) == 0
    assert skill_md.read_text() == "CUSTOM"
    # but a skill that was absent still gets installed
    assert (tmp_path / ".claude" / "skills" / "bmad-auto-review" / "SKILL.md").is_file()


def test_install_skills_force(tmp_path):
    skill_md = tmp_path / ".claude" / "skills" / "bmad-auto-dev" / "SKILL.md"
    skill_md.parent.mkdir(parents=True)
    skill_md.write_text("CUSTOM", encoding="utf-8")
    assert install_into(tmp_path, force_skills=True) == 0
    assert skill_md.read_text() != "CUSTOM"


def test_install_no_skills(tmp_path):
    assert install_into(tmp_path, skills=False) == 0
    # hooks still installed, but no skill tree created
    assert (tmp_path / ".claude" / "settings.json").is_file()
    assert not (tmp_path / ".claude" / "skills").exists()


def test_install_unknown_cli(tmp_path):
    assert install_into(tmp_path, clis=("acme-cli",)) == 1
    assert not (tmp_path / ".automator").exists()


def test_install_resolves_legacy_alias(tmp_path):
    assert install_into(tmp_path, clis=("claude-code-tmux",)) == 0
    assert (tmp_path / ".claude" / "settings.json").is_file()


def test_provision_worktree_lays_down_skills_and_hook(tmp_path):
    """A worktree must receive the bmad-auto-* skills + signal hook even though
    those dirs are gitignored (absent from a fresh checkout), or the session
    can't find /bmad-auto-dev and the Stop hook never fires."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    provision_worktree(wt, [claude], repo)

    # skills installed into the claude skill tree
    for skill in MODULE_SKILLS:
        assert (wt / claude.skill_tree / skill / "SKILL.md").is_file()
    # hook registered, baked to the MAIN repo's relay (absolute) — nothing written
    # into the worktree's .automator/ (which a project may not gitignore)
    settings = json.loads((wt / claude.hooks.config_path).read_text())
    assert set(claude.hooks.events) <= set(settings["hooks"])
    cmd = settings["hooks"]["Stop"][0]["hooks"][0]["command"]
    assert str((repo / ".automator" / "bmad_auto_hook.py")) in cmd
    assert not (wt / ".automator").exists()


def test_provision_worktree_covers_multiple_profiles(tmp_path):
    """Dev=claude + review=codex provisions both skill trees (.claude/skills and
    .agents/skills) and both hook configs."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude, codex = get_profile("claude"), get_profile("codex")
    provision_worktree(wt, [claude, codex], repo)

    assert (wt / claude.skill_tree / "bmad-auto-dev" / "SKILL.md").is_file()
    assert (wt / codex.skill_tree / "bmad-auto-dev" / "SKILL.md").is_file()
    assert (wt / claude.hooks.config_path).is_file()
    assert (wt / codex.hooks.config_path).is_file()


def test_provision_worktree_does_not_clobber_existing_skill(tmp_path):
    """A skill the checkout already carries (project commits its own skill tree)
    is left untouched, so no diff is merged back."""
    wt, repo = tmp_path / "wt", tmp_path / "repo"
    claude = get_profile("claude")
    existing = wt / claude.skill_tree / "bmad-auto-dev" / "SKILL.md"
    existing.parent.mkdir(parents=True)
    existing.write_text("COMMITTED", encoding="utf-8")

    provision_worktree(wt, [claude], repo)
    assert existing.read_text() == "COMMITTED"
    # a skill that was absent is still laid down
    assert (wt / claude.skill_tree / "bmad-auto-review" / "SKILL.md").is_file()


def test_provision_worktree_empty_profiles_is_noop(tmp_path):
    provision_worktree(tmp_path / "wt", [], tmp_path / "repo")
    assert not (tmp_path / "wt").exists()
