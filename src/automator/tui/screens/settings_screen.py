"""Settings editor for .automator/policy.toml.

The form is populated from the raw tomlkit document, so unset keys show
default placeholders instead of baked-in values, and saving only touches keys
the user actually changed — tomlkit keeps everything else, comments included,
byte-identical. Validation is policy.loads() itself via PolicyDoc.validate();
errors block the save and land in the strip above the buttons. Cleared fields
delete their keys (unset = default / inherit); the adapter extra_args fields
distinguish "use profile defaults" (override off → key absent) from "replace
with this list" (override on, possibly empty — None ≠ []).
"""

from __future__ import annotations

import shlex
from dataclasses import dataclass
from itertools import groupby
from pathlib import Path

from rich.text import Text
from textual import events
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, VerticalScroll
from textual.screen import Screen
from textual.validation import Number
from textual.widget import Widget
from textual.widgets import (
    Button,
    Collapsible,
    Footer,
    Header,
    Input,
    Label,
    Select,
    Static,
    Switch,
    TextArea,
)

from ...engines.plugin import EDITOR_MODES
from ...policy import (
    BRANCH_PER_MODES,
    GATE_MODES,
    ISOLATION_MODES,
    MERGE_STRATEGIES,
    POLICY_FILE,
    RETRO_MODES,
    SWEEP_AUTO_MODES,
    AdapterPolicy,
    EnginePolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    ReviewPolicy,
    ScmPolicy,
    SweepPolicy,
    TuiPolicy,
)
from ..settings import STAGES, PolicyDoc


@dataclass(frozen=True)
class _Field:
    section: str
    key: str
    kind: str  # select | int | float | str | switch | lines | args
    options: tuple[str, ...] = ()
    default: object = None
    placeholder: str = ""
    minimum: float | None = None
    maximum: float | None = None
    label: str = ""  # display override; falls back to key when empty
    description: str = ""  # muted caption shown below the field row

    @property
    def widget_id(self) -> str:
        return f"{self.section}-{self.key}".replace(".", "-")


def _stage_fields(stage: str) -> tuple[_Field, ...]:
    section = f"adapter.{stage}"
    return (
        _Field(section, "name", "str", placeholder="inherit from [adapter]"),
        _Field(section, "model", "str", placeholder="inherit / client default"),
        _Field(section, "extra_args", "args"),
    )


_FIELDS: tuple[_Field, ...] = (
    _Field(
        "gates",
        "mode",
        "select",
        options=tuple(sorted(GATE_MODES)),
        default=GatesPolicy.mode,
    ),
    _Field(
        "gates",
        "retrospective",
        "select",
        options=tuple(sorted(RETRO_MODES)),
        default=GatesPolicy.retrospective,
    ),
    _Field(
        "review",
        "enabled",
        "switch",
        default=ReviewPolicy.enabled,
        label="separate review session",
        description=(
            "ON: triple review runs in a dedicated 2nd session · "
            "OFF: quick-dev runs its own tri-review inline, story straight to done"
        ),
    ),
    _Field(
        "limits",
        "max_review_cycles",
        "int",
        minimum=1,
        default=LimitsPolicy.max_review_cycles,
    ),
    _Field(
        "limits",
        "max_dev_attempts",
        "int",
        minimum=1,
        default=LimitsPolicy.max_dev_attempts,
    ),
    _Field(
        "limits",
        "session_timeout_min",
        "int",
        minimum=1,
        default=LimitsPolicy.session_timeout_min,
    ),
    _Field(
        "limits",
        "stop_without_result_nudges",
        "int",
        minimum=0,
        default=LimitsPolicy.stop_without_result_nudges,
    ),
    _Field(
        "limits",
        "max_tokens_per_story",
        "int",
        minimum=1,
        default=LimitsPolicy.max_tokens_per_story,
    ),
    _Field(
        "limits",
        "cache_read_weight",
        "float",
        minimum=0.0,
        maximum=1.0,
        default=LimitsPolicy.cache_read_weight,
    ),
    _Field("verify", "commands", "lines"),
    _Field("notify", "desktop", "switch", default=NotifyPolicy.desktop),
    _Field("notify", "file", "switch", default=NotifyPolicy.file),
    _Field("adapter", "name", "str", placeholder="CLI profile — default: claude"),
    _Field("adapter", "model", "str", placeholder="CLI default model"),
    _Field("adapter", "extra_args", "args"),
    _Field(
        "adapter",
        "cleanup_session_on_finish",
        "switch",
        default=AdapterPolicy.cleanup_session_on_finish,
    ),
    *(spec for stage in STAGES for spec in _stage_fields(stage)),
    _Field(
        "sweep",
        "auto",
        "select",
        options=tuple(sorted(SWEEP_AUTO_MODES)),
        default=SweepPolicy.auto,
    ),
    _Field("sweep", "max_bundles", "int", minimum=1, default=SweepPolicy.max_bundles),
    _Field(
        "sweep",
        "max_triage_attempts",
        "int",
        minimum=1,
        default=SweepPolicy.max_triage_attempts,
    ),
    _Field("sweep", "repeat", "switch", default=SweepPolicy.repeat),
    _Field("sweep", "max_cycles", "int", minimum=1, default=SweepPolicy.max_cycles),
    _Field(
        "scm",
        "isolation",
        "select",
        options=tuple(sorted(ISOLATION_MODES)),
        default=ScmPolicy.isolation,
        description=(
            "none: work in place on the checked-out branch (default) · "
            "worktree: run each story in its own git worktree, merge back to the target branch"
        ),
    ),
    _Field(
        "scm",
        "branch_per",
        "select",
        options=tuple(sorted(BRANCH_PER_MODES)),
        default=ScmPolicy.branch_per,
        description=(
            "worktree mode: one branch per story, or one shared branch per run "
            "(run forces delete-branch off so the shared branch survives)"
        ),
    ),
    _Field(
        "scm",
        "target_branch",
        "str",
        placeholder="default: the branch checked out at run start",
        description="worktree mode: branch all units merge back into (created if missing)",
    ),
    _Field(
        "scm",
        "merge_strategy",
        "select",
        options=tuple(sorted(MERGE_STRATEGIES)),
        default=ScmPolicy.merge_strategy,
        description="worktree mode: how a unit branch lands on the target — ff, merge, or squash",
    ),
    _Field(
        "scm",
        "delete_branch",
        "switch",
        default=ScmPolicy.delete_branch,
        description="worktree mode: delete the unit branch after a successful merge",
    ),
    _Field(
        "scm",
        "keep_failed",
        "switch",
        default=ScmPolicy.keep_failed,
        description="worktree mode: keep a failed unit's worktree + branch mounted for inspection",
    ),
    _Field(
        "scm",
        "seed_adapter_defaults",
        "switch",
        default=ScmPolicy.seed_adapter_defaults,
        label="seed adapter configs",
        description=(
            "worktree mode: copy each loaded adapter's gitignored MCP/CLI configs "
            "(.mcp.json, .claude/settings.json, .codex/config.toml …) into the worktree "
            "so isolated sessions can reach their MCP server"
        ),
    ),
    _Field(
        "scm",
        "worktree_seed",
        "lines",
        label="extra worktree seed files",
        description=(
            "worktree mode: additional project-relative gitignored files to copy into "
            "each worktree, one per line — on top of the adapter defaults above"
        ),
    ),
    _Field(
        "scm",
        "commit_message_template",
        "str",
        placeholder="blank = built-in default; {story_key} / {run_id} substituted",
        label="commit message template",
        description="commit message dev sessions use for a story/bundle commit when set",
    ),
    _Field(
        "scm",
        "failed_diff_max_mb",
        "int",
        minimum=1,
        default=ScmPolicy.failed_diff_max_mb,
        label="failed-diff size cap (MB)",
        description=(
            "per-file size limit for untracked files captured into a kept-failed "
            "unit's changes.patch · oversized files are skipped with a marker"
        ),
    ),
    _Field(
        "scm",
        "failed_diff_unlimited",
        "switch",
        default=ScmPolicy.failed_diff_unlimited,
        label="uncap failed-diff size",
        description=(
            "⚠ ON: capture failed-unit diffs with NO size limit (overrides the cap "
            "above) — may produce very large patches; a warning is logged when active"
        ),
    ),
    _Field(
        "engine",
        "name",
        "str",
        default=EnginePolicy.name,
        placeholder="'' = disabled",
        label="plugin name",
        description=(
            "game-engine plugin to enable — '' disables the layer (default); 'unity' "
            "is bundled; a custom plugin lives under .automator/engines/<name>/"
        ),
    ),
    _Field(
        "engine",
        "editor_mode",
        "select",
        options=tuple(sorted(EDITOR_MODES)),
        default=EnginePolicy.editor_mode,
        description=(
            "shared → agent works in place on your live Editor (needs scm.isolation = "
            "none); per_worktree → one managed Editor per worktree (needs isolation = "
            "worktree)"
        ),
    ),
    _Field(
        "engine",
        "mcp",
        "select",
        options=("ivanmurzak", "coplaydev"),
        default=EnginePolicy.mcp,
        label="Editor MCP",
        description="which Editor MCP server the plugin's scripts target",
    ),
    _Field(
        "engine",
        "unity_path",
        "str",
        default=EnginePolicy.unity_path,
        placeholder="'' = auto-detect",
        description=(
            "explicit Editor binary for a per_worktree launch; '' auto-detects · "
            "ignored in shared mode (your own Editor is reused)"
        ),
    ),
    _Field(
        "engine",
        "ready_timeout_sec",
        "int",
        minimum=1,
        default=EnginePolicy.ready_timeout_sec,
        description="how long the readiness gate blocks for the Editor + MCP to come up",
    ),
    _Field(
        "engine",
        "ready_grace_sec",
        "int",
        minimum=-1,
        default=EnginePolicy.ready_grace_sec,
        description=(
            "delay before the first readiness probe; -1 = auto (120s cold per_worktree, "
            "0s warm shared) · counts against ready_timeout_sec"
        ),
    ),
    _Field(
        "tui",
        "low_frame_rate",
        "switch",
        default=TuiPolicy.low_frame_rate,
        label="low frame rate",
        description=(
            "cap to 15fps + disable animations — fixes repaint tearing/garbage over "
            "slow or SSH links · takes effect next time the TUI launches"
        ),
    ),
)

# collect() sentinel for a field whose widget holds an unusable value
_INVALID = object()

# brief description shown after each section name in its collapsible header
_SECTION_DESC = {
    "gates": "approval gates, escalation & retrospective behavior",
    "review": "separate adversarial review session toggle",
    "limits": "cycle/attempt caps, timeout & token budget",
    "verify": "post-implementation verification commands",
    "notify": "desktop & file notifications",
    "adapter": "CLI client, model & bypass flags (base for all stages)",
    "adapter.dev": "dev-stage adapter overrides",
    "adapter.review": "review-stage adapter overrides",
    "adapter.triage": "triage-stage adapter overrides",
    "sweep": "deferred-work sweep automation",
    "scm": "git isolation, branching & merge-back",
    "engine": "opt-in layer for game projects driving a live Editor via MCP (Unity)",
    "tui": "dashboard rendering (slow-link / SSH tuning)",
}

# display name for a section's collapsible header, when it should differ from the
# raw TOML section key (which stays the policy key). Falls back to the key.
_SECTION_LABEL = {
    "engine": "Game Engine",
}


class SettingsScreen(Screen[None]):
    DEFAULT_CSS = """
    SettingsScreen #form {
        padding: 0 2;
    }
    SettingsScreen #note {
        padding: 1 1 0 1;
        color: $text-muted;
    }
    SettingsScreen .field {
        height: auto;
    }
    SettingsScreen .fieldname {
        width: 30;
        padding: 1 1;
        color: $text-muted;
    }
    SettingsScreen .fielddesc {
        padding: 0 1 1 32;
        color: $text-muted;
        text-style: italic;
    }
    SettingsScreen .field Input, SettingsScreen .field Select {
        width: 1fr;
    }
    SettingsScreen .field Switch {
        margin: 0 1;
    }
    SettingsScreen .field TextArea {
        width: 1fr;
        height: 5;
    }
    SettingsScreen .field TextArea.-editing {
        border: tall $accent;
    }
    SettingsScreen #errors {
        display: none;
        margin-top: 1;
        padding: 0 1;
        background: $error 20%;
    }
    SettingsScreen #errors.-visible {
        display: block;
    }
    SettingsScreen .buttons {
        height: auto;
        align-horizontal: right;
        padding: 1;
    }
    SettingsScreen .buttons Button {
        margin-left: 2;
    }
    """

    BINDINGS = [
        Binding("escape", "back", "back"),
        Binding("ctrl+s", "save", "save"),
        Binding("ctrl+e", "toggle_all", "expand/collapse all"),
        Binding("up", "nav_prev", "prev", show=False, priority=True),
        Binding("down", "nav_next", "next", show=False, priority=True),
        Binding("enter", "edit_field", "edit", show=False, priority=True),
    ]

    def __init__(self, project: Path, doc: PolicyDoc):
        super().__init__()
        self._path = project / POLICY_FILE
        self._doc = doc
        # The TextArea currently in cursor-edit mode (None = navigation mode).
        self._editing: Widget | None = None

    # ------------------------------------------------------------- compose

    def compose(self) -> ComposeResult:
        yield Header()
        with VerticalScroll(id="form"):
            yield Static(
                Text(
                    f"{self._path}\nrunning engines snapshot policy at start — "
                    "changes apply to new runs and resumes",
                    style="dim",
                ),
                id="note",
            )
            for section, fields in groupby(_FIELDS, key=lambda f: f.section):
                name = _SECTION_LABEL.get(section, section)
                desc = _SECTION_DESC.get(section)
                title = f"{name} — {desc}" if desc else name
                with Collapsible(title=title, collapsed=True):
                    for spec in fields:
                        yield from self._compose_field(spec)
            yield Static(id="errors")
            with Horizontal(classes="buttons"):
                yield Button("save", variant="primary", id="save")
                yield Button("cancel", id="cancel")
        yield Footer()

    def _compose_field(self, spec: _Field) -> ComposeResult:
        raw = self._doc.get(spec.section, spec.key)
        with Horizontal(classes="field"):
            yield Label(spec.label or spec.key, classes="fieldname")
            if spec.kind == "select":
                yield Select(
                    [(o, o) for o in spec.options],
                    prompt=f"default: {spec.default}",
                    value=str(raw) if raw in spec.options else Select.NULL,
                    id=spec.widget_id,
                )
            elif spec.kind in ("int", "float"):
                yield Input(
                    value=None if raw is None else str(raw),
                    placeholder=f"default: {spec.default}",
                    type="integer" if spec.kind == "int" else "number",
                    validators=[Number(minimum=spec.minimum, maximum=spec.maximum)],
                    valid_empty=True,
                    id=spec.widget_id,
                )
            elif spec.kind == "str":
                yield Input(
                    value=None if raw is None else str(raw),
                    placeholder=spec.placeholder,
                    id=spec.widget_id,
                )
            elif spec.kind == "switch":
                yield Switch(
                    value=bool(spec.default) if raw is None else bool(raw),
                    id=spec.widget_id,
                )
            elif spec.kind == "lines":
                yield TextArea(
                    "\n".join(str(c) for c in raw) if raw else "",
                    soft_wrap=False,
                    id=spec.widget_id,
                )
            elif spec.kind == "args":
                override = raw is not None
                yield Switch(value=override, id=f"{spec.widget_id}-override", classes="argswitch")
                yield Input(
                    value=shlex.join(str(a) for a in raw) if override else None,
                    placeholder="profile default flags — toggle to override",
                    disabled=not override,
                    id=spec.widget_id,
                )
        if spec.description:
            yield Static(spec.description, classes="fielddesc")

    def on_mount(self) -> None:
        self._show_errors(self._doc.validate())

    # ------------------------------------------------------------ keyboard

    def check_action(self, action: str, parameters: tuple[object, ...]) -> bool | None:
        # While editing a TextArea, let its own up/down move the cursor instead
        # of moving focus, and let escape exit edit mode rather than the screen.
        if action in ("nav_next", "nav_prev"):
            if self._editing is not None and self.focused is self._editing:
                return False  # TextArea cursor owns up/down in edit mode
            if any(select.expanded for select in self.query(Select)):
                return False  # an open dropdown owns up/down to pick an option
            return True
        if action == "edit_field":
            # Only intercept Enter to start cursor-editing a TextArea; for every
            # other widget Enter keeps its native meaning (open Select, toggle
            # Switch, submit Input) and for a TextArea already editing it inserts
            # a newline.
            return isinstance(self.focused, TextArea) and self._editing is None
        if action == "back":
            return self._editing is None
        return True

    def action_nav_next(self) -> None:
        self._exit_edit()
        self.focus_next()

    def action_nav_prev(self) -> None:
        self._exit_edit()
        self.focus_previous()

    def action_edit_field(self) -> None:
        area = self.focused
        if isinstance(area, TextArea):
            self._editing = area
            area.add_class("-editing")

    def action_toggle_all(self) -> None:
        self._exit_edit()
        sections = list(self.query(Collapsible))
        target = any(not c.collapsed for c in sections)  # something open -> collapse all
        for c in sections:
            c.collapsed = target

    def on_key(self, event: events.Key) -> None:
        if event.key == "escape" and self._editing is not None:
            self._exit_edit()
            event.stop()

    def _exit_edit(self) -> None:
        if self._editing is not None:
            self._editing.remove_class("-editing")
            self._editing = None

    # -------------------------------------------------------------- events

    def on_switch_changed(self, event: Switch.Changed) -> None:
        if event.switch.has_class("argswitch") and event.switch.id:
            target = event.switch.id.removesuffix("-override")
            self.query_one(f"#{target}", Input).disabled = not event.value

    def on_button_pressed(self, event: Button.Pressed) -> None:
        if event.button.id == "save":
            self.action_save()
        else:
            self.action_back()

    def action_back(self) -> None:
        self.app.pop_screen()

    # ---------------------------------------------------------------- save

    def _collect(self, spec: _Field, problems: list[str]) -> object:
        """Widget state -> raw policy value; None means unset (delete key)."""
        ident = f"{spec.section}.{spec.key}"
        if spec.kind == "select":
            select = self.query_one(f"#{spec.widget_id}", Select)
            return None if select.is_blank() else str(select.value)
        if spec.kind in ("int", "float"):
            box = self.query_one(f"#{spec.widget_id}", Input)
            value = box.value.strip()
            if not value:
                return None
            result = box.validate(value)
            if result is not None and not result.is_valid:
                problems.append(f"{ident}: {'; '.join(result.failure_descriptions)}")
                return _INVALID
            try:
                return int(value) if spec.kind == "int" else float(value)
            except ValueError:
                problems.append(f"{ident}: not a valid number")
                return _INVALID
        if spec.kind == "str":
            return self.query_one(f"#{spec.widget_id}", Input).value.strip() or None
        if spec.kind == "switch":
            return self.query_one(f"#{spec.widget_id}", Switch).value
        if spec.kind == "lines":
            text = self.query_one(f"#{spec.widget_id}", TextArea).text
            return [line.strip() for line in text.splitlines() if line.strip()]
        if spec.kind == "args":
            if not self.query_one(f"#{spec.widget_id}-override", Switch).value:
                return None
            try:
                return shlex.split(self.query_one(f"#{spec.widget_id}", Input).value)
            except ValueError as e:
                problems.append(f"{ident}: {e}")
                return _INVALID
        raise AssertionError(spec.kind)

    @staticmethod
    def _changed(spec: _Field, current: object, desired: object) -> bool:
        if desired is None:
            # adapter.model = "" means "CLI default" just like an absent key;
            # don't rewrite the template line for an untouched empty input
            return current is not None and not (spec.kind == "str" and current == "")
        if current is None:
            # an unset key already behaves like the default — don't bake it in
            if spec.kind == "switch" and desired == spec.default:
                return False
            if spec.kind == "lines" and desired == []:
                return False
            return True
        if isinstance(desired, list):
            return [str(item) for item in current] != desired
        return current != desired

    def action_save(self) -> None:
        problems: list[str] = []
        desired = {spec: self._collect(spec, problems) for spec in _FIELDS}
        if problems:
            self._show_errors("\n".join(problems))
            return
        for spec, value in desired.items():
            if self._changed(spec, self._doc.get(spec.section, spec.key), value):
                self._doc.set(spec.section, spec.key, value)
        error = self._doc.validate()
        if error:
            self._show_errors(error)
            return
        try:
            self._doc.save(self._path)
        except OSError as e:
            self._show_errors(f"save failed: {e}")
            return
        self.app.notify(f"policy saved: {self._path}")
        self.app.pop_screen()

    def _show_errors(self, message: str | None) -> None:
        strip = self.query_one("#errors", Static)
        if message:
            strip.update(Text(f"✗ {message}"))
            strip.add_class("-visible")
        else:
            strip.update("")
            strip.remove_class("-visible")
