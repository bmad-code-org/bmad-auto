"""Deterministic post-session verification. Never trust LLM self-reports.

verify_dev / verify_review check artifacts on disk and git state against
what the session's result.json claims; run_verify_commands executes the
policy's test/lint gates with the orchestrator's own subprocess calls.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from . import deferredwork
from .bmadconfig import ProjectPaths
from .model import StoryTask
from .policy import Policy
from .sprintstatus import story_status

GIT_TIMEOUT_S = 120
COMMAND_TIMEOUT_S = 30 * 60


class GitError(Exception):
    pass


@dataclass(frozen=True)
class VerifyOutcome:
    ok: bool
    reason: str = ""
    severity: str = ""  # "" | "CRITICAL" | "PREFERENCE" — set when not retryable
    # fixable failures carry concrete evidence (failing command output) that a
    # feedback-driven repair session can act on; non-fixable retries start over
    fixable: bool = False

    @classmethod
    def passed(cls) -> "VerifyOutcome":
        return cls(ok=True)

    @classmethod
    def retry(cls, reason: str, fixable: bool = False) -> "VerifyOutcome":
        return cls(ok=False, reason=reason, fixable=fixable)

    @classmethod
    def escalate(cls, reason: str, severity: str = "CRITICAL") -> "VerifyOutcome":
        return cls(ok=False, reason=reason, severity=severity)

    @property
    def retryable(self) -> bool:
        return not self.ok and not self.severity


def _git(repo: Path, *args: str) -> tuple[int, str]:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args],
        capture_output=True,
        text=True,
        timeout=GIT_TIMEOUT_S,
    )
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def rev_parse_head(repo: Path) -> str:
    rc, out = _git(repo, "rev-parse", "HEAD")
    if rc != 0:
        raise GitError(f"git rev-parse HEAD failed in {repo}: {out}")
    return out


def worktree_clean(repo: Path) -> bool:
    rc, out = _git(repo, "status", "--porcelain")
    if rc != 0:
        raise GitError(f"git status failed in {repo}: {out}")
    return out == ""


def same_commit(a: str, b: str) -> bool:
    """Hash equality tolerant of abbreviated forms (>= 7 chars, git's default
    --short length); sessions sometimes report `git rev-parse --short HEAD`."""
    if len(a) < 7 or len(b) < 7:
        return a == b
    return a.startswith(b) or b.startswith(a)


def has_changes_since(repo: Path, baseline: str) -> bool:
    """True if tracked changes since baseline OR untracked files exist."""
    rc, _ = _git(repo, "diff", "--quiet", baseline, "--")
    if rc != 0:
        return True
    rc, out = _git(repo, "ls-files", "--others", "--exclude-standard")
    return rc == 0 and out != ""


def reset_hard(repo: Path, baseline: str, keep: tuple[str, ...] = (".automator",)) -> None:
    rc, out = _git(repo, "reset", "--hard", baseline)
    if rc != 0:
        raise GitError(f"git reset --hard {baseline} failed: {out}")
    clean_args = ["clean", "-fd"]
    for path in keep:
        clean_args += ["-e", path]
    rc, out = _git(repo, *clean_args)
    if rc != 0:
        raise GitError(f"git clean failed: {out}")


def read_frontmatter(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return {}
    parts = text.split("---", 2)
    if len(parts) < 3:
        return {}
    try:
        doc = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return {}
    return doc if isinstance(doc, dict) else {}


def set_frontmatter_status(path: Path, status: str) -> bool:
    """Rewrite the `status:` field in a spec's `---`…`---` frontmatter block.

    A minimal in-place line replacement (not a YAML round-trip) so the spec's
    formatting, comments, and field order survive — only the status value
    changes. Returns True when the file was rewritten, False when it has no
    frontmatter or already carries `status`. Idempotent.
    """
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return False
    parts = text.split("---", 2)
    if len(parts) < 3:
        return False
    block_lines = parts[1].splitlines(keepends=True)
    replaced = False
    for i, line in enumerate(block_lines):
        stripped = line.lstrip()
        if stripped.startswith("status:") and not stripped.startswith("status_"):
            indent = line[: len(line) - len(stripped)]
            newline = "\n" if line.endswith("\n") else ""
            block_lines[i] = f"{indent}status: {status}{newline}"
            replaced = True
            break
    if not replaced:
        return False
    rebuilt = parts[0] + "---" + "".join(block_lines) + "---" + parts[2]
    if rebuilt == text:  # already at the target value — idempotent no-op
        return False
    path.write_text(rebuilt, encoding="utf-8")
    return True


def resolve_spec_path(spec_file: str, paths: ProjectPaths) -> Path:
    p = Path(spec_file)
    if p.is_absolute():
        return p
    candidate = paths.project / p
    if candidate.is_file():
        return candidate
    return paths.implementation_artifacts / p


def verify_dev(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
) -> VerifyOutcome:
    rj = result_json or {}
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # With review disabled, the dev session runs its own internal review and
    # finalizes straight to done; otherwise it hands off at in-review.
    expected = "in-review" if review_enabled else "done"
    fm = read_frontmatter(spec_path)
    status = str(fm.get("status", "")).strip()
    if status != expected:
        return VerifyOutcome.retry(f"spec status is {status!r}, expected {expected!r}: {spec_path}")

    claimed_baseline = str(fm.get("baseline_commit", "")).strip()
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        if not same_commit(claimed_baseline, task.baseline_commit):
            return VerifyOutcome.retry(
                f"spec baseline_commit {claimed_baseline[:12]} does not match "
                f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
            )

    if task.baseline_commit:
        try:
            if not has_changes_since(paths.project, task.baseline_commit):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint not in ("review", "done"):
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected 'review'"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


def verify_dev_bundle(
    task: StoryTask,
    paths: ProjectPaths,
    result_json: dict[str, Any] | None,
    review_enabled: bool = True,
) -> VerifyOutcome:
    """verify_dev for a deferred-work bundle: bundles have no sprint-status
    entry, but the session must claim exactly the dw ids the bundle owns."""
    rj = result_json or {}
    spec_file = rj.get("spec_file")
    if not spec_file:
        return VerifyOutcome.retry("dev result.json missing spec_file")
    spec_path = resolve_spec_path(str(spec_file), paths)
    if not spec_path.is_file():
        return VerifyOutcome.retry(f"claimed spec file does not exist: {spec_path}")

    # With review disabled, the dev session finalizes the bundle straight to done.
    expected = "in-review" if review_enabled else "done"
    fm = read_frontmatter(spec_path)
    status = str(fm.get("status", "")).strip()
    if status != expected:
        return VerifyOutcome.retry(f"spec status is {status!r}, expected {expected!r}: {spec_path}")

    claimed_baseline = str(fm.get("baseline_commit", "")).strip()
    if task.baseline_commit and claimed_baseline not in ("", "NO_VCS"):
        if not same_commit(claimed_baseline, task.baseline_commit):
            return VerifyOutcome.retry(
                f"spec baseline_commit {claimed_baseline[:12]} does not match "
                f"orchestrator-recorded baseline {task.baseline_commit[:12]}"
            )

    if task.baseline_commit:
        try:
            if not has_changes_since(paths.project, task.baseline_commit):
                return VerifyOutcome.retry("no changes in worktree since baseline commit")
        except GitError as e:
            return VerifyOutcome.escalate(str(e))

    claimed_ids = {str(i) for i in rj.get("dw_ids", [])}
    if claimed_ids != set(task.dw_ids):
        return VerifyOutcome.retry(
            f"result.json dw_ids {sorted(claimed_ids)} do not match the bundle's "
            f"{sorted(task.dw_ids)}"
        )

    task.spec_file = str(spec_path)
    return VerifyOutcome.passed()


@dataclass(frozen=True)
class CommandResult:
    command: str
    returncode: int
    output_tail: str


def run_verify_commands(policy: Policy, cwd: Path) -> list[CommandResult]:
    results = []
    for command in policy.verify.commands:
        try:
            # Verify commands are operator-authored shell strings from the project's
            # policy (e.g. "pytest -q && ruff check"); shell=True is intentional here.
            proc = subprocess.run(  # nosec B602
                command,
                shell=True,
                cwd=cwd,
                capture_output=True,
                text=True,
                timeout=COMMAND_TIMEOUT_S,
            )
            output = (proc.stdout + proc.stderr)[-2000:]
            results.append(CommandResult(command, proc.returncode, output))
        except subprocess.TimeoutExpired:
            results.append(CommandResult(command, -1, "timed out"))
    return results


def verify_commands_outcome(policy: Policy, cwd: Path) -> VerifyOutcome:
    """Run the policy's deterministic verify commands. Failures are fixable:
    the captured output is concrete feedback a repair session can act on."""
    for result in run_verify_commands(policy, cwd):
        if result.returncode != 0:
            return VerifyOutcome.retry(
                f"verify command failed (rc={result.returncode}): {result.command}\n"
                f"{result.output_tail}",
                fixable=True,
            )
    return VerifyOutcome.passed()


def verify_review(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = read_frontmatter(Path(task.spec_file))
    status = str(fm.get("status", "")).strip()
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    sprint = story_status(paths.sprint_status, task.story_key)
    if sprint != "done":
        return VerifyOutcome.retry(
            f"sprint-status for {task.story_key} is {sprint!r}, expected 'done'"
        )

    return verify_commands_outcome(policy, paths.project)


def verify_review_bundle(task: StoryTask, paths: ProjectPaths, policy: Policy) -> VerifyOutcome:
    """verify_review for a deferred-work bundle: no sprint-status check, but
    every dw id the bundle owns must be marked done in the ledger on disk —
    the LLM is told to flip them; this gate is why we can trust it happened."""
    if not task.spec_file:
        return VerifyOutcome.retry("no spec file recorded for task")
    fm = read_frontmatter(Path(task.spec_file))
    status = str(fm.get("status", "")).strip()
    if status != "done":
        return VerifyOutcome.retry(f"spec status is {status!r}, expected 'done'")

    ledger = paths.deferred_work
    text = ledger.read_text(encoding="utf-8") if ledger.is_file() else ""
    entries = {e.id: e for e in deferredwork.parse_ledger(text)}
    not_done = sorted(
        i for i in task.dw_ids if i not in entries or not entries[i].status.startswith("done")
    )
    if not_done:
        return VerifyOutcome.retry(
            "deferred-work entries not marked done in "
            f"{ledger}: {', '.join(not_done)} — set each to `status: done <date>` "
            "with a `resolution:` line",
            fixable=True,
        )

    return verify_commands_outcome(policy, paths.project)


def commit_story(repo: Path, message: str) -> str:
    rc, out = _git(repo, "add", "-A")
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "commit", "-m", message)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)


def commit_paths(repo: Path, message: str, paths: list[Path]) -> str | None:
    """Commit exactly `paths` (and nothing else), leaving any unrelated working
    or staged changes untouched. Unlike commit_story's `add -A`, this is safe to
    call out of band (e.g. `bmad-auto decisions`) when the tree may hold the
    user's own uncommitted work. Returns the new HEAD sha, or None when the
    given paths had no changes to commit. Paths outside the repo are ignored."""
    rels: list[str] = []
    repo_root = repo.resolve()
    for p in paths:
        try:
            rels.append(str(Path(p).resolve().relative_to(repo_root)))
        except ValueError:
            continue
    if not rels:
        return None
    rc, out = _git(repo, "add", "--", *rels)
    if rc != 0:
        raise GitError(f"git add failed: {out}")
    rc, out = _git(repo, "status", "--porcelain", "--", *rels)
    if rc != 0:
        raise GitError(f"git status failed: {out}")
    if not out:
        return None  # nothing changed in these paths
    # pathspec form commits only `rels`, ignoring any other staged changes
    rc, out = _git(repo, "commit", "-m", message, "--", *rels)
    if rc != 0:
        raise GitError(f"git commit failed: {out}")
    return rev_parse_head(repo)
