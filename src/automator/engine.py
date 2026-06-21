"""The deterministic control loop.

Per story: dev session -> artifact verification -> bounded review loop
-> deterministic verify commands -> orchestrator commit. The engine never
edits sprint-status.yaml or spec files; it re-reads them to decide and
verify. All creative work happens inside disposable adapter sessions.
"""

from __future__ import annotations

import shutil
import signal
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from . import gates, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec
from .bmadconfig import ProjectPaths
from .escalation import (
    Action,
    critical_escalations,
    decide_dev,
    decide_review_session,
    preference_escalations,
)
from .install import provision_worktree
from .journal import Journal, save_state
from .model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_ESCALATION,
    PAUSE_SPEC_APPROVAL,
    Phase,
    RunState,
    SessionRecord,
    StoryTask,
)
from .plugins import HookBus, HookContext, PluginRegistry
from .policy import Policy
from .runs import kill_session
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable
from .statemachine import advance
from .workspace import (
    UnitWorkspace,
    Workspace,
    close_unit_workspace,
    discard_worktree,
    open_unit_workspace,
    unit_worktrees_dir,
)


class RunPaused(Exception):
    def __init__(self, reason: str, stage: str, story_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.story_key = story_key


class RunStopped(Exception):
    """Raised from the SIGTERM/SIGINT handler to unwind the loop cleanly so the
    engine can mark the run `stopped` (a deliberate stop, distinct from a
    crash) and tear down its in-flight agent session."""


@dataclass(frozen=True)
class RunSummary:
    run_id: str
    done: int
    deferred: int
    escalated: int
    paused: bool
    paused_reason: str
    total_tokens: int

    def render(self) -> str:
        lines = [
            f"run {self.run_id}: {self.done} done, {self.deferred} deferred, "
            f"{self.escalated} escalated, {self.total_tokens:,} tokens"
        ]
        if self.paused:
            lines.append(f"PAUSED: {self.paused_reason}")
        return "\n".join(lines)


# CLI profile name -> the agent id the Unity-MCP CLI's `setup-mcp` expects (see
# `unity-mcp-cli setup-mcp --list`). All but claude differ only by claude's
# "-code" suffix; codex/gemini/cursor and any custom profile pass through as-is.
_SETUP_MCP_AGENT_IDS = {"claude": "claude-code"}


def _setup_mcp_agent_id(profile_name: str) -> str:
    """Map a CLI profile name to its Unity-MCP `setup-mcp` agent id."""
    return _SETUP_MCP_AGENT_IDS.get(profile_name, profile_name)


class Engine:
    # The engine that installed the process-wide stop handlers; nested
    # auto-sweep runs (same process) see it set and let RunStopped propagate up.
    _stop_signals_owner: "Engine | None" = None

    def __init__(
        self,
        paths: ProjectPaths,
        policy: Policy,
        adapter: CodingCLIAdapter,
        run_dir: Path,
        journal: Journal,
        state: RunState,
        max_stories: int | None = None,
        epic_filter: int | None = None,
        story_filter: str | None = None,
        review_adapter: CodingCLIAdapter | None = None,
        sweep_factory: Callable[[str], None] | None = None,
        registry: PluginRegistry | None = None,
    ):
        self.paths = paths
        # where code+git work + artifact reads happen. isolation="none" (today's
        # only mode) → the repo root in place; Phase 3 swaps in per-unit worktrees.
        self.workspace = Workspace.default(paths)
        self.policy = policy
        self.adapters = {
            "dev": adapter,
            "review": review_adapter if review_adapter is not None else adapter,
        }
        self.run_dir = run_dir
        self.journal = journal
        self.state = state
        self.max_stories = max_stories
        self.epic_filter = epic_filter
        self.story_filter = story_filter
        # spawns a child deferred-work sweep run (injected by the CLI to
        # avoid an engine -> sweep import cycle); see _maybe_auto_sweep
        self.sweep_factory = sweep_factory
        # plugin hook bus. Built silently (no journal handed to the registry) so a
        # zero-plugin run — the only builtin is the data-only `example` — adds
        # nothing to the journal and stays byte-identical to today. The bus
        # journals actual hook activity itself; a single "plugins-active" line
        # records the live plugins only when at least one binds a stage. The
        # game-engine layer (Unity) is now itself a plugin: enabling it in
        # [plugins] gives it lifecycle hooks that gate/manage the Editor.
        self._registry = (
            registry if registry is not None else PluginRegistry.build(self.paths.repo_root, policy)
        )
        # let every in-process plugin reject an incompatible config at startup
        # (e.g. the Unity plugin's editor_mode↔scm.isolation coupling) so the run
        # fails fast rather than mid-unit.
        self._registry.validate(policy)
        self._bus = HookBus(self._registry, journal)
        # stages at which some active plugin injects a provided workflow session
        # (Phase 4). Precomputed once for an O(1) guard so a run whose plugins
        # provide no workflows stays byte-identical (no extra sessions, no journal).
        self._workflow_stages = self._registry.workflow_stages()
        if self._bus.any_active():
            self.journal.append("plugins-active", plugins=self._bus.active_plugins())
        # stop-signal bookkeeping (see run())
        self._owns_signals = False
        self._stopping = False
        self._prev_handlers: dict[int, object] = {}

    # ------------------------------------------------------------- top level

    def run(self) -> RunSummary:
        self._install_stop_signals()
        try:
            try:
                # target-branch setup can raise RunPaused (detached HEAD, unborn
                # repo), so it must sit inside the pause handler, not before it.
                self._emit_run_boundary("pre_run")
                self._ensure_target_branch()
                self._loop()
                self.state.finished = True
                self._gc_run_worktrees()
                self._emit("post_run")
                self.journal.append("run-complete")
                # tear down the run's agent session now that it finished. Only
                # the outermost engine owns this (nested auto-sweep never sets
                # _owns_signals); stop already kills it, and pause/interrupt
                # leave it for resume to reuse.
                if self._owns_signals and self.policy.adapter.cleanup_session_on_finish:
                    kill_session(self.state.run_id)
            except RunPaused as pause:
                self.state.paused_reason = pause.reason
                self.state.paused_stage = pause.stage
                self.state.paused_story_key = pause.story_key
                self.journal.append(
                    "run-paused",
                    reason=pause.reason,
                    stage=pause.stage,
                    story_key=pause.story_key,
                )
            except RunStopped:
                # the loop was interrupted inside adapter.run(), so the agent
                # window is still live — tear the whole run session down.
                kill_session(self.state.run_id)
                if not self._owns_signals:
                    raise  # nested auto-sweep: let the owner record the stop
                self.state.stopped = True
                self.journal.append("run-stop")
            finally:
                self._save()
        finally:
            self._restore_stop_signals()
        summary = self.summary()
        gates.notify(self.policy, self.run_dir, "bmad-auto run finished", summary.render())
        return summary

    # ---------------------------------------------------------- stop signals

    def _install_stop_signals(self) -> None:
        """Make SIGTERM/SIGINT unwind the loop as a RunStopped. Only the
        outermost engine in the process owns the handlers (nested auto-sweep
        runs let the exception propagate up to it); install is best-effort and
        silently skipped off the main thread (signal.signal raises there)."""
        if Engine._stop_signals_owner is not None:
            return

        def handler(signum, frame):  # noqa: ANN001 - stdlib signal signature
            if self._stopping:
                return  # already unwinding; don't re-raise during teardown
            self._stopping = True
            raise RunStopped()

        try:
            for sig in (signal.SIGTERM, signal.SIGINT):
                self._prev_handlers[sig] = signal.signal(sig, handler)
        except ValueError:
            # not on the main thread — cannot install; degrade to no handler
            self._restore_stop_signals()
            return
        self._owns_signals = True
        Engine._stop_signals_owner = self

    def _restore_stop_signals(self) -> None:
        for sig, prev in self._prev_handlers.items():
            try:
                signal.signal(sig, prev)
            except (ValueError, TypeError):
                pass
        self._prev_handlers.clear()
        if Engine._stop_signals_owner is self:
            Engine._stop_signals_owner = None
        self._owns_signals = False

    # ----------------------------------------------------- worktree isolation

    @property
    def _isolated(self) -> bool:
        return self.policy.scm.isolation == "worktree"

    def _ensure_target_branch(self) -> None:
        """Resolve (once, at run start) the branch every unit merges back into.

        No-op unless isolation=worktree. Default target is the branch checked out
        now; a configured target is created if missing and checked out in the
        main repo (merges land on whatever the main repo has checked out, and a
        unit worktree must never check out the target itself). Pinned in state so
        resume keeps targeting the same branch."""
        if not self._isolated or self.state.target_branch:
            return
        if self.policy.scm.failed_diff_unlimited:
            # the safety cap is off; make sure the operator knows a failed unit
            # could write a very large forensic patch.
            self.journal.append(
                "scm-failed-diff-unlimited",
                note="failed-unit diff capture is uncapped (scm.failed_diff_unlimited); "
                "changes.patch may be very large",
            )
        repo = self.paths.repo_root
        configured = self.policy.scm.target_branch.strip()
        if configured:
            if not verify.branch_exists(repo, configured):
                try:
                    verify.create_branch(repo, configured, "HEAD")
                except verify.GitError as e:
                    # e.g. an unborn repo (no commit to base a branch on).
                    raise RunPaused(
                        f"cannot create target branch {configured!r}: {e}",
                        PAUSE_ESCALATION,
                        "",
                    ) from e
                self.journal.append("target-branch-created", branch=configured)
            if verify.current_branch(repo) != configured:
                verify.checkout_branch(repo, configured)
                self.journal.append("target-branch-checkout", branch=configured)
            self.state.target_branch = configured
        else:
            current = verify.current_branch(repo)
            if current == "HEAD":
                # detached HEAD has no branch to merge into; merges would land on
                # an unreferenced commit. Require a real branch (or a configured
                # target) before isolating work into worktrees.
                raise RunPaused(
                    "isolation=worktree on a detached HEAD: check out a branch or "
                    "set scm.target_branch before running",
                    PAUSE_ESCALATION,
                    "",
                )
            self.state.target_branch = current
        self.journal.append("target-branch", branch=self.state.target_branch)
        self._save()

    def _worktree_profiles(self):
        """The distinct CLI profiles of the dev + review adapters, for provisioning
        their skills/hooks into a worktree. Adapters without a `profile` (e.g. test
        fakes) contribute nothing, so provisioning is a no-op for them."""
        seen: dict[str, object] = {}
        for adapter in (self.adapters["dev"], self.adapters["review"]):
            profile = getattr(adapter, "profile", None)
            if profile is not None and profile.name not in seen:
                seen[profile.name] = profile
        return list(seen.values())

    def _engine_agent_ids(self) -> list[str]:
        """The Unity-MCP `setup-mcp` agent ids for every CLI that runs in a
        worktree (dev + review). A worktree can host more than one agent — e.g.
        dev=claude, review=codex — and each reads its own MCP config file, so the
        per_worktree setup must point every one of them at the worktree's Editor,
        not just the dev agent. Deduped, order-preserving; empty for test fakes."""
        ids: list[str] = []
        for profile in self._worktree_profiles():
            agent = _setup_mcp_agent_id(profile.name)
            if agent not in ids:
                ids.append(agent)
        return ids

    def _run_isolated(self, task: StoryTask, drive: Callable[[StoryTask], None]) -> None:
        """Run one unit's `drive` body in a fresh per-unit worktree, then merge
        it back into the target branch. `drive` either returns (DONE/DEFERRED →
        integrate) or raises RunPaused (spec-approval gate / escalation → leave
        the worktree mounted for resume/inspection, integration skipped)."""
        try:
            unit = open_unit_workspace(
                self.paths.repo_root,
                self.paths,
                self.state.run_id,
                task.story_key,
                self.state.target_branch,
                self.policy.scm.branch_per,
                self.run_dir,
            )
        except verify.GitError as e:
            # could not mount a worktree (e.g. branch_per=run with a kept-failed
            # unit still holding the shared branch). Defer this unit rather than
            # crash the whole run; the operator can free the branch and re-run.
            task.defer_reason = f"could not open worktree: {e}"
            task.phase = Phase.DEFERRED  # deliberate: no legal move from PENDING
            self.journal.append("worktree-open-failed", story_key=task.story_key, error=str(e))
            gates.notify(
                self.policy, self.run_dir, f"worktree open failed: {task.story_key}", str(e)
            )
            self._save()
            return
        task.worktree_path = str(unit.path)
        task.branch = unit.branch
        # A worktree checks out tracked files only, but the bmad-auto-* skill
        # trees + signal-hook config are typically gitignored, so they are absent
        # from the fresh checkout. Re-lay them into the worktree so the session
        # finds /bmad-auto-dev and the Stop-signal hook fires. Also seed the loaded
        # adapters' gitignored MCP/CLI configs so isolated sessions can reach their
        # MCP server (seed_adapter_defaults) plus any extra project-listed paths.
        profiles = self._worktree_profiles()
        scm = self.policy.scm
        seeds: list[str] = []
        if scm.seed_adapter_defaults:
            for profile in profiles:
                seeds.extend(profile.seed_files)
        seeds.extend(scm.worktree_seed)
        # plugins (e.g. the Unity engine) may prime an isolated checkout with
        # gitignored paths they need — e.g. an MCP-generated skill tree + client
        # config so the worktree's Editor MCP is reachable. Aggregate every loaded
        # plugin's declared seeds.
        seeds.extend(self._registry.seed_files())
        provision_worktree(
            unit.path,
            profiles,
            self.paths.repo_root,
            seed_files=list(dict.fromkeys(seeds)),  # dedupe, preserve order
            seed_globs=self._registry.seed_globs(),
        )
        self.journal.append(
            "worktree-opened", story_key=task.story_key, branch=unit.branch, path=str(unit.path)
        )
        self._save()
        prev = self.workspace
        self.workspace = unit.workspace
        try:
            # A plugin (e.g. the Unity engine) may launch the unit's managed Editor
            # at pre_worktree_setup + wait for its MCP at pre_ready_gate before
            # driving. A veto (defer) at either stage leaves the task DEFERRED and
            # skips drive(); both fall through to _integrate_unit, which tears the
            # (empty) worktree down via the DEFERRED path.
            if self._gate_unit(task):
                self._emit("post_worktree_setup", task)
                drive(task)
        finally:
            # always run teardown — on success, on a deferral, and on a RunPaused
            # (spec gate / escalation) propagating through — before the workspace is
            # restored, so a managed Editor never outlives its worktree. Teardown
            # stages are observe-only (a veto here cannot un-tear-down).
            self._emit("pre_worktree_teardown", task)
            self._emit("post_worktree_teardown", task)
            self.workspace = prev
        # reached only on a normal return (DONE or DEFERRED); a RunPaused from the
        # spec gate or an escalation propagates past here, leaving the worktree up.
        self._integrate_unit(task, unit)

    def _failed_diff_max_bytes(self) -> int | None:
        """Per-untracked-file size cap for a failed unit's forensic patch, in
        bytes — or None when the operator lifted the cap (scm.failed_diff_unlimited)."""
        scm = self.policy.scm
        if scm.failed_diff_unlimited:
            return None
        return scm.failed_diff_max_mb * 1_048_576

    def _integrate_unit(self, task: StoryTask, unit: UnitWorkspace) -> None:
        self._emit("pre_integrate", task)
        scm = self.policy.scm
        if task.phase == Phase.DONE:
            # Merge the unit branch into the target branch locally. We open PRs
            # ourselves by hand once the branch has landed; the orchestrator only
            # commits the worktree onto the selected target.
            self._merge_local(task, unit)
        else:  # DEFERRED — capture the diff, keep or drop per keep_failed
            patch = close_unit_workspace(
                unit,
                success=False,
                keep_failed=scm.keep_failed,
                run_dir=self.run_dir,
                unit_key=task.story_key,
                delete_branch=scm.delete_branch,
                diff_max_file_bytes=self._failed_diff_max_bytes(),
            )
            self.journal.append(
                "unit-closed",
                story_key=task.story_key,
                branch=unit.branch,
                kept=scm.keep_failed,
                patch=str(patch) if patch else None,
            )

    def _merge_local(self, task: StoryTask, unit: UnitWorkspace) -> None:
        """Merge a DONE unit's branch into the target branch from the main repo."""
        self._emit("pre_merge", task)
        scm = self.policy.scm
        repo = self.paths.repo_root
        target = self.state.target_branch
        # A per_worktree Unity Editor can leak asset writes into the *main*
        # checkout (see the unity plugin's worktree setup), dirtying the target with the very
        # files this branch already committed. Reconcile that first: clean only
        # the leaked copies of incoming files; refuse (escalate) if anything dirty
        # falls outside this branch's path set — that may be real operator work.
        try:
            cleaned = verify.clean_incoming_collisions(repo, target, unit.branch)
        except verify.GitError as e:
            reason = (
                f"merge of {unit.branch} into {target} blocked: the target checkout has "
                f"uncommitted changes that are not part of this branch (likely a Unity "
                f"Editor wrote into the main project) — clean them, then "
                f"`bmad-auto resume {self.state.run_id}`. {e}"
            )
            self._keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return
        if cleaned:
            self.journal.append(
                "merge-target-cleaned",
                story_key=task.story_key,
                branch=unit.branch,
                paths=cleaned,
            )
        try:
            verify.merge_branch(
                repo,
                unit.branch,
                strategy=scm.merge_strategy,
                message=self._merge_message(task),
            )
        except verify.GitError as e:
            # genuine content conflict against the target: keep the branch for
            # manual merge. The unit committed cleanly (phase is already DONE,
            # which has no legal transition), so escalate directly.
            reason = (
                f"merge of {unit.branch} into {target} failed "
                f"(content conflict against the target): {e}"
            )
            self._keep_branch_and_escalate(task, unit, reason)  # always raises RunPaused
            return  # defensive: never fall through to the success teardown below
        self.journal.append(
            "unit-merged",
            story_key=task.story_key,
            branch=unit.branch,
            target=self.state.target_branch,
        )
        self._emit("post_merge", task)
        close_unit_workspace(
            unit,
            success=True,
            keep_failed=scm.keep_failed,
            run_dir=self.run_dir,
            unit_key=task.story_key,
            delete_branch=scm.delete_branch,
        )

    def _keep_branch_and_escalate(self, task: StoryTask, unit: UnitWorkspace, reason: str) -> None:
        """Preserve a DONE unit's branch (no delete, kept for manual merge) and
        escalate. Shared by the two merge-back failure paths: a target dirtied
        with stray work, and a genuine content conflict."""
        close_unit_workspace(
            unit,
            success=False,
            keep_failed=True,
            run_dir=self.run_dir,
            unit_key=task.story_key,
            delete_branch=False,
            diff_max_file_bytes=self._failed_diff_max_bytes(),
        )
        self._escalate_unit(task, reason)  # always raises RunPaused

    def _escalate_unit(self, task: StoryTask, reason: str) -> None:
        """Mark a DONE unit ESCALATED, notify, and pause the run. DONE has no
        legal transition, so the phase is set directly rather than via advance()."""
        task.phase = Phase.ESCALATED
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-auto resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _merge_message(self, task: StoryTask) -> str:
        return f"Merge {task.branch} into {self.state.target_branch} (bmad-auto)"

    def _gc_run_worktrees(self) -> None:
        """Reclaim this run's worktree scaffolding once it finishes cleanly.

        DONE units drop their worktree at merge time; this is a safety net for a
        worktree leaked by a crash between merge and teardown, plus it prunes
        stale git admin entries and removes the now-empty run worktree dir.
        Worktrees deliberately kept for inspection (a kept-failed/escalated unit)
        are left in place and journaled so the operator can find them."""
        if not self._isolated:
            return
        repo = self.paths.repo_root
        for task in self.state.tasks.values():
            if task.phase == Phase.DONE and task.worktree_path:
                wt = Path(task.worktree_path)
                if wt.is_dir():
                    discard_worktree(repo, task.worktree_path, task.branch)
            elif task.terminal and task.worktree_path and Path(task.worktree_path).is_dir():
                # kept on purpose (keep_failed): leave it, but surface where.
                self.journal.append(
                    "worktree-kept", story_key=task.story_key, path=task.worktree_path
                )
        verify.worktree_prune(repo)
        worktrees_parent = unit_worktrees_dir(self.run_dir)
        if worktrees_parent.is_dir() and not any(worktrees_parent.iterdir()):
            worktrees_parent.rmdir()

    def _reopen_unit(self, task: StoryTask) -> UnitWorkspace:
        """Reconstruct the UnitWorkspace for an in-flight unit on resume, from
        the worktree path + branch persisted on the task. The worktree must still
        be mounted — if it was pruned out from under us we cannot safely reuse it,
        so escalate rather than run a session in a missing directory."""
        wt = Path(task.worktree_path)
        if not wt.is_dir():
            self._escalate_unit(
                task,
                f"worktree for {task.story_key} is gone ({wt}); cannot resume in place",
            )
        # spec_file is persisted relative to the worktree (model.to_dict) so the
        # state stays portable; re-absolutize it against the reopened worktree.
        if task.spec_file and not Path(task.spec_file).is_absolute():
            task.spec_file = str(wt / task.spec_file)
        return UnitWorkspace(
            workspace=Workspace(root=wt, paths=self.paths.rebased(wt)),
            repo_root=self.paths.repo_root,
            branch=task.branch,
            path=wt,
            baseline=task.baseline_commit or "",
        )

    def summary(self) -> RunSummary:
        tasks = self.state.tasks.values()
        return RunSummary(
            run_id=self.state.run_id,
            done=sum(1 for t in tasks if t.phase == Phase.DONE),
            deferred=sum(1 for t in tasks if t.phase == Phase.DEFERRED),
            escalated=sum(1 for t in tasks if t.phase == Phase.ESCALATED),
            paused=self.state.paused,
            paused_reason=self.state.paused_reason or "",
            total_tokens=sum(t.tokens.total for t in tasks),
        )

    def _loop(self) -> None:
        self._finish_inflight()
        started = 0
        while True:
            if self.max_stories is not None and started >= self.max_stories:
                self.journal.append("max-stories-reached", count=started)
                return
            self._emit("pre_pick_next")
            story = self._pick_next()
            self._emit("post_pick_next", story_key=(story.key if story is not None else None))
            if story is None:
                self._maybe_auto_sweep("run-end", "run-end")
                return
            if self.state.current_epic is not None and story.epic != self.state.current_epic:
                self._epic_boundary(self.state.current_epic, story.epic)
            self.state.current_epic = story.epic
            task = StoryTask(story_key=story.key, epic=story.epic)
            self.state.tasks[story.key] = task
            self.journal.append("story-start", story_key=story.key)
            self._save()
            started += 1
            self._run_story(task)

    def _pick_next(self):
        ss = load_sprint_status(self.paths.sprint_status)
        if ss.unknown_keys:
            self.journal.append("sprint-status-unknown-keys", keys=list(ss.unknown_keys))
        skip = set(self.state.tasks)  # anything this run already touched
        while True:
            story = next_actionable(ss, skip)
            if story is None:
                return None
            if self.epic_filter is not None and story.epic != self.epic_filter:
                skip.add(story.key)
                continue
            if self.story_filter is not None and story.key != self.story_filter:
                skip.add(story.key)
                continue
            return story

    def _rollback_or_pause(self, task: StoryTask) -> None:
        """Recover from a failed in-place attempt.

        With ``scm.rollback_on_failure`` OFF (default) the orchestrator never
        touches the working tree: it emits a bold manual-recovery notice and
        pauses the run (stop-and-wait), so nothing proceeds on a half-finished
        tree. With it ON, it does the safest possible automatic rollback —
        revert the attempt's tracked changes to baseline and delete only the
        untracked files this run created (the whole BMAD output folder and every
        pre-existing untracked file are preserved; there is no blanket
        ``git clean``)."""
        if not self.policy.scm.rollback_on_failure:
            self._pause_for_manual_recovery(task, task.baseline_commit or "")
            return  # unreachable: _pause_for_manual_recovery always raises
        self.journal.append(
            "rollback-auto",
            story_key=task.story_key,
            baseline=task.baseline_commit or "",
            note="reverting tracked changes + run-created untracked files",
        )
        self._safe_reset(task)

    def _safe_reset(self, task: StoryTask) -> None:
        """Revert tracked changes to the task baseline and remove only the
        untracked files this run created — never a blanket `git clean`. Used by
        the gated rollback (when enabled) and by internal ledger recovery (sweep
        migration), which restores the orchestrator's own state and must not
        pause."""
        keep = [".automator"]
        for protected in (
            self.workspace.paths.output_folder,
            self.workspace.paths.implementation_artifacts,
            self.workspace.paths.planning_artifacts,
        ):
            try:
                keep.append(str(protected.relative_to(self.workspace.root)))
            except ValueError:
                pass  # configured outside the repo; nothing to protect here
        verify.safe_rollback(
            self.workspace.root,
            task.baseline_commit or "",
            baseline_untracked=task.baseline_untracked,
            keep=tuple(keep),
        )

    def _pause_for_manual_recovery(self, task: StoryTask, baseline: str) -> None:
        """OFF path: leave the tree untouched, surface bold manual-recovery
        instructions, and pause the run. Always raises RunPaused."""
        short = baseline[:12] or "the run's baseline commit"
        notice = (
            "**ACTION REQUIRED — manual rollback needed**\n"
            f"Story **{task.story_key}** failed and auto-rollback is OFF, so the "
            "working tree was left exactly as-is for you to inspect.\n"
            "To discard this attempt yourself:\n"
            "  1. **BACK UP any untracked files you want to keep** — the reset "
            "below deletes uncommitted work.\n"
            f"  2. `git reset --hard {short}` then review/remove leftover "
            "untracked files.\n"
            "  3. **Restore the files you backed up in step 1.**\n"
            f"Then run `bmad-auto resume {self.state.run_id}`. To let the "
            "orchestrator do a safe automatic rollback next time, enable "
            "`[scm] rollback_on_failure` (it discards the attempt's uncommitted "
            "work but never deletes pre-existing untracked files)."
        )
        self.journal.append("rollback-manual-required", story_key=task.story_key, baseline=baseline)
        gates.notify(
            self.policy,
            self.run_dir,
            f"ACTION REQUIRED: manual rollback for {task.story_key}",
            notice,
        )
        self._save()
        raise RunPaused(notice, PAUSE_ESCALATION, task.story_key)

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            isolated = self._isolated and task.worktree_path
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                # paused at the spec-approval gate: dev verified, review pending
                self.journal.append("resume-review", story_key=task.story_key)
                if isolated:
                    unit = self._reopen_unit(task)
                    prev = self.workspace
                    self.workspace = unit.workspace
                    try:
                        self._review_and_commit(task)
                    finally:
                        self.workspace = prev
                    self._integrate_unit(task, unit)
                else:
                    self._review_and_commit(task)
            else:
                self.journal.append(
                    "resume-restart", story_key=task.story_key, phase=str(task.phase)
                )
                if isolated:
                    # drop the half-built worktree; _run_story mounts a fresh one
                    discard_worktree(self.paths.repo_root, task.worktree_path, task.branch)
                    task.worktree_path = ""
                    task.branch = ""
                elif task.baseline_commit:
                    self._rollback_or_pause(task)
                task.phase = Phase.PENDING  # deliberate reset, not a normal transition
                self._save()
                self._run_story(task)

    # ------------------------------------------------------------- per story

    def _gate_unit(self, task: StoryTask) -> bool:
        """per_worktree gate: emit ``pre_worktree_setup`` then ``pre_ready_gate``
        so a plugin (e.g. the Unity engine) can launch + wait for the unit's
        managed Editor. Returns True to proceed; a veto at either stage routes the
        unit to DEFERRED/PAUSE via ``_vetoed`` (which raises on pause) and returns
        False. A zero-plugin run takes the O(1) fast path and proceeds."""
        ctx = self._emit("pre_worktree_setup", task)
        if self._vetoed(ctx, task):
            return False
        ctx = self._emit("pre_ready_gate", task)
        if self._vetoed(ctx, task):
            return False
        self._emit("post_ready_gate", task)
        return True

    # --------------------------------------------------------- plugin hook bus

    def _emit(self, stage: str, task: StoryTask | None = None, **fields) -> HookContext | None:
        """Fire plugin hooks for ``stage``, or return None on the O(1) no-op fast
        path (no plugin binds the stage → a zero-plugin run does no work). Builds
        a HookContext from the task + extra fields, dispatches it through the bus,
        and returns it so the caller can read whitelisted mutations / resolve a
        veto. ``ctx.shared`` aliases ``state.plugin_shared`` so cross-stage
        mutations persist automatically."""
        if not self._bus.active(stage):
            return None
        ctx = self._make_context(stage, task, **fields)
        self._bus.emit(stage, ctx)
        return ctx

    def _make_context(self, stage: str, task: StoryTask | None, **fields) -> HookContext:
        base: dict = {
            "run_id": self.state.run_id,
            "repo_root": str(self.paths.repo_root),
            "run_dir": str(self.run_dir),
            "shared": self.state.plugin_shared,
            # the dev + review CLI agent ids in this unit's worktree, for a plugin
            # that routes per-agent config (the Unity engine's MCP routing).
            "agents": tuple(self._engine_agent_ids()),
        }
        if task is not None:
            base.update(
                story_key=task.story_key,
                epic=task.epic,
                phase=str(task.phase),
                attempt=task.attempt,
                worktree=task.worktree_path or str(self.workspace.root),
                branch=task.branch or None,
            )
        base.update(fields)
        return HookContext(stage, **base)

    def _vetoed(self, ctx: HookContext | None, task: StoryTask) -> bool:
        """Route a per-unit veto onto the engine's existing control flow. Returns
        True if the unit was vetoed (the caller should stop driving it).

        The phase is set *directly* (not via ``advance``) because a veto can fire
        from a stage with no legal transition to a terminal phase (e.g. PENDING) —
        the same deliberate move the engine's own gate-failure / DONE-unit paths
        make. ``skip`` quietly retires the unit (DEFERRED, no notify) so the loop
        continues and resume sees a terminal task; ``defer`` notifies; ``pause``
        escalates and raises RunPaused."""
        if ctx is None:
            return False
        veto = ctx.resolved_veto()
        if veto is None:
            return False
        msg = f"plugin {veto.plugin_id!r} vetoed {ctx.stage}: {veto.reason}".rstrip(": ")
        self.journal.append(
            "plugin-veto",
            stage=ctx.stage,
            action=veto.action,
            plugin=veto.plugin_id,
            reason=veto.reason,
            story_key=task.story_key,
        )
        if veto.action == "pause":
            task.phase = Phase.ESCALATED  # deliberate: veto stage may have no legal advance
            self.journal.append("story-escalated", story_key=task.story_key, reason=msg)
            gates.notify(
                self.policy,
                self.run_dir,
                f"CRITICAL escalation: {task.story_key}",
                f"{msg} — resolve, then `bmad-auto resume {self.state.run_id}`",
            )
            self._save()
            raise RunPaused(msg, PAUSE_ESCALATION, task.story_key)
        task.defer_reason = msg
        task.phase = Phase.DEFERRED  # deliberate set; the veto stage may have no legal advance
        if veto.action == "defer":
            self.journal.append("story-deferred", story_key=task.story_key, reason=msg)
            gates.notify(self.policy, self.run_dir, f"story deferred: {task.story_key}", msg)
        else:  # skip: retire quietly, no human notification
            self.journal.append("story-skipped", story_key=task.story_key, reason=msg)
        self._save()
        return True

    def _emit_run_boundary(self, stage: str) -> None:
        """Fire a run-level stage (no task). A ``pause`` veto raises RunPaused so
        the run records as paused; ``defer``/``skip`` have no per-unit target here
        and are advisory (the bus already journalled them)."""
        ctx = self._emit(stage)
        if ctx is None:
            return
        veto = ctx.resolved_veto()
        if veto is not None and veto.action == "pause":
            raise RunPaused(
                f"plugin {veto.plugin_id!r} vetoed {stage}: {veto.reason}".rstrip(": "),
                PAUSE_ESCALATION,
                None,
            )

    def _emit_session_gate(
        self, task: StoryTask, role: str, prompt: str, env: dict[str, str], session_stage: str
    ) -> tuple[str, dict[str, str], HookContext | None]:
        """Fire the role-specific then generic session hooks before a session
        launches, sharing one context so the generic ``pre_session`` sees the
        role hook's mutations. Returns the (possibly rewritten) prompt + env and
        the context (None on the fast path). A veto is left on the context for
        the caller to turn into a synthesized ``vetoed`` SessionResult."""
        if not (self._bus.active(session_stage) or self._bus.active("pre_session")):
            return prompt, env, None
        ctx = self._make_context(
            "pre_session", task, role=role, proposed_prompt=prompt, proposed_env=dict(env)
        )
        # role-specific stage first (its mutations are visible to pre_session)
        ctx._stage = session_stage
        self._bus.emit(session_stage, ctx)
        ctx._stage = "pre_session"
        self._bus.emit("pre_session", ctx)
        if ctx.proposed_prompt is not None:
            prompt = ctx.proposed_prompt
        if ctx.proposed_env:
            env = dict(ctx.proposed_env)
        return prompt, env, ctx

    def _run_workflows(self, stage: str, task: StoryTask, seq: int) -> bool:
        """Run every plugin-provided workflow bound to ``stage`` as an extra agent
        session through the generic ``_run_session`` path — the conservative form
        of custom orchestration (no new pipeline stage; an injected session in the
        unit's live worktree). Returns True iff a *blocking* workflow's session
        did not complete and the unit was therefore deferred (the caller must stop
        driving it). O(1) no-op when no active plugin provides a workflow here, so
        a workflow-free run stays byte-identical.

        A workflow session is just another session: it fires ``pre_workflow_session``
        + ``pre_session`` + ``post_session`` and is recorded on the task like any
        other, so token budgets and the transcript trail account for it."""
        if stage not in self._workflow_stages:
            return False
        for lp, wf in self._registry.workflows_for(stage):
            prompt = (
                lp.manifest.render(wf.prompt)
                .replace("{story_key}", task.story_key)
                .replace("{run_id}", self.state.run_id)
            )
            self.journal.append(
                "workflow-start",
                plugin=lp.name,
                workflow=wf.name,
                stage=stage,
                role=wf.role,
                story_key=task.story_key,
            )
            result = self._run_session(
                task,
                role=wf.role,
                prompt=prompt,
                seq=seq,
                session_stage="pre_workflow_session",
                label=f"{lp.name}.{wf.name}",
            )
            self.journal.append(
                "workflow-end",
                plugin=lp.name,
                workflow=wf.name,
                status=result.status,
                story_key=task.story_key,
            )
            if wf.blocking and result.status != "completed":
                self._defer(
                    task,
                    f"blocking workflow {wf.name!r} ({lp.name}) did not complete: {result.status}",
                )
                return True
        return False

    def _run_story(self, task: StoryTask) -> None:
        ctx = self._emit("pre_story", task)
        if self._vetoed(ctx, task):
            return
        if self._isolated:
            self._run_isolated(task, self._drive_story)
        else:
            # in-place (non-isolated) ready gate: a plugin (e.g. a shared-mode
            # Unity engine) needs the live Editor up before any session starts.
            # The per_worktree gate runs inside _run_isolated, after that
            # worktree's own Editor has launched.
            ctx = self._emit("pre_ready_gate", task)
            if self._vetoed(ctx, task):
                return
            self._emit("post_ready_gate", task)
            self._drive_story(task)
        self._emit("post_story", task)

    def _drive_story(self, task: StoryTask) -> None:
        if not self._dev_phase(task):
            return
        if gates.pause_after_spec(self.policy):
            gates.notify(
                self.policy,
                self.run_dir,
                f"spec ready for approval: {task.story_key}",
                f"review {task.spec_file}, then `bmad-auto resume {self.state.run_id}`",
            )
            raise RunPaused(
                f"awaiting spec approval for {task.story_key}",
                PAUSE_SPEC_APPROVAL,
                task.story_key,
            )
        self._review_and_commit(task)

    def _dev_phase(self, task: StoryTask) -> bool:
        if self._vetoed(self._emit("pre_dev_phase", task), task):
            return False
        task.baseline_commit = verify.rev_parse_head(self.workspace.root)
        # snapshot untracked files now so a later rollback removes only what THIS
        # attempt creates, never files the user already had on disk.
        task.baseline_untracked = sorted(verify.untracked_files(self.workspace.root))
        feedback: Path | None = None
        while True:
            task.attempt += 1
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
            )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            if result.status == "completed":
                outcome = self._verify_dev_artifacts(task, result.result_json)
                if outcome.ok:
                    # deterministic gates run here too: a broken build must not
                    # reach the (far more expensive) review loop
                    outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
            self._emit(
                "post_dev_verify",
                task,
                session_status=result.status,
                result_json=result.result_json,
                verify_reason=(outcome.reason if outcome is not None else None),
            )
            decision = decide_dev(task, result, outcome, self.policy)
            self.journal.append(
                "dev-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                action=str(decision.action),
                reason=decision.reason,
            )
            self._save()
            if decision.action == Action.PROCEED:
                self._emit("post_dev_phase", task)
                if self._run_workflows("post_dev_phase", task, task.attempt):
                    return False
                return True
            if decision.action == Action.RETRY:
                if outcome is not None and outcome.fixable:
                    # work exists and the failure is concrete: keep the tree,
                    # hand the failing output to a repair session
                    feedback = self._write_feedback(task, decision.reason)
                else:
                    feedback = None
                    self._rollback_or_pause(task)
                continue
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return False
            self._escalate(task, decision.reason)

    def _review_and_commit(self, task: StoryTask) -> None:
        if not self.policy.review.enabled:
            self._skip_review_and_commit(task)
            return
        if self._vetoed(self._emit("pre_review_phase", task), task):
            return
        clean = False
        while task.review_cycle < self.policy.limits.max_review_cycles:
            task.review_cycle += 1
            advance(task, Phase.REVIEW_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="review",
                prompt=self._review_prompt(task),
                seq=task.review_cycle,
            )
            advance(task, Phase.REVIEW_VERIFY)
            self._save()
            self._emit(
                "post_review_session",
                task,
                role="review",
                session_status=result.status,
                result_json=result.result_json,
            )
            decision = decide_review_session(task, result, self.policy)
            if decision.action == Action.PAUSE:
                self._escalate(task, decision.reason)
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return
            if decision.action == Action.RETRY:
                self.journal.append(
                    "review-retry", story_key=task.story_key, reason=decision.reason
                )
                continue

            rj = result.result_json or {}
            for pref in preference_escalations(rj):
                self.journal.append("preference-escalation", story_key=task.story_key, **pref)
            self.journal.append(
                "review-result",
                story_key=task.story_key,
                cycle=task.review_cycle,
                clean=bool(rj.get("clean")),
                patched=rj.get("patched", 0),
                deferred=rj.get("deferred", 0),
                dismissed=rj.get("dismissed", 0),
            )
            self._emit("post_review_result", task, role="review", result_json=rj)
            if self._run_workflows("post_review_result", task, task.review_cycle):
                return
            if rj.get("clean"):
                outcome = self._verify_review(task)
                if outcome.ok:
                    clean = True
                    break
                self.journal.append(
                    "review-verify-failed",
                    story_key=task.story_key,
                    reason=outcome.reason,
                )
                if outcome.fixable and task.review_cycle < self.policy.limits.max_review_cycles:
                    # failing verify commands are dev work, not review work: a
                    # re-review of the same tree cannot make them pass. Repair
                    # with the failing output as feedback, then re-review.
                    if not self._fix_phase(task, outcome.reason):
                        self._defer(task, "verify commands kept failing after clean review")
                        return
                continue
            # not clean: patches were applied; loop runs a fresh review of the new tree

        if not clean:
            self._defer(task, "review did not converge to clean within budget")
            return

        self._commit(task)

    def _skip_review_and_commit(self, task: StoryTask) -> None:
        """review.enabled = false: the dev session ran quick-dev's own internal
        triple-review and finalized the story to done. No separate review
        session runs — validate the deterministic gates (verify commands,
        spec/sprint = done) and commit, repairing once if verify is fixable."""
        self.journal.append("review-skipped", story_key=task.story_key)
        outcome = self._verify_review(task)
        if not outcome.ok and outcome.fixable and self._fix_phase(task, outcome.reason):
            outcome = self._verify_review(task)
        if not outcome.ok:
            self._defer(task, f"verify failed with review disabled: {outcome.reason}")
            return
        self._commit(task)

    def _commit(self, task: StoryTask) -> None:
        advance(task, Phase.COMMITTING)
        self._save()
        message = self._commit_message(task)
        # pre_commit: a plugin may rewrite the commit message or escalate (pause).
        # A defer/skip veto would have to unwind a COMMITTING task (no legal move
        # to DEFERRED), so only pause is honored here — _escalate sets ESCALATED
        # directly, which COMMITTING does allow.
        ctx = self._emit("pre_commit", task, proposed_commit_message=message)
        if ctx is not None:
            veto = ctx.resolved_veto()
            if veto is not None and veto.action == "pause":
                self._escalate(task, f"plugin {veto.plugin_id!r} vetoed pre_commit: {veto.reason}")
            if ctx.proposed_commit_message:
                message = ctx.proposed_commit_message
        try:
            task.commit_sha = verify.commit_story(self.workspace.root, message)
        except verify.GitError as e:
            self._escalate(task, f"commit failed: {e}")
        advance(task, Phase.DONE)
        self.journal.append("story-done", story_key=task.story_key, commit=task.commit_sha)
        self._emit("post_commit", task)
        self._save()
        weighted = task.tokens.weighted_total(self.policy.limits.cache_read_weight)
        if weighted > self.policy.limits.max_tokens_per_story:
            self.journal.append(
                "token-budget-exceeded",
                story_key=task.story_key,
                weighted=weighted,
                total=task.tokens.total,
            )

    # ----------------------------------------------------- override seams
    # SweepEngine reuses the dev/review pipeline for deferred-work bundles by
    # overriding these (bundles have no sprint-status entry).

    def _verify_dev_artifacts(self, task: StoryTask, result_json: dict | None):
        return verify.verify_dev(
            task, self.workspace.paths, result_json, review_enabled=self.policy.review.enabled
        )

    def _verify_review(self, task: StoryTask):
        return verify.verify_review(task, self.workspace.paths, self.policy)

    def _review_prompt(self, task: StoryTask) -> str:
        return f"/bmad-auto-review {task.spec_file}"

    def _render_commit_template(self, task: StoryTask) -> str | None:
        """The configured commit message template with {story_key}/{run_id}
        substituted, or None when no template is set. Used by both the story and
        sweep-bundle commit paths so a filled-out template wins everywhere."""
        template = self.policy.scm.commit_message_template.strip()
        if not template:
            return None
        # literal substitution (not str.format) so stray braces in the
        # template — e.g. a JSON trailer — don't raise.
        return template.replace("{story_key}", task.story_key).replace(
            "{run_id}", self.state.run_id
        )

    def _commit_message(self, task: StoryTask) -> str:
        rendered = self._render_commit_template(task)
        if rendered is not None:
            return rendered
        if self.policy.review.enabled:
            return f"story {task.story_key}: implemented and reviewed via bmad-auto"
        return f"story {task.story_key}: implemented via bmad-auto"

    # ------------------------------------------------------------- helpers

    def _run_session(
        self,
        task: StoryTask,
        role: str,
        prompt: str,
        seq: int,
        session_stage: str | None = None,
        label: str | None = None,
    ) -> SessionResult:
        # ``label`` names a non-standard session (a plugin-provided workflow) so
        # its task_id stays distinct from the role's own dev/review attempts.
        task_id = f"{task.story_key}-{label or role}-{seq}"
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        env = {
            "BMAD_AUTO_MODE": "1",
            "BMAD_AUTO_RUN_DIR": str(self.run_dir),
            "BMAD_AUTO_TASK_ID": task_id,
            "BMAD_AUTO_STORY_KEY": task.story_key,
        }
        if role == "dev" and not self.policy.review.enabled:
            # tells the dev skill to run its own internal triple-review and
            # finalize straight to done (the orchestrator runs no review session)
            env["BMAD_AUTO_SKIP_REVIEW"] = "1"
        # plugin session hooks: a role-specific stage (pre_dev_session / fix /
        # migrate / ...) then the generic pre_session, both able to rewrite the
        # prompt + env or veto the session. A veto synthesizes a `vetoed` result
        # so the existing decide_dev/decide_review_session route it (retry → defer).
        prompt, env, sctx = self._emit_session_gate(
            task, role, prompt, env, session_stage or f"pre_{role}_session"
        )
        if sctx is not None:
            veto = sctx.resolved_veto()
            if veto is not None:
                self.journal.append(
                    "plugin-veto",
                    stage=sctx.stage,
                    action=veto.action,
                    plugin=veto.plugin_id,
                    reason=veto.reason,
                    task_id=task_id,
                    role=role,
                )
                return SessionResult(status="vetoed")
        spec = SessionSpec(
            task_id=task_id,
            role=role,
            prompt=prompt,
            cwd=self.workspace.root,
            env=env,
            model=cfg.model,
            timeout_s=self.policy.limits.session_timeout_min * 60,
        )
        self.journal.set_active_log(task_id)
        self.journal.append("session-start", task_id=task_id, role=role, prompt=prompt)
        result = adapter.run(spec)
        usage = adapter.read_usage(result)
        task.record_session(
            SessionRecord(
                task_id=task_id,
                role=role,
                status=result.status,
                session_id=result.session_id,
                transcript_path=result.transcript_path,
                usage=usage,
            )
        )
        self.journal.append(
            "session-end",
            task_id=task_id,
            status=result.status,
            tokens=usage.total if usage else None,
        )
        self._emit(
            "post_session",
            task,
            role=role,
            session_status=result.status,
            result_json=result.result_json,
        )
        return result

    def _dev_prompt(self, task: StoryTask, feedback: Path | None) -> str:
        prompt = f"/bmad-auto-dev {task.story_key}"
        if feedback is not None:
            prompt += f" --feedback {feedback}"
        return prompt

    def _write_feedback(self, task: StoryTask, reason: str) -> Path:
        """Persist a verification failure where the next session can read it —
        deterministic evidence must reach the LLM, not just the journal."""
        path = self.run_dir / "feedback" / f"{task.story_key}-{len(task.sessions)}.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            f"# Verification feedback: {task.story_key}\n\n"
            "The previous session's work failed deterministic verification.\n"
            "Repair the working tree so verification passes, without violating\n"
            "the spec's frozen intent.\n\n"
            f"```\n{reason}\n```\n",
            encoding="utf-8",
        )
        return path

    def _fix_phase(self, task: StoryTask, reason: str) -> bool:
        """Feedback-driven repair after a clean review whose verify commands
        failed. Consumes the story's dev-attempt budget; returns True once the
        commands pass so the review loop can re-review the repaired tree."""
        while task.attempt < self.policy.limits.max_dev_attempts:
            task.attempt += 1
            feedback = self._write_feedback(task, reason)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=self._dev_prompt(task, feedback),
                seq=task.attempt,
                session_stage="pre_fix_session",
            )
            advance(task, Phase.DEV_VERIFY)
            crits = critical_escalations(result.result_json)
            if crits:
                details = "; ".join(str(e.get("detail", e.get("type", "?"))) for e in crits)
                self._escalate(task, f"CRITICAL escalation from fix session: {details}")
            outcome = None
            if result.status == "completed":
                outcome = verify.verify_commands_outcome(self.policy, self.workspace.root)
                if not outcome.ok:
                    reason = outcome.reason
            ok = outcome is not None and outcome.ok
            self.journal.append(
                "fix-decision",
                story_key=task.story_key,
                attempt=task.attempt,
                session_status=result.status,
                ok=ok,
            )
            self._save()
            if ok:
                return True
        return False

    def _defer(self, task: StoryTask, reason: str) -> None:
        task.defer_reason = reason
        advance(task, Phase.DEFERRED)
        if self._isolated:
            # the failed work lives in the unit's worktree; the diff is captured
            # and the worktree kept/dropped by _integrate_unit. Don't touch the
            # tree here (no reset into the main repo — there's nothing to undo).
            self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
            gates.notify(self.policy, self.run_dir, f"story deferred: {task.story_key}", reason)
            self._save()
            return
        if task.baseline_commit:
            self._stash_deferred_artifacts(task)
            deferred_work = self.workspace.paths.deferred_work
            snapshot = (
                deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
            )
            self._rollback_or_pause(task)
            # reset reverts tracked deferred-work.md edits; restore review-found
            # defer entries — they are real knowledge worth keeping
            if snapshot is not None:
                current = (
                    deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
                )
                if current != snapshot:
                    deferred_work.parent.mkdir(parents=True, exist_ok=True)
                    deferred_work.write_text(snapshot, encoding="utf-8")
        self.journal.append("story-deferred", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"story deferred: {task.story_key}",
            reason,
        )
        self._save()

    def _stash_deferred_artifacts(self, task: StoryTask) -> None:
        """Move the deferred story's spec out of the artifacts dir into the run
        dir: a leftover in-review spec would confuse the next attempt, but the
        work in it is worth keeping for the human."""
        if not task.spec_file:
            return
        spec_path = Path(task.spec_file)
        if not spec_path.is_file():
            return
        dest = self.run_dir / "deferred" / task.story_key
        dest.mkdir(parents=True, exist_ok=True)
        shutil.move(str(spec_path), str(dest / spec_path.name))
        self.journal.append(
            "deferred-artifacts-stashed",
            story_key=task.story_key,
            stashed_to=str(dest / spec_path.name),
        )

    def _escalate(self, task: StoryTask, reason: str) -> None:
        advance(task, Phase.ESCALATED)
        self.journal.append("story-escalated", story_key=task.story_key, reason=reason)
        gates.notify(
            self.policy,
            self.run_dir,
            f"CRITICAL escalation: {task.story_key}",
            f"{reason} — resolve, then `bmad-auto resume {self.state.run_id}`",
        )
        self._save()
        raise RunPaused(reason, PAUSE_ESCALATION, task.story_key)

    def _maybe_auto_sweep(self, kind: str, trigger: str) -> None:
        """Run a child deferred-work sweep when policy [sweep].auto matches.
        The child is its own resumable run; a paused or failed child is
        journaled + notified but never interrupts this run."""
        if self.policy.sweep.auto != kind or self.sweep_factory is None:
            return
        if trigger in self.state.sweeps_triggered:
            return  # already fired before a pause/resume of this run
        self.state.sweeps_triggered.append(trigger)
        self._save()
        try:
            clean = verify.worktree_clean(self.workspace.root)
        except verify.GitError:
            clean = False
        if not clean:
            # should not happen at these call sites (everything committed or
            # reset); refuse rather than sweep on top of stray changes
            self.journal.append("sweep-auto-skipped-dirty", trigger=trigger)
            return
        self.journal.append("sweep-auto-trigger", trigger=trigger)
        try:
            self.sweep_factory(trigger)
            self.journal.append("sweep-auto-finished", trigger=trigger)
        except Exception as e:  # noqa: BLE001 — child must never break the parent
            self.journal.append("sweep-auto-failed", trigger=trigger, error=str(e))
            gates.notify(self.policy, self.run_dir, "auto sweep failed", f"{trigger}: {e}")

    def _epic_boundary(self, finished_epic: int, next_epic: int) -> None:
        self.journal.append("epic-boundary", finished=finished_epic, next=next_epic)
        self._emit("pre_epic_boundary", epic=finished_epic)
        self._maybe_auto_sweep("per-epic", f"epic-{finished_epic}")
        if self.policy.gates.retrospective != "never":
            gates.notify(
                self.policy,
                self.run_dir,
                f"epic {finished_epic} stories complete",
                "retrospective suggested: run /bmad-retrospective when convenient",
            )
        self._emit("post_epic_boundary", epic=finished_epic)
        if gates.pause_at_epic_boundary(self.policy):
            self.state.current_epic = next_epic  # don't re-trigger this gate on resume
            self._save()
            raise RunPaused(
                f"epic {finished_epic} boundary — `bmad-auto resume {self.state.run_id}` "
                f"to continue with epic {next_epic}",
                PAUSE_EPIC_BOUNDARY,
            )

    def _save(self) -> None:
        save_state(self.run_dir, self.state)
