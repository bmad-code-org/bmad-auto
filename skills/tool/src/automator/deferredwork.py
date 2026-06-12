"""Deterministic reading and editing of the deferred-work ledger.

The ledger (`{implementation_artifacts}/deferred-work.md`) is append-only
markdown written by skills per bmad-auto-dev/deferred-work-format.md:
`### DW-<seq>: <title>` headings with `origin:`/`location:`/`reason:`/`status:`
field lines. The orchestrator never trusts an LLM to have edited it — status
flips and decision records happen here, and gates re-read the file from disk.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

HEADING_RE = re.compile(r"^### (DW-\d+): (.+?)\s*$", re.MULTILINE)
ANY_HEADING_RE = re.compile(r"^#{1,6} ", re.MULTILINE)
STATUS_RE = re.compile(r"^status:[ \t]*(.*)$", re.MULTILINE)


@dataclass(frozen=True)
class DWEntry:
    id: str
    title: str
    status: str  # the status field value, "" when the line is missing
    body: str  # full entry text including the heading
    span: tuple[int, int]  # char offsets of the entry in the ledger text

    @property
    def open(self) -> bool:
        return self.status.split()[0] == "open" if self.status else False


def parse_ledger(text: str) -> list[DWEntry]:
    """Extract DW entries; non-conforming sections are skipped, an entry
    without a status line parses with status "" (not open)."""
    entries = []
    headings = list(HEADING_RE.finditer(text))
    for i, m in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        # an entry also ends at any intervening heading (e.g. a "## Deferred
        # from:" section header between freeform and DW-format content)
        other = ANY_HEADING_RE.search(text, m.end(), end)
        if other:
            end = other.start()
        body = text[m.start() : end]
        status_m = STATUS_RE.search(body)
        entries.append(
            DWEntry(
                id=m.group(1),
                title=m.group(2),
                status=status_m.group(1).strip() if status_m else "",
                body=body,
                span=(m.start(), end),
            )
        )
    return entries


def open_ids(text: str) -> set[str]:
    return {e.id for e in parse_ledger(text) if e.open}


def _find_entry(text: str, dw_id: str) -> DWEntry | None:
    for entry in parse_ledger(text):
        if entry.id == dw_id:
            return entry
    return None


def _insert_after_status(text: str, entry: DWEntry, line: str) -> str:
    """Insert a field line right after the entry's status line (or at the end
    of the entry when no status line exists)."""
    status_m = STATUS_RE.search(entry.body)
    if status_m:
        pos = entry.span[0] + status_m.end()
        return text[:pos] + "\n" + line + text[pos:]
    insert_at = entry.span[0] + len(entry.body.rstrip())
    return text[:insert_at] + "\n" + line + text[insert_at:]


def mark_done(path: Path, dw_id: str, date: str, note: str) -> bool:
    """Flip one entry to `status: done <date>` and record a resolution note.
    Returns False (no write) when the entry is missing or already done."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None or not entry.open:
        return False
    status_m = STATUS_RE.search(entry.body)
    assert status_m is not None  # open implies a status line
    start = entry.span[0] + status_m.start()
    end = entry.span[0] + status_m.end()
    text = text[:start] + f"status: done {date}" + text[end:]
    entry = _find_entry(text, dw_id)
    assert entry is not None
    text = _insert_after_status(text, entry, f"resolution: {note}")
    path.write_text(text, encoding="utf-8")
    return True


def append_decision(path: Path, dw_id: str, date: str, label: str, detail: str) -> bool:
    """Record a human decision on an entry without changing its status."""
    if not path.is_file():
        return False
    text = path.read_text(encoding="utf-8")
    entry = _find_entry(text, dw_id)
    if entry is None:
        return False
    detail_part = f" — {detail}" if detail else ""
    text = _insert_after_status(text, entry, f"decision: {date} {label}{detail_part}")
    path.write_text(text, encoding="utf-8")
    return True
