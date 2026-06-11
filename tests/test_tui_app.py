"""Coarse Pilot smoke tests for the dashboard. Fine-grained data correctness
lives in test_tui_data.py; here we only prove the wiring: app mounts, the run
table populates and auto-selects the newest run, selection switches the task
table, and the journal pane picks up appended events on a poll."""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from textual.widgets import DataTable, RichLog

from automator.journal import Journal, save_state
from automator.model import Phase, RunState, StoryTask
from automator.runs import RUNS_DIR
from automator.tui.app import BmadAutoApp
from automator.tui.screens.dashboard import DashboardScreen
from automator.tui.widgets import RunHeader
from conftest import install_bmad_config, write_sprint


def make_run(
    root: Path,
    run_id: str,
    *,
    finished: bool = False,
    run_type: str = "story",
    alive: bool = False,
    tasks: dict[str, StoryTask] | None = None,
) -> Path:
    run_dir = root / RUNS_DIR / run_id
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="2026-06-11T10:00:00",
        run_type=run_type,
        finished=finished,
        tasks=tasks or {},
    )
    save_state(run_dir, state)
    if alive:
        (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
    return run_dir


async def until(pilot, condition, timeout: float = 5.0) -> None:
    """Wait for a predicate across thread-worker polls and their callbacks."""
    waited = 0.0
    while not condition():
        if waited >= timeout:
            raise AssertionError("condition not met before timeout")
        await pilot.pause(0.05)
        waited += 0.05


def dashboard(app: BmadAutoApp) -> DashboardScreen:
    assert isinstance(app.screen, DashboardScreen)
    return app.screen


async def test_empty_project_shows_hint(project):
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        assert screen.query_one("#runs", DataTable).row_count == 0
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "no runs found" in header


async def test_run_table_populates_and_selects_newest(project):
    root = project.project
    make_run(root, "20260611-100000-aaaa", finished=True)
    make_run(root, "20260611-110000-bbbb", run_type="sweep", alive=True)
    app = BmadAutoApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        runs = screen.query_one("#runs", DataTable)
        await until(pilot, lambda: runs.row_count == 2)
        await until(pilot, lambda: screen.selected_run_id == "20260611-110000-bbbb")
        await until(
            pilot,
            lambda: "20260611-110000-bbbb"
            in str(screen.query_one("#runheader", RunHeader).content),
        )
        header = str(screen.query_one("#runheader", RunHeader).content)
        assert "[sweep]" in header
        assert "running" in header  # our own pid is alive


async def test_selection_switches_task_table(project):
    root = project.project
    task = StoryTask(story_key="1-1-login", epic=1, phase=Phase.DONE)
    task.commit_sha = "abc1234def567890"
    make_run(root, "20260611-100000-aaaa", finished=True, tasks={"1-1-login": task})
    make_run(root, "20260611-110000-bbbb", alive=True)
    app = BmadAutoApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        runs = screen.query_one("#runs", DataTable)
        tasks_table = screen.query_one("#tasks", DataTable)
        await until(pilot, lambda: screen.selected_run_id == "20260611-110000-bbbb")
        assert tasks_table.row_count == 0  # newest run has no tasks
        runs.move_cursor(row=0)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        await until(pilot, lambda: tasks_table.row_count == 1)
        assert tasks_table.get_row_at(0)[0] == "1-1-login"


async def test_journal_pane_updates_after_poll(project):
    root = project.project
    run_dir = make_run(root, "20260611-100000-aaaa", alive=True)
    app = BmadAutoApp(root)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)
        await until(pilot, lambda: screen.selected_run_id == "20260611-100000-aaaa")
        Journal(run_dir).append("story-start", story_key="1-2-search")
        screen._tick(force_rescan=False)  # manual poll, no 1s wait
        journal = screen.query_one("#journal", RichLog)

        def has_line() -> bool:
            return any("story-start" in strip.text for strip in journal.lines)

        await until(pilot, has_line)
        assert any("1-2-search" in strip.text for strip in journal.lines)


async def test_sprint_tab_shows_counts(project):
    install_bmad_config(project)
    write_sprint(project, {"1-1-a": "done", "1-2-b": "backlog", "1-3-c": "backlog"})
    make_run(project.project, "20260611-100000-aaaa", finished=True)
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        screen = dashboard(app)

        def sprint_text() -> str:
            from textual.widgets import Static

            return str(screen.query_one("#sprint", Static).content)

        await until(pilot, lambda: "3 stories" in sprint_text())
        assert "2 actionable" in sprint_text()


def test_cli_tui_hint_without_textual(project, monkeypatch, capsys):
    """`bmad-auto tui` prints the install hint when the extra is missing."""
    import builtins

    from automator import cli

    real_import = builtins.__import__

    def fake_import(name, *args, **kwargs):
        if name.partition(".")[0] == "textual":
            raise ModuleNotFoundError(f"No module named '{name}'", name=name)
        return real_import(name, *args, **kwargs)

    monkeypatch.delitem(__import__("sys").modules, "automator.tui.app", raising=False)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    rc = cli.main(["tui", "--project", str(project.project)])
    assert rc == 1
    assert "bmad-automator[tui]" in capsys.readouterr().err


@pytest.mark.parametrize("binding", ["r", "s", "e", "a", "v", "g"])
async def test_control_bindings_stubbed(project, binding):
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press(binding)
        await until(pilot, lambda: len(app._notifications) > 0)
