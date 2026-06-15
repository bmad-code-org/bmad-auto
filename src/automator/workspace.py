"""Where code+git work happens, decoupled from where run state lives.

A Workspace pairs the directory sessions run in (and git operates on) with the
artifact paths rebased onto it. Run state (run_dir, journal, state.json) always
lives in the main repo and is passed separately — it never moves.

- isolation = none → Workspace.default(paths): root = paths.repo_root, behavior
  identical to operating directly on the project.
- isolation = worktree (Phase 3) → per unit: root = the worktree dir, paths =
  paths.rebased(worktree_dir).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .bmadconfig import ProjectPaths


@dataclass(frozen=True)
class Workspace:
    root: Path  # where sessions run (cwd) and git operates
    paths: ProjectPaths  # artifact paths rebased onto `root`

    @classmethod
    def default(cls, paths: ProjectPaths) -> Workspace:
        """The zero-config workspace: work happens in the repo root in place."""
        return cls(root=paths.repo_root, paths=paths)
