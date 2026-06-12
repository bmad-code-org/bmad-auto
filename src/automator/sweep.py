"""Deferred-work sweep: triage the ledger, decide, execute bundles.

A sweep is its own run type. One LLM triage session classifies every open
deferred-work entry (verified against actual code — ledger statuses are
unreliable); the orchestrator validates the result deterministically, asks
the human about decision items (interactive runs only), then drives each
work bundle through the inherited dev -> review -> verify -> commit pipeline.
The orchestrator performs all ledger edits it can do deterministically and
gates on the ones it delegates (verify.verify_review_bundle).
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import deferredwork, gates, verify
from .engine import Engine
from .escalation import critical_escalations
from .model import Phase, StoryTask
from .statemachine import advance


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


TRIAGE_KEY = "sweep-triage"
TRIAGE_WORKFLOW = "deferred-sweep-triage"
MIGRATE_KEY = "sweep-migrate"
MIGRATE_WORKFLOW = "deferred-sweep-migrate"
BUNDLE_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,39}$")
DECISION_EFFECTS = ("build", "close", "keep-open")


# ------------------------------------------------------------- triage plan


@dataclass(frozen=True)
class ResolvedEntry:
    id: str
    evidence: str


@dataclass(frozen=True)
class Bundle:
    name: str
    dw_ids: tuple[str, ...]
    intent: str
    decision_note: str = ""  # human-decision context appended to the intent file


@dataclass(frozen=True)
class DecisionOption:
    key: str
    label: str
    effect: str  # build | close | keep-open
    intent: str = ""  # required when effect == "build"
    resolution: str = ""  # optional when effect == "close"
    bundle_name: str = ""  # optional name override for the built bundle


@dataclass(frozen=True)
class Decision:
    id: str
    question: str
    context: str
    options: tuple[DecisionOption, ...]
    recommendation: str

    def option(self, key: str) -> DecisionOption | None:
        for opt in self.options:
            if opt.key == key:
                return opt
        return None


@dataclass(frozen=True)
class TriagePlan:
    open_ids: frozenset[str]
    already_resolved: tuple[ResolvedEntry, ...] = ()
    bundles: tuple[Bundle, ...] = ()
    blocked: tuple[tuple[str, str], ...] = ()  # (id, blocker)
    skip: tuple[tuple[str, str], ...] = ()  # (id, reason)
    decisions: tuple[Decision, ...] = ()


def validate_triage(
    rj: dict[str, Any] | None, expected_open_ids: set[str] | None
) -> tuple[TriagePlan | None, list[str]]:
    """Deterministic validation of the triage session's result.json. Returns
    (plan, []) or (None, errors). expected_open_ids=None skips the ledger
    equality check (used when reloading a previously validated plan)."""
    errors: list[str] = []
    rj = rj or {}
    if rj.get("workflow") != TRIAGE_WORKFLOW:
        return None, [f"workflow must be {TRIAGE_WORKFLOW!r}: got {rj.get('workflow')!r}"]

    claimed_open = {str(i) for i in rj.get("open_ids", [])}
    if expected_open_ids is not None and claimed_open != expected_open_ids:
        missed = sorted(expected_open_ids - claimed_open)
        invented = sorted(claimed_open - expected_open_ids)
        return None, [
            "open_ids do not match the ledger's open entries"
            + (f"; missing: {', '.join(missed)}" if missed else "")
            + (f"; not open in the ledger: {', '.join(invented)}" if invented else "")
        ]
    universe = expected_open_ids if expected_open_ids is not None else claimed_open

    seen: dict[str, str] = {}  # id -> category that claimed it

    def claim(dw_id: str, category: str) -> None:
        if dw_id not in universe:
            errors.append(f"{category} references unknown/closed id {dw_id}")
        elif dw_id in seen:
            errors.append(f"{dw_id} appears in both {seen[dw_id]} and {category}")
        else:
            seen[dw_id] = category

    resolved = []
    for item in rj.get("already_resolved", []):
        dw_id = str(item.get("id", ""))
        evidence = str(item.get("evidence", "")).strip()
        claim(dw_id, "already_resolved")
        if not evidence:
            errors.append(f"already_resolved {dw_id} has no evidence")
        resolved.append(ResolvedEntry(dw_id, evidence))

    bundles = []
    names: set[str] = set()
    for item in rj.get("bundles", []):
        name = str(item.get("name", ""))
        if not BUNDLE_NAME_RE.match(name):
            errors.append(f"bundle name {name!r} invalid (want {BUNDLE_NAME_RE.pattern})")
        if name in names:
            errors.append(f"duplicate bundle name {name!r}")
        names.add(name)
        dw_ids = [str(i) for i in item.get("dw_ids", [])]
        if not dw_ids:
            errors.append(f"bundle {name!r} has no dw_ids")
        for dw_id in dw_ids:
            claim(dw_id, f"bundle {name!r}")
        intent = str(item.get("intent", "")).strip()
        if not intent:
            errors.append(f"bundle {name!r} has no intent")
        bundles.append(Bundle(name, tuple(dw_ids), intent))

    blocked = []
    for item in rj.get("blocked", []):
        dw_id = str(item.get("id", ""))
        blocker = str(item.get("blocker", "")).strip()
        claim(dw_id, "blocked")
        if not blocker:
            errors.append(f"blocked {dw_id} names no blocker")
        blocked.append((dw_id, blocker))

    skip = []
    for item in rj.get("skip", []):
        dw_id = str(item.get("id", ""))
        reason = str(item.get("reason", "")).strip()
        claim(dw_id, "skip")
        if not reason:
            errors.append(f"skip {dw_id} gives no reason")
        skip.append((dw_id, reason))

    decisions = []
    for item in rj.get("decisions", []):
        dw_id = str(item.get("id", ""))
        claim(dw_id, "decisions")
        question = str(item.get("question", "")).strip()
        if not question:
            errors.append(f"decision {dw_id} has no question")
        options = []
        keys: set[str] = set()
        for raw in item.get("options", []):
            key = str(raw.get("key", ""))
            effect = str(raw.get("effect", ""))
            intent = str(raw.get("intent", "")).strip()
            if not key or key in keys:
                errors.append(f"decision {dw_id}: missing/duplicate option key {key!r}")
            keys.add(key)
            if effect not in DECISION_EFFECTS:
                errors.append(f"decision {dw_id} option {key}: bad effect {effect!r}")
            if effect == "build" and not intent:
                errors.append(f"decision {dw_id} option {key}: effect 'build' needs intent")
            bundle_name = str(raw.get("bundle_name", ""))
            if bundle_name and not BUNDLE_NAME_RE.match(bundle_name):
                errors.append(f"decision {dw_id} option {key}: bad bundle_name {bundle_name!r}")
            options.append(
                DecisionOption(
                    key=key,
                    label=str(raw.get("label", "")).strip() or key,
                    effect=effect,
                    intent=intent,
                    resolution=str(raw.get("resolution", "")).strip(),
                    bundle_name=bundle_name,
                )
            )
        if len(options) < 2:
            errors.append(f"decision {dw_id} needs at least 2 options")
        recommendation = str(item.get("recommendation", ""))
        if recommendation not in keys:
            errors.append(f"decision {dw_id}: recommendation {recommendation!r} not an option")
        decisions.append(
            Decision(
                dw_id,
                question,
                str(item.get("context", "")).strip(),
                tuple(options),
                recommendation,
            )
        )

    unclaimed = sorted(universe - set(seen))
    if unclaimed:
        errors.append(f"open entries not triaged: {', '.join(unclaimed)}")

    if errors:
        return None, errors
    return (
        TriagePlan(
            open_ids=frozenset(universe),
            already_resolved=tuple(resolved),
            bundles=tuple(bundles),
            blocked=tuple(blocked),
            skip=tuple(skip),
            decisions=tuple(decisions),
        ),
        [],
    )


# ---------------------------------------------------------- migration plan


def validate_migration(
    rj: dict[str, Any] | None,
    manifest: list[dict[str, Any]],
    pre_canonical: dict[str, str],
    new_text: str,
) -> list[str]:
    """Deterministic validation of a legacy-ledger migration session: the
    rewritten ledger must contain zero legacy items, preserve every
    pre-existing canonical entry's status, continue DW numbering, and the
    result.json mapping must cover the manifest exactly. Returns errors,
    empty on success."""
    rj = rj or {}
    if rj.get("workflow") != MIGRATE_WORKFLOW:
        return [f"workflow must be {MIGRATE_WORKFLOW!r}: got {rj.get('workflow')!r}"]
    errors: list[str] = []

    leftovers = deferredwork.parse_legacy(new_text)
    if leftovers:
        listed = "; ".join(f"{e.section or 'top level'}: {e.title[:60]}" for e in leftovers[:10])
        errors.append(f"{len(leftovers)} legacy item(s) still parse as legacy: {listed}")

    entries: dict[str, deferredwork.DWEntry] = {}
    dupes: set[str] = set()
    for e in deferredwork.parse_ledger(new_text):
        if e.id in entries:
            dupes.add(e.id)
        entries[e.id] = e
    if dupes:
        errors.append("duplicate DW ids: " + ", ".join(sorted(dupes)))

    def first_word(status: str) -> str:
        return status.split()[0] if status.split() else ""

    pre_max = max((int(i.split("-")[1]) for i in pre_canonical), default=0)
    for dw_id, status in pre_canonical.items():
        e = entries.get(dw_id)
        if e is None:
            errors.append(f"pre-existing {dw_id} disappeared")
        elif first_word(e.status) != first_word(status):
            errors.append(f"pre-existing {dw_id} status changed: {status!r} -> {e.status!r}")
    for dw_id, e in entries.items():
        if dw_id in pre_canonical:
            continue
        if int(dw_id.split("-")[1]) <= pre_max:
            errors.append(f"new entry {dw_id} does not continue numbering past DW-{pre_max}")
        if first_word(e.status) not in ("open", "done"):
            errors.append(f"new entry {dw_id} has status {e.status!r}; want open or done")

    manifest_by_key = {str(m["key"]): m for m in manifest}
    mapping = rj.get("mapping", [])
    if not isinstance(mapping, list):
        return errors + ["mapping must be a list of {key, dw_id}"]
    seen_keys: set[str] = set()
    for item in mapping:
        key = str(item.get("key", "")) if isinstance(item, dict) else ""
        dw_id = str(item.get("dw_id", "")) if isinstance(item, dict) else ""
        source = manifest_by_key.get(key)
        if source is None:
            errors.append(f"mapping invents unknown key {key!r}")
            continue
        if key in seen_keys:
            errors.append(f"mapping repeats key {key!r}")
        seen_keys.add(key)
        target = entries.get(dw_id)
        if target is None:
            errors.append(f"mapping {key} -> {dw_id}: no such entry in the ledger")
        elif (first_word(target.status) == "done") != bool(source["done"]):
            want = "done" if source["done"] else "open"
            errors.append(f"mapping {key} -> {dw_id}: manifest says {want}, ledger disagrees")
    missing = sorted(set(manifest_by_key) - seen_keys)
    if missing:
        errors.append("manifest keys not mapped: " + ", ".join(missing))
    return errors


# --------------------------------------------------------------- prompting


class DecisionPrompter:
    """Walks the human through pending decisions on the terminal. Injection
    points exist so tests can script answers.

    The interactive terminal prompt is the v1 protocol: observers (the TUI
    dashboard, ATTENTION watchers) learn a sweep is blocked from the
    decision-pending journal event written just before ask() and attach to
    the sweep's tmux window to answer. A decisions-file protocol — engine
    writes the pending question to a file and polls for an answer the TUI
    could write in-app — is deliberately deferred to v2; it needs timeout +
    ownership semantics this run-blocking prompt avoids."""

    def __init__(
        self,
        input_fn: Callable[[str], str] = input,
        print_fn: Callable[[str], None] = print,
    ):
        self.input_fn = input_fn
        self.print_fn = print_fn

    def ask(self, decision: Decision) -> DecisionOption:
        p = self.print_fn
        p("")
        p(f"── decision needed: {decision.id} " + "─" * 30)
        p(decision.question)
        if decision.context:
            p("")
            p(decision.context)
        p("")
        for opt in decision.options:
            marker = "  (recommended)" if opt.key == decision.recommendation else ""
            p(f"  [{opt.key}] {opt.label} — {opt.effect}{marker}")
            if opt.intent:
                p(f"      {opt.intent}")
        keys = [o.key for o in decision.options]
        while True:
            raw = self.input_fn(
                f"choice [{'/'.join(keys)}] (enter = {decision.recommendation}): "
            ).strip()
            if not raw:
                raw = decision.recommendation
            chosen = decision.option(raw)
            if chosen is not None:
                return chosen
            p(f"  invalid choice {raw!r}")


# ------------------------------------------------------------ sweep engine


class SweepEngine(Engine):
    """Engine variant whose loop processes the deferred-work ledger instead
    of sprint-status. Bundles reuse the inherited story pipeline through the
    override seams; the triage session has its own phase pair."""

    def __init__(
        self,
        *args: Any,
        triage_adapter: Any = None,
        prompting: bool = False,
        decisions_only: bool = False,
        max_bundles: int | None = None,
        prompter: DecisionPrompter | None = None,
        **kwargs: Any,
    ):
        super().__init__(*args, **kwargs)
        self.adapters["triage"] = (
            triage_adapter if triage_adapter is not None else self.adapters["dev"]
        )
        self.prompting = prompting
        self.decisions_only = decisions_only
        self.max_bundles = max_bundles if max_bundles is not None else self.policy.sweep.max_bundles
        self.prompter = prompter or DecisionPrompter()
        self.state.run_type = "sweep"

    # the date stamped into ledger edits; isolated for tests
    def _today(self) -> str:
        return time.strftime("%Y-%m-%d")

    # ------------------------------------------------------------ main loop

    def _loop(self) -> None:
        ledger = self.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        if deferredwork.has_legacy(text):
            self._ensure_migration(text)
            text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        open_now = deferredwork.open_ids(text)
        if not open_now:
            self.journal.append("sweep-nothing-open", ledger=str(ledger))
            return

        plan = self._ensure_triage(open_now)
        self._close_resolved(plan)
        answers = self._decisions_phase(plan)
        bundles = self._materialize_bundles(plan, answers)
        if self.decisions_only:
            self.journal.append("sweep-decisions-only", bundles_not_run=len(bundles))
            return
        for bundle in bundles:
            self._run_bundle(bundle)

    def _run_story(self, task: StoryTask) -> None:
        # no spec-approval gate for bundles: the bundle intent came from the
        # validated triage plan (and, for decision bundles, from the human)
        if self._dev_phase(task):
            self._review_and_commit(task)

    def _run_bundle(self, bundle: Bundle) -> None:
        key = f"dw-{bundle.name}"
        task = self.state.tasks.get(key)
        if task is not None and task.terminal:
            return  # finished (or adjudicated) in a previous resume cycle
        if task is None:
            task = StoryTask(story_key=key, epic=0, dw_ids=list(bundle.dw_ids))
            self.state.tasks[key] = task
            self.journal.append("bundle-start", story_key=key, dw_ids=list(bundle.dw_ids))
        else:
            # interrupted mid-bundle: same recovery as Engine._finish_inflight
            self.journal.append("resume-restart", story_key=key, phase=str(task.phase))
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                self._save()
                self._review_and_commit(task)
                return
            if task.baseline_commit:
                self._reset_to(task.baseline_commit)
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        task.bundle_file = str(self._write_intent(bundle))
        self._save()
        self._run_story(task)

    # ------------------------------------------------------------ migration

    def _ensure_migration(self, text: str) -> None:
        """Pre-DW-format ledger content (older BMAD-method projects) blocks a
        sweep: open_ids() cannot see it and mark_done() cannot flip it. One
        LLM session rewrites the legacy items into canonical DW entries; the
        orchestrator pins exactly what to convert (a manifest from
        parse_legacy), validates the rewrite deterministically, and restores
        the original ledger before any retry."""
        ledger = self.paths.deferred_work
        task = self.state.tasks.get(MIGRATE_KEY)
        if task is None:
            task = StoryTask(story_key=MIGRATE_KEY, epic=0)
            self.state.tasks[MIGRATE_KEY] = task
        elif task.phase != Phase.PENDING:
            # resumed mid-migration or retrying after an escalation: restart
            self.journal.append("resume-restart", story_key=MIGRATE_KEY, phase=str(task.phase))
            if task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
            if task.baseline_commit and not verify.worktree_clean(self.paths.project):
                self._reset_to(task.baseline_commit)  # a session died mid-rewrite
                text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition
        if not task.baseline_commit:
            task.baseline_commit = verify.rev_parse_head(self.paths.project)

        legacy = deferredwork.parse_legacy(text)
        pre_canonical = {e.id: e.status for e in deferredwork.parse_ledger(text)}
        manifest = [
            {
                "key": e.key,
                "id": e.id,
                "title": e.title,
                "section": e.section,
                "done": e.done,
                "severity": e.severity,
            }
            for e in legacy
        ]
        manifest_path = self.run_dir / "migrate-manifest.json"
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._migrate_prompt(manifest_path, feedback),
                seq=task.attempt,
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from migration session: {details}")
            new_text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
            if result.status != "completed":
                errors = [f"migration session {result.status}"]
            else:
                errors = validate_migration(result.result_json, manifest, pre_canonical, new_text)
            self.journal.append(
                "migrate-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=not errors,
                errors=errors,
            )
            if not errors:
                advance(task, Phase.DONE)
                self._save()
                (self.run_dir / "migrate-result.json").write_text(
                    json.dumps(result.result_json, indent=2), encoding="utf-8"
                )
                self._commit_ledger(
                    "chore(sweep): migrate legacy deferred-work entries to DW format"
                )
                post = deferredwork.parse_ledger(new_text)
                self.journal.append(
                    "sweep-migrated",
                    converted=len(manifest),
                    entries_now=len(post),
                    open_now=sum(1 for e in post if e.open),
                )
                return
            # never re-prompt over a half-broken rewrite; the baseline reset
            # covers tracked files, the explicit write covers an untracked
            # ledger that `git reset` cannot restore
            self._reset_to(task.baseline_commit)
            ledger.parent.mkdir(parents=True, exist_ok=True)
            ledger.write_text(text, encoding="utf-8")
            if task.attempt >= self.policy.sweep.max_migration_attempts:
                self._escalate(
                    task, "migration failed deterministic validation: " + "; ".join(errors)
                )
            feedback = self._write_feedback(
                task,
                "The legacy-ledger migration failed deterministic validation:\n- "
                + "\n- ".join(errors),
            )

    def _migrate_prompt(self, manifest: Path, feedback: Path | None) -> str:
        prompt = f"/bmad-auto-sweep --migrate {manifest}"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    # --------------------------------------------------------------- triage

    def _ensure_triage(self, open_now: set[str]) -> TriagePlan:
        triage_path = self.run_dir / "triage.json"
        if triage_path.is_file():
            # already validated this run; the ledger has moved since (closes,
            # decisions), so skip the open-set equality re-check
            plan, errors = validate_triage(_read_json(triage_path), None)
            if plan is not None:
                return plan
            self.journal.append("sweep-triage-reload-failed", errors=errors)

        task = self.state.tasks.get(TRIAGE_KEY)
        if task is None:
            task = StoryTask(story_key=TRIAGE_KEY, epic=0)
            self.state.tasks[TRIAGE_KEY] = task
        elif task.phase != Phase.PENDING:
            # resumed mid-triage or retrying after an escalation: restart
            self.journal.append("resume-restart", story_key=TRIAGE_KEY, phase=str(task.phase))
            if task.phase == Phase.ESCALATED:
                task.attempt = 0  # the human resumed deliberately; fresh budget
            task.phase = Phase.PENDING  # deliberate reset, not a normal transition

        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.TRIAGE_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="triage",
                prompt=self._triage_prompt(feedback),
                seq=task.attempt,
            )
            advance(task, Phase.TRIAGE_VERIFY)
            self._save()
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from triage session: {details}")
            if result.status != "completed":
                plan, errors = None, [f"triage session {result.status}"]
            else:
                plan, errors = validate_triage(result.result_json, open_now)
            self.journal.append(
                "triage-decision",
                attempt=task.attempt,
                session_status=result.status,
                ok=plan is not None,
                errors=errors,
            )
            if plan is not None:
                advance(task, Phase.DONE)
                self._save()
                triage_path.write_text(json.dumps(result.result_json, indent=2), encoding="utf-8")
                self.journal.append(
                    "sweep-triage-result",
                    bundles=len(plan.bundles),
                    decisions=len(plan.decisions),
                    already_resolved=len(plan.already_resolved),
                    blocked=len(plan.blocked),
                    skip=len(plan.skip),
                )
                return plan
            if task.attempt >= self.policy.sweep.max_triage_attempts:
                self._escalate(task, "triage output failed validation: " + "; ".join(errors))
            feedback = self._write_feedback(
                task,
                "The triage result.json failed deterministic validation:\n- " + "\n- ".join(errors),
            )

    def _triage_prompt(self, feedback: Path | None) -> str:
        prompt = "/bmad-auto-sweep"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    # ------------------------------------------------------ ledger phases

    def _close_resolved(self, plan: TriagePlan) -> None:
        ledger = self.paths.deferred_work
        closed = []
        for entry in plan.already_resolved:
            if deferredwork.mark_done(
                ledger, entry.id, self._today(), f"already resolved: {entry.evidence}"
            ):
                closed.append(entry.id)
        if closed:
            self.journal.append("sweep-resolved-closed", dw_ids=closed)
        self._commit_ledger("chore(sweep): close resolved deferred-work entries")

    def _decisions_phase(self, plan: TriagePlan) -> dict[str, dict[str, str]]:
        decisions_path = self.run_dir / "decisions.json"
        answers: dict[str, dict[str, str]] = (
            _read_json(decisions_path) if decisions_path.is_file() else {}
        )
        pending = [d for d in plan.decisions if d.id not in answers]
        if not self.prompting:
            for decision in pending:
                self.journal.append("decision-skipped-unattended", dw_id=decision.id)
            if pending:
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"{len(pending)} deferred-work decisions pending",
                    "run `bmad-auto sweep` interactively to answer them",
                )
        else:
            for decision in pending:
                # announce before blocking on input so observers (TUI, ATTENTION
                # watchers) can tell a sweep is waiting on a human
                self.journal.append(
                    "decision-pending", dw_id=decision.id, question=decision.question
                )
                gates.notify(
                    self.policy,
                    self.run_dir,
                    f"decision needed: {decision.id}",
                    decision.question,
                )
                option = self.prompter.ask(decision)
                answers[decision.id] = {
                    "key": option.key,
                    "label": option.label,
                    "effect": option.effect,
                    "answered_at": self._today(),
                }
                tmp = decisions_path.with_suffix(".tmp")
                tmp.write_text(json.dumps(answers, indent=2), encoding="utf-8")
                tmp.replace(decisions_path)
                self.journal.append(
                    "decision-answered",
                    dw_id=decision.id,
                    key=option.key,
                    effect=option.effect,
                )
                self._apply_decision_effect(decision, option)
        self._commit_ledger("chore(sweep): record deferred-work decisions")
        return answers

    def _apply_decision_effect(self, decision: Decision, option: DecisionOption) -> None:
        ledger = self.paths.deferred_work
        detail = option.resolution or option.intent
        deferredwork.append_decision(ledger, decision.id, self._today(), option.label, detail)
        if option.effect == "close":
            note = "closed by human decision" + (
                f": {option.resolution}" if option.resolution else ""
            )
            deferredwork.mark_done(ledger, decision.id, self._today(), note)

    def _commit_ledger(self, message: str) -> None:
        """Commit pending orchestrator ledger edits; bundles need a clean
        baseline. No-op when the tree is already clean."""
        if verify.worktree_clean(self.paths.project):
            return
        sha = verify.commit_story(self.paths.project, message)
        self.journal.append("sweep-ledger-commit", message=message, commit=sha)

    # ---------------------------------------------------------- bundles

    def _materialize_bundles(
        self, plan: TriagePlan, answers: dict[str, dict[str, str]]
    ) -> list[Bundle]:
        bundles = list(plan.bundles)
        for decision in plan.decisions:
            answer = answers.get(decision.id)
            if not answer or answer.get("effect") != "build":
                continue
            option = decision.option(str(answer.get("key")))
            if option is None or option.effect != "build":
                continue
            name = option.bundle_name or "decision-" + decision.id.lower()
            bundles.append(
                Bundle(
                    name=name,
                    dw_ids=(decision.id,),
                    intent=option.intent,
                    decision_note=(
                        f"The human chose option {option.key} ({option.label}) for the "
                        f"question: {decision.question}"
                    ),
                )
            )
        if len(bundles) > self.max_bundles:
            dropped = [b.name for b in bundles[self.max_bundles :]]
            self.journal.append("sweep-bundles-truncated", dropped=dropped)
            bundles = bundles[: self.max_bundles]
        return bundles

    def _write_intent(self, bundle: Bundle) -> Path:
        ledger = self.paths.deferred_work
        text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
        entries = {e.id: e for e in deferredwork.parse_ledger(text)}
        blocks = [entries[i].body.rstrip() for i in bundle.dw_ids if i in entries]
        lines = [
            f"# Deferred-work bundle: {bundle.name}",
            "",
            f"bundle_name: {bundle.name}",
            f"dw_ids: {', '.join(bundle.dw_ids)}",
            "",
            "## Intent",
            "",
            bundle.intent,
        ]
        if bundle.decision_note:
            lines += ["", "## Human decision", "", bundle.decision_note]
        lines += ["", "## Ledger entries (verbatim)", "", "\n\n".join(blocks), ""]
        path = self.run_dir / "bundles" / bundle.name / "intent.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("\n".join(lines), encoding="utf-8")
        return path

    # ------------------------------------------------------ override seams

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        prompt = f"/bmad-auto-dev --dw-bundle {task.bundle_file}"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev_bundle(task, self.paths, result_json)

    def _verify_review(self, task: StoryTask):
        return verify.verify_review_bundle(task, self.paths, self.policy)

    def _commit_message(self, task: StoryTask) -> str:
        return f"sweep {task.story_key}: {', '.join(task.dw_ids)} via bmad-auto"
