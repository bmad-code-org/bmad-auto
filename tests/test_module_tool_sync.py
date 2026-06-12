"""Drift guard: skills/tool/ is a vendored copy of the orchestrator package.

The bauto BMAD module ships the bmad-auto tool inside it (skills/tool/) so the
plugin is self-contained — `bmad-auto-setup` pip-installs it at setup time. The
canonical source stays at the repo root (src/automator, pyproject.toml,
README.md) where development + `pip install -e .` happen; skills/tool/ must
mirror it byte-for-byte. This test turns drift into a CI failure.

To refresh the vendored copy after editing the tool, run:
    python scripts/sync-tool.py
(or `python scripts/sync-tool.py --check` to verify without writing).
"""

import filecmp
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
CANONICAL_PKG = REPO / "src" / "automator"
VENDORED_PKG = REPO / "skills" / "tool" / "src" / "automator"
MIRRORED_FILES = ["pyproject.toml", "README.md"]


def _assert_identical(canonical: Path, vendored: Path) -> None:
    cmp = filecmp.dircmp(canonical, vendored)
    stack = [cmp]
    problems: list[str] = []
    while stack:
        node = stack.pop()
        rel = Path(node.left).relative_to(canonical)
        for name in node.left_only:
            problems.append(f"missing from vendored copy: {rel / name}")
        for name in node.right_only:
            problems.append(f"extra in vendored copy: {rel / name}")
        _, mismatch, errors = filecmp.cmpfiles(
            node.left, node.right, node.common_files, shallow=False
        )
        for name in mismatch + errors:
            problems.append(f"content differs: {rel / name}")
        stack.extend(node.subdirs.values())
    assert not problems, (
        f"{vendored} has drifted from canonical {canonical}; "
        f"re-vendor from src/ to fix (see this test's docstring):\n  " + "\n  ".join(problems)
    )


def test_vendored_package_matches_source() -> None:
    assert CANONICAL_PKG.is_dir(), f"canonical package missing: {CANONICAL_PKG}"
    assert VENDORED_PKG.is_dir(), f"vendored package missing: {VENDORED_PKG}"
    _assert_identical(CANONICAL_PKG, VENDORED_PKG)


@pytest.mark.parametrize("name", MIRRORED_FILES)
def test_vendored_metadata_file_matches_source(name: str) -> None:
    canonical = REPO / name
    vendored = REPO / "skills" / "tool" / name
    assert canonical.is_file(), f"canonical file missing: {canonical}"
    assert vendored.is_file(), f"vendored file missing: {vendored}"
    assert filecmp.cmp(
        canonical, vendored, shallow=False
    ), f"{vendored} has drifted from {canonical}; re-copy it to fix."
