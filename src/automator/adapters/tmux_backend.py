"""tmux backend for the terminal-multiplexer seam.

This is the **only** file in the codebase allowed to shell out to ``tmux`` —
every POSIX-shell trailer and tmux invocation is quarantined here so a future
non-POSIX backend (an eventual native-Windows "psmux") can replace it wholesale.
See :mod:`.multiplexer` for the contract.

Phase 1 implemented the subset the generic adapter drives plus the parked-window
trailer; Phase 2 fills in the rest as the other call sites (``runs.py``,
``tui/launch.py``, ``probe.py``, ``tui/data.py``) migrate onto the seam.
"""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import time
from pathlib import Path

from .multiplexer import MultiplexerError, TerminalMultiplexer

TMUX_TIMEOUT_S = 30
# Per-window option value (vs a pane id) telling the parked trailer to detach the
# client rather than switch it. Pane ids are %N, so this never collides with one.
PARKED_RETURN_DETACH = "detach"


class TmuxError(MultiplexerError):
    pass


class TmuxMultiplexer(TerminalMultiplexer):
    def _tmux(self, *args: str) -> str:
        proc = subprocess.run(
            ["tmux", *args], capture_output=True, text=True, timeout=TMUX_TIMEOUT_S
        )
        if proc.returncode != 0:
            raise TmuxError(f"tmux {' '.join(args[:2])} failed: {proc.stderr.strip()}")
        return proc.stdout.strip()

    # ----------------------------------------------------------- sessions

    def has_session(self, name: str) -> bool:
        # has-session returns nonzero for an absent session (a normal answer, not an
        # error), so this can't go through _tmux. But a timeout or a missing binary
        # is a real backend failure: raise the seam type so callers catch it via
        # MultiplexerError instead of a raw subprocess error escaping.
        try:
            probe = subprocess.run(
                ["tmux", "has-session", "-t", f"={name}"],
                capture_output=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except (subprocess.TimeoutExpired, OSError) as exc:
            raise TmuxError(f"tmux has-session failed: {exc}") from exc
        return probe.returncode == 0

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # Window 0 is a plain shell so the session survives task windows closing.
        # Geometry is pinned only when both dimensions are given (detached agent
        # sessions); the control session omits it and takes tmux's default size.
        geometry = ["-x", str(cols), "-y", str(lines)] if cols and lines else []
        self._tmux("new-session", "-d", "-s", name, "-c", str(cwd), *geometry)

    def set_session_option(self, name: str, option: str, value: str) -> None:
        # set-option has no '=' exact-match form; callers pass a unique full
        # session name so plain-name targeting resolves it unambiguously.
        self._tmux("set-option", "-t", name, option, value)

    def kill_session(self, name: str) -> None:
        # Tolerant of tmux being absent / the session already gone: a best-effort
        # teardown backstop, never a hard failure.
        if not shutil.which("tmux"):
            return
        try:
            subprocess.run(
                ["tmux", "kill-session", "-t", f"={name}"],
                capture_output=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            pass

    def list_sessions(self) -> list[str]:
        # [] when tmux is missing, no server is running, or the query fails — the
        # absence of sessions and the absence of tmux are indistinguishable here
        # and callers treat both as "nothing live".
        if not shutil.which("tmux"):
            return []
        try:
            proc = subprocess.run(
                ["tmux", "list-sessions", "-F", "#{session_name}"],
                capture_output=True,
                text=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return []
        if proc.returncode != 0:  # no server / no sessions
            return []
        return [line for line in proc.stdout.splitlines() if line]

    def session_options(self, option: str) -> dict[str, str]:
        # Map session name -> value of ``option`` ("" when unset). Same missing
        # tmux / no-server tolerance as list_sessions().
        if not shutil.which("tmux"):
            return {}
        try:
            proc = subprocess.run(
                ["tmux", "list-sessions", "-F", f"#{{session_name}}\t#{{{option}}}"],
                capture_output=True,
                text=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return {}
        if proc.returncode != 0:  # no server / no sessions
            return {}
        options: dict[str, str] = {}
        for line in proc.stdout.splitlines():
            name, _, value = line.partition("\t")
            if name:
                options[name] = value
        return options

    # ------------------------------------------------------------ windows

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        env_args: list[str] = []
        for key, value in env.items():
            env_args += ["-e", f"{key}={value}"]
        return self._tmux(
            "new-window",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "-P",
            "-F",
            "#{window_id}",
            *env_args,
            command,
        )

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # The window runs under explicit `sh -c` (the user's login shell may be
        # fish); the trailing `read` keeps the exit status inspectable instead of
        # tmux closing the window the moment the process exits. After the read the
        # return trailer switches an attached client back to its origin pane:
        #   - return_opt == a pane id (%N): switch that client back there
        #     (`switch-client -l` is a best-effort fallback when it is gone);
        #   - return_opt == PARKED_RETURN_DETACH: detach the client so a blocking
        #     `tmux attach` returns and a suspended TUI resumes;
        #   - unset/empty: nobody attached interactively -> park as-is.
        return_trailer = (
            f"ret=$(tmux show-options -wqv {return_opt} 2>/dev/null); "
            f'if [ "$ret" = "{PARKED_RETURN_DETACH}" ]; then tmux detach-client 2>/dev/null; '
            'elif [ -n "$ret" ]; then '
            'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; '
            "fi"
        )
        inner = shlex.join(argv)
        shell = (
            f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; '
            f"read -r; {return_trailer}"
        )
        return self._tmux(
            "new-window",
            "-d",
            "-P",
            "-F",
            "#{window_id}",
            "-t",
            f"={session}:",
            "-n",
            name,
            "-c",
            str(cwd),
            "sh",
            "-c",
            shell,
        )

    def list_window_ids(self, session: str) -> list[str]:
        # display-message -t <dead-window> exits 0 with empty output, so list the
        # session's window ids and check membership instead.
        probe = subprocess.run(
            ["tmux", "list-windows", "-t", f"={session}", "-F", "#{window_id}"],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if probe.returncode != 0:
            return []
        return probe.stdout.split()

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # A CLI that crashes on launch (bad args, instant auth failure) can take
        # its window down before pipe-pane attaches, which races as "can't find
        # window". That is not a setup failure, so tolerate it instead of raising.
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", f"cat >> {shlex.quote(str(log_file))}")
        except TmuxError:
            pass

    def send_text(self, window_id: str, text: str) -> None:
        self._tmux("send-keys", "-t", window_id, "-l", text)
        time.sleep(0.3)  # let the TUI ingest the paste before submitting
        self._tmux("send-keys", "-t", window_id, "Enter")

    def kill_window(self, target: str) -> None:
        subprocess.run(
            ["tmux", "kill-window", "-t", target],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        fmt = "\t".join(f"#{{{field}}}" for field in fields)
        probe = subprocess.run(
            ["tmux", "list-windows", "-t", f"={session}", "-F", fmt],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if probe.returncode != 0:
            return []
        rows: list[tuple[str, ...]] = []
        for line in probe.stdout.splitlines():
            parts = line.split("\t")
            parts += [""] * (len(fields) - len(parts))  # tolerate unset trailing fields
            rows.append(tuple(parts[: len(fields)]))
        return rows

    def window_alive(self, session: str, window_id: str) -> bool:
        return window_id in self.list_window_ids(session)

    def select_window(self, target: str) -> None:
        subprocess.run(
            ["tmux", "select-window", "-t", target],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def set_window_option(self, target: str, option: str, value: str) -> None:
        subprocess.run(
            ["tmux", "set-option", "-w", "-t", target, option, value],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def unset_window_option(self, target: str, option: str) -> None:
        subprocess.run(
            ["tmux", "set-option", "-wu", "-t", target, option],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )

    def show_window_option(self, target: str, option: str) -> str:
        proc = subprocess.run(
            ["tmux", "show-options", "-wqv", "-t", target, option],
            capture_output=True,
            text=True,
            timeout=TMUX_TIMEOUT_S,
        )
        return proc.stdout.strip() if proc.returncode == 0 else ""

    # ----------------------------------------------------- client / attach

    def attach_target_argv(self, target: str) -> list[str]:
        # Inside tmux, nesting an attach is refused, so switch this client
        # instead (a `switch-client -l` brings it back).
        if os.environ.get("TMUX"):
            return ["tmux", "switch-client", "-t", target]
        return ["tmux", "attach", "-t", target]

    def current_pane_id(self) -> str | None:
        return self._display_message("#{pane_id}")

    def current_window_id(self) -> str | None:
        return self._display_message("#{window_id}")

    def current_session(self) -> str | None:
        return self._display_message("#{session_name}")

    def _display_message(self, fmt: str) -> str | None:
        """Resolve a tmux format string against this process's client, or None
        when not inside tmux / tmux is unavailable."""
        try:
            proc = subprocess.run(
                ["tmux", "display-message", "-p", fmt],
                capture_output=True,
                text=True,
                timeout=TMUX_TIMEOUT_S,
            )
        except (subprocess.SubprocessError, OSError):
            return None
        return proc.stdout.strip() if proc.returncode == 0 else None

    def detach_client(self) -> None:
        subprocess.run(["tmux", "detach-client"], capture_output=True, timeout=TMUX_TIMEOUT_S)

    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        proc = subprocess.run(
            ["tmux", "switch-client", "-t", target],
            capture_output=True,
            timeout=TMUX_TIMEOUT_S,
        )
        if proc.returncode == 0:
            return True
        if last_fallback:
            fb = subprocess.run(
                ["tmux", "switch-client", "-l"],
                capture_output=True,
                timeout=TMUX_TIMEOUT_S,
            )
            return fb.returncode == 0
        return False

    def available(self) -> bool:
        return shutil.which("tmux") is not None

    def version(self) -> str | None:
        if not shutil.which("tmux"):
            return None
        try:
            return self._tmux("-V")
        except (MultiplexerError, subprocess.SubprocessError, OSError):
            return None
