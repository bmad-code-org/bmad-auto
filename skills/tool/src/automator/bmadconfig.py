"""Resolve BMAD artifact paths from _bmad/bmm/config.yaml."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml


class BmadConfigError(Exception):
    pass


@dataclass(frozen=True)
class ProjectPaths:
    project: Path
    implementation_artifacts: Path
    planning_artifacts: Path

    @property
    def sprint_status(self) -> Path:
        return self.implementation_artifacts / "sprint-status.yaml"

    @property
    def deferred_work(self) -> Path:
        return self.implementation_artifacts / "deferred-work.md"


def _resolve(raw: str, project: Path) -> Path:
    return Path(raw.replace("{project-root}", str(project))).resolve()


def load_paths(project: Path) -> ProjectPaths:
    project = project.resolve()
    config_path = project / "_bmad" / "bmm" / "config.yaml"
    if not config_path.is_file():
        raise BmadConfigError(f"BMAD config not found: {config_path} (is BMAD installed here?)")
    try:
        doc = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as e:
        raise BmadConfigError(f"invalid YAML in {config_path}: {e}") from e

    impl = doc.get("implementation_artifacts")
    plan = doc.get("planning_artifacts")
    if not impl or not plan:
        raise BmadConfigError(
            f"{config_path} missing implementation_artifacts/planning_artifacts keys"
        )
    return ProjectPaths(
        project=project,
        implementation_artifacts=_resolve(str(impl), project),
        planning_artifacts=_resolve(str(plan), project),
    )
