"""PolicyDoc edit-model semantics (comment preservation, unset-vs-empty,
stage-table lifecycle, authoritative validation) plus Pilot tests proving the
settings screen produces a minimal diff on save and blocks invalid values.

Structural assertions parse the dumped TOML instead of substring-matching:
POLICY_TEMPLATE contains commented example lines like '# [adapter.dev]' that
make substring checks lie."""

from __future__ import annotations

import tomllib

from test_tui_app import until
from textual.widgets import Collapsible, Input, Select, Switch

from automator import policy as policy_mod
from automator.policy import POLICY_FILE, POLICY_TEMPLATE
from automator.tui.app import BmadAutoApp
from automator.tui.screens.dashboard import DashboardScreen
from automator.tui.screens.settings_screen import SettingsScreen
from automator.tui.settings import PolicyDoc


def fresh_doc(tmp_path) -> PolicyDoc:
    return PolicyDoc.load(tmp_path / "missing-policy.toml")  # template-backed


def test_load_missing_file_starts_from_template(tmp_path):
    doc = fresh_doc(tmp_path)
    assert doc.get("limits", "max_review_cycles") == 3
    assert "# bmad-auto orchestration policy" in doc.dumps()
    assert doc.validate() is None


def test_set_preserves_comments_with_minimal_diff(tmp_path):
    path = tmp_path / "policy.toml"
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")
    doc = PolicyDoc.load(path)
    doc.set("limits", "max_review_cycles", 5)
    doc.save(path)
    new = path.read_text(encoding="utf-8")
    assert "# cache reads bill at ~0.1x" in new
    changed = set(POLICY_TEMPLATE.splitlines()) ^ set(new.splitlines())
    assert changed == {"max_review_cycles = 3", "max_review_cycles = 5"}


def test_clear_deletes_key_and_default_applies(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("gates", "mode", None)
    assert doc.get("gates", "mode") is None
    assert "mode" not in tomllib.loads(doc.dumps())["gates"]
    assert policy_mod.loads(doc.dumps()).gates.mode == "per-epic"


def test_stage_table_set_and_clear(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("adapter.dev", "model", "opus")
    assert policy_mod.loads(doc.dumps()).adapter.dev.model == "opus"
    doc.set("adapter.dev", "model", None)
    # the emptied stage table is dropped entirely: unset = inherit
    assert "dev" not in tomllib.loads(doc.dumps())["adapter"]
    assert policy_mod.loads(doc.dumps()).adapter.dev.model is None
    # the base [adapter] table is untouched
    assert doc.get("adapter", "name") == "claude"


def test_stage_table_with_remaining_keys_survives_clear(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("adapter.review", "name", "codex")
    doc.set("adapter.review", "model", "gpt-5-codex")
    doc.set("adapter.review", "model", None)
    assert tomllib.loads(doc.dumps())["adapter"]["review"] == {"name": "codex"}


def test_extra_args_none_vs_empty(tmp_path):
    doc = fresh_doc(tmp_path)
    assert policy_mod.loads(doc.dumps()).adapter.extra_args is None
    doc.set("adapter", "extra_args", [])
    assert policy_mod.loads(doc.dumps()).adapter.extra_args == ()
    doc.set("adapter", "extra_args", None)
    assert policy_mod.loads(doc.dumps()).adapter.extra_args is None


def test_scalar_added_next_to_existing_stage_table_parses(tmp_path):
    # tomlkit must place a new [adapter] scalar before the [adapter.dev]
    # header, not after it (where TOML would assign it to the stage table)
    doc = fresh_doc(tmp_path)
    doc.set("adapter.dev", "model", "opus")
    doc.set("adapter", "extra_args", ["--foo"])
    pol = policy_mod.loads(doc.dumps())
    assert pol.adapter.extra_args == ("--foo",)
    assert pol.adapter.dev.model == "opus"


def test_validate_surfaces_policy_error(tmp_path):
    doc = fresh_doc(tmp_path)
    doc.set("gates", "mode", "bogus")
    error = doc.validate()
    assert error is not None and "gates.mode" in error
    doc.set("gates", "mode", "none")
    assert doc.validate() is None


def test_save_creates_parent_and_leaves_no_tmp(tmp_path):
    path = tmp_path / ".automator" / "policy.toml"
    doc = PolicyDoc.load(path)
    doc.set("limits", "max_dev_attempts", 3)
    doc.save(path)
    assert policy_mod.load(path).limits.max_dev_attempts == 3
    assert list(path.parent.iterdir()) == [path]


# ----------------------------------------------------------- settings screen


async def open_settings(app, pilot) -> SettingsScreen:
    await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
    await pilot.press("g")
    await until(pilot, lambda: isinstance(app.screen, SettingsScreen))
    return app.screen


def expand_all(screen) -> None:
    """Sections start collapsed; expand them so inner fields are focusable."""
    for c in screen.query(Collapsible):
        c.collapsed = False


def write_policy(project) -> None:
    path = project.project / POLICY_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(POLICY_TEMPLATE, encoding="utf-8")


async def test_settings_screen_saves_minimal_diff(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#limits-max_review_cycles", Input).value = "5"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        new = (project.project / POLICY_FILE).read_text(encoding="utf-8")
        changed = set(POLICY_TEMPLATE.splitlines()) ^ set(new.splitlines())
        assert changed == {"max_review_cycles = 3", "max_review_cycles = 5"}


async def test_settings_screen_untouched_save_writes_nothing_new(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(app, pilot)
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert (project.project / POLICY_FILE).read_text(encoding="utf-8") == POLICY_TEMPLATE


async def test_settings_screen_blocks_invalid_value(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#limits-cache_read_weight", Input).value = "5"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        from textual.widgets import Static

        strip = screen.query_one("#errors", Static)
        assert "cache_read_weight" in str(strip.content)
        assert (project.project / POLICY_FILE).read_text(encoding="utf-8") == POLICY_TEMPLATE


async def test_settings_screen_review_toggle_roundtrip(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#review-enabled", Switch).value = False
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.review.enabled is False


async def test_settings_screen_low_frame_rate_toggle_roundtrip(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#tui-low_frame_rate", Switch).value = True
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.tui.low_frame_rate is True


# ------------------------------------------------------- engine (Game Engine)


async def test_settings_screen_engine_section_labeled_game_engine(project):
    """The [engine] section renders under the friendlier 'Game Engine' header."""
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        titles = [str(c.title) for c in screen.query(Collapsible)]
        assert any(t.startswith("Game Engine") for t in titles)
        assert not any(t.startswith("engine —") for t in titles)


async def test_settings_screen_engine_name_roundtrip(project):
    """Enabling the layer (name = unity) persists; shared mode is valid against
    the default scm.isolation = none, so the save goes through."""
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#engine-name", Input).value = "unity"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.engine.name == "unity"
        assert pol.engine.editor_mode == "shared"  # default preserved


async def test_settings_screen_engine_mcp_select_roundtrip(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#engine-name", Input).value = "unity"
        screen.query_one("#engine-mcp", Select).value = "coplaydev"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.engine.mcp == "coplaydev"


async def test_settings_screen_engine_per_worktree_roundtrip(project):
    """per_worktree requires scm.isolation = worktree; set both in one save."""
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#engine-name", Input).value = "unity"
        screen.query_one("#engine-editor_mode", Select).value = "per_worktree"
        screen.query_one("#scm-isolation", Select).value = "worktree"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.engine.editor_mode == "per_worktree"
        assert pol.scm.isolation == "worktree"


async def test_settings_screen_engine_ready_fields_roundtrip(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#engine-name", Input).value = "unity"
        screen.query_one("#engine-ready_timeout_sec", Input).value = "300"
        screen.query_one("#engine-ready_grace_sec", Input).value = "30"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.engine.ready_timeout_sec == 300
        assert pol.engine.ready_grace_sec == 30


async def test_settings_screen_engine_coupling_blocks_save(project):
    """per_worktree against the default scm.isolation = none is an invalid combo;
    the authoritative policy.loads() rejects it and the screen stays open."""
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#engine-name", Input).value = "unity"
        screen.query_one("#engine-editor_mode", Select).value = "per_worktree"
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(app.screen, SettingsScreen)
        from textual.widgets import Static

        strip = screen.query_one("#errors", Static)
        assert "isolation" in str(strip.content)
        assert (project.project / POLICY_FILE).read_text(encoding="utf-8") == POLICY_TEMPLATE


async def test_settings_screen_stage_override_roundtrip(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        screen.query_one("#adapter-review-model", Input).value = "gpt-5-codex"
        override = screen.query_one("#adapter-extra_args-override", Switch)
        override.value = True
        await pilot.pause()
        args_box = screen.query_one("#adapter-extra_args", Input)
        assert not args_box.disabled  # override switch enables the input
        args_box.value = "--permission-mode bypassPermissions"
        await pilot.press("ctrl+s")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        pol = policy_mod.load(project.project / POLICY_FILE)
        assert pol.adapter.review.model == "gpt-5-codex"
        assert pol.adapter.extra_args == ("--permission-mode", "bypassPermissions")


# ----------------------------------------------- section collapse / expand-all


async def test_sections_start_collapsed_and_toggle_all(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        sections = list(screen.query(Collapsible))
        assert sections  # sanity: the form is grouped into sections
        assert all(c.collapsed for c in sections)  # every section starts collapsed

        await pilot.press("ctrl+e")  # expand all
        await pilot.pause()
        assert all(not c.collapsed for c in sections)

        await pilot.press("ctrl+e")  # collapse all again
        await pilot.pause()
        assert all(c.collapsed for c in sections)


# -------------------------------------------------- arrow nav + enter-to-edit


async def test_arrow_keys_navigate_fields(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        screen.query_one("#limits-max_review_cycles", Input).focus()
        await pilot.pause()
        before = app.focused
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not before  # arrow advanced focus
        await pilot.press("up")
        await pilot.pause()
        assert app.focused is before  # and back again


async def test_arrow_down_skips_disabled_args_input(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        # override off -> the args Input is disabled and out of the focus chain
        args_box = screen.query_one("#adapter-extra_args", Input)
        assert args_box.disabled
        screen.query_one("#adapter-extra_args-override", Switch).focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not args_box


async def test_enter_opens_select_and_arrows_pick(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        from textual.widgets import Select

        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        select = screen.query_one("#gates-mode", Select)
        start = select.value
        select.focus()
        await pilot.pause()
        await pilot.press("enter")  # open the dropdown
        await pilot.pause()
        assert select.expanded
        await pilot.press("down")  # dropdown owns up/down while open
        await pilot.press("enter")  # pick the highlighted option
        await pilot.pause()
        assert not select.expanded
        assert select.value != start


async def test_textarea_enter_edit_mode_and_escape(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        from textual.widgets import TextArea

        screen = await open_settings(app, pilot)
        expand_all(screen)
        await pilot.pause()
        area = screen.query_one("#verify-commands", TextArea)

        # nav mode: down leaves the TextArea
        area.focus()
        await pilot.pause()
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is not area

        # enter -> edit mode: down keeps focus (cursor moves within the box)
        area.focus()
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert area.has_class("-editing")
        await pilot.press("down")
        await pilot.pause()
        assert app.focused is area

        # escape exits edit mode without leaving the screen
        await pilot.press("escape")
        await pilot.pause()
        assert not area.has_class("-editing")
        assert isinstance(app.screen, SettingsScreen)


async def test_escape_in_nav_mode_pops_screen(project):
    write_policy(project)
    app = BmadAutoApp(project.project)
    async with app.run_test(size=(100, 40)) as pilot:
        await open_settings(app, pilot)
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
