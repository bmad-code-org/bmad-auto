"""Read-only model of sprint-status.yaml — the single source of workflow truth.

The orchestrator NEVER writes this file. Only the BMAD skills mutate it
(via sync-sprint-status); the orchestrator re-reads it to pick the next
story and to verify what a session claims to have done.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

EPIC_RE = re.compile(r"^epic-(\d+)$")
RETRO_RE = re.compile(r"^epic-(\d+)-retrospective$")
STORY_RE = re.compile(r"^(\d+)-(\d+)-(.+)$")

STORY_STATUSES = {"backlog", "ready-for-dev", "in-progress", "review", "done"}
LEGACY_STORY_STATUSES = {"drafted": "ready-for-dev"}
ACTIONABLE_STATUSES = {"backlog", "ready-for-dev"}


class SprintStatusError(Exception):
    pass


@dataclass(frozen=True)
class Story:
    key: str
    epic: int
    num: int
    slug: str
    status: str


@dataclass(frozen=True)
class SprintStatus:
    path: Path
    epics: dict[int, str]
    stories: tuple[Story, ...]
    retros: dict[int, str]
    unknown_keys: tuple[str, ...]


def load(path: Path) -> SprintStatus:
    if not path.is_file():
        raise SprintStatusError(f"sprint status file not found: {path}")
    try:
        doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as e:
        raise SprintStatusError(f"sprint status is not valid YAML: {path}: {e}") from e
    if not isinstance(doc, dict):
        raise SprintStatusError(f"sprint status has no top-level mapping: {path}")
    dev = doc.get("development_status")
    if not isinstance(dev, dict):
        raise SprintStatusError(f"sprint status missing development_status map: {path}")

    epics: dict[int, str] = {}
    stories: list[Story] = []
    retros: dict[int, str] = {}
    unknown: list[str] = []
    for key, raw_status in dev.items():
        key = str(key)
        status = str(raw_status).strip()
        if m := RETRO_RE.match(key):
            retros[int(m.group(1))] = status
        elif m := EPIC_RE.match(key):
            epics[int(m.group(1))] = status
        elif m := STORY_RE.match(key):
            status = LEGACY_STORY_STATUSES.get(status, status)
            stories.append(
                Story(
                    key=key,
                    epic=int(m.group(1)),
                    num=int(m.group(2)),
                    slug=m.group(3),
                    status=status,
                )
            )
        else:
            unknown.append(key)

    return SprintStatus(
        path=path,
        epics=epics,
        stories=tuple(stories),
        retros=retros,
        unknown_keys=tuple(unknown),
    )


def next_actionable(ss: SprintStatus, skip: set[str] | None = None) -> Story | None:
    """First story in file order whose status allows starting work."""
    skip = skip or set()
    for story in ss.stories:
        if story.key in skip:
            continue
        if story.status in ACTIONABLE_STATUSES:
            return story
    return None


def story_status(path: Path, key: str) -> str | None:
    """Fresh re-read of one story's status, for post-session verification."""
    ss = load(path)
    for story in ss.stories:
        if story.key == key:
            return story.status
    return None
