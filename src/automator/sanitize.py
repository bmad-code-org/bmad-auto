"""PII-scrubbing chokepoint for `bmad-auto probe-adapter`.

Pure stdlib, no automator imports — the single audited place that decides what
data from a foreign CLI is safe to show a maintainer. The probe command routes
every captured payload, every help/version blob, and every discovered path
through here before rendering; nothing is displayed raw.

Guarantees:
- token *counts* are non-PII, so numbers/bools/null pass through verbatim;
- dict **keys** are kept verbatim — field names/casing are the whole point of a
  payload probe — but every leaf **string** is `$HOME`-redacted and then kept
  ONLY if it matches a conservative identifier shape (a short slug with no
  spaces / `@` / `/`, e.g. ``claude-opus-4-8`` or ``session-abc_123``);
  anything else (prose, code, paths, emails) becomes ``<redacted:str>``;
- list lengths are preserved (the count is structural, the contents aren't);
- recursion is depth-guarded so a pathological payload can't blow the stack.
"""

from __future__ import annotations

import os
import re
from typing import Any

# A conservative "this is a machine identifier, not prose or PII" shape: starts
# alphanumeric, then only word-ish chars (letters, digits, ``.`` ``_`` ``-``),
# bounded length. No spaces, no ``@``, no ``/`` — so emails, paths, and sentences
# can never satisfy it. Model ids and session/conversation ids do.
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_IDENTIFIER_MAX = 80

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

_REDACTED_STR = "<redacted:str>"
_REDACTED_EMAIL = "<redacted:email>"
_REDACTED_DEPTH = "<redacted:depth>"


def _home() -> str:
    home = os.path.expanduser("~")
    return home if home and home != "~" else ""


def redact_home(s: str) -> str:
    """Replace the current user's home directory prefix with ``~``.

    Catches the literal expanded home (``/home/alice`` -> ``~``); the munged,
    slash-stripped forms some CLIs use for directory names (``-home-alice-...``)
    do not match a path and are handled by the identifier filter instead.
    """
    home = _home()
    if home and home != "/" and home in s:
        s = s.replace(home, "~")
    return s


def looks_like_identifier(s: str) -> bool:
    """True for a short machine slug safe to surface verbatim (no PII)."""
    return 0 < len(s) <= _IDENTIFIER_MAX and bool(_IDENTIFIER_RE.match(s))


def scrub_text(s: str, *, max_lines: int | None = None) -> str:
    """Sanitize free text (a CLI's ``--help`` / ``--version`` / a log tail).

    Less aggressive than :func:`scrub_json` — help text is the CLI's own and
    flag lines must survive — so we only redact the home dir and any emails,
    then optionally cap the line count.
    """
    s = redact_home(s)
    s = _EMAIL_RE.sub(_REDACTED_EMAIL, s)
    if max_lines is not None:
        lines = s.splitlines()
        if len(lines) > max_lines:
            dropped = len(lines) - max_lines
            lines = lines[:max_lines] + [f"… ({dropped} more lines redacted)"]
        s = "\n".join(lines)
    return s


def _scrub(obj: Any, depth: int, max_depth: int) -> Any:
    if depth > max_depth:
        return _REDACTED_DEPTH
    # bool is an int subclass — handled by the numeric branch; both pass through.
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    if isinstance(obj, str):
        red = redact_home(obj)
        return red if looks_like_identifier(red) else _REDACTED_STR
    if isinstance(obj, dict):
        return {str(k): _scrub(v, depth + 1, max_depth) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_scrub(v, depth + 1, max_depth) for v in obj]
    # any other type (shouldn't appear in JSON) is treated as an opaque string
    return _REDACTED_STR


def scrub_json(obj: Any, *, max_depth: int = 40) -> Any:
    """Recursively sanitize a JSON-shaped value (see module docstring)."""
    return _scrub(obj, 0, max_depth)


def scrub_event_payload(payload: Any) -> Any:
    """Sanitize one captured hook payload — the probe's per-event chokepoint."""
    return scrub_json(payload)
