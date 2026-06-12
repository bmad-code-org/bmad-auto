"""Core data model: story lifecycle phases, per-task records, run state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


class Phase(StrEnum):
    PENDING = "pending"
    DEV_RUNNING = "dev-running"
    DEV_VERIFY = "dev-verify"
    REVIEW_RUNNING = "review-running"
    REVIEW_VERIFY = "review-verify"
    COMMITTING = "committing"
    # sweep-only: the triage session classifying open deferred-work entries
    TRIAGE_RUNNING = "triage-running"
    TRIAGE_VERIFY = "triage-verify"
    DONE = "done"
    DEFERRED = "deferred"
    ESCALATED = "escalated"


TERMINAL_PHASES = frozenset({Phase.DONE, Phase.DEFERRED, Phase.ESCALATED})

# Pause stages recorded in RunState.paused_stage
PAUSE_SPEC_APPROVAL = "spec-approval"
PAUSE_EPIC_BOUNDARY = "epic-boundary"
PAUSE_ESCALATION = "escalation"
PAUSE_STORY_GATE = "story-gate"


@dataclass
class TokenUsage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_creation_tokens: int = 0

    def add(self, other: "TokenUsage") -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_creation_tokens += other.cache_creation_tokens

    @property
    def total(self) -> int:
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_read_tokens
            + self.cache_creation_tokens
        )

    def weighted_total(self, cache_read_weight: float) -> int:
        """Cost-proportional total: cache reads are billed at ~0.1x base input
        on all supported vendors (Anthropic/OpenAI/Gemini, June 2026), so raw
        totals mostly measure context re-reads; the budget discounts them."""
        return (
            self.input_tokens
            + self.output_tokens
            + self.cache_creation_tokens
            + round(self.cache_read_tokens * cache_read_weight)
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_creation_tokens": self.cache_creation_tokens,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "TokenUsage":
        return cls(
            input_tokens=int(d.get("input_tokens", 0)),
            output_tokens=int(d.get("output_tokens", 0)),
            cache_read_tokens=int(d.get("cache_read_tokens", 0)),
            cache_creation_tokens=int(d.get("cache_creation_tokens", 0)),
        )


@dataclass
class SessionRecord:
    task_id: str
    role: str  # "dev" | "review"
    status: str  # SessionResult.status
    session_id: str | None = None
    transcript_path: str | None = None
    usage: TokenUsage | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "role": self.role,
            "status": self.status,
            "session_id": self.session_id,
            "transcript_path": self.transcript_path,
            "usage": self.usage.to_dict() if self.usage else None,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SessionRecord":
        usage = d.get("usage")
        return cls(
            task_id=d["task_id"],
            role=d["role"],
            status=d["status"],
            session_id=d.get("session_id"),
            transcript_path=d.get("transcript_path"),
            usage=TokenUsage.from_dict(usage) if usage else None,
        )


@dataclass
class StoryTask:
    story_key: str
    epic: int
    phase: Phase = Phase.PENDING
    attempt: int = 0
    review_cycle: int = 0
    baseline_commit: str | None = None
    spec_file: str | None = None
    commit_sha: str | None = None
    defer_reason: str | None = None
    # sweep bundles only: the deferred-work ids this task closes and the
    # rendered intent file handed to dev sessions
    dw_ids: list[str] = field(default_factory=list)
    bundle_file: str | None = None
    sessions: list[SessionRecord] = field(default_factory=list)
    tokens: TokenUsage = field(default_factory=TokenUsage)

    @property
    def terminal(self) -> bool:
        return self.phase in TERMINAL_PHASES

    def record_session(self, record: SessionRecord) -> None:
        self.sessions.append(record)
        if record.usage:
            self.tokens.add(record.usage)

    def to_dict(self) -> dict[str, Any]:
        return {
            "story_key": self.story_key,
            "epic": self.epic,
            "phase": str(self.phase),
            "attempt": self.attempt,
            "review_cycle": self.review_cycle,
            "baseline_commit": self.baseline_commit,
            "spec_file": self.spec_file,
            "commit_sha": self.commit_sha,
            "defer_reason": self.defer_reason,
            "dw_ids": self.dw_ids,
            "bundle_file": self.bundle_file,
            "sessions": [s.to_dict() for s in self.sessions],
            "tokens": self.tokens.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "StoryTask":
        return cls(
            story_key=d["story_key"],
            epic=int(d["epic"]),
            phase=Phase(d["phase"]),
            attempt=int(d.get("attempt", 0)),
            review_cycle=int(d.get("review_cycle", 0)),
            baseline_commit=d.get("baseline_commit"),
            spec_file=d.get("spec_file"),
            commit_sha=d.get("commit_sha"),
            defer_reason=d.get("defer_reason"),
            dw_ids=[str(i) for i in d.get("dw_ids", [])],
            bundle_file=d.get("bundle_file"),
            sessions=[SessionRecord.from_dict(s) for s in d.get("sessions", [])],
            tokens=TokenUsage.from_dict(d.get("tokens", {})),
        )


@dataclass
class RunState:
    run_id: str
    project: str
    started_at: str
    policy_snapshot: dict[str, Any] = field(default_factory=dict)
    current_epic: int | None = None
    paused_reason: str | None = None
    paused_stage: str | None = None
    paused_story_key: str | None = None
    finished: bool = False
    run_type: str = "story"  # "story" | "sweep" — resume/status dispatch on it
    # auto-sweep triggers already fired this run (e.g. "epic-1", "run-end");
    # guards re-fire on resume
    sweeps_triggered: list[str] = field(default_factory=list)
    tasks: dict[str, StoryTask] = field(default_factory=dict)

    @property
    def paused(self) -> bool:
        return self.paused_reason is not None

    def handled_keys(self) -> set[str]:
        """Story keys this run already drove to a terminal phase."""
        return {k for k, t in self.tasks.items() if t.terminal}

    def clear_pause(self) -> None:
        self.paused_reason = None
        self.paused_stage = None
        self.paused_story_key = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "project": self.project,
            "started_at": self.started_at,
            "policy_snapshot": self.policy_snapshot,
            "current_epic": self.current_epic,
            "paused_reason": self.paused_reason,
            "paused_stage": self.paused_stage,
            "paused_story_key": self.paused_story_key,
            "finished": self.finished,
            "run_type": self.run_type,
            "sweeps_triggered": self.sweeps_triggered,
            "tasks": {k: t.to_dict() for k, t in self.tasks.items()},
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunState":
        return cls(
            run_id=d["run_id"],
            project=d["project"],
            started_at=d["started_at"],
            policy_snapshot=d.get("policy_snapshot", {}),
            current_epic=d.get("current_epic"),
            paused_reason=d.get("paused_reason"),
            paused_stage=d.get("paused_stage"),
            paused_story_key=d.get("paused_story_key"),
            finished=bool(d.get("finished", False)),
            run_type=str(d.get("run_type", "story")),
            sweeps_triggered=[str(s) for s in d.get("sweeps_triggered", [])],
            tasks={k: StoryTask.from_dict(t) for k, t in d.get("tasks", {}).items()},
        )
