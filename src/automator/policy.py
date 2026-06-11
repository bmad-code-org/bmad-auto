"""Policy-as-data: .automator/policy.toml -> immutable Policy dataclasses."""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

GATE_MODES = {"none", "per-epic", "per-story-spec-approval"}
RETRO_MODES = {"never", "notify", "auto"}


class PolicyError(Exception):
    pass


@dataclass(frozen=True)
class GatesPolicy:
    mode: str = "per-epic"
    on_escalation: str = "pause"  # CRITICAL escalations always pause; field reserved
    retrospective: str = "notify"


@dataclass(frozen=True)
class LimitsPolicy:
    max_review_cycles: int = 3
    max_dev_attempts: int = 2
    session_timeout_min: int = 45
    stop_without_result_nudges: int = 1
    max_tokens_per_story: int = 2_000_000


@dataclass(frozen=True)
class VerifyPolicy:
    commands: tuple[str, ...] = ()


@dataclass(frozen=True)
class NotifyPolicy:
    desktop: bool = True
    file: bool = True


@dataclass(frozen=True)
class AdapterPolicy:
    name: str = "claude"  # CLI profile name; "claude-code-tmux" kept as legacy alias
    model_dev: str = ""
    model_review: str = ""
    # None = use the profile's default bypass flags; a list replaces them
    extra_args: tuple[str, ...] | None = None


@dataclass(frozen=True)
class Policy:
    gates: GatesPolicy = field(default_factory=GatesPolicy)
    limits: LimitsPolicy = field(default_factory=LimitsPolicy)
    verify: VerifyPolicy = field(default_factory=VerifyPolicy)
    notify: NotifyPolicy = field(default_factory=NotifyPolicy)
    adapter: AdapterPolicy = field(default_factory=AdapterPolicy)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def _section(doc: dict[str, Any], name: str) -> dict[str, Any]:
    value = doc.get(name, {})
    if not isinstance(value, dict):
        raise PolicyError(f"[{name}] must be a table")
    return value


def load(path: Path | None) -> Policy:
    """Load policy from TOML; a missing file yields all defaults."""
    doc: dict[str, Any] = {}
    if path is not None and path.is_file():
        try:
            doc = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as e:
            raise PolicyError(f"invalid policy TOML: {path}: {e}") from e

    gates_d = _section(doc, "gates")
    limits_d = _section(doc, "limits")
    verify_d = _section(doc, "verify")
    notify_d = _section(doc, "notify")
    adapter_d = _section(doc, "adapter")

    gates = GatesPolicy(
        mode=str(gates_d.get("mode", GatesPolicy.mode)),
        on_escalation=str(gates_d.get("on_escalation", GatesPolicy.on_escalation)),
        retrospective=str(gates_d.get("retrospective", GatesPolicy.retrospective)),
    )
    if gates.mode not in GATE_MODES:
        raise PolicyError(f"gates.mode must be one of {sorted(GATE_MODES)}: got {gates.mode!r}")
    if gates.retrospective not in RETRO_MODES:
        raise PolicyError(
            f"gates.retrospective must be one of {sorted(RETRO_MODES)}: got {gates.retrospective!r}"
        )

    limits = LimitsPolicy(
        max_review_cycles=int(limits_d.get("max_review_cycles", LimitsPolicy.max_review_cycles)),
        max_dev_attempts=int(limits_d.get("max_dev_attempts", LimitsPolicy.max_dev_attempts)),
        session_timeout_min=int(
            limits_d.get("session_timeout_min", LimitsPolicy.session_timeout_min)
        ),
        stop_without_result_nudges=int(
            limits_d.get("stop_without_result_nudges", LimitsPolicy.stop_without_result_nudges)
        ),
        max_tokens_per_story=int(
            limits_d.get("max_tokens_per_story", LimitsPolicy.max_tokens_per_story)
        ),
    )
    if limits.max_review_cycles < 1 or limits.max_dev_attempts < 1:
        raise PolicyError("limits.max_review_cycles and limits.max_dev_attempts must be >= 1")

    verify = VerifyPolicy(commands=tuple(str(c) for c in verify_d.get("commands", ())))
    notify = NotifyPolicy(
        desktop=bool(notify_d.get("desktop", NotifyPolicy.desktop)),
        file=bool(notify_d.get("file", NotifyPolicy.file)),
    )
    raw_extra = adapter_d.get("extra_args")
    adapter = AdapterPolicy(
        name=str(adapter_d.get("name", AdapterPolicy.name)),
        model_dev=str(adapter_d.get("model_dev", AdapterPolicy.model_dev)),
        model_review=str(adapter_d.get("model_review", AdapterPolicy.model_review)),
        extra_args=None if raw_extra is None else tuple(str(a) for a in raw_extra),
    )
    return Policy(gates=gates, limits=limits, verify=verify, notify=notify, adapter=adapter)


POLICY_TEMPLATE = """\
# bmad-auto orchestration policy. All keys optional; defaults shown.

[gates]
mode = "per-epic"            # none | per-epic | per-story-spec-approval
retrospective = "notify"     # never | notify | auto (auto unsupported in v1)

[limits]
max_review_cycles = 3
max_dev_attempts = 2
session_timeout_min = 45
stop_without_result_nudges = 1
max_tokens_per_story = 2000000

[verify]
# Deterministic gates run by the orchestrator after a clean review, before commit.
commands = []                # e.g. ["pytest -q", "ruff check ."]

[notify]
desktop = true               # notify-send, best-effort
file = true                  # ATTENTION file in the run dir

[adapter]
name = "claude"              # claude | codex | gemini | <custom .automator/profiles/*.toml>
model_dev = ""               # empty = CLI default model
model_review = ""
# extra_args replaces the profile's default permission-bypass flags when set:
# extra_args = ["--permission-mode", "bypassPermissions"]
"""
