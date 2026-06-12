"""Small presentation widgets for the dashboard.

Rendering builds rich Text objects rather than markup strings: pause reasons,
defer reasons and journal fields are arbitrary engine output and must never be
interpreted as markup.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

from rich.text import Text
from textual.widgets import Static
from textual.widgets.option_list import Option

from ..model import Phase, RunState
from . import data

STATUS_GLYPHS = {
    data.RUNNING: "▶",
    data.PAUSED: "⏸",
    data.FINISHED: "✔",
    data.INTERRUPTED: "✖",
    data.UNKNOWN: "?",
}

STATUS_STYLES = {
    data.RUNNING: "green",
    data.PAUSED: "yellow",
    data.FINISHED: "dim",
    data.INTERRUPTED: "bold red",
    data.UNKNOWN: "dim",
}


def status_cell(status: str) -> Text:
    return Text(STATUS_GLYPHS.get(status, "?"), style=STATUS_STYLES.get(status, ""))


class RunHeader(Static):
    """One-glance summary of the selected run, or the empty-state hint."""

    def show_empty(self, project: Path) -> None:
        text = Text()
        text.append("no runs found", style="bold")
        text.append(f"  ({project})\n", style="dim")
        text.append(
            "start one with `bmad-auto run` or `bmad-auto sweep`"
            " — or `bmad-auto init` if this project is not set up yet",
            style="dim",
        )
        self.update(text)

    def show_starting(self, run_id: str) -> None:
        text = Text()
        text.append(run_id, style="bold")
        text.append("  ⧗ starting…", style="yellow")
        text.append(
            "\nwaiting for the engine to write state.json"
            " — if nothing appears, attach to tmux session bmad-auto-ctl",
            style="dim",
        )
        self.update(text)

    def show_run(
        self,
        run_id: str,
        status: str,
        state: RunState | None,
        decision: tuple[str, str] | None = None,
    ) -> None:
        text = Text()
        text.append(run_id, style="bold")
        if state is not None and state.run_type != "story":
            text.append(f" [{state.run_type}]")
        text.append("  ")
        text.append(
            f"{STATUS_GLYPHS.get(status, '?')} {status}",
            style=STATUS_STYLES.get(status, ""),
        )
        if state is None:
            text.append("\nstate unavailable", style="dim")
            self.update(text)
            return
        text.append(f"  started {state.started_at}", style="dim")
        if state.current_epic is not None:
            text.append(f"  epic {state.current_epic}", style="dim")

        counts = {Phase.DONE: 0, Phase.DEFERRED: 0, Phase.ESCALATED: 0}
        tokens = 0
        for task in state.tasks.values():
            if task.phase in counts:
                counts[task.phase] += 1
            tokens += task.tokens.total
        text.append("\n")
        text.append(f"tasks {len(state.tasks)}", style="dim")
        text.append(f"  done {counts[Phase.DONE]}", style="green")
        text.append(f"  deferred {counts[Phase.DEFERRED]}", style="yellow")
        style = "red" if counts[Phase.ESCALATED] else "dim"
        text.append(f"  escalated {counts[Phase.ESCALATED]}", style=style)
        text.append(f"  {tokens:,} tokens", style="dim")

        if status == data.PAUSED:
            text.append("\n⏸ paused", style="bold yellow")
            if state.paused_stage:
                text.append(f" ({state.paused_stage})", style="yellow")
            if state.paused_reason:
                text.append(f" — {state.paused_reason}", style="yellow")
            text.append("  · press e to resume", style="dim")
        elif status == data.INTERRUPTED:
            text.append(
                "\n✖ engine gone — run was interrupted · press e to resume",
                style="bold red",
            )
        if decision is not None and status not in (data.FINISHED, data.INTERRUPTED):
            dw_id, question = decision
            text.append(f"\n⚑ decision needed: {dw_id}", style="bold yellow")
            if question:
                text.append(f" — {_short(question, 100)}", style="yellow")
            text.append("\n  press a to attach and answer", style="bold yellow")
        self.update(text)


# ------------------------------------------------------------ journal lines

# kind substrings -> style, first match wins; anything else renders dim
_JOURNAL_STYLES = (
    ("escalat", "red"),
    ("failed", "red"),
    ("done", "green"),
    ("complete", "green"),
    ("finished", "green"),
    ("decision", "yellow"),
    ("deferred", "yellow"),
    ("boundary", "yellow"),
    ("truncated", "yellow"),
    ("start", "cyan"),
    ("resume", "cyan"),
)


# metadata fields not worth a column on every line; log_task/log_pos drive
# the journal -> log jump, not the human
_JOURNAL_HIDDEN_FIELDS = ("ts", "kind", "log_task", "log_pos")


def journal_line(entry: dict[str, Any]) -> Text:
    kind = str(entry.get("kind", "?"))
    style = next((s for sub, s in _JOURNAL_STYLES if sub in kind), "dim")
    ts = entry.get("ts")
    clock = ""
    if isinstance(ts, (int, float)):
        clock = time.strftime("%H:%M:%S", time.localtime(ts))
    text = Text()
    text.append(f"{clock:8s} ", style="dim")
    text.append(f"{kind:24s}", style=style)
    fields = "  ".join(
        f"{k}={_short(v)}" for k, v in entry.items() if k not in _JOURNAL_HIDDEN_FIELDS
    )
    if fields:
        text.append(" " + fields)
    return text


class JournalEntryOption(Option):
    """One journal entry as an OptionList row; carries the raw entry so
    selecting it can jump to the entry's position in the pane log."""

    def __init__(self, entry: dict[str, Any]) -> None:
        super().__init__(journal_line(entry))
        self.entry = entry


def _short(value: Any, limit: int = 60) -> str:
    s = str(value)
    return s if len(s) <= limit else s[: limit - 1] + "…"
