"""Shared fixtures: a sandbox BMAD project with a real git repo, and helpers
that simulate the side effects skill sessions would have on disk."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
import yaml

from automator.adapters.base import SessionResult, SessionSpec
from automator.bmadconfig import ProjectPaths
from automator.verify import rev_parse_head

SPRINT_TEMPLATE = {
    "generated": "01-06-2026 10:00",
    "last_updated": "01-06-2026 10:00",
    "project": "sandbox",
    "project_key": "NOKEY",
    "tracking_system": "file-system",
    "development_status": {},
}


def git(repo: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", "-C", str(repo), *args], capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def project(tmp_path: Path) -> ProjectPaths:
    """Git repo with BMAD-shaped artifact dirs and an initial commit."""
    root = tmp_path / "sandbox"
    impl = root / "_bmad-output" / "implementation-artifacts"
    plan = root / "_bmad-output" / "planning-artifacts"
    impl.mkdir(parents=True)
    plan.mkdir(parents=True)
    (root / "src.txt").write_text("original\n")
    (root / ".gitignore").write_text(".automator/runs/\n")  # as `bmad-auto init` would
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "test@test")
    git(root, "config", "user.name", "test")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "initial")
    return ProjectPaths(project=root, implementation_artifacts=impl, planning_artifacts=plan)


def install_bmad_config(paths: ProjectPaths) -> None:
    """Write the _bmad/bmm/config.yaml that bmadconfig.load_paths resolves."""
    cfg = paths.project / "_bmad" / "bmm"
    cfg.mkdir(parents=True)
    (cfg / "config.yaml").write_text(
        "implementation_artifacts: '{project-root}/_bmad-output/implementation-artifacts'\n"
        "planning_artifacts: '{project-root}/_bmad-output/planning-artifacts'\n"
    )


def write_sprint(paths: ProjectPaths, statuses: dict[str, str]) -> None:
    doc = dict(SPRINT_TEMPLATE)
    doc["development_status"] = dict(statuses)
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def set_sprint(paths: ProjectPaths, key: str, status: str) -> None:
    doc = yaml.safe_load(paths.sprint_status.read_text())
    doc["development_status"][key] = status
    paths.sprint_status.write_text(yaml.safe_dump(doc, sort_keys=False))


def write_spec(path: Path, status: str, baseline: str) -> None:
    path.write_text(
        f"---\ntitle: 'test'\ntype: 'feature'\nstatus: '{status}'\n"
        f"baseline_commit: '{baseline}'\n---\n\n## Intent\n\ntest spec\n"
    )


def spec_path(paths: ProjectPaths, story_key: str) -> Path:
    return paths.implementation_artifacts / f"spec-{story_key}.md"


def dev_effect(paths: ProjectPaths, story_key: str):
    """Simulate a successful quick-dev automation session."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        source.write_text(source.read_text() + f"change for {story_key}\n")
        sp = spec_path(paths, story_key)
        write_spec(sp, "in-review", baseline)
        set_sprint(paths, story_key, "review")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "quick-dev",
                "story_key": story_key,
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 3,
                "tasks_done": 3,
                "verification": [],
                "escalations": [],
            },
        )

    return effect


def review_effect(paths: ProjectPaths, story_key: str, clean: bool, patched: int = 0):
    """Simulate a code-review automation session."""

    def effect(spec: SessionSpec) -> SessionResult:
        if clean:
            sp = spec_path(paths, story_key)
            baseline = _spec_baseline(sp)
            write_spec(sp, "done", baseline)
            set_sprint(paths, story_key, "done")
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


def _spec_baseline(path: Path) -> str:
    for line in path.read_text().splitlines():
        if line.startswith("baseline_commit:"):
            return line.split(":", 1)[1].strip().strip("'\"")
    return ""


# ----------------------------------------------------------- sweep helpers


def write_ledger(paths: ProjectPaths, statuses: dict[str, str], commit: bool = True) -> None:
    """Write a DW-format deferred-work ledger; statuses maps id -> status
    value. Committed by default — sweeps start from a clean tree."""
    parts = ["# Deferred Work\n"]
    for dw_id, status in statuses.items():
        parts.append(
            f"### {dw_id}: item {dw_id}\n\norigin: test, 2026-06-01\n"
            f"location: src.txt:1\nreason: test entry.\nstatus: {status}\n"
        )
    paths.deferred_work.write_text("\n".join(parts), encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "ledger")


def mark_ledger_done(paths: ProjectPaths, dw_ids, date: str = "2026-06-11") -> None:
    from automator import deferredwork

    for dw_id in dw_ids:
        deferredwork.mark_done(paths.deferred_work, dw_id, date, "built in test")


def write_legacy_ledger(paths: ProjectPaths, text: str, commit: bool = True) -> None:
    """Write a freeform (pre-DW-format) deferred-work ledger verbatim."""
    paths.deferred_work.write_text(text, encoding="utf-8")
    if commit:
        git(paths.project, "add", "-A")
        git(paths.project, "commit", "-q", "-m", "legacy ledger")


def migrate_effect(paths: ProjectPaths, new_ledger_text: str, mapping):
    """Simulate a /bmad-auto-sweep --migrate session: rewrites the ledger to
    canonical DW format and reports the manifest-key -> dw_id mapping."""

    def effect(spec: SessionSpec) -> SessionResult:
        paths.deferred_work.write_text(new_ledger_text, encoding="utf-8")
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "deferred-sweep-migrate",
                "mapping": list(mapping),
                "escalations": [],
            },
        )

    return effect


def bundle_spec_path(paths: ProjectPaths, name: str) -> Path:
    return paths.implementation_artifacts / f"spec-dw-{name}.md"


def triage_effect(result_json: dict):
    """Simulate a deferred-sweep triage session returning the given result."""

    def effect(spec: SessionSpec) -> SessionResult:
        return SessionResult(status="completed", result_json=result_json)

    return effect


def bundle_dev_effect(paths: ProjectPaths, name: str, dw_ids, mark_ledger: bool = True):
    """Simulate a quick-dev bundle session (--dw-bundle): edits code, writes
    the bundle spec, and (like step-auto-finalize bundle mode) marks the
    bundle's ledger entries done."""

    def effect(spec: SessionSpec) -> SessionResult:
        baseline = rev_parse_head(paths.project)
        source = paths.project / "src.txt"
        source.write_text(source.read_text() + f"change for dw-{name}\n")
        sp = bundle_spec_path(paths, name)
        write_spec(sp, "in-review", baseline)
        if mark_ledger:
            mark_ledger_done(paths, dw_ids)
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "quick-dev",
                "story_key": f"dw-{name}",
                "spec_file": str(sp),
                "baseline_commit": baseline,
                "tasks_total": 1,
                "tasks_done": 1,
                "verification": [],
                "escalations": [],
                "dw_ids": list(dw_ids),
            },
        )

    return effect


def bundle_review_effect(paths: ProjectPaths, name: str, clean: bool = True):
    """Simulate a code-review session over a bundle spec (no sprint sync)."""

    def effect(spec: SessionSpec) -> SessionResult:
        if clean:
            sp = bundle_spec_path(paths, name)
            write_spec(sp, "done", _spec_baseline(sp))
        return SessionResult(
            status="completed",
            result_json={
                "workflow": "code-review",
                "clean": clean,
                "patched": 0,
                "deferred": 0,
                "dismissed": 0,
                "escalations": [],
            },
        )

    return effect
