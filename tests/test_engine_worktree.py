"""Phase 3: isolation="worktree" — each unit runs in its own git worktree and
merges back into the target branch locally. Sessions run inside the worktree
(spec.cwd), so the effects here write artifacts rebased onto that checkout.

Exercised end-to-end against the conftest `project` sandbox with the mock
adapter (no tmux, no LLM).
"""

from __future__ import annotations

from conftest import _spec_baseline, git, set_sprint, write_spec, write_sprint

from automator.adapters.base import SessionResult
from automator.adapters.mock import MockAdapter
from automator.engine import Engine
from automator.journal import Journal, load_state
from automator.model import Phase, RunState, StoryTask, TokenUsage
from automator.policy import EnginePolicy, GatesPolicy, NotifyPolicy, Policy, ScmPolicy
from automator.verify import (
    branch_exists,
    current_branch,
    rev_parse_head,
    worktree_clean,
    worktree_list,
)

QUIET = NotifyPolicy(desktop=False, file=True)


def wt_policy(**scm) -> Policy:
    return Policy(
        gates=GatesPolicy(mode="none"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree", **scm),
    )


def commit_sprint(project, statuses: dict[str, str]) -> None:
    """Worktrees are checkouts of a commit, so the sprint board (and artifact
    dirs) must be committed before the run, not left untracked."""
    write_sprint(project, statuses)
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "sprint")


def wt_dev_effect(project, story_key):
    """Dev session running inside the unit worktree (spec.cwd)."""

    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        baseline = rev_parse_head(cwd)
        src = cwd / "src.txt"
        src.write_text(src.read_text() + f"change for {story_key}\n")
        sp = wt.implementation_artifacts / f"spec-{story_key}.md"
        skip_review = spec.env.get("BMAD_AUTO_SKIP_REVIEW") == "1"
        final = "done" if skip_review else "in-review"
        write_spec(sp, final, baseline)
        set_sprint(wt, story_key, final if skip_review else "review")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "quick-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
            },
        )

    return effect


def wt_review_effect(project, story_key, clean: bool, patched: int = 0):
    def effect(spec):
        cwd = spec.cwd
        wt = project.rebased(cwd)
        if clean:
            sp = wt.implementation_artifacts / f"spec-{story_key}.md"
            write_spec(sp, "done", _spec_baseline(sp))
            set_sprint(wt, story_key, "done")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "code-review",
                "clean": clean,
                "patched": patched,
                "deferred": 0,
                "dismissed": 0,
                "escalations": [],
            },
        )

    return effect


def make_engine(project, script, policy=None, run_id="test-run", **kwargs):
    run_dir = project.project / ".automator" / "runs" / run_id
    adapter = MockAdapter(script, usage_per_session=TokenUsage(input_tokens=10, output_tokens=5))
    state = RunState(run_id=run_id, project=str(project.project), started_at="now")
    engine = Engine(
        paths=project,
        policy=policy or wt_policy(),
        adapter=adapter,
        run_dir=run_dir,
        journal=Journal(run_dir),
        state=state,
        **kwargs,
    )
    return engine, adapter


def journal_kinds(engine):
    return [e["kind"] for e in engine.journal.entries()]


# ----------------------------------------------------------------- happy path


def test_worktree_happy_path_merges_to_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    head_before = rev_parse_head(project.project)
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()

    assert summary.done == 1 and summary.deferred == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DONE
    # the unit's work landed on the target branch (main, checked out in the repo)
    assert engine.state.target_branch == "main"
    assert rev_parse_head(project.project) != head_before
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    # worktree cleaned up, branch deleted (delete_branch default), tree clean
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert not branch_exists(project.project, "automator/test-run/1-1-a")
    assert worktree_clean(project.project)
    kinds = journal_kinds(engine)
    assert "worktree-opened" in kinds and "unit-merged" in kinds


def test_worktree_run_dir_is_outside_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    opened = []
    orig = Journal.append

    def spy(self, kind, **kw):
        if kind == "worktree-opened":
            opened.append(kw["path"])
        return orig(self, kind, **kw)

    Journal.append = spy
    try:
        engine.run()
    finally:
        Journal.append = orig

    assert opened, "expected a worktree-opened event"
    wt = opened[0]
    # run state lives in the main repo, never inside the worktree
    assert str(engine.run_dir.resolve()).startswith(str(project.project.resolve()))
    assert not str(engine.run_dir.resolve()).startswith(str(wt))


def test_worktree_multiple_stories_serialize_onto_target(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [
            wt_dev_effect(project, "1-1-a"),
            wt_review_effect(project, "1-1-a", clean=True),
            wt_dev_effect(project, "1-2-b"),
            wt_review_effect(project, "1-2-b", clean=True),
        ],
    )
    summary = engine.run()

    assert summary.done == 2
    src = (project.project / "src.txt").read_text()
    assert "change for 1-1-a" in src and "change for 1-2-b" in src
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


# ----------------------------------------------------------------- branch naming


def test_branch_per_story_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="story", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "automator/test-run/1-1-a"
    assert branch_exists(project.project, "automator/test-run/1-1-a")


def test_branch_per_run_naming(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(branch_per="run", delete_branch=False),
    )
    engine.run()
    assert engine.state.tasks["1-1-a"].branch == "automator/test-run"
    assert branch_exists(project.project, "automator/test-run")


# ----------------------------------------------------------------- merge strategies


def test_worktree_squash_merge_linear_history(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="squash"),
    )
    summary = engine.run()
    assert summary.done == 1
    assert git(project.project, "log", "--oneline", "--merges") == ""  # squash → linear


# ----------------------------------------------------------------- failure preservation


def _defer_script(project, key):
    """Dev succeeds, then review never converges → plateau defer."""
    return [wt_dev_effect(project, key)] + [
        wt_review_effect(project, key, clean=False, patched=1) for _ in range(3)
    ]


def test_worktree_defer_keeps_failed_unit(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, _defer_script(project, "1-1-a"))
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    # the failed unit's diff is preserved for forensics
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file()
    assert "change for 1-1-a" in patch.read_text()
    # keep_failed default → worktree + branch remain mounted for inspection
    assert branch_exists(project.project, "automator/test-run/1-1-a")
    listed = [p.resolve() for p in worktree_list(project.project)]
    assert project.project.resolve() in listed and len(listed) == 2
    # the main repo is untouched by the failed unit
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()
    assert worktree_clean(project.project)


def test_worktree_defer_without_keep_drops_worktree_but_saves_patch(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project, _defer_script(project, "1-1-a"), policy=wt_policy(keep_failed=False)
    )
    summary = engine.run()

    assert summary.deferred == 1
    patch = engine.run_dir / "failed" / "1-1-a" / "changes.patch"
    assert patch.is_file() and "change for 1-1-a" in patch.read_text()
    # not kept → worktree removed, branch deleted
    assert not branch_exists(project.project, "automator/test-run/1-1-a")
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_worktree_defer_then_next_story_succeeds(project):
    """A deferred (kept) unit must not block the next story's worktree/merge."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [
        wt_dev_effect(project, "1-2-b"),
        wt_review_effect(project, "1-2-b", clean=True),
    ]
    engine, _ = make_engine(project, script)
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 1
    assert "change for 1-2-b" in (project.project / "src.txt").read_text()
    assert "change for 1-1-a" not in (project.project / "src.txt").read_text()


def test_branch_per_run_kept_failure_defers_next_unit_gracefully(project):
    """branch_per=run shares one branch; a kept-failed unit holds it, so the
    next unit can't mount it. That degrades to a deferral, never a crash."""
    commit_sprint(project, {"1-1-a": "ready-for-dev", "1-2-b": "ready-for-dev"})
    script = _defer_script(project, "1-1-a") + [wt_dev_effect(project, "1-2-b")]
    engine, _ = make_engine(project, script, policy=wt_policy(branch_per="run"))
    summary = engine.run()

    assert summary.deferred == 2 and summary.done == 0 and not summary.paused
    assert "could not open worktree" in engine.state.tasks["1-2-b"].defer_reason
    assert "worktree-open-failed" in journal_kinds(engine)


# ----------------------------------------------------------------- configured target


def test_configured_target_branch_created_and_checked_out(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(target_branch="integration"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert engine.state.target_branch == "integration"
    assert current_branch(project.project) == "integration"
    assert branch_exists(project.project, "integration")
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()


def test_worktree_merge_conflict_escalates_and_keeps_branch(project):
    """A unit whose ff-only merge can't fast-forward (target diverged) escalates
    cleanly without an illegal DONE->ESCALATED transition, keeping its branch."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(merge_strategy="ff"),
    )
    # diverge the target right after the worktree is cut so ff-only cannot apply
    import automator.engine as eng

    real_open = eng.open_unit_workspace

    def diverging_open(*a, **k):
        unit = real_open(*a, **k)
        (project.project / "diverge.txt").write_text("target moved\n")
        git(project.project, "add", "-A")
        git(project.project, "commit", "-q", "-m", "target diverges")
        return unit

    eng.open_unit_workspace = diverging_open
    try:
        summary = engine.run()
    finally:
        eng.open_unit_workspace = real_open

    assert summary.paused and summary.escalated == 1
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.ESCALATED
    # the unit branch is kept for manual merge
    assert branch_exists(project.project, "automator/test-run/1-1-a")


# ----------------------------------------------------------------- resume


def test_worktree_spec_approval_pause_resumes_in_same_worktree(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    gated = Policy(
        gates=GatesPolicy(mode="per-story-spec-approval"),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
    )
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")], policy=gated)
    summary = engine.run()

    assert summary.paused
    saved = load_state(engine.run_dir)
    task = saved.tasks["1-1-a"]
    assert task.phase == Phase.DEV_VERIFY and task.worktree_path and task.branch
    # the worktree stays mounted across the pause so resume can review in it
    assert branch_exists(project.project, "automator/test-run/1-1-a")
    assert len(worktree_list(project.project)) == 2

    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter([wt_review_effect(project, "1-1-a", clean=True)])
    resumed = Engine(
        paths=project,
        policy=gated,
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary2 = resumed.run()

    assert summary2.done == 1
    assert [s.role for s in adapter.sessions] == ["review"]
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert worktree_clean(project.project)


def test_worktree_crash_restart_discards_stale_worktree(project):
    """A unit interrupted before the spec gate is restarted fresh: the stale
    worktree is discarded and a new one mounted, not stacked on top."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(project, [wt_dev_effect(project, "1-1-a")])
    # simulate an interrupted unit left mid-flight (DEV_RUNNING, worktree mounted)
    from automator.workspace import open_unit_workspace

    unit = open_unit_workspace(
        project.project, project, "test-run", "1-1-a", "main", "story", engine.run_dir
    )
    task = StoryTask("1-1-a", 1)
    engine.state.tasks["1-1-a"] = task
    task.phase = Phase.DEV_RUNNING
    task.worktree_path = str(unit.path)
    task.branch = unit.branch
    task.baseline_commit = unit.baseline
    engine._save()

    # resume with a full dev+review script → restart should succeed
    state = load_state(engine.run_dir)
    state.clear_pause()
    adapter = MockAdapter(
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)]
    )
    resumed = Engine(
        paths=project,
        policy=wt_policy(),
        adapter=adapter,
        run_dir=engine.run_dir,
        journal=engine.journal,
        state=state,
    )
    summary = resumed.run()

    assert summary.done == 1
    assert "change for 1-1-a" in (project.project / "src.txt").read_text()
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


# ----------------------------------------------------------------- regression guard


def test_isolation_none_leaves_no_worktrees(project):
    """The default (isolation=none) path must not create branches/worktrees."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=Policy(gates=GatesPolicy(mode="none"), notify=QUIET),  # isolation defaults to none
    )
    summary = engine.run()
    assert summary.done == 1
    assert engine.state.target_branch == ""  # never resolved in none mode
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]
    assert "worktree-opened" not in journal_kinds(engine)


# ----------------------------------------------------------------- new guards (review hardening)


def test_detached_head_pauses_instead_of_landing_on_unreferenced_commit(project):
    """isolation=worktree with no configured target on a detached HEAD has no
    branch to merge into; the run must pause rather than commit onto a nameless
    detached HEAD that the next checkout would orphan."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    git(project.project, "checkout", "--detach")
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
    )
    summary = engine.run()
    assert summary.paused
    assert "detached HEAD" in (engine.state.paused_reason or "")
    # nothing was isolated into a worktree
    assert [p.resolve() for p in worktree_list(project.project)] == [project.project.resolve()]


def test_commit_message_template_applied(project):
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=wt_policy(commit_message_template="feat({story_key}): via {run_id}"),
    )
    summary = engine.run()
    assert summary.done == 1
    # the story's commit message (not the merge commit) used the template
    log = git(project.project, "log", "--format=%s")
    assert "feat(1-1-a): via test-run" in log
    assert "implemented" not in log  # built-in default was not used


# ------------------------------------------------ per_worktree engine plugin


def _write_engine_plugin(
    project, name, *, ready="true", setup="true", teardown="true", seed_globs=None
):
    """A project-local engine plugin whose hooks are shell stubs (no real Unity).
    Commands are TOML literal strings, so they may embed double quotes but not
    single quotes."""
    eng_dir = project.project / ".automator" / "engines" / name
    eng_dir.mkdir(parents=True)
    lines = [
        f'name = "{name}"',
        f"ready_cmd = '{ready}'",
        f"worktree_setup_cmd = '{setup}'",
        f"worktree_teardown_cmd = '{teardown}'",
    ]
    if seed_globs:
        globs = ", ".join(f'"{g}"' for g in seed_globs)
        lines.append(f"seed_globs = [{globs}]")
    (eng_dir / "engine.toml").write_text("\n".join(lines) + "\n")


def _pw_policy(name, **gates):
    return Policy(
        gates=GatesPolicy(mode=gates.get("mode", "none")),
        notify=QUIET,
        scm=ScmPolicy(isolation="worktree"),
        engine=EnginePolicy(name=name, editor_mode="per_worktree"),
    )


def test_per_worktree_setup_then_gate_then_teardown_and_seed(project):
    """Happy path: the worktree is seeded, setup launches the Editor, the gate
    waits (and only passes because setup ran first), the agent runs, teardown
    quits the Editor. Ordering is proven by the gate depending on a setup marker."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    # a gitignored MCP skill dir present in the main repo (untracked) to be seeded
    skill = project.project / ".claude" / "skills" / "gameobject-create"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("tool", encoding="utf-8")
    # setup asserts the seed reached its cwd (the worktree) before marking ready;
    # the gate fails unless that marker exists -> proves seed+setup precede the gate.
    _write_engine_plugin(
        project,
        "stub",
        setup="test -f .claude/skills/gameobject-create/SKILL.md "
        '&& touch "$BMAD_AUTO_RUN_DIR/setup-done"',
        ready='test -f "$BMAD_AUTO_RUN_DIR/setup-done"',
        teardown='touch "$BMAD_AUTO_RUN_DIR/teardown-done"',
        seed_globs=[".claude/skills/*"],
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy("stub"),
    )
    summary = engine.run()

    assert summary.done == 1
    assert (engine.run_dir / "setup-done").is_file()
    assert (engine.run_dir / "teardown-done").is_file()
    kinds = journal_kinds(engine)
    for k in ("engine-setup", "engine-setup-ok", "engine-ready", "engine-teardown-ok"):
        assert k in kinds, k
    # the dev + review sessions actually ran (gate let them through)
    assert [s.role for s in adapter.sessions] == ["dev", "review"]


def test_per_worktree_setup_failure_defers_and_skips_session(project):
    """A setup failure (Editor wouldn't launch) defers the unit, never starts a
    session, still tears down best-effort, and closes the (empty) worktree."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_engine_plugin(
        project,
        "stub",
        setup="exit 3",
        teardown='touch "$BMAD_AUTO_RUN_DIR/teardown-done"',
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a"), wt_review_effect(project, "1-1-a", clean=True)],
        policy=_pw_policy("stub"),
    )
    summary = engine.run()

    assert summary.deferred == 1 and summary.done == 0 and not summary.paused
    task = engine.state.tasks["1-1-a"]
    assert task.phase == Phase.DEFERRED
    assert "worktree setup failed" in task.defer_reason
    assert adapter.sessions == []  # gate/setup ran before any dev session
    kinds = journal_kinds(engine)
    assert "engine-setup-failed" in kinds and "engine-ready" not in kinds
    # teardown still ran; the deferred unit's worktree is kept (keep_failed default)
    # for inspection, exactly like any other deferral — but the Editor was quit.
    assert (engine.run_dir / "teardown-done").is_file()
    assert len(worktree_list(project.project)) == 2


def test_per_worktree_ready_gate_failure_defers(project):
    """Setup succeeds but the Editor never reports ready -> defer + teardown."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_engine_plugin(
        project,
        "stub",
        ready="exit 1",
        teardown='touch "$BMAD_AUTO_RUN_DIR/teardown-done"',
    )
    engine, adapter = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy("stub"),
    )
    summary = engine.run()

    assert summary.deferred == 1 and not summary.paused
    assert engine.state.tasks["1-1-a"].phase == Phase.DEFERRED
    assert adapter.sessions == []
    kinds = journal_kinds(engine)
    assert "engine-setup-ok" in kinds and "engine-not-ready" in kinds
    assert (engine.run_dir / "teardown-done").is_file()


def test_per_worktree_teardown_runs_on_pause(project):
    """A spec-approval pause leaves the worktree mounted, but the managed Editor
    is still torn down (teardown runs in the finally, even as RunPaused unwinds)."""
    commit_sprint(project, {"1-1-a": "ready-for-dev"})
    _write_engine_plugin(
        project,
        "stub",
        teardown='touch "$BMAD_AUTO_RUN_DIR/teardown-done"',
    )
    engine, _ = make_engine(
        project,
        [wt_dev_effect(project, "1-1-a")],
        policy=_pw_policy("stub", mode="per-story-spec-approval"),
    )
    summary = engine.run()

    assert summary.paused
    # the worktree stays up for resume, but the Editor was quit
    assert len(worktree_list(project.project)) == 2
    assert (engine.run_dir / "teardown-done").is_file()
    assert "engine-teardown-ok" in journal_kinds(engine)


def test_spec_file_serialized_relative_to_worktree():
    """A worktree task persists spec_file relative to its worktree so a kept run's
    state stays portable (no dangling absolute path into a pruned worktree)."""
    task = StoryTask(story_key="1-1-a", epic=1, phase=Phase.DEFERRED)
    task.worktree_path = "/repo/.automator/runs/run/worktrees/1-1-a"
    task.spec_file = "/repo/.automator/runs/run/worktrees/1-1-a/_out/spec.md"
    assert task.to_dict()["spec_file"] == "_out/spec.md"
    # a spec living outside the worktree stays absolute
    task.spec_file = "/elsewhere/spec.md"
    assert task.to_dict()["spec_file"] == "/elsewhere/spec.md"
    # in-place mode (no worktree) is unchanged
    task.worktree_path = ""
    task.spec_file = "/repo/_out/spec.md"
    assert task.to_dict()["spec_file"] == "/repo/_out/spec.md"
