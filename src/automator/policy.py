"""Policy-as-data: .automator/policy.toml -> immutable Policy dataclasses."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

POLICY_FILE = Path(".automator") / "policy.toml"

GATE_MODES = {"none", "per-epic", "per-story-spec-approval"}
RETRO_MODES = {"never", "notify", "auto"}
SWEEP_AUTO_MODES = {"never", "per-epic", "run-end"}
ISOLATION_MODES = {"none", "worktree"}
BRANCH_PER_MODES = {"story", "run"}
MERGE_STRATEGIES = {"ff", "merge", "squash"}
EDITOR_MODES = {"shared", "per_worktree"}


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class GatesPolicy:
    mode: str = "per-epic"
    on_escalation: str = "pause"  # CRITICAL escalations always pause; field reserved
    retrospective: str = "notify"


@dataclass(frozen=True)
class LimitsPolicy:
    max_review_cycles: int = 3
    max_dev_attempts: int = 2
    session_timeout_min: int = 90
    stop_without_result_nudges: int = 1
    max_tokens_per_story: int = 2_000_000
    # weight of cache-read tokens in the budget check (1.0 = count raw)
    cache_read_weight: float = 0.1


@dataclass(frozen=True)
class VerifyPolicy:
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyPolicy:
    desktop: bool = True
    file: bool = True


@dataclass(frozen=True)
class ReviewPolicy:
    # When False, the orchestrator skips the separate bmad-auto-review session;
    # the dev session runs quick-dev's own internal triple-review instead and
    # finalizes the story straight to done.
    enabled: bool = True


@dataclass(frozen=True)
class TuiPolicy:
    # low_frame_rate caps Textual to 15fps and disables animations (sets
    # TEXTUAL_FPS / TEXTUAL_ANIMATIONS before the app imports textual). Fixes
    # repaint tearing/garbage when driving the TUI over a slow/high-latency
    # link (SSH, Tailscale) where a 60fps update stream can't drain in time.
    low_frame_rate: bool = False


@dataclass(frozen=True)
class SweepPolicy:
    auto: str = "never"  # never | per-epic | run-end
    max_bundles: int = 5  # bundles executed per sweep; triage excess is truncated
    max_triage_attempts: int = 2
    max_migration_attempts: int = 2  # legacy-ledger migration retries before escalating
    repeat: bool = False  # re-triage after a cycle completes; continue on new deferred work
    max_cycles: int = 5  # total cycles per sweep run when repeat is on


@dataclass(frozen=True)
class StageAdapterPolicy:
    """Per-stage overrides; None = inherit from [adapter]."""

    name: str | None = None
    model: str | None = None
    extra_args: tuple[str, ...] | None = None


@dataclass(frozen=True)
class ResolvedAdapter:
    name: str
    model: str
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None


@dataclass(frozen=True)
class AdapterPolicy:
    name: str = "claude"  # CLI profile name; "claude-code-tmux" kept as legacy alias
    model: str = ""
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None = None
    # kill the run's bmad-auto-<id> tmux session when it finishes (False keeps
    # it around for post-run inspection)
    cleanup_session_on_finish: bool = True
    dev: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    review: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)
    triage: StageAdapterPolicy = field(default_factory=StageAdapterPolicy)

    def resolved(self, role: str) -> ResolvedAdapter:
        stage = {"dev": self.dev, "review": self.review, "triage": self.triage}.get(role)
        if stage is None:
            return ResolvedAdapter(self.name, self.model, self.extra_args)
        name = stage.name if stage.name is not None else self.name
        # model and extra_args are client-specific: inherit from the base only
        # when the stage runs the same client; a client switch falls back to
        # that profile's defaults (CLI default model, profile bypass flags).
        same_client = name == self.name
        return ResolvedAdapter(
            name=name,
            model=(stage.model if stage.model is not None else (self.model if same_client else "")),
            extra_args=(
                stage.extra_args
                if stage.extra_args is not None
                else (self.extra_args if same_client else None)
            ),
        )


@dataclass(frozen=True)
class ScmPolicy:
    # isolation = none  -> work happens in place on the checked-out branch
    #                      (today's behavior; no branches, no merge-back).
    # isolation = worktree -> each unit runs in its own git worktree/branch and
    #                      merges back into target_branch locally (Phase 3).
    isolation: str = "none"  # none | worktree
    branch_per: str = "story"  # story | run (worktree mode only)
    target_branch: str = ""  # "" = the branch checked out at run start
    merge_strategy: str = "merge"  # ff | merge | squash
    delete_branch: bool = True  # delete the unit branch after a successful merge
    keep_failed: bool = True  # keep a failed unit's worktree+branch for inspection
    # failed_diff_max_mb caps the per-file size (MB) of untracked files captured
    # into a kept-failed unit's forensic changes.patch, so a stray build dir or
    # huge log can't blow it up; oversized files are skipped with a labelled
    # marker in the patch. failed_diff_unlimited lifts the cap entirely (capture
    # everything regardless of size) — convenient but may produce very large
    # patches, so a warning is journalled when it's active.
    failed_diff_max_mb: int = 5
    failed_diff_unlimited: bool = False
    # commit_message_template, when non-empty, is the commit message dev sessions
    # use for a story's commit (placeholders {story_key} and {run_id} are
    # substituted). Empty = the built-in default message.
    commit_message_template: str = ""
    # max_parallel: units in flight at once. Parallel fan-out (Phase 5) is not
    # built yet, so any value > 1 is clamped to 1 in loads() — the knob exists
    # but is inert until the parallel scheduler lands.
    max_parallel: int = 1
    # A `git worktree add` checks out tracked files only, so gitignored MCP/CLI
    # configs are missing from every fresh worktree and isolated sessions can't
    # reach their MCP server. seed_adapter_defaults copies each loaded adapter's
    # own seed_files (e.g. claude -> .mcp.json/.claude/settings.json) into the
    # worktree; worktree_seed adds extra project-specific paths on top.
    seed_adapter_defaults: bool = True
    worktree_seed: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        # branch_per="run" shares a single branch across every unit in the run;
        # deleting it after the first unit's merge would defeat that (the next
        # unit would re-cut a fresh branch). Coerce delete_branch off so the
        # shared-branch semantics actually hold, regardless of how this policy
        # was constructed.
        if self.branch_per == "run" and self.delete_branch:
            object.__setattr__(self, "delete_branch", False)


@dataclass(frozen=True)
class EnginePolicy:
    # Game-engine plugin layer (niche, opt-in). name = "" leaves it disabled and
    # the orchestrator behaves exactly as before. name = "unity" (or any plugin
    # in automator/data/engines/ or .automator/engines/) turns it on.
    name: str = ""
    # shared        -> agent works in place on the project the live Editor already
    #                  has open (requires scm.isolation = none).
    # per_worktree  -> one managed Editor per worktree (requires isolation = worktree).
    editor_mode: str = "shared"
    # which Editor MCP server the plugin's scripts target; passed through to them.
    mcp: str = "ivanmurzak"
    # Editor binary used to launch a per_worktree Editor; "" lets the plugin
    # auto-detect. Ignored in shared mode (the operator's own Editor is reused).
    unity_path: str = ""
    # how long the readiness gate blocks for the Editor + MCP to come up.
    ready_timeout_sec: int = 600
    # seconds to wait before the FIRST readiness probe, to let a cold-launched
    # Editor start before we check it (a fast-failing probe against a not-yet-
    # listening Editor otherwise gives up early). Counts against ready_timeout_sec.
    # -1 = auto: the plugin picks a per-mode default (per_worktree launches a cold
    # Editor so it waits; shared reuses a warm one so it does not).
    ready_grace_sec: int = -1


@dataclass(frozen=True)
class Policy:
    gates: GatesPolicy = field(default_factory=GatesPolicy)
    limits: LimitsPolicy = field(default_factory=LimitsPolicy)
    verify: VerifyPolicy = field(default_factory=VerifyPolicy)
    notify: NotifyPolicy = field(default_factory=NotifyPolicy)
    review: ReviewPolicy = field(default_factory=ReviewPolicy)
    adapter: AdapterPolicy = field(default_factory=AdapterPolicy)
    sweep: SweepPolicy = field(default_factory=SweepPolicy)
    scm: ScmPolicy = field(default_factory=ScmPolicy)
    engine: EnginePolicy = field(default_factory=EnginePolicy)
    tui: TuiPolicy = field(default_factory=TuiPolicy)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{name}] must be a table")
    return value


def _stage_adapter(adapter_d: dict[str, Any], key: str) -> StageAdapterPolicy:
    raw = adapter_d.get(key, {})
    if not isinstance(raw, dict):
        raise PolicyError(f"[adapter.{key}] must be a table")
    raw_extra = raw.get("extra_args")
    return StageAdapterPolicy(
        name=None if raw.get("name") is None else str(raw["name"]),
        model=None if raw.get("model") is None else str(raw["model"]),
        extra_args=None if raw_extra is None else tuple(str(a) for a in raw_extra),
    )


def load(path: Path | None) -> Policy:
    """Load policy from a TOML file; a missing file yields all defaults."""
    if path is None or not path.is_file():
        return loads("")
    try:
        return loads(path.read_text(encoding="utf-8"))
    except PolicyError as e:
        raise PolicyError(f"{path}: {e}") from e


def loads(text: str) -> Policy:
    """Parse and validate policy TOML text; empty text yields all defaults."""
    try:
        doc: dict[str, Any] = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise PolicyError(f"invalid policy TOML: {e}") from e

    gates_d = _section(doc, "gates")
    limits_d = _section(doc, "limits")
    verify_d = _section(doc, "verify")
    notify_d = _section(doc, "notify")
    review_d = _section(doc, "review")
    adapter_d = _section(doc, "adapter")
    sweep_d = _section(doc, "sweep")
    scm_d = _section(doc, "scm")
    engine_d = _section(doc, "engine")
    tui_d = _section(doc, "tui")

    gates = GatesPolicy(
        mode=str(gates_d.get("mode", GatesPolicy.mode)),
        on_escalation=str(gates_d.get("on_escalation", GatesPolicy.on_escalation)),
        retrospective=str(gates_d.get("retrospective", GatesPolicy.retrospective)),
    )
    if gates.mode not in GATE_MODES:
        raise PolicyError(f"gates.mode must be one of {sorted(GATE_MODES)}: got {gates.mode!r}")
    if gates.retrospective not in RETRO_MODES:
        raise PolicyError(
            f"gates.retrospective must be one of {sorted(RETRO_MODES)}: got {gates.retrospective!r}"
        )

    limits = LimitsPolicy(
        max_review_cycles=int(limits_d.get("max_review_cycles", LimitsPolicy.max_review_cycles)),
        max_dev_attempts=int(limits_d.get("max_dev_attempts", LimitsPolicy.max_dev_attempts)),
        session_timeout_min=int(
            limits_d.get("session_timeout_min", LimitsPolicy.session_timeout_min)
        ),
        stop_without_result_nudges=int(
            limits_d.get("stop_without_result_nudges", LimitsPolicy.stop_without_result_nudges)
        ),
        max_tokens_per_story=int(
            limits_d.get("max_tokens_per_story", LimitsPolicy.max_tokens_per_story)
        ),
        cache_read_weight=float(limits_d.get("cache_read_weight", LimitsPolicy.cache_read_weight)),
    )
    if limits.max_review_cycles < 1 or limits.max_dev_attempts < 1:
        raise PolicyError("limits.max_review_cycles and limits.max_dev_attempts must be >= 1")
    if not 0.0 <= limits.cache_read_weight <= 1.0:
        raise PolicyError(
            f"limits.cache_read_weight must be between 0 and 1: got {limits.cache_read_weight}"
        )

    verify = VerifyPolicy(commands=tuple(str(c) for c in verify_d.get("commands", ())))
    notify = NotifyPolicy(
        desktop=bool(notify_d.get("desktop", NotifyPolicy.desktop)),
        file=bool(notify_d.get("file", NotifyPolicy.file)),
    )
    review = ReviewPolicy(enabled=bool(review_d.get("enabled", ReviewPolicy.enabled)))
    for legacy, replacement in (
        ("model_dev", "[adapter.dev] model"),
        ("model_review", "[adapter.review] model"),
    ):
        if legacy in adapter_d:
            raise PolicyError(f"adapter.{legacy} was removed — use {replacement} instead")
    raw_extra = adapter_d.get("extra_args")
    adapter = AdapterPolicy(
        name=str(adapter_d.get("name", AdapterPolicy.name)),
        model=str(adapter_d.get("model", AdapterPolicy.model)),
        extra_args=None if raw_extra is None else tuple(str(a) for a in raw_extra),
        cleanup_session_on_finish=bool(
            adapter_d.get("cleanup_session_on_finish", AdapterPolicy.cleanup_session_on_finish)
        ),
        dev=_stage_adapter(adapter_d, "dev"),
        review=_stage_adapter(adapter_d, "review"),
        triage=_stage_adapter(adapter_d, "triage"),
    )
    sweep = SweepPolicy(
        auto=str(sweep_d.get("auto", SweepPolicy.auto)),
        max_bundles=int(sweep_d.get("max_bundles", SweepPolicy.max_bundles)),
        max_triage_attempts=int(
            sweep_d.get("max_triage_attempts", SweepPolicy.max_triage_attempts)
        ),
        max_migration_attempts=int(
            sweep_d.get("max_migration_attempts", SweepPolicy.max_migration_attempts)
        ),
        repeat=bool(sweep_d.get("repeat", SweepPolicy.repeat)),
        max_cycles=int(sweep_d.get("max_cycles", SweepPolicy.max_cycles)),
    )
    if sweep.auto not in SWEEP_AUTO_MODES:
        raise PolicyError(
            f"sweep.auto must be one of {sorted(SWEEP_AUTO_MODES)}: got {sweep.auto!r}"
        )
    if (
        min(
            sweep.max_bundles,
            sweep.max_triage_attempts,
            sweep.max_migration_attempts,
            sweep.max_cycles,
        )
        < 1
    ):
        raise PolicyError(
            "sweep.max_bundles, sweep.max_triage_attempts, "
            "sweep.max_migration_attempts and sweep.max_cycles must be >= 1"
        )
    requested_parallel = int(scm_d.get("max_parallel", ScmPolicy.max_parallel))
    if requested_parallel < 1:
        raise PolicyError(f"scm.max_parallel must be >= 1: got {requested_parallel}")
    scm = ScmPolicy(
        isolation=str(scm_d.get("isolation", ScmPolicy.isolation)),
        branch_per=str(scm_d.get("branch_per", ScmPolicy.branch_per)),
        target_branch=str(scm_d.get("target_branch", ScmPolicy.target_branch)),
        merge_strategy=str(scm_d.get("merge_strategy", ScmPolicy.merge_strategy)),
        delete_branch=bool(scm_d.get("delete_branch", ScmPolicy.delete_branch)),
        keep_failed=bool(scm_d.get("keep_failed", ScmPolicy.keep_failed)),
        failed_diff_max_mb=int(scm_d.get("failed_diff_max_mb", ScmPolicy.failed_diff_max_mb)),
        failed_diff_unlimited=bool(
            scm_d.get("failed_diff_unlimited", ScmPolicy.failed_diff_unlimited)
        ),
        commit_message_template=str(
            scm_d.get("commit_message_template", ScmPolicy.commit_message_template)
        ),
        # Phase 5 parallel fan-out is unbuilt: clamp to 1 so the knob is inert.
        max_parallel=min(requested_parallel, 1),
        seed_adapter_defaults=bool(
            scm_d.get("seed_adapter_defaults", ScmPolicy.seed_adapter_defaults)
        ),
        worktree_seed=tuple(str(s) for s in scm_d.get("worktree_seed", ())),
    )
    if scm.isolation not in ISOLATION_MODES:
        raise PolicyError(
            f"scm.isolation must be one of {sorted(ISOLATION_MODES)}: got {scm.isolation!r}"
        )
    if scm.branch_per not in BRANCH_PER_MODES:
        raise PolicyError(
            f"scm.branch_per must be one of {sorted(BRANCH_PER_MODES)}: got {scm.branch_per!r}"
        )
    if scm.merge_strategy not in MERGE_STRATEGIES:
        raise PolicyError(
            f"scm.merge_strategy must be one of {sorted(MERGE_STRATEGIES)}: "
            f"got {scm.merge_strategy!r}"
        )
    if scm.failed_diff_max_mb < 1:
        raise PolicyError(f"scm.failed_diff_max_mb must be >= 1: got {scm.failed_diff_max_mb}")
    engine = EnginePolicy(
        name=str(engine_d.get("name", EnginePolicy.name)),
        editor_mode=str(engine_d.get("editor_mode", EnginePolicy.editor_mode)),
        mcp=str(engine_d.get("mcp", EnginePolicy.mcp)),
        unity_path=str(engine_d.get("unity_path", EnginePolicy.unity_path)),
        ready_timeout_sec=int(engine_d.get("ready_timeout_sec", EnginePolicy.ready_timeout_sec)),
        ready_grace_sec=int(engine_d.get("ready_grace_sec", EnginePolicy.ready_grace_sec)),
    )
    if engine.editor_mode not in EDITOR_MODES:
        raise PolicyError(
            f"engine.editor_mode must be one of {sorted(EDITOR_MODES)}: got {engine.editor_mode!r}"
        )
    if engine.ready_timeout_sec < 1:
        raise PolicyError(f"engine.ready_timeout_sec must be >= 1: got {engine.ready_timeout_sec}")
    # editor_mode and scm.isolation are coupled: a live Editor MCP must act on the
    # same folder the agent edits. shared = the agent's warm Editor on the checkout
    # in place (no worktree); per_worktree = one Editor per isolated worktree.
    if engine.name:
        if engine.editor_mode == "shared" and scm.isolation != "none":
            raise PolicyError(
                "engine.editor_mode = 'shared' requires scm.isolation = 'none' "
                f"(the agent works in place on the live Editor's checkout); got "
                f"scm.isolation = {scm.isolation!r}"
            )
        if engine.editor_mode == "per_worktree" and scm.isolation != "worktree":
            raise PolicyError(
                "engine.editor_mode = 'per_worktree' requires scm.isolation = 'worktree'; "
                f"got scm.isolation = {scm.isolation!r}"
            )
    tui = TuiPolicy(low_frame_rate=bool(tui_d.get("low_frame_rate", TuiPolicy.low_frame_rate)))
    return Policy(
        gates=gates,
        limits=limits,
        verify=verify,
        notify=notify,
        review=review,
        adapter=adapter,
        sweep=sweep,
        scm=scm,
        engine=engine,
        tui=tui,
    )


POLICY_TEMPLATE = """\
# bmad-auto orchestration policy. All keys optional; defaults shown.

[gates]
mode = "per-epic"            # none | per-epic | per-story-spec-approval
retrospective = "notify"     # never | notify | auto (auto unsupported in v1)

[limits]
max_review_cycles = 3
max_dev_attempts = 2
session_timeout_min = 90
stop_without_result_nudges = 1
max_tokens_per_story = 2000000
cache_read_weight = 0.1      # cache reads bill at ~0.1x input on all vendors; 1.0 = count raw

[verify]
# Deterministic gates run by the orchestrator after a clean review, before commit.
commands = []                # e.g. ["pytest -q", "ruff check ."]

[notify]
desktop = true               # notify-send, best-effort
file = true                  # ATTENTION file in the run dir

[review]
# enabled = true  -> run the separate bmad-auto-review session after each dev pass
#                    (quick-dev's own internal triple-review is skipped in this mode).
# enabled = false -> skip that session; the dev pass runs quick-dev's internal
#                    triple-review instead and finalizes the story straight to done.
enabled = true

[adapter]
name = "claude"              # claude | codex | gemini | <custom .automator/profiles/*.toml>
model = ""                   # empty = CLI default model
cleanup_session_on_finish = true  # kill the run's tmux session when it finishes (false keeps it for inspection)
# extra_args replaces the profile's default permission-bypass flags when set:
# extra_args = ["--permission-mode", "bypassPermissions"]

# Per-stage overrides for the dev, review and sweep-triage passes. Unset keys
# inherit from [adapter] when the stage runs the same client; a stage that
# switches client falls back to that profile's defaults instead (model and
# extra_args are client-specific). Stage tables must come after the [adapter]
# keys above.
# [adapter.dev]
# model = "opus"
# [adapter.review]
# name = "codex"
# model = "gpt-5-codex"
# [adapter.triage]
# model = "opus"

[sweep]
# Deferred-work sweep: triage + execute open deferred-work.md entries.
auto = "never"               # never | per-epic | run-end (auto-triggered sweeps never prompt)
max_bundles = 5              # bundles executed per sweep; triage excess is truncated
max_triage_attempts = 2      # triage validation retries before escalating
max_migration_attempts = 2   # legacy-ledger migration retries before escalating
repeat = false               # after a cycle completes, re-triage and continue on newly deferred work
max_cycles = 5               # safety cap on total cycles per sweep run when repeat = true

[scm]
# Source-control isolation + merge-back. Defaults reproduce today's behavior:
# work happens in place on the checked-out branch, with no branches.
isolation = "none"           # none | worktree
branch_per = "story"         # story | run (worktree mode only; "run" forces delete_branch = false)
target_branch = ""           # "" = the branch checked out at run start
merge_strategy = "merge"     # ff | merge | squash (worktree mode merges the unit branch into target locally)
delete_branch = true         # delete the unit branch after a successful merge
keep_failed = true           # keep a failed unit's worktree+branch for inspection
failed_diff_max_mb = 5       # per-file size cap (MB) for untracked files in a kept-failed unit's changes.patch; oversized files are skipped with a marker
failed_diff_unlimited = false # true = capture the failed-unit diff with no size cap (may produce very large patches; warns when active)
# commit_message_template: when set, the commit message dev sessions use for a
# story's commit. {story_key} and {run_id} are substituted. Empty = built-in default.
commit_message_template = ""
max_parallel = 1             # units in flight at once (parallel fan-out unbuilt; values > 1 clamp to 1)
# A git worktree checks out tracked files only, so gitignored MCP/CLI configs are
# absent from every fresh worktree and isolated sessions can't reach their MCP
# server. seed_adapter_defaults copies each loaded adapter's own config files
# (claude -> .mcp.json/.claude/settings.json, codex -> .codex/config.toml, etc.)
# into the worktree; worktree_seed adds extra project-specific gitignored paths.
seed_adapter_defaults = true # seed each loaded adapter's default gitignored configs into worktrees
worktree_seed = []           # extra gitignored files to copy into each worktree, on top of adapter defaults

[engine]
# Niche, opt-in game-engine layer for projects whose dev/sweep cycle drives a
# live engine Editor (e.g. a Unity project the agent controls via an Editor MCP).
# Leave name empty for normal projects — nothing changes.
name = ""                    # "" = disabled. "unity" enables the bundled Unity plugin.
# editor_mode couples with [scm] isolation, because a live Editor MCP must act on
# the same folder the agent edits:
#   shared       -> agent works in place on the project the live Editor already has
#                   open. Requires scm.isolation = "none". Zero relaunches.
#   per_worktree -> one managed Editor per worktree. Requires scm.isolation = "worktree".
editor_mode = "shared"       # shared | per_worktree
mcp = "ivanmurzak"           # which Editor MCP the plugin scripts target: ivanmurzak | coplaydev
unity_path = ""              # Editor binary for a per_worktree launch ("" = auto-detect; unused in shared)
ready_timeout_sec = 600      # how long the readiness gate waits for the Editor + MCP to come up
ready_grace_sec = -1         # delay before the first readiness probe (lets a cold Editor start);
                             # -1 = auto (per_worktree waits, shared does not). Counts against ready_timeout_sec.

[tui]
# low_frame_rate = true caps Textual to 15fps and disables animations (sets
# TEXTUAL_FPS=15 / TEXTUAL_ANIMATIONS=none at launch). Fixes repaint tearing
# over slow/high-latency links (SSH, Tailscale). Equivalent to launching with
# `bmad-auto tui --low-frame-rate`. Takes effect the next time the TUI starts.
low_frame_rate = false
"""
