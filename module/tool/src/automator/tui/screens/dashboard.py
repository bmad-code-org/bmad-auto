"""Live read-only dashboard: run picker, run detail, journal/log/sprint tails.

Polling model: a 1s interval kicks an exclusive thread worker that does all
filesystem I/O through the stat-gated readers in tui.data and produces an
immutable snapshot; the snapshot is applied to widgets back on the event loop.
Selecting a run replaces the whole poll context and bumps a generation
counter, so a stale in-flight snapshot for the previous run is dropped on
arrival rather than painted over the new one. The run list itself is
selection-independent and is always applied.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from rich.text import Text
from textual import work
from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical
from textual.screen import Screen
from textual.widgets import (
    DataTable,
    Footer,
    Header,
    RichLog,
    Static,
    TabbedContent,
    TabPane,
)

from ...model import RunState
from ...runs import RUNS_DIR
from .. import data
from ..widgets import RunHeader, journal_line, status_cell

# Keep at most this many parsed journal entries per run for active-task
# tracking; the visible pane is bounded separately by RichLog max_lines.
_MAX_ENTRIES = 500

_RESCAN_EVERY = 3  # run-list + sprint rescan cadence, in 1s ticks

_LAUNCH_TIMEOUT = 10.0  # seconds before a pending launch is presumed failed


class _PollContext:
    """Mutable state for polling one selected run. Constructed on the UI
    thread (constructors do no I/O), then mutated only inside the poll worker
    — exclusive=True serializes workers, and a superseded worker holds the
    previous context object, never this one."""

    def __init__(self, run_dir: Path):
        self.run_dir = run_dir
        self.watcher = data.RunWatcher(run_dir)
        self.journal = data.JournalTail(run_dir)
        self.entries: list[dict[str, Any]] = []
        self.log: data.LogTail | None = None
        self.log_task: str | None = None
        self.attention_seen = 0
        self.first_poll = True
        self.decision_toasted: str | None = None  # dw_id already announced


@dataclass
class _Snapshot:
    generation: int
    runs: list[data.RunInfo] | None = None  # None: no rescan this tick
    sprint_refreshed: bool = False
    sprint: data.SprintSummary | None = None
    has_run: bool = False
    run_id: str = ""
    status: str = data.UNKNOWN
    state: RunState | None = None
    new_entries: list[dict[str, Any]] = field(default_factory=list)
    log_task: str | None = None
    log_reset: bool = False
    new_log: str = ""
    attention_reset: bool = False
    new_attention: str = ""
    toast_attention: bool = False
    decision: tuple[str, str] | None = None  # (dw_id, question) awaiting a human
    toast_decision: bool = False


class DashboardScreen(Screen[None]):
    def __init__(self, project: Path):
        super().__init__()
        self.project = project
        self._generation = 0
        self._ctx: _PollContext | None = None
        self._tick_count = 0
        self._run_rows: list[str] = []  # row keys, table order (oldest first)
        self._task_rows: set[str] = set()
        self._pending_run: str | None = None  # just-launched run, no state.json yet
        self._pending_deadline = 0.0
        self._decision: tuple[str, str] | None = None

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            yield DataTable(id="runs", cursor_type="row")
            with Vertical(id="detail"):
                yield RunHeader(id="runheader")
                yield DataTable(id="tasks", cursor_type="row")
                with TabbedContent(id="tabs"):
                    with TabPane("Journal", id="tab-journal"):
                        yield RichLog(id="journal", max_lines=2000, auto_scroll=True)
                    with TabPane("Log", id="tab-log"):
                        yield RichLog(id="log", max_lines=2000, auto_scroll=True)
                    with TabPane("Sprint", id="tab-sprint"):
                        yield Static(id="sprint")
                    with TabPane("Attention", id="tab-attention"):
                        yield RichLog(id="attention", max_lines=500, auto_scroll=True)
        yield Footer()

    def on_mount(self) -> None:
        runs = self.query_one("#runs", DataTable)
        runs.add_column("st", key="st", width=2)
        runs.add_column("run", key="run")
        runs.add_column("type", key="type")
        tasks = self.query_one("#tasks", DataTable)
        tasks.add_column("story", key="story", width=30)
        tasks.add_column("phase", key="phase", width=16)
        tasks.add_column("dev", key="dev", width=5)
        tasks.add_column("review", key="review", width=6)
        tasks.add_column("tokens", key="tokens", width=12)
        tasks.add_column("info", key="info")
        self.query_one("#runheader", RunHeader).show_empty(self.project)
        self.set_interval(1.0, self._tick)
        self._tick()

    # ------------------------------------------------------------- selection

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.data_table.id == "runs" and event.row_key is not None:
            self._select_run(str(event.row_key.value))

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        if event.data_table.id == "runs" and event.row_key is not None:
            self._select_run(str(event.row_key.value))

    @property
    def selected_run_id(self) -> str | None:
        return self._ctx.run_dir.name if self._ctx else None

    @property
    def decision_pending(self) -> tuple[str, str] | None:
        """(dw_id, question) the selected run's sweep is blocked on, if any —
        the attach action uses this to target the orchestrator window."""
        return self._decision

    def _select_run(self, run_id: str) -> None:
        if self._ctx is not None and self._ctx.run_dir.name == run_id:
            return
        self._generation += 1
        self._ctx = _PollContext(self.project / RUNS_DIR / run_id)
        self._decision = None
        tasks = self.query_one("#tasks", DataTable)
        tasks.clear()
        self._task_rows.clear()
        for log_id in ("#journal", "#log", "#attention"):
            self.query_one(log_id, RichLog).clear()
        self.query_one("#runheader", RunHeader).show_run(run_id, data.UNKNOWN, None)
        self._tick(force_rescan=False)

    def expect_run(self, run_id: str) -> None:
        """A launch just happened: select the run before its dir exists, show
        a 'starting' header until state.json appears, and complain past the
        launch timeout."""
        self._pending_run = run_id
        self._pending_deadline = time.monotonic() + _LAUNCH_TIMEOUT
        self._select_run(run_id)
        self.query_one("#runheader", RunHeader).show_starting(run_id)

    # --------------------------------------------------------------- polling

    def _tick(self, force_rescan: bool | None = None) -> None:
        if self._pending_run is not None and time.monotonic() > self._pending_deadline:
            self._pending_run = None
            self.notify(
                "launch may have failed — attach to tmux session bmad-auto-ctl",
                severity="error",
                timeout=15,
            )
        if force_rescan is None:
            force_rescan = self._tick_count % _RESCAN_EVERY == 0
            self._tick_count += 1
        self._poll(self._ctx, self._generation, force_rescan)

    @work(thread=True, exclusive=True, group="poll")
    def _poll(self, ctx: _PollContext | None, generation: int, rescan: bool) -> None:
        snap = _Snapshot(generation=generation)
        if rescan:
            snap.runs = data.discover_runs(self.project)
            snap.sprint_refreshed = True
            snap.sprint = data.sprint_summary(self.project)
        if ctx is not None:
            snap.has_run = True
            snap.run_id = ctx.run_dir.name
            snap.state = ctx.watcher.state()
            snap.status = ctx.watcher.status()
            snap.new_entries = ctx.journal.read_new()
            ctx.entries.extend(snap.new_entries)
            del ctx.entries[:-_MAX_ENTRIES]
            snap.decision = data.pending_decision(ctx.entries)
            if snap.decision is not None and ctx.decision_toasted != snap.decision[0]:
                snap.toast_decision = True
            ctx.decision_toasted = snap.decision[0] if snap.decision else None
            task = data.active_task_id(ctx.run_dir, ctx.entries)
            if task != ctx.log_task:
                ctx.log_task = task
                ctx.log = (
                    data.LogTail(ctx.run_dir / data.LOGS_DIR / f"{task}.log") if task else None
                )
                snap.log_reset = True
            snap.log_task = task
            if ctx.log is not None:
                snap.new_log = ctx.log.read_new()
            attention = ctx.watcher.attention()
            if len(attention) < ctx.attention_seen:
                snap.attention_reset = True
                snap.new_attention = attention
            else:
                snap.new_attention = attention[ctx.attention_seen :]
            ctx.attention_seen = len(attention)
            snap.toast_attention = bool(snap.new_attention.strip()) and not ctx.first_poll
            ctx.first_poll = False
        self.app.call_from_thread(self._apply, snap)

    # ------------------------------------------------------------ applying

    def _apply(self, snap: _Snapshot) -> None:
        if snap.runs is not None:
            self._apply_runs(snap.runs)
        if snap.sprint_refreshed:
            self._apply_sprint(snap.sprint)
        if not snap.has_run or snap.generation != self._generation:
            return  # selection changed mid-poll: per-run parts are stale

        self._decision = snap.decision
        header = self.query_one("#runheader", RunHeader)
        if snap.run_id == self._pending_run and snap.state is None:
            header.show_starting(snap.run_id)  # launched, state.json not yet written
        else:
            if snap.run_id == self._pending_run:
                self._pending_run = None  # the engine is up
            header.show_run(snap.run_id, snap.status, snap.state, snap.decision)
        if snap.state is not None:
            self._apply_tasks(snap.state)
        if snap.toast_decision and snap.decision is not None:
            self.notify(
                snap.decision[1] or snap.decision[0],
                title=f"decision needed: {snap.decision[0]} — press a to attach",
                severity="warning",
                timeout=30,
            )

        journal = self.query_one("#journal", RichLog)
        for entry in snap.new_entries:
            journal.write(journal_line(entry))

        log = self.query_one("#log", RichLog)
        if snap.log_reset:
            log.clear()
            if snap.log_task:
                log.write(Text(f"— {snap.log_task}.log —", style="dim"))
        if snap.new_log:
            log.write(Text.from_ansi(snap.new_log.rstrip("\n")))

        attention = self.query_one("#attention", RichLog)
        if snap.attention_reset:
            attention.clear()
        if snap.new_attention.strip():
            attention.write(Text(snap.new_attention.rstrip("\n")))
            if snap.toast_attention:
                last = snap.new_attention.strip().splitlines()[-1]
                self.notify(last, title="attention", severity="warning", timeout=10)

    def _apply_runs(self, runs: list[data.RunInfo]) -> None:
        table = self.query_one("#runs", DataTable)
        ids = [r.run_id for r in runs]
        if not runs:
            if self._run_rows:
                table.clear()
                self._run_rows.clear()
            if self._ctx is None:
                self.query_one("#runheader", RunHeader).show_empty(self.project)
            return
        if any(known not in ids for known in self._run_rows):
            # a run dir disappeared — rare enough to just rebuild
            table.clear()
            self._run_rows.clear()
        first_populate = not self._run_rows
        added: list[str] = []
        for run in runs:
            if run.run_id in self._run_rows:
                table.update_cell(run.run_id, "st", status_cell(run.status))
            else:
                table.add_row(
                    status_cell(run.status),
                    run.run_id,
                    run.run_type,
                    key=run.run_id,
                )
                self._run_rows.append(run.run_id)
                added.append(run.run_id)
        if first_populate:
            if self.selected_run_id in ids:
                # a pre-selected (just-launched) run beats auto-select-newest
                table.move_cursor(row=ids.index(self.selected_run_id))
            else:
                table.move_cursor(row=len(ids) - 1)  # newest; RowHighlighted selects
                self._select_run(ids[-1])
        elif self.selected_run_id in added:
            # the selected run was launched before its dir existed; its row
            # just appeared — bring the cursor to it
            table.move_cursor(row=self._run_rows.index(self.selected_run_id))

    def _apply_tasks(self, state: RunState) -> None:
        table = self.query_one("#tasks", DataTable)
        for key, task in state.tasks.items():
            tokens = f"{task.tokens.total:,}" if task.tokens.total else "-"
            info = task.defer_reason or (task.commit_sha or "")[:12]
            cells = {
                "phase": str(task.phase),
                "dev": f"×{task.attempt}",
                "review": f"×{task.review_cycle}",
                "tokens": tokens,
                "info": info,
            }
            if key in self._task_rows:
                for column, value in cells.items():
                    table.update_cell(key, column, value)
            else:
                table.add_row(key, *cells.values(), key=key)
                self._task_rows.add(key)

    def _apply_sprint(self, summary: data.SprintSummary | None) -> None:
        widget = self.query_one("#sprint", Static)
        if summary is None:
            widget.update(
                Text(
                    "sprint status unavailable — is this an initialized BMAD project?",
                    style="dim",
                )
            )
            return
        text = Text()
        text.append(f"{summary.total} stories", style="bold")
        text.append(f"  ·  {summary.actionable} actionable\n\n", style="green")
        for status, count in sorted(summary.by_status.items(), key=lambda kv: -kv[1]):
            text.append(f"  {status:24s} {count}\n")
        widget.update(text)
