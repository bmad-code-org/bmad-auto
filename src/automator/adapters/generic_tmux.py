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
import subprocess
import time
from pathlib import Path

from ..model import TokenUsage
from ..policy import Policy
from ..signals import SignalWatcher
from ..tokens import read_usage as tally_usage
from .base import CodingCLIAdapter, SessionHandle, SessionResult, SessionSpec
from .profile import CLIProfile

TMUX_TIMEOUT_S = 30
RESULT_GRACE_S = 15.0
RESULT_POLL_S = 0.5
EVENT_KINDS = {"SessionStart", "Stop", "SessionEnd"}
NUDGE_TEXT = (
    "You are running in bmad-auto automation mode. Finish the workflow now: "
    "complete any remaining steps and write the result JSON file to "
    "$BMAD_AUTO_RUN_DIR/tasks/$BMAD_AUTO_TASK_ID/result.json, then end your turn."
)


class TmuxError(Exception):
    pass


class GenericTmuxAdapter(CodingCLIAdapter):
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
    ):
        self.run_dir = run_dir
        self.policy = policy
        self.profile = profile
        # None = use the profile's default bypass flags; a tuple replaces them
        self.extra_args = extra_args
        self.name = f"{profile.name}-tmux"
        self.binary = binary or profile.binary
        self.session_name = f"bmad-auto-{run_dir.name}"
        self.watcher = SignalWatcher(run_dir / "events")
        self.tasks_dir = run_dir / "tasks"
        self.logs_dir = run_dir / "logs"
        self.tasks_dir.mkdir(parents=True, exist_ok=True)
        self.logs_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ tmux

    def _tmux(self, *args: str) -> str:
        proc = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S
        )
        if proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    def _ensure_session(self, cwd: Path) -> None:
        probe = subprocess.run(
            ["tmux", "has-session", "-t", f"={self.session_name}"],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if probe.returncode != 0:
            # Window 0 is a plain shell so the session survives task windows closing.
            self._tmux(
                "new-session", "-d", "-s", self.session_name, "-c", str(cwd), "-x", "220", "-y", "50"
            )

    def build_command(self, spec: SessionSpec) -> str:
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
        return " ".join(shlex.quote(a) for a in argv)

    # --------------------------------------------------------------- adapter

    def start_session(self, spec: SessionSpec) -> SessionHandle:
        task_dir = self.tasks_dir / spec.task_id
        task_dir.mkdir(parents=True, exist_ok=True)
        (task_dir / "prompt.txt").write_text(spec.prompt + "\n", encoding="utf-8")

        self._ensure_session(spec.cwd)
        env_args: list[str] = []
        for key, value in {**self.profile.env, **spec.env}.items():
            env_args += ["-e", f"{key}={value}"]
        window_id = self._tmux(
            "new-window",
            "-t",
            f"={self.session_name}:",
            "-n",
            spec.task_id[-40:],
            "-c",
            str(spec.cwd),
            "-P",
            "-F",
            "#{window_id}",
            *env_args,
            self.build_command(spec),
        )
        log_file = self.logs_dir / f"{spec.task_id}.log"
        self._tmux("pipe-pane", "-t", window_id, "-o", f"cat >> {shlex.quote(str(log_file))}")
        return SessionHandle(task_id=spec.task_id, native_id=window_id)

    def wait_for_completion(self, handle: SessionHandle, spec: SessionSpec) -> SessionResult:
        deadline = time.monotonic() + spec.timeout_s
        session_id: str | None = None
        transcript_path: str | None = None
        nudges_left = self.policy.limits.stop_without_result_nudges

        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return SessionResult(
                    status="timeout", session_id=session_id, transcript_path=transcript_path
                )
            event = self.watcher.wait_for(
                handle.task_id, EVENT_KINDS, timeout_s=min(remaining, 5.0)
            )
            if event is None:
                if not self._window_alive(handle):
                    # died without a SessionEnd hook (killed, crashed hard)
                    return self._final(handle, "crashed", session_id, transcript_path)
                continue
            session_id = event.session_id or session_id
            transcript_path = event.transcript_path or transcript_path

            if event.event == "SessionStart":
                continue
            if event.event == "Stop":
                result_json = self._await_result(handle.task_id)
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
                return self._final(handle, "stalled", session_id, transcript_path)
            if event.event == "SessionEnd":
                return self._final(handle, "crashed", session_id, transcript_path)

    def _final(
        self, handle: SessionHandle, fallback: str, session_id: str | None, transcript: str | None
    ) -> SessionResult:
        """Session is gone or done responding: completed if the result file
        landed anyway, otherwise the fallback status."""
        result_json = self._read_result(handle.task_id)
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
        # display-message -t <dead-window> exits 0 with empty output, so list
        # the session's window ids and check membership instead.
        probe = subprocess.run(
            ["tmux", "list-windows", "-t", f"={self.session_name}", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if probe.returncode != 0:
            return False
        return handle.native_id in probe.stdout.split()

    def send_text(self, handle: SessionHandle, text: str) -> None:
        self._tmux("send-keys", "-t", handle.native_id, "-l", text)
        time.sleep(0.3)  # let the TUI ingest the paste before submitting
        self._tmux("send-keys", "-t", handle.native_id, "Enter")

    def kill(self, handle: SessionHandle) -> None:
        subprocess.run(
            ["tmux", "kill-window", "-t", handle.native_id],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def read_usage(self, result: SessionResult) -> TokenUsage | None:
        if not result.transcript_path:
            return None
        return tally_usage(self.profile.usage_parser, Path(result.transcript_path))
