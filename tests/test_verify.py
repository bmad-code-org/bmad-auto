from conftest import git, spec_path, write_spec, write_sprint

from automator import verify
from automator.model import StoryTask
from automator.policy import Policy, VerifyPolicy


def make_task(paths, story_key="1-1-a"):
    task = StoryTask(story_key=story_key, epic=1)
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def dev_result(sp):
    return {"workflow": "quick-dev", "spec_file": str(sp)}


def test_verify_dev_happy(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_missing_spec_file_claim(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, {})
    assert not out.ok and out.retryable and "missing spec_file" in out.reason


def test_verify_dev_spec_does_not_exist(project):
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(project.project / "ghost.md"))
    assert not out.ok and "does not exist" in out.reason


def test_verify_dev_wrong_status(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "draft", task.baseline_commit)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "expected 'in-review'" in out.reason


def test_verify_dev_review_disabled_expects_done(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)
    assert out.ok
    # the in-review handoff status is now rejected
    write_spec(sp, "in-review", task.baseline_commit)
    out = verify.verify_dev(task, project, dev_result(sp), review_enabled=False)
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_dev_lying_baseline(project):
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "deadbeef" * 5)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "does not match" in out.reason


def test_verify_dev_short_hash_baseline(project):
    # Sessions sometimes write `git rev-parse --short HEAD`; an abbreviation
    # of the recorded baseline is the same commit, not a lie.
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit[:7])
    (project.project / "src.txt").write_text("changed\n")

    out = verify.verify_dev(task, project, dev_result(sp))
    assert out.ok


def test_verify_dev_no_changes(project):
    # Spec claims NO_VCS baseline (skips the mismatch check); everything is
    # committed, so there are no changes since the orchestrator's baseline.
    write_sprint(project, {"1-1-a": "review"})
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", "NO_VCS")
    git(project.project, "add", "-A")
    git(project.project, "commit", "-q", "-m", "artifacts")
    task = make_task(project)
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "no changes" in out.reason


def test_verify_dev_sprint_not_synced(project):
    write_sprint(project, {"1-1-a": "in-progress"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    out = verify.verify_dev(task, project, dev_result(sp))
    assert not out.ok and "sprint-status" in out.reason


def test_verify_review_happy_and_commands(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    ok_policy = Policy(verify=VerifyPolicy(commands=("true",)))
    assert verify.verify_review(task, project, ok_policy).ok

    fail_policy = Policy(verify=VerifyPolicy(commands=("true", "false")))
    out = verify.verify_review(task, project, fail_policy)
    assert not out.ok and "verify command failed" in out.reason


def test_verify_review_spec_not_done(project):
    write_sprint(project, {"1-1-a": "done"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "in-review", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "expected 'done'" in out.reason


def test_verify_review_sprint_not_done(project):
    write_sprint(project, {"1-1-a": "review"})
    task = make_task(project)
    sp = spec_path(project, "1-1-a")
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    out = verify.verify_review(task, project, Policy())
    assert not out.ok and "sprint-status" in out.reason


def make_bundle_task(paths, dw_ids=("DW-1", "DW-2")):
    task = StoryTask(story_key="dw-test-bundle", epic=0, dw_ids=list(dw_ids))
    task.baseline_commit = verify.rev_parse_head(paths.project)
    return task


def bundle_ledger(paths, statuses: dict[str, str]) -> None:
    parts = []
    for dw_id, status in statuses.items():
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test\nlocation: n/a\n"
            f"reason: test\nstatus: {status}\n"
        )
    paths.deferred_work.parent.mkdir(parents=True, exist_ok=True)
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")


def test_verify_dev_bundle_happy_skips_sprint(project):
    # no sprint-status entry for the bundle key — must still pass
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "quick-dev", "spec_file": str(sp), "dw_ids": ["DW-2", "DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert out.ok
    assert task.spec_file == str(sp)


def test_verify_dev_bundle_dw_ids_mismatch(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "in-review", task.baseline_commit)
    (project.project / "src.txt").write_text("changed\n")
    rj = {"workflow": "quick-dev", "spec_file": str(sp), "dw_ids": ["DW-1"]}
    out = verify.verify_dev_bundle(task, project, rj)
    assert not out.ok and "dw_ids" in out.reason


def test_verify_review_bundle_ledger_gate(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)

    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "open"})
    out = verify.verify_review_bundle(task, project, Policy())
    assert not out.ok and out.fixable and "DW-2" in out.reason and "DW-1" not in out.reason

    bundle_ledger(project, {"DW-1": "done 2026-06-11", "DW-2": "done 2026-06-11"})
    assert verify.verify_review_bundle(task, project, Policy()).ok


def test_verify_review_bundle_missing_entry_fails(project):
    task = make_bundle_task(project)
    sp = project.implementation_artifacts / "spec-dw-test-bundle.md"
    write_spec(sp, "done", task.baseline_commit)
    task.spec_file = str(sp)
    bundle_ledger(project, {"DW-1": "done 2026-06-11"})  # DW-2 absent entirely
    out = verify.verify_review_bundle(task, project, Policy())
    assert not out.ok and out.fixable and "DW-2" in out.reason


def test_safe_rollback_reverts_tracked_and_removes_run_created(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))  # snapshot before the attempt
    (repo / "src.txt").write_text("dirty\n")  # tracked edit
    (repo / "junk.txt").write_text("run-created\n")  # untracked, created now
    keep = repo / ".automator" / "runs" / "r1"
    keep.mkdir(parents=True)
    (keep / "state.json").write_text("{}")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".automator",))
    assert (repo / "src.txt").read_text() == "original\n"  # tracked reverted
    assert not (repo / "junk.txt").exists()  # run-created removed
    assert (keep / "state.json").exists()  # .automator preserved


def test_safe_rollback_preserves_preexisting_untracked(project):
    repo = project.project
    (repo / "_bmad-output").mkdir(exist_ok=True)
    (repo / "_bmad-output" / "project-context.md").write_text("keep me\n")
    (repo / ".design-build").mkdir()
    (repo / ".design-build" / "x").write_text("keep me too\n")
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))  # includes the two files above
    (repo / "junk.txt").write_text("run-created\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".automator",))
    assert (repo / "_bmad-output" / "project-context.md").read_text() == "keep me\n"
    assert (repo / ".design-build" / "x").read_text() == "keep me too\n"
    assert not (repo / "junk.txt").exists()  # only run-created file removed


def test_safe_rollback_keep_dir_protects_run_created(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    out = repo / "_bmad-output"
    out.mkdir(exist_ok=True)
    (out / "fresh-artifact.md").write_text("generated this run\n")  # run-created

    verify.safe_rollback(
        repo, baseline, baseline_untracked=snap, keep=(".automator", "_bmad-output")
    )
    assert (out / "fresh-artifact.md").exists()  # protected by keep even though new


def test_safe_rollback_none_snapshot_removes_nothing(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    (repo / "src.txt").write_text("dirty\n")
    (repo / "junk.txt").write_text("untracked\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=None, keep=(".automator",))
    assert (repo / "src.txt").read_text() == "original\n"  # tracked still reverted
    assert (repo / "junk.txt").exists()  # no snapshot => never delete untracked


def test_safe_rollback_prunes_emptied_dirs(project):
    repo = project.project
    baseline = verify.rev_parse_head(repo)
    snap = sorted(verify.untracked_files(repo))
    nested = repo / "tmpdir" / "sub"
    nested.mkdir(parents=True)
    (nested / "f.txt").write_text("x\n")

    verify.safe_rollback(repo, baseline, baseline_untracked=snap, keep=(".automator",))
    assert not (repo / "tmpdir").exists()  # emptied parent dirs pruned


def test_worktree_clean_ignores_policy_file(project):
    # A tracked-but-modified .automator/policy.toml (rewritten by the TUI
    # settings editor) must not count as a dirty tree, or every settings edit
    # would force a commit before run/sweep/validate.
    pol = project.project / ".automator" / "policy.toml"
    pol.parent.mkdir(parents=True, exist_ok=True)
    pol.write_text('[gates]\nmode = "none"\n')
    git(project.project, "add", "-f", str(pol))
    git(project.project, "commit", "-q", "-m", "track policy")
    assert verify.worktree_clean(project.project)

    pol.write_text('[gates]\nmode = "per-epic"\n')  # edit the tracked config
    assert verify.worktree_clean(project.project)  # still "clean"

    (project.project / "src.txt").write_text("real change\n")  # any other edit
    assert not verify.worktree_clean(project.project)


def test_worktree_clean_flags_untracked_non_policy(project):
    (project.project / "stray.txt").write_text("untracked\n")
    assert not verify.worktree_clean(project.project)


def test_commit_story(project):
    task = make_task(project)
    (project.project / "src.txt").write_text("done work\n")
    sha = verify.commit_story(project.project, f"story {task.story_key}: via bmad-auto")
    assert sha != task.baseline_commit
    assert verify.worktree_clean(project.project)


def test_commit_paths_commits_only_listed(project):
    base = verify.rev_parse_head(project.project)
    (project.project / "src.txt").write_text("ledger-ish edit\n")  # the "tracked" target
    (project.project / "other.txt").write_text("unrelated work\n")  # must be left alone

    sha = verify.commit_paths(project.project, "chore: targeted", [project.project / "src.txt"])
    assert sha is not None and sha != base
    # only src.txt landed in the commit; other.txt is still uncommitted
    status = git(project.project, "status", "--porcelain")
    assert "other.txt" in status
    assert "src.txt" not in status


def test_commit_paths_noop_when_unchanged(project):
    assert verify.commit_paths(project.project, "noop", [project.project / "src.txt"]) is None
    # a path outside the repo is ignored, not an error
    assert verify.commit_paths(project.project, "noop", [project.project.parent / "x"]) is None


def test_read_frontmatter_tolerates_garbage(project):
    p = project.project / "x.md"
    p.write_text("no frontmatter here")
    assert verify.read_frontmatter(p) == {}
    p.write_text("---\n: : :\nbroken yaml [\n---\nbody")
    assert verify.read_frontmatter(p) == {}
