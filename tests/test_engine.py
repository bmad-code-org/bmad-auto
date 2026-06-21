"""Engine scenario tests against the mock adapter — no tmux, no LLM."""

import os
import signal
from pathlib import Path

import pytest
from conftest import dev_effect, review_effect, spec_path, write_sprint

from automator.adapters.base import SessionResult
from automator.adapters.mock import MockAdapter
from automator.engine import Engine, RunStopped
from automator.journal import Journal, load_state
from automator.model import (
    PAUSE_EPIC_BOUNDARY,
    PAUSE_SPEC_APPROVAL,
    Phase,
    RunState,
    TokenUsage,
)
from automator.policy import (
    AdapterPolicy,
    GatesPolicy,
    LimitsPolicy,
    NotifyPolicy,
    Policy,
    ScmPolicy,
    StageAdapterPolicy,
    SweepPolicy,
    VerifyPolicy,
)
from automator.verify import rev_parse_head, worktree_clean

QUIET = NotifyPolicy(desktop=False, file=True)


def make_engine(project, script, policy=None, **kwargs) -> tuple[Engine, MockAdapter]:
    run_dir = project.project / ".automator" / "runs" / "test-run"
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id="test-run", project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy
        or Policy(
            gates=GatesPolicy(mode="none"),
            notify=QUIET,
            # in-place tests exercise the retry/defer continuation path, which
            # needs auto-rollback on; the OFF (pause) default is covered by its
            # own tests.
            scm=ScmPolicy(rollback_on_failure=True),
        ),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def resume_engine(project, engine, script, policy=None) -> tuple[Engine, MockAdapter]:
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(script)
    new_engine = Engine(
        paths=project,
        policy=policy or engine.policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    return new_engine, adapter


def test_token_budget_discounts_cache_reads(project):
    """Raw totals dominated by cache reads must not trip the budget; the
    weighted total (cache reads at 0.1x) is what's checked."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # per session: raw = 620k (would bust 1.2M over 2 sessions), weighted = 80k
    usage = TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000)
    run_dir = project.project / ".automator" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_tokens_per_story=1_200_000),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert summary.total_tokens == 2 * 620_000  # display stays raw
    journal_text = (run_dir / "journal.jsonl").read_text()
    assert "token-budget-exceeded" not in journal_text


def test_token_budget_exceeded_journals_weighted(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    usage = TokenUsage(input_tokens=15_000, output_tokens=5_000, cache_read_tokens=600_000)
    run_dir = project.project / ".automator" / "runs" / "test-run"
    adapter = MockAdapter(
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        usage_per_session=usage,
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        limits=LimitsPolicy(max_tokens_per_story=100_000),  # < 2 x 80k weighted
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    engine.run()

    entries = [
        line
        for line in (run_dir / "journal.jsonl").read_text().splitlines()
        if "token-budget-exceeded" in line
    ]
    assert len(entries) == 1
    assert '"weighted": 160000' in entries[0]


def test_happy_path(project):
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    assert task.commit_sha and task.commit_sha != task.baseline_commit
    assert worktree_clean(project.project)
    assert summary.total_tokens == 30  # 2 sessions x 15
    assert [s.role for s in adapter.sessions] == ["dev", "review"]
    assert adapter.sessions[0].env["BMAD_AUTO_MODE"] == "1"
    assert adapter.sessions[1].prompt.startswith("/bmad-auto-review ")


def test_inplace_ready_gate_veto_defers_before_any_session(project):
    """A plugin gating pre_ready_gate in non-isolated (in-place) mode — e.g. a
    shared-mode Unity engine waiting on the live Editor — defers the unit via the
    bus veto path before any dev session runs. Proves the engine emits the ready
    gate + honors a veto outside the worktree path, with no engine-specific code."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    # a declarative plugin whose blocking pre_ready_gate hook fails -> defer veto
    plug = project.project / ".automator" / "plugins" / "gate"
    plug.mkdir(parents=True)
    (plug / "plugin.toml").write_text(
        '[plugin]\nname = "gate"\napi_version = 1\n'
        "[hooks.pre_ready_gate]\ncmd = 'exit 1'\nblocking = true\n"
    )
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")])
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert adapter.sessions == []  # gate vetoed before the dev session
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "plugin-veto" in kinds and "story-deferred" in kinds


def test_review_disabled_skips_review_session(project):
    from automator.policy import ReviewPolicy

    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    pol = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        review=ReviewPolicy(enabled=False),
    )
    # only a dev session is scripted — no review_effect at all
    engine, adapter = make_engine(project, [dev_effect(project, "1-1-a")], policy=pol)
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE and task.commit_sha
    assert task.review_cycle == 0
    # exactly one session, and it carries the skip-review signal
    assert [s.role for s in adapter.sessions] == ["dev"]
    assert adapter.sessions[0].env["BMAD_AUTO_SKIP_REVIEW"] == "1"
    kinds = {e["kind"] for e in Journal(engine.run_dir).entries()}
    assert "review-skipped" in kinds
    msg = _head_commit_message(project.project)
    assert "implemented via bmad-auto" in msg and "reviewed" not in msg


def _head_commit_message(repo: Path) -> str:
    import subprocess

    return subprocess.run(
        ["git", "log", "-1", "--pretty=%B"],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout


def test_finish_kills_session_when_enabled(project, monkeypatch):
    import automator.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    assert engine.state.finished
    assert killed == ["test-run"]


def test_finish_keeps_session_when_disabled(project, monkeypatch):
    import automator.engine as engine_mod

    killed: list[str] = []
    monkeypatch.setattr(engine_mod, "kill_session", lambda rid: killed.append(rid))
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(cleanup_session_on_finish=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
    )
    engine.run()
    assert engine.state.finished
    assert killed == []


def test_per_stage_adapter_and_model_dispatch(project):
    """Dev and review sessions go to their own adapters with per-stage models."""
    write_sprint(project, {"epic-1": "backlog", "1-1-a": "ready-for-dev"})
    run_dir = project.project / ".automator" / "runs" / "test-run"
    dev_mock = MockAdapter([dev_effect(project, "1-1-a")])
    review_mock = MockAdapter([review_effect(project, "1-1-a", clean=True)])
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        adapter=AdapterPolicy(
            name="claude",
            model="opus",
            review=StageAdapterPolicy(name="codex", model="gpt-5-codex"),
        ),
    )
    engine = Engine(
        paths=project,
        policy=policy,
        adapter=dev_mock,
        review_adapter=review_mock,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=RunState(run_id="test-run", project=str(project.project), started_at="now"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert [s.role for s in dev_mock.sessions] == ["dev"]
    assert [s.role for s in review_mock.sessions] == ["review"]
    assert dev_mock.sessions[0].model == "opus"
    assert review_mock.sessions[0].model == "gpt-5-codex"


def test_review_loop_converges_within_budget(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=False, patched=2),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.tasks["1-1-a"].review_cycle == 2


def test_plateau_defer_when_review_never_clean(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "did not converge" in task.defer_reason
    # repo rolled back for the next story
    assert (project.project / "src.txt").read_text() == "original\n"
    assert rev_parse_head(project.project) == task.baseline_commit
    # the in-review spec is stashed into the run dir, not left in artifacts
    from conftest import spec_path

    assert not spec_path(project, "1-1-a").exists()
    stashed = engine.run_dir / "deferred" / "1-1-a" / "spec-1-1-a.md"
    assert stashed.is_file() and "in-review" in stashed.read_text()


def test_defer_preserves_deferred_work_additions(project):
    """Review sessions append real knowledge to deferred-work.md; a plateau
    defer's git reset must not erase it."""
    from conftest import git
    from conftest import review_effect as make_review

    project.deferred_work.write_text("# Deferred Work\n")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "seed deferred-work")
    write_sprint(project, {"1-1-a": "ready-for-dev"})

    def reviewing_with_defer(spec):
        with project.deferred_work.open("a") as f:
            f.write("\n### DW-1: pre-existing flaky retry\n\nstatus: open\n")
        return make_review(project, "1-1-a", clean=False, patched=1)(spec)

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")] + [reviewing_with_defer for _ in range(3)],
    )
    summary = engine.run()
    assert summary.deferred == 1
    assert "DW-1: pre-existing flaky retry" in project.deferred_work.read_text()


def test_rollback_off_pauses_with_manual_notice(project):
    """Production default (rollback_on_failure=False): a would-be defer reset
    never touches the tree — it pauses with bold manual-recovery instructions."""
    from automator.model import PAUSE_ESCALATION

    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=False),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
        policy=policy,
    )
    precious = project.project / "keep-me.txt"
    precious.write_text("precious\n")  # an untracked file the user wants kept
    summary = engine.run()

    assert summary.paused
    state = load_state(engine.run_dir)
    assert state.paused_stage == PAUSE_ESCALATION
    reason = state.paused_reason.lower()
    assert "manual rollback" in reason and "back up" in reason
    # the orchestrator left the tree exactly as-is — no reset, nothing deleted
    assert not worktree_clean(project.project)
    assert precious.read_text() == "precious\n"
    kinds = [e["kind"] for e in engine.journal.entries()]
    assert "rollback-manual-required" in kinds


def test_rollback_on_preserves_preexisting_untracked(project):
    """With rollback_on_failure=True the auto-rollback reverts tracked changes
    but never deletes untracked files that predate the attempt."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(rollback_on_failure=True),
    )
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a")]
        + [review_effect(project, "1-1-a", clean=False, patched=1) for _ in range(3)],
        policy=policy,
    )
    precious = project.project / "user-notes.txt"
    precious.write_text("keep me\n")  # untracked, present before baseline capture
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert rev_parse_head(project.project) == task.baseline_commit  # tracked reverted
    assert precious.read_text() == "keep me\n"  # pre-existing untracked survives


def test_dev_stall_retries_then_succeeds(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="stalled"),
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.tasks["1-1-a"].attempt == 2


def test_dev_exhausted_defers_and_run_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, adapter = make_engine(
        project,
        [
            SessionResult(status="timeout"),
            SessionResult(status="crashed"),
            dev_effect(project, "1-2-b"),
            review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()
    assert summary.deferred == 1 and summary.done == 1
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert engine.state.tasks["1-2-b"].phase == Phase.DONE


def test_critical_escalation_pauses_and_resume_continues(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    escalating = SessionResult(
        status="completed",
        result_json={
            "workflow": "quick-dev",
            "escalations": [{"type": "missing-config", "severity": "CRITICAL", "detail": "boom"}],
        },
    )
    engine, _ = make_engine(project, [escalating])
    summary = engine.run()

    assert summary.paused and summary.escalated == 1
    saved = load_state(engine.run_dir)
    assert saved.paused_reason and "boom" in saved.paused_reason
    assert saved.tasks["1-1-a"].phase == Phase.ESCALATED

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "1-2-b"), review_effect(project, "1-2-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 1 and not summary2.paused
    assert resumed.state.finished


def test_epic_boundary_gate_pause_and_resume(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    gated = Policy(gates=GatesPolicy(mode="per-epic"), notify=QUIET)
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=gated,
    )
    summary = engine.run()
    assert summary.done == 1 and summary.paused
    assert load_state(engine.run_dir).paused_stage == PAUSE_EPIC_BOUNDARY

    resumed, _ = resume_engine(
        project,
        engine,
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)],
    )
    summary2 = resumed.run()
    assert summary2.done == 2 and not summary2.paused


def test_spec_approval_gate_pause_then_resume_reviews(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(gates=GatesPolicy(mode="per-story-spec-approval"), notify=QUIET)
    engine, _ = make_engine(project, [dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    assert saved.paused_stage == PAUSE_SPEC_APPROVAL
    assert saved.tasks["1-1-a"].phase == Phase.DEV_VERIFY
    assert saved.tasks["1-1-a"].spec_file

    resumed, adapter = resume_engine(
        project, engine, [review_effect(project, "1-1-a", clean=True)], policy=gated
    )
    summary2 = resumed.run()
    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]


def test_dev_verify_command_failure_routes_feedback_fix(project):
    """A broken build never reaches review: the dev-stage gate fails, the tree
    is kept, and the next dev session gets the failing output as feedback."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def fix(spec):
        marker.write_text("ok\n")
        sp = spec_path(project, "1-1-a")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "quick-dev",
                "story_key": "1-1-a",
                "spec_file": str(sp),
                "baseline_commit": rev_parse_head(project.project),
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [{"command": f"test -f {marker}", "ok": True}],
                "escalations": [],
            },
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(f"test -f {marker}",)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.attempt == 2
    prompts = [s.prompt for s in adapter.sessions]
    assert "--feedback" not in prompts[0] and "--feedback" in prompts[1]
    feedback = Path(prompts[1].split("--feedback ", 1)[1])
    assert "test -f" in feedback.read_text()
    # the first attempt's work survived: no reset between attempts
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_review_verify_failure_routes_fix_session_then_rereview(project):
    """Verify commands failing after a clean review route to a feedback-driven
    dev fix session and a fresh review cycle — not a blind re-review."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()  # the review's "patch" broke the verify gate
        return review_effect(project, "1-1-a", clean=True)(spec)

    def fix(spec):
        marker.write_text("ok\n")
        return SessionResult(
            status="completed", result_json={"workflow": "quick-dev", "escalations": []}
        )

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(f"test -f {marker}",)),
    )
    engine, adapter = make_engine(
        project,
        [
            dev_with_marker,
            breaking_review,
            fix,
            review_effect(project, "1-1-a", clean=True),
        ],
        policy=policy,
    )
    summary = engine.run()

    assert summary.done == 1
    task = engine.state.tasks["1-1-a"]
    assert task.review_cycle == 2 and task.attempt == 2
    assert [s.role for s in adapter.sessions] == ["dev", "review", "dev", "review"]
    assert "--feedback" in adapter.sessions[2].prompt


def test_review_verify_failure_without_fix_budget_defers(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    marker = project.project / "fixed.marker"

    def dev_with_marker(spec):
        marker.write_text("ok\n")
        return dev_effect(project, "1-1-a")(spec)

    def breaking_review(spec):
        marker.unlink()
        return review_effect(project, "1-1-a", clean=True)(spec)

    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=(f"test -f {marker}",)),
        limits=LimitsPolicy(max_dev_attempts=1),
    )
    engine, adapter = make_engine(project, [dev_with_marker, breaking_review], policy=policy)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert "kept failing" in engine.state.tasks["1-1-a"].defer_reason


def test_verify_commands_never_pass_defers_at_dev(project):
    """Unfixable verify failures exhaust the dev budget and defer before any
    review session is spent."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        verify=VerifyPolicy(commands=("false",)),
    )
    engine, adapter = make_engine(
        project,
        [dev_effect(project, "1-1-a"), dev_effect(project, "1-1-a")],
        policy=policy,
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0
    assert [s.role for s in adapter.sessions] == ["dev", "dev"]
    assert "--feedback" in adapter.sessions[1].prompt


def test_max_stories_limit(project):
    write_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        max_stories=1,
    )
    summary = engine.run()
    assert summary.done == 1
    assert "1-2-b" not in engine.state.tasks


def test_run_end_auto_sweep_fires_once(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=calls.append,
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert calls == ["run-end"]
    assert load_state(engine.run_dir).sweeps_triggered == ["run-end"]


def test_per_epic_auto_sweep_fires_at_boundary(project):
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="per-epic")
    )
    calls = []
    engine, _ = make_engine(
        project,
        [
            dev_effect(project, "1-1-a"),
            review_effect(project, "1-1-a", clean=True),
            dev_effect(project, "2-1-b"),
            review_effect(project, "2-1-b", clean=True),
        ],
        policy=policy,
        sweep_factory=calls.append,
    )
    summary = engine.run()
    assert summary.done == 2
    assert calls == ["epic-1"]  # boundary only; run-end mode not set


def test_auto_sweep_no_refire_on_resume(project):
    """The per-epic trigger is recorded before the gate pause, so resuming
    the run must not fire the same sweep again."""
    write_sprint(
        project,
        {
            "epic-1": "backlog",
            "1-1-a": "ready-for-dev",
            "epic-2": "backlog",
            "2-1-b": "ready-for-dev",
        },
    )
    policy = Policy(
        gates=GatesPolicy(mode="per-epic"),
        notify=QUIET,
        sweep=SweepPolicy(auto="per-epic"),
    )
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=calls.append,
    )
    assert engine.run().paused
    assert calls == ["epic-1"]

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [dev_effect(project, "2-1-b"), review_effect(project, "2-1-b", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=policy,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
        sweep_factory=calls.append,
    )
    assert resumed.run().done == 2
    assert calls == ["epic-1"]  # not re-fired


def test_auto_sweep_failure_does_not_pause_parent(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    policy = Policy(gates=GatesPolicy(mode="none"), notify=QUIET, sweep=SweepPolicy(auto="run-end"))

    def exploding(trigger):
        raise RuntimeError("child sweep blew up")

    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        policy=policy,
        sweep_factory=exploding,
    )
    summary = engine.run()
    assert summary.done == 1 and not summary.paused
    assert engine.state.finished
    journal = (engine.run_dir / "journal.jsonl").read_text()
    assert "sweep-auto-failed" in journal and "child sweep blew up" in journal


def test_no_auto_sweep_by_default(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    calls = []
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
        sweep_factory=calls.append,
    )
    engine.run()
    assert calls == []


def test_journal_records_decisions(project):
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    kinds = [e["kind"] for e in engine.journal.entries()]
    for expected in (
        "story-start",
        "session-start",
        "dev-decision",
        "review-result",
        "story-done",
        "run-complete",
    ):
        assert expected in kinds


def test_journal_stamps_log_position(tmp_path):
    journal = Journal(tmp_path)
    journal.append("run-start")
    journal.set_active_log("t-dev-1")
    journal.append("session-start", task_id="t-dev-1")  # log file not created yet
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "t-dev-1.log").write_bytes(b"x" * 37)
    journal.append("dev-decision", story_key="1-1-a")
    journal.append("custom", log_task="elsewhere", log_pos=5)  # caller fields win

    entries = journal.entries()
    assert "log_task" not in entries[0] and "log_pos" not in entries[0]
    assert entries[1]["log_task"] == "t-dev-1" and entries[1]["log_pos"] == 0
    assert entries[2]["log_task"] == "t-dev-1" and entries[2]["log_pos"] == 37
    assert entries[3]["log_task"] == "elsewhere" and entries[3]["log_pos"] == 5


def test_journal_log_position_covers_post_session_entries(project):
    """The active log is set at session-start and deliberately not cleared:
    post-session entries (decisions, story-done) point at the end of the log
    of the session they are about."""
    write_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [dev_effect(project, "1-1-a"), review_effect(project, "1-1-a", clean=True)],
    )
    engine.run()
    entries = engine.journal.entries()
    starts = [e for e in entries if e["kind"] == "session-start"]
    assert len(starts) == 2  # dev + review
    assert all(e["log_task"] == e["task_id"] for e in starts)
    assert all(isinstance(e["log_pos"], int) for e in starts)
    story_start = next(e for e in entries if e["kind"] == "story-start")
    assert "log_task" not in story_start  # written before any session
    dev_decision = next(e for e in entries if e["kind"] == "dev-decision")
    assert dev_decision["log_task"] == starts[0]["task_id"]
    story_done = next(e for e in entries if e["kind"] == "story-done")
    assert story_done["log_task"] == starts[-1]["task_id"]


# ----------------------------------------------------------- stop / SIGTERM


def test_run_stopped_via_real_signal(project, monkeypatch):
    """SIGTERM unwinds the loop as RunStopped: the run is marked stopped, the
    agent session is torn down, and the prior signal handlers are restored."""
    killed = []
    monkeypatch.setattr("automator.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])
    monkeypatch.setattr(engine, "_loop", lambda: os.kill(os.getpid(), signal.SIGTERM))

    prev_term = signal.getsignal(signal.SIGTERM)
    prev_int = signal.getsignal(signal.SIGINT)
    summary = engine.run()

    assert summary is not None
    assert load_state(engine.run_dir).stopped is True
    assert killed == ["test-run"]
    assert "run-stop" in (engine.run_dir / "journal.jsonl").read_text()
    assert signal.getsignal(signal.SIGTERM) is prev_term
    assert signal.getsignal(signal.SIGINT) is prev_int
    assert Engine._stop_signals_owner is None


def test_nested_engine_reraises_runstopped(project, monkeypatch):
    """A nested auto-sweep engine does not own the handlers, so it re-raises
    RunStopped for the outer (owning) engine to record — it still tears down
    its own agent session."""
    killed = []
    monkeypatch.setattr("automator.engine.kill_session", lambda rid: killed.append(rid))
    engine, _ = make_engine(project, [])

    def boom():
        raise RunStopped()

    monkeypatch.setattr(engine, "_loop", boom)
    sentinel = object()
    Engine._stop_signals_owner = sentinel  # pretend an outer engine owns signals
    try:
        with pytest.raises(RunStopped):
            engine.run()
    finally:
        Engine._stop_signals_owner = None

    assert load_state(engine.run_dir).stopped is False  # owner records it, not us
    assert killed == ["test-run"]
