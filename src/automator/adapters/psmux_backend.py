"""psmux backend for the terminal-multiplexer seam (native Windows, no WSL).

psmux ships a ``tmux.exe`` drop-in with the same stable-id scheme and ``-t``
targeting, so ``subprocess.run(["tmux", …])`` and almost every argv resolve
unchanged. This backend therefore *subclasses* :class:`~.tmux_base.BaseTmuxBackend`
(the tmux-family base, not its POSIX leaf — psmux is a sibling of
:class:`~.tmux_backend.TmuxMultiplexer`) and overrides only the genuinely-divergent
operations:

  * **encoding** — Windows defaults to cp1252, which garbles ``-F`` output and
    multibyte separators; every read forces ``encoding="utf-8"``. Done once, in
    the shared ``_run`` seam, so it covers inherited reads too.
  * **parked window** — the POSIX ``sh -c "…; read -r; …"`` trailer has no
    ``/bin/sh`` natively; re-expressed in pwsh (run argv, capture
    ``$LASTEXITCODE``, ``Read-Host`` to park, then the same ``tmux`` return verbs).
  * **env injection** — psmux ``new-window -e`` does not propagate env to the
    child; inject it as a pwsh ``$env:K='v'`` prelude instead (names validated).
  * **pipe-pane** — ``cat`` is not native; use a pwsh sink, best-effort.
  * **kill-session** — psmux silently ignores the ``=name`` exact-match form;
    plain-name targeting actually removes the session.

Two facts shape every pwsh construction here: psmux runs a window command as an
**argv with an explicit interpreter** (``pwsh …``), and a pwsh ``-Command "…"``
string is mangled crossing the tmux→ConPTY→pwsh quoting layers — so the script
is always passed via ``-EncodedCommand`` (base64 UTF-16LE).
"""

from __future__ import annotations

import base64
import os
import re
import shlex
import shutil
import subprocess
from pathlib import Path

from .multiplexer import MultiplexerError
from .tmux_base import PARKED_RETURN_DETACH, TMUX_TIMEOUT_S, BaseTmuxBackend

# A safe POSIX/pwsh env-var name. Validated before interpolation into the pwsh
# prelude so a hostile key (e.g. "X; rm -rf /") can't break out of the assignment.
_ENV_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# psmux refuses to create a session when it detects it is running inside one: with
# any of these set, `new-session` prints "sessions should be nested with care, unset
# PSMUX_SESSION to force" and returns 0 *without creating the session* — so the
# immediate set-option then fails with "no server running on session" (psmux #424).
# The engine runs inside the bmad-auto-ctl pane, so strip the guard vars for the
# create call; the session lands on the same default server and targets normally
# afterwards. (Verified on psmux 3.3.6.)
_NESTING_GUARD_ENV = ("PSMUX_SESSION", "PSMUX_ACTIVE", "TMUX")

# psmux injects agent-teams env at the tmux-server level (claude-code-fix-tty on,
# the default), so bmad-auto's own `claude` would launch in teammate mode. -NoProfile
# bypasses the profile *wrapper* but not these process-level vars (measured); clear
# them in-window before the command runs. Scoped to our pane — no global `set-option`,
# nothing to restore. A caller that genuinely wants a var back just sets it via env.
_CLEAR_TEAMMATE_ENV = (
    "Remove-Item Env:CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS -ErrorAction SilentlyContinue; "
    "Remove-Item Env:PSMUX_CLAUDE_TEAMMATE_MODE -ErrorAction SilentlyContinue; "
)


class PsmuxError(MultiplexerError):
    pass


def _enc(ps_source: str) -> str:
    """Encode a pwsh script for ``-EncodedCommand`` (base64 UTF-16LE) — the only
    form that survives the tmux→ConPTY→pwsh quoting layers intact."""
    return base64.b64encode(ps_source.encode("utf-16-le")).decode("ascii")


def _pwsh_argv(ps_source: str) -> list[str]:
    # -NoProfile: skip the user profile — keeps window-startup fast and bypasses
    # psmux's claude-code-fix-tty *wrapper* (defined in the profile), so `claude`
    # resolves to the real executable, not the teammate-mode function. The
    # process-level PSMUX_CLAUDE_TEAMMATE_MODE env injection that rides the tmux
    # server env regardless of -NoProfile is cleared separately by
    # _CLEAR_TEAMMATE_ENV at the head of each launched window's source.
    return ["pwsh", "-NoProfile", "-EncodedCommand", _enc(ps_source)]


def _ps_single(value: str | Path) -> str:
    """Escape a value for a pwsh single-quoted literal (``''`` escapes ``'``)."""
    return str(value).replace("'", "''")


def _pwsh_call(command: str) -> str:
    # Precondition: `command` must be a shlex.quote-shaped (POSIX) string — the
    # sole caller routes through GenericAdapter.build_command, which quotes each
    # arg. posix=True split treats `\` as an escape, so a raw Windows path like
    # `C:\new\settings.json` would silently lose its backslashes here; pre-quoted
    # input avoids that.
    try:
        argv = shlex.split(command, posix=True)
    except ValueError as exc:
        raise PsmuxError(f"invalid command quoting for psmux: {exc}") from exc
    if not argv:
        raise PsmuxError("empty command for psmux window")
    return "& " + " ".join(f"'{_ps_single(a)}'" for a in argv)


class PsmuxMultiplexer(BaseTmuxBackend):
    def _run(
        self, argv: list[str], *, check: bool = True, env: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        # Override the base spawn primitive for the two Windows divergences the base
        # `_run` cannot carry: force utf-8 decoding (Windows defaults to cp1252, which
        # garbles `-F` output and multibyte separators — every read covered once here,
        # inherited ones included), and forward an optional `env` (the base forwards
        # none) so new_session can strip psmux's nesting guard. `argv` excludes "tmux",
        # prepended here exactly as the base does.
        proc = subprocess.run(
            ["tmux", *argv],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="backslashreplace",
            timeout=TMUX_TIMEOUT_S,
            env=env,
        )
        if check and proc.returncode != 0:
            raise PsmuxError(f"tmux {' '.join(argv[:2])} failed: {proc.stderr.strip()}")
        return proc

    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        # Same geometry contract as the base, but the create call runs with the
        # nesting-guard env stripped (see _NESTING_GUARD_ENV) so psmux actually
        # creates the session from inside the control pane instead of no-op'ing.
        # The base's `_tmux` forwards no env, so go through the `_run` override with
        # check=False (to raise the psmux-specific message) and the env passthrough.
        geometry = ["-x", str(cols), "-y", str(lines)] if cols and lines else []
        env = {k: v for k, v in os.environ.items() if k not in _NESTING_GUARD_ENV}
        proc = self._run(
            ["new-session", "-d", "-s", name, "-c", str(cwd), *geometry],
            check=False,
            env=env,
        )
        if proc.returncode != 0:
            raise PsmuxError(f"tmux new-session failed: {proc.stderr.strip()}")

    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        # `-e` does not reach the child on psmux; layer env via a pwsh prelude.
        # The command runs under an explicit pwsh interpreter because psmux execs a
        # bare command string as a program name (it would silently die otherwise).
        source = f"{_CLEAR_TEAMMATE_ENV}{self._env_prelude(env)}{_pwsh_call(command)}"
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
            *_pwsh_argv(source),
        )

    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        # Re-express the POSIX parked trailer in pwsh. The return-trailer verbs
        # are the *same* tmux verbs as the POSIX backend (they hit tmux.exe and are
        # protocol-identical) — only the shell wrapper changes.
        if not argv:
            raise PsmuxError("empty argv for parked window")
        inner = "& " + " ".join(f"'{_ps_single(a)}'" for a in argv)
        return_trailer = (
            f"$ret = (tmux show-options -wqv '{_ps_single(return_opt)}') 2>$null; "
            f"if ($ret -eq '{PARKED_RETURN_DETACH}') {{ tmux detach-client 2>$null }} "
            "elseif ($ret) { tmux switch-client -t $ret 2>$null; "
            "if ($LASTEXITCODE -ne 0) { tmux switch-client -l 2>$null } }"
        )
        source = (
            f"{_CLEAR_TEAMMATE_ENV}{inner}; $ec = $LASTEXITCODE; "
            'Write-Host "[bmad-auto exited $ec — press enter]"; '
            f"Read-Host; {return_trailer}"
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
            *_pwsh_argv(source),
        )

    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        # Debug-only and best-effort, but still attach a native pwsh sink.
        sink = (
            f"$p='{_ps_single(log_file)}'; "
            "$d=Split-Path -Parent $p; "
            "if ($d) { New-Item -ItemType Directory -Force -Path $d | Out-Null }; "
            "$input | Add-Content -LiteralPath $p -Encoding utf8"
        )
        try:
            self._tmux("pipe-pane", "-t", window_id, "-o", " ".join(_pwsh_argv(sink)))
        except (MultiplexerError, subprocess.SubprocessError, OSError):
            pass

    def kill_session(self, name: str) -> None:
        # The `=name` exact-match form is silently ignored by psmux's kill-session
        # (returns 0, doesn't kill); plain-name actually removes it.
        # Same best-effort tolerance as the POSIX backend.
        if not shutil.which("tmux"):
            return
        try:
            self._run(["kill-session", "-t", name], check=False)
        except (subprocess.SubprocessError, OSError):
            pass

    def _env_prelude(self, env: dict[str, str]) -> str:
        """A pwsh ``$env:K='v'; …; `` prelude setting each var, or '' when empty.
        Names are validated against ``_ENV_NAME`` and values single-quoted so the
        prelude can't be broken out of."""
        parts: list[str] = []
        for key, value in env.items():
            if not _ENV_NAME.match(key):
                raise PsmuxError(f"unsafe env var name for psmux prelude: {key!r}")
            parts.append(f"$env:{key}='{_ps_single(value)}'")
        return "; ".join(parts) + "; " if parts else ""

    def available(self) -> bool:
        return all(shutil.which(name) is not None for name in ("psmux", "tmux", "pwsh"))
