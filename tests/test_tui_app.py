"""Coarse Pilot smoke tests for the dashboard and run control. Fine-grained
data correctness lives in test_tui_data.py, exact launch argv in
test_tui_launch.py; here we only prove the wiring: app mounts, the run table
populates and auto-selects the newest run, selection switches the task table,
the journal pane picks up appended events on a poll, and the r/s/e/a/v
bindings drive modals into tui.launch calls (monkeypatched — no real tmux)."""

from __future__ import annotations

import os
from pathlib import Path

from textual.widgets import Checkbox, DataTable, Input, RichLog

from automator.journal import Journal, save_state
from automator.model import Phase, RunState, StoryTask
from automator.runs import RUNS_DIR
from automator.tui import data, launch
from automator.tui.app import BmadAutoApp
from automator.tui.screens.dashboard import DashboardScreen
from automator.tui.screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    StartRunModal,
    StartSweepModal,
    TextOutputModal,
)
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
    paused_stage: str | None = None,
    paused_reason: str | None = None,
) -> Path:
    run_dir = root / RUNS_DIR / run_id
    state = RunState(
        run_id=run_id,
        project=str(root),
        started_at="2026-06-11T10:00:00",
        run_type=run_type,
        finished=finished,
        tasks=tasks or {},
        paused_stage=paused_stage,
        paused_reason=paused_reason,
    )
    save_state(run_dir, state)
    if alive:
        (run_dir / "engine.pid").write_text(str(os.getpid()), encoding="utf-8")
    return run_dir


def notifications(app: BmadAutoApp) -> list[str]:
    return [n.message for n in app._notifications]


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


async def test_settings_binding_stubbed(project):
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("g")
        await until(pilot, lambda: len(app._notifications) > 0)
        assert any("phase 5" in m for m in notifications(app))


# ------------------------------------------------------------- run control


async def test_start_run_modal_escape_cancels(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.press("escape")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        assert not calls


async def test_start_run_modal_launches(project, monkeypatch):
    calls = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

    def fake_start(proj, run_id, *, epic, story, max_stories):
        calls.update(
            project=proj, run_id=run_id, epic=epic, story=story, max_stories=max_stories
        )

    monkeypatch.setattr(launch, "start_run_detached", fake_start)
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        app.screen.query_one("#epic", Input).value = "2"
        app.screen.query_one("#max-stories", Input).value = "3"
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))
        assert calls["project"] == project.project
        assert calls["epic"] == 2
        assert calls["story"] is None
        assert calls["max_stories"] == 3
        screen = dashboard(app)
        # the launched run is pre-selected and shown as starting
        assert screen._pending_run == calls["run_id"]
        assert screen.selected_run_id == calls["run_id"]
        await until(
            pilot,
            lambda: "starting" in str(screen.query_one("#runheader", RunHeader).content),
        )


async def test_dirty_worktree_blocks_launch(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    (project.project / "src.txt").write_text("dirty\n")
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click("#ok")
        await until(pilot, lambda: any("not clean" in m for m in notifications(app)))
        assert not calls


async def test_live_run_asks_for_confirmation(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "start_run_detached", lambda *a, **kw: calls.append(a))
    make_run(project.project, "20260611-100000-aaaa", alive=True)  # our pid: running
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        await pilot.click("#ok")
        await until(
            pilot,
            lambda: isinstance(app.screen, ConfirmModal)
            and not isinstance(app.screen, ConfirmResumeModal),
        )
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))


async def test_start_sweep_modal_launches(project, monkeypatch):
    calls = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

    def fake_sweep(proj, run_id, *, no_prompt, decisions_only, max_bundles):
        calls.update(
            run_id=run_id,
            no_prompt=no_prompt,
            decisions_only=decisions_only,
            max_bundles=max_bundles,
        )

    monkeypatch.setattr(launch, "start_sweep_detached", fake_sweep)
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("s")
        await until(pilot, lambda: isinstance(app.screen, StartSweepModal))
        app.screen.query_one("#no-prompt", Checkbox).value = True
        await pilot.click("#ok")
        await until(pilot, lambda: bool(calls))
        assert calls["no_prompt"] is True
        assert calls["decisions_only"] is False
        assert calls["max_bundles"] is None
        assert dashboard(app)._pending_run == calls["run_id"]


async def test_dry_run_shows_captured_output(project, monkeypatch):
    seen = {}
    monkeypatch.setattr(launch, "tmux_available", lambda: True)

    def fake_captured(tail):
        seen["tail"] = tail
        return 0, "would process 2 stories\n"

    monkeypatch.setattr(launch, "run_captured", fake_captured)
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("r")
        await until(pilot, lambda: isinstance(app.screen, StartRunModal))
        app.screen.query_one("#dry-run", Checkbox).value = True
        await pilot.click("#ok")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        assert seen["tail"][0] == "run"
        assert "--dry-run" in seen["tail"]
        await pilot.click("#ok")
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))


async def test_validate_shows_output_modal(project, monkeypatch):
    monkeypatch.setattr(launch, "run_captured", lambda tail: (1, "FAIL: no policy\n"))
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("v")
        await until(pilot, lambda: isinstance(app.screen, TextOutputModal))
        labels = app.screen.query("Label")
        assert any("exit 1" in str(label.content) for label in labels)


async def test_resume_confirm_launches(project, monkeypatch):
    calls = []
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "resume_detached", lambda proj, rid: calls.append(rid))
    monkeypatch.setattr(data, "liveness", lambda run_dir: "dead")
    make_run(
        project.project,
        "20260611-100000-aaaa",
        paused_stage="DEV_VERIFY",
        paused_reason="verify failed",
    )
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: isinstance(app.screen, ConfirmResumeModal))
        await pilot.click("#ok")
        await until(pilot, lambda: calls == ["20260611-100000-aaaa"])


async def test_resume_finished_run_refused(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    make_run(project.project, "20260611-100000-aaaa", finished=True)
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("e")
        await until(pilot, lambda: any("already finished" in m for m in notifications(app)))
        assert isinstance(app.screen, DashboardScreen)


async def test_attach_without_tmux_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: False)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await pilot.press("a")
        await until(pilot, lambda: any("tmux not found" in m for m in notifications(app)))


async def test_attach_without_agent_session_notifies(project, monkeypatch):
    monkeypatch.setattr(launch, "tmux_available", lambda: True)
    monkeypatch.setattr(launch, "session_exists", lambda session: False)
    make_run(project.project, "20260611-100000-aaaa")
    app = BmadAutoApp(project.project)
    async with app.run_test() as pilot:
        await until(pilot, lambda: isinstance(app.screen, DashboardScreen))
        await until(pilot, lambda: dashboard(app).selected_run_id is not None)
        await pilot.press("a")
        await until(
            pilot, lambda: any("no live agent session" in m for m in notifications(app))
        )
