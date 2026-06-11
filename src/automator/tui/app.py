"""`bmad-auto tui` application shell.

Observer/launcher only: the TUI never runs engines in-process. This phase is
the read-only dashboard; the r/s/e/a/v bindings (run control) and g (settings)
land in later phases and currently just say so.
"""

from __future__ import annotations

from pathlib import Path

from textual.app import App
from textual.binding import Binding

from .screens.dashboard import DashboardScreen


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

    def on_mount(self) -> None:
        self.push_screen(DashboardScreen(self.project))

    def action_toggle_dark(self) -> None:
        self.theme = (
            "textual-light" if self.theme == "textual-dark" else "textual-dark"
        )

    def _not_yet(self, what: str, phase: str) -> None:
        self.notify(f"{what} lands in TUI {phase} — not implemented yet",
                    severity="warning")

    def action_start_run(self) -> None:
        self._not_yet("starting runs", "phase 4")

    def action_start_sweep(self) -> None:
        self._not_yet("starting sweeps", "phase 4")

    def action_resume_run(self) -> None:
        self._not_yet("resume", "phase 4")

    def action_attach(self) -> None:
        self._not_yet("attach", "phase 4")

    def action_validate(self) -> None:
        self._not_yet("validate", "phase 4")

    def action_settings(self) -> None:
        self._not_yet("the settings editor", "phase 5")


def run_tui(project: Path) -> int:
    BmadAutoApp(project).run()
    return 0
