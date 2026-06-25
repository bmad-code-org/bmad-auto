"""Generic coding-CLI driver: interactive sessions in tmux windows, observed via hooks.

Each pipeline step gets a fresh tmux window running the full interactive CLI
with the skill invocation as the initial prompt. Completion is detected
exclusively through hook-written event files (Stop/SessionEnd) plus the
presence of the skill-written result.json — the pane is piped to a log file
for human debugging but NEVER parsed for control flow.

Everything CLI-specific (binary, prompt rendering, bypass flags, usage
parser) comes from a declarative CLIProfile; each CLI's hook config registers
the shared relay script under its native event names but passes the canonical
event name as argv, so this adapter only ever sees canonical events. CLIs
without a SessionEnd hook (e.g. Codex) are covered by the window-death
fallback.
"""

from __future__ import annotations

import json
import shlex
import time
from pathlib import Path

from .. import devcontract, runs
from ..journal import LOGS_DIR
from ..model import TokenUsage
from ..policy import Policy
from ..signals import SignalWatcher
from ..tokens import read_usage as tally_usage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec
from .multiplexer import TerminalMultiplexer, get_multiplexer
from .profile import CLIProfile

# Pane geometry for agent windows; mirrored in tui.data for log emulation.
PANE_COLUMNS = 220
PANE_LINES = 50
RESULT_GRACE_S = 15.0
RESULT_POLL_S = 0.5
EVENT_KINDS = {"SessionStart", "Stop", "SessionEnd"}
NUDGE_TEXT = (
    "You are running in bmad-auto automation mode. Finish the workflow now: "
    "complete any remaining steps and write the result JSON file to "
    "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json, then end your turn."
)


class GenericAdapter(CodingCLIAdapter):
    injection = "tmux-initial-prompt"
    observation = "hook-signal"
    state = "local-jsonl"

    def __init__(
        self,
        run_dir: Path,
        policy: Policy,
        profile: CLIProfile,
        binary: str | None = None,
        extra_args: tuple[str, ...] | None = None,
        usage_grace_s: float | None = None,
        stop_without_result_nudges: int | None = None,
        mux: TerminalMultiplexer | None = None,
    ):
        self.run_dir = run_dir
        self.policy = policy
        self.profile = profile
        self.mux = mux or get_multiplexer()
        # None = use the profile's default bypass flags; a tuple replaces them
        self.extra_args = extra_args
        # Effective timing knobs: an explicit [adapter]/[adapter.<stage>] override
        # wins, else the CLI profile's shipped default, else the global fallback.
        self._usage_grace_s = usage_grace_s if usage_grace_s is not None else profile.usage_grace_s
        self._stop_nudges = (
            stop_without_result_nudges
            if stop_without_result_nudges is not None
            else (
                profile.stop_without_result_nudges
                if profile.stop_without_result_nudges is not None
                else policy.limits.stop_without_result_nudges
            )
        )
        self.name = f"{profile.name}-tmux"
        self.binary = binary or profile.binary
        self.session_name = f"bmad-auto-{run_dir.name}"
        self.watcher = SignalWatcher(run_dir / "events")
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / LOGS_DIR
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # --------------------------------------------------------- multiplexer

    def _ensure_session(self, cwd: Path) -> None:
        if not self.mux.has_session(self.session_name):
            self.mux.new_session(self.session_name, cwd, PANE_COLUMNS, PANE_LINES)
            # Tag the session with its project so a cleanup in another project
            # never prunes this run (run_dir = <project>/.automator/runs/<id>).
            project = self.run_dir.parents[2]
            self.mux.set_session_option(
                self.session_name, runs.PROJECT_OPTION, runs.project_tag(project)
            )

    def interactive_argv(self, spec: SessionSpec) -> list[str]:
        extra = self.extra_args
        if extra is None:
            extra = self.profile.bypass_args
        argv = [
            self.binary,
            *self.profile.launch_args,
            self.profile.render_prompt(spec.prompt),
            *extra,
        ]
        if spec.model:
            argv += [self.profile.model_flag, spec.model]
        return argv

    def interactive_env(self, spec: SessionSpec) -> dict[str, str]:
        return {**self.profile.env, **spec.env}

    def build_command(self, spec: SessionSpec) -> str:
        return " ".join(shlex.quote(a) for a in self.interactive_argv(spec))

    # --------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = self.tasks_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "prompt.txt").write_text(spec.prompt + "\n", encoding="utf-8")
        # A re-armed/resumed run reuses task_ids; drop any prior cycle's result
        # so a session that writes nothing can't be read as a stale completion.
        (task_dir / "result.json").unlink(missing_ok=True)

        self._ensure_session(spec.cwd)
        # Stamped before launch: hook events carry wall-clock ns, and
        # wait_for_completion ignores anything older than this floor so a reused
        # task_id's earlier Stop event cannot replay.
        launched_ns = time.time_ns()
        window_id = self.mux.new_window(
            self.session_name,
            spec.task_id[-40:],
            spec.cwd,
            {**self.profile.env, **spec.env},
            self.build_command(spec),
        )
        log_file = self.logs_dir / f"{spec.task_id}.log"
        # pipe_pane tolerates the window having already died (a CLI that crashes on
        # launch can take it down before the tee attaches); the dead window is then
        # reported as a crash in wait_for_completion.
        self.mux.pipe_pane(window_id, log_file)
        return SessionHandle(task_id=spec.task_id, native_id=window_id, launched_ns=launched_ns)

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        deadline = time.monotonic() + spec.timeout_s
        session_id: str | None = None
        transcript_path: str | None = None
        nudges_left = self._stop_nudges

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SessionResult(
                    status="timeout",
                    session_id=session_id,
                    transcript_path=transcript_path,
                )
            event = self.watcher.wait_for(
                handle.task_id,
                EVENT_KINDS,
                timeout_s=min(remaining, 5.0),
                since_ns=handle.launched_ns,
            )
            if event is None:
                if not self._window_alive(handle):
                    # died without a SessionEnd hook (killed, crashed hard)
                    return self._final(handle, spec, "crashed", session_id, transcript_path)
                continue
            if (
                event.event == "Stop"
                and self.profile.subagent_stop_without_transcript
                and not event.transcript_path
            ):
                # Copilot fires agentStop for each subagent turn with an empty
                # transcriptPath and a tool-use session id; that is not the main
                # session's turn-end. Ignore it (before accumulating the junk
                # session id) so a subagent's premature Stop is not read as a
                # result-less completion -> false stall, and the main session's
                # real transcript is preserved for usage tallying.
                continue
            session_id = event.session_id or session_id
            transcript_path = event.transcript_path or transcript_path

            if event.event == "SessionStart":
                continue
            if event.event == "Stop":
                result_json = self._result_json(handle, spec, wait=True)
                if result_json is not None:
                    return SessionResult(
                        status="completed",
                        result_json=result_json,
                        session_id=session_id,
                        transcript_path=transcript_path,
                    )
                if nudges_left > 0:
                    nudges_left -= 1
                    self.send_text(handle, NUDGE_TEXT)
                    continue
                return self._final(handle, spec, "stalled", session_id, transcript_path)
            if event.event == "SessionEnd":
                return self._final(handle, spec, "crashed", session_id, transcript_path)

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        """Acquire this session's result dict. Base behavior: read the
        skill-written ``result.json`` (briefly awaiting it on the Stop event,
        reading once otherwise). Subclasses whose skill writes no result.json
        (GenericDevAdapter) override this to synthesize the dict from another
        on-disk artifact."""
        return self._await_result(handle.task_id) if wait else self._read_result(handle.task_id)

    def _final(
        self,
        handle: SessionHandle,
        spec: SessionSpec,
        fallback: str,
        session_id: str | None,
        transcript: str | None,
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status."""
        result_json = self._result_json(handle, spec, wait=False)
        status = "completed" if result_json is not None else fallback
        return SessionResult(
            status=status,
            result_json=result_json,
            session_id=session_id,
            transcript_path=transcript,
        )

    def _result_path(self, task_id: str) -> Path:
        return self.tasks_dir / task_id / "result.json"

    def _read_result(self, task_id: str) -> dict | None:
        path = self._result_path(task_id)
        if not path.is_file():
            return None
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return None
        return data if isinstance(data, dict) else None

    def _await_result(self, task_id: str, grace_s: float = RESULT_GRACE_S) -> dict | None:
        deadline = time.monotonic() + grace_s
        while True:
            result = self._read_result(task_id)
            if result is not None or time.monotonic() >= deadline:
                return result
            time.sleep(RESULT_POLL_S)

    def _window_alive(self, handle: SessionHandle) -> bool:
        return handle.native_id in self.mux.list_window_ids(self.session_name)

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self.mux.send_text(handle.native_id, text)

    def kill(self, handle: SessionHandle) -> None:
        self.mux.kill_window(handle.native_id)

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        if not result.transcript_path:
            return None
        path = Path(result.transcript_path)
        # Some CLIs flush their token totals only on shutdown (Copilot writes
        # modelMetrics in the trailing session.shutdown line, ~1s after the
        # turn-end hook). Poll up to the effective grace so we don't sample the
        # transcript before the totals land. grace 0 = read once (today's path).
        deadline = time.monotonic() + self._usage_grace_s
        while True:
            usage = tally_usage(self.profile.usage_parser, path)
            if usage is not None or time.monotonic() >= deadline:
                return usage
            time.sleep(RESULT_POLL_S)


class GenericDevAdapter(GenericAdapter):
    """Dev adapter for Alex Verhovsky's generic ``bmad-dev-auto`` skill.

    That skill writes NO ``result.json`` — its outcome lives in the spec it
    leaves on disk (frontmatter ``status:`` plus an appended ``## Auto Run
    Result``, or, when it never created a spec, a ``bmad-dev-auto-result-*.md``
    fallback). On the Stop event we locate that artifact and synthesize the
    legacy result dict from it via :mod:`devcontract`, so verify/escalation and
    the rest of the pipeline consume it unchanged. Selected by
    ``policy.dev.skill == "bmad-dev-auto"`` (see ``cli._make_adapters``).
    """

    def __init__(self, *args, impl_artifacts: Path, **kwargs):
        super().__init__(*args, **kwargs)
        self.impl_artifacts = impl_artifacts
        # The generic skill never writes result.json, so the base "write the
        # result JSON file" nudge is meaningless — and actively misleading — for
        # it. A Stop without a terminal spec is a genuine stall.
        self._stop_nudges = 0

    def _result_json(self, handle: SessionHandle, spec: SessionSpec, *, wait: bool) -> dict | None:
        # Mirror the base _await_result poll: the skill's terminal spec may not be
        # flushed to disk the instant the Stop event fires, so briefly await it when
        # wait=True instead of reading once and mis-reporting a stall.
        deadline = time.monotonic() + RESULT_GRACE_S
        while True:
            spec_path = devcontract.find_result_artifact(
                self.impl_artifacts, since_ns=handle.launched_ns
            )
            if spec_path is not None:
                story_key = spec.env.get("BMAD_AUTO_STORY_KEY") or None
                return devcontract.synthesize_result(spec_path, story_key=story_key).result_json
            if not wait or time.monotonic() >= deadline:
                return None
            time.sleep(RESULT_POLL_S)


# Back-compat alias: the adapter was ``GenericTmuxAdapter`` before tmux moved
# behind the multiplexer seam. Keeps existing imports stable.
GenericTmuxAdapter = GenericAdapter
