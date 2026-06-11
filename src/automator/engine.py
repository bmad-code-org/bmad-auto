"""The deterministic control loop.

Per story: dev session -> artifact verification -> bounded review loop
-> deterministic verify commands -> orchestrator commit. The engine never
edits sprint-status.yaml or spec files; it re-reads them to decide and
verify. All creative work happens inside disposable adapter sessions.
"""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from . import gates, verify
from .adapters.base import CodingCLIAdapter, SessionResult, SessionSpec
from .bmadconfig import ProjectPaths
from .escalation import Action, decide_dev, decide_review_session, preference_escalations
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
from .policy import Policy
from .sprintstatus import load as load_sprint_status
from .sprintstatus import next_actionable
from .statemachine import advance


class RunPaused(Exception):
    def __init__(self, reason: str, stage: str, story_key: str | None = None):
        super().__init__(reason)
        self.reason = reason
        self.stage = stage
        self.story_key = story_key


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


class Engine:
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
    ):
        self.paths = paths
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

    # ------------------------------------------------------------- top level

    def run(self) -> RunSummary:
        try:
            self._loop()
            self.state.finished = True
            self.journal.append("run-complete")
        except RunPaused as pause:
            self.state.paused_reason = pause.reason
            self.state.paused_stage = pause.stage
            self.state.paused_story_key = pause.story_key
            self.journal.append(
                "run-paused", reason=pause.reason, stage=pause.stage, story_key=pause.story_key
            )
        finally:
            self._save()
        summary = self.summary()
        gates.notify(self.policy, self.run_dir, "bmad-auto run finished", summary.render())
        return summary

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
            story = self._pick_next()
            if story is None:
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

    def _reset_to(self, baseline: str) -> None:
        """Roll back code changes, preserving run state and BMAD artifacts
        (sprint-status etc. may be untracked in young projects — `git clean`
        must never eat them)."""
        keep = [".automator"]
        for artifact_dir in (
            self.paths.implementation_artifacts,
            self.paths.planning_artifacts,
        ):
            try:
                keep.append(str(artifact_dir.relative_to(self.paths.project)))
            except ValueError:
                pass  # artifacts configured outside the repo; nothing to protect
        verify.reset_hard(self.paths.project, baseline, keep=tuple(keep))

    def _finish_inflight(self) -> None:
        """Complete or roll back tasks interrupted by a pause or crash."""
        for task in list(self.state.tasks.values()):
            if task.terminal:
                continue
            if task.phase == Phase.DEV_VERIFY and task.spec_file:
                # paused at the spec-approval gate: dev verified, review pending
                self.journal.append("resume-review", story_key=task.story_key)
                self._review_and_commit(task)
            else:
                self.journal.append(
                    "resume-restart", story_key=task.story_key, phase=str(task.phase)
                )
                if task.baseline_commit:
                    self._reset_to(task.baseline_commit)
                task.phase = Phase.PENDING  # deliberate reset, not a normal transition
                self._save()
                self._run_story(task)

    # ------------------------------------------------------------- per story

    def _run_story(self, task: StoryTask) -> None:
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
        while True:
            task.attempt += 1
            task.baseline_commit = verify.rev_parse_head(self.paths.project)
            advance(task, Phase.DEV_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="dev",
                prompt=f"/bmad-quick-dev {task.story_key}",
                seq=task.attempt,
            )
            advance(task, Phase.DEV_VERIFY)
            outcome = None
            if result.status == "completed":
                outcome = verify.verify_dev(task, self.paths, result.result_json)
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
                return True
            if decision.action == Action.RETRY:
                self._reset_to(task.baseline_commit)
                continue
            if decision.action == Action.DEFER:
                self._defer(task, decision.reason)
                return False
            self._escalate(task, decision.reason)

    def _review_and_commit(self, task: StoryTask) -> None:
        clean = False
        while task.review_cycle < self.policy.limits.max_review_cycles:
            task.review_cycle += 1
            advance(task, Phase.REVIEW_RUNNING)
            self._save()
            result = self._run_session(
                task,
                role="review",
                prompt=f"/bmad-code-review {task.spec_file}",
                seq=task.review_cycle,
            )
            advance(task, Phase.REVIEW_VERIFY)
            self._save()
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
            if rj.get("clean"):
                outcome = verify.verify_review(task, self.paths, self.policy)
                if outcome.ok:
                    clean = True
                    break
                self.journal.append(
                    "review-verify-failed", story_key=task.story_key, reason=outcome.reason
                )
                continue
            # not clean: patches were applied; loop runs a fresh review of the new tree

        if not clean:
            self._defer(task, "review did not converge to clean within budget")
            return

        advance(task, Phase.COMMITTING)
        self._save()
        try:
            task.commit_sha = verify.commit_story(self.paths.project, task)
        except verify.GitError as e:
            self._escalate(task, f"commit failed: {e}")
        advance(task, Phase.DONE)
        self.journal.append("story-done", story_key=task.story_key, commit=task.commit_sha)
        self._save()
        weighted = task.tokens.weighted_total(self.policy.limits.cache_read_weight)
        if weighted > self.policy.limits.max_tokens_per_story:
            self.journal.append(
                "token-budget-exceeded",
                story_key=task.story_key,
                weighted=weighted,
                total=task.tokens.total,
            )

    # ------------------------------------------------------------- helpers

    def _run_session(self, task: StoryTask, role: str, prompt: str, seq: int) -> SessionResult:
        task_id = f"{task.story_key}-{role}-{seq}"
        adapter = self.adapters[role]
        cfg = self.policy.adapter.resolved(role)
        spec = SessionSpec(
            task_id=task_id,
            role=role,
            prompt=prompt,
            cwd=self.paths.project,
            env={
                "BMAD_AUTO_MODE": "1",
                "BMAD_AUTO_RUN_DIR": str(self.run_dir),
                "BMAD_AUTO_TASK_ID": task_id,
                "BMAD_AUTO_STORY_KEY": task.story_key,
            },
            model=cfg.model,
            timeout_s=self.policy.limits.session_timeout_min * 60,
        )
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
        return result

    def _defer(self, task: StoryTask, reason: str) -> None:
        task.defer_reason = reason
        advance(task, Phase.DEFERRED)
        if task.baseline_commit:
            self._stash_deferred_artifacts(task)
            deferred_work = self.paths.deferred_work
            snapshot = (
                deferred_work.read_text(encoding="utf-8") if deferred_work.is_file() else None
            )
            self._reset_to(task.baseline_commit)
            # reset reverts tracked deferred-work.md edits; restore review-found
            # defer entries — they are real knowledge worth keeping
            if snapshot is not None:
                current = (
                    deferred_work.read_text(encoding="utf-8")
                    if deferred_work.is_file()
                    else None
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

    def _epic_boundary(self, finished_epic: int, next_epic: int) -> None:
        self.journal.append("epic-boundary", finished=finished_epic, next=next_epic)
        if self.policy.gates.retrospective != "never":
            gates.notify(
                self.policy,
                self.run_dir,
                f"epic {finished_epic} stories complete",
                "retrospective suggested: run /bmad-retrospective when convenient",
            )
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
