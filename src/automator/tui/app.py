"""`bmad-auto tui` application shell.

Observer/launcher only: the TUI never runs engines in-process. Run control
(r/s/e) launches detached bmad-auto processes in the bmad-auto-ctl tmux
session via tui.launch; validate and dry runs are captured into a modal.
The g (settings) binding lands in phase 5 and currently just says so.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from collections.abc import Callable
from pathlib import Path

from textual import work
from textual.app import App, SuspendNotSupported
from textual.binding import Binding

from .. import runs, verify
from ..journal import load_state
from ..runs import RUNS_DIR
from . import data, launch
from .screens.dashboard import DashboardScreen
from .screens.modals import (
    ConfirmModal,
    ConfirmResumeModal,
    StartRunModal,
    StartSweepModal,
    TextOutputModal,
)


class BmadAutoApp(App[None]):
    TITLE = "bmad-auto"

    CSS = """
    #runs {
        width: 34;
        border-right: solid $primary-darken-2;
    }
    #detail {
        width: 1fr;
    }
    #runheader {
        height: auto;
        padding: 0 1;
        background: $boost;
        border-bottom: solid $primary-darken-2;
    }
    #tasks {
        height: auto;
        max-height: 35%;
    }
    #tabs {
        height: 1fr;
    }
    #sprint {
        padding: 1 2;
    }
    """

    BINDINGS = [
        Binding("q", "quit", "quit"),
        Binding("r", "start_run", "run"),
        Binding("s", "start_sweep", "sweep"),
        Binding("e", "resume_run", "resume"),
        Binding("a", "attach", "attach"),
        Binding("v", "validate", "validate"),
        Binding("g", "settings", "settings"),
        Binding("d", "toggle_dark", "dark"),
    ]

    def __init__(self, project: Path):
        super().__init__()
        self.project = project.resolve()
        self.sub_title = str(self.project)
        self._dashboard = DashboardScreen(self.project)

    def on_mount(self) -> None:
        self.push_screen(self._dashboard)

    def action_toggle_dark(self) -> None:
        self.theme = (
            "textual-light" if self.theme == "textual-dark" else "textual-dark"
        )

    # ------------------------------------------------------------ run control

    def _tmux_missing(self) -> bool:
        if launch.tmux_available():
            return False
        self.notify("tmux not found on PATH — launch/attach disabled", severity="error")
        return True

    def _guarded(self, go: Callable[[], None]) -> None:
        """Pre-launch guard mirroring the CLI: clean worktree required, plus a
        confirm when another engine is already live."""
        try:
            if not verify.worktree_clean(self.project):
                self.notify(
                    "git worktree is not clean — commit or stash first", severity="error"
                )
                return
        except verify.GitError as e:
            self.notify(f"git check failed: {e}", severity="error")
            return
        live = [r.run_id for r in data.discover_runs(self.project) if r.status == data.RUNNING]
        if live:
            self.push_screen(
                ConfirmModal(
                    "another run is live",
                    f"running now: {', '.join(live)}\n"
                    "launching another engine on the same project may conflict.",
                    confirm_label="launch anyway",
                ),
                lambda ok: go() if ok else None,
            )
        else:
            go()

    def action_start_run(self) -> None:
        if self._tmux_missing():
            return
        self.push_screen(StartRunModal(), self._start_run_result)

    def _start_run_result(self, result: dict | None) -> None:
        if not result:
            return
        if result["dry_run"]:
            tail = ["run", "--project", str(self.project), "--dry-run"]
            if result["epic"] is not None:
                tail += ["--epic", str(result["epic"])]
            if result["story"]:
                tail += ["--story", result["story"]]
            if result["max_stories"] is not None:
                tail += ["--max-stories", str(result["max_stories"])]
            self._show_captured("run --dry-run", tail)
            return

        def go() -> None:
            run_id = runs.new_run_id()
            try:
                launch.start_run_detached(
                    self.project,
                    run_id,
                    epic=result["epic"],
                    story=result["story"],
                    max_stories=result["max_stories"],
                )
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"run {run_id} launched (tmux session {launch.CTL_SESSION})")
            self._dashboard.expect_run(run_id)

        self._guarded(go)

    def action_start_sweep(self) -> None:
        if self._tmux_missing():
            return
        self.push_screen(StartSweepModal(), self._start_sweep_result)

    def _start_sweep_result(self, result: dict | None) -> None:
        if not result:
            return
        if result["dry_run"]:
            self._show_captured(
                "sweep --dry-run", ["sweep", "--project", str(self.project), "--dry-run"]
            )
            return

        def go() -> None:
            run_id = runs.new_run_id()
            try:
                launch.start_sweep_detached(
                    self.project,
                    run_id,
                    no_prompt=result["no_prompt"],
                    decisions_only=result["decisions_only"],
                    max_bundles=result["max_bundles"],
                )
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"sweep {run_id} launched (tmux session {launch.CTL_SESSION})")
            self._dashboard.expect_run(run_id)

        self._guarded(go)

    def action_resume_run(self) -> None:
        if self._tmux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        run_dir = self.project / RUNS_DIR / run_id
        try:
            state = load_state(run_dir)
        except (OSError, KeyError, ValueError):
            self.notify(f"state for run {run_id} is unreadable", severity="error")
            return
        if state.finished:
            self.notify(f"run {run_id} already finished", severity="warning")
            return
        engine_alive = data.liveness(run_dir) == "alive"

        def done(ok: bool | None) -> None:
            if not ok:
                return
            try:
                launch.resume_detached(self.project, run_id)
            except launch.LaunchError as e:
                self.notify(str(e), severity="error")
                return
            self.notify(f"resume of {run_id} launched (tmux session {launch.CTL_SESSION})")

        self.push_screen(ConfirmResumeModal(run_id, state, engine_alive), done)

    def action_attach(self) -> None:
        if self._tmux_missing():
            return
        run_id = self._dashboard.selected_run_id
        if run_id is None:
            self.notify("no run selected", severity="warning")
            return
        session = runs.session_name(run_id)
        if not launch.session_exists(session):
            self.notify(
                f"no live agent session ({session}) — the run may be between sessions; "
                f"the engine itself runs in tmux session {launch.CTL_SESSION}",
                severity="warning",
                timeout=10,
            )
            return
        argv = runs.attach_argv(run_id)
        if os.environ.get("TMUX"):
            subprocess.call(argv)  # switch-client: this client comes right back
            return
        try:
            with self.suspend():
                subprocess.call(argv)
        except SuspendNotSupported:
            self.notify(
                f"cannot suspend here — run manually: {shlex.join(argv)}",
                severity="warning",
                timeout=10,
            )

    def action_validate(self) -> None:
        self._show_captured("validate", ["validate", "--project", str(self.project)])

    @work(thread=True, exclusive=True, group="captured")
    def _show_captured(self, title: str, tail: list[str]) -> None:
        rc, out = launch.run_captured(tail)
        self.call_from_thread(self.push_screen, TextOutputModal(title, rc, out))

    def action_settings(self) -> None:
        self.notify(
            "the settings editor lands in TUI phase 5 — not implemented yet",
            severity="warning",
        )


def run_tui(project: Path) -> int:
    BmadAutoApp(project).run()
    return 0
