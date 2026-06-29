"""psmux validation harness — probes how the installed psmux actually behaves
on the tmux operations that are risky to assume on native Windows.

Each live probe shells out to a real native-Windows + psmux host and records
one behavior; the findings drive the design of the psmux multiplexer backend.
The probes ARE the tests. Each prints its hazard -> observed -> implication line
during a live run; set ``PSMUX_FINDINGS_OUT=<path>`` to also write the full
table to that file.

Cross-platform contract: deterministic, no hard sleeps (file-sentinels are
polled to a deadline), and skips cleanly where psmux is absent. Live probes are
guarded by ``HAVE_PSMUX`` so they never run (and never fail) on Linux/macOS or
any host without native psmux; the one guard test below is active everywhere
and proves that degradation works.

Two hard-won facts about psmux window commands shape every probe below
(measured on this host, tmux 3.3.6 emulated):
  * psmux runs a window command as an **argv with an explicit interpreter**
    (``pwsh ...`` / ``cmd /c ...``). A bare pwsh-expression string is exec'd as
    a program name and silently dies.
  * A pwsh ``-Command "...$env:..; .."`` string gets mangled crossing the
    tmux -> ConPTY -> pwsh quoting layers and silently fails to run. Passing the
    script via ``-EncodedCommand`` (base64 UTF-16LE) survives intact, so every
    pwsh probe here uses it.
"""

from __future__ import annotations

import base64
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import pytest

# psmux installs a `tmux.exe` alias, so the guard requires psmux specifically
# and the commands this harness launches.
HAVE_PSMUX = (
    sys.platform == "win32"
    and shutil.which("psmux") is not None
    and shutil.which("tmux") is not None
    and shutil.which("pwsh") is not None
)

_LIVE = pytest.mark.skipif(not HAVE_PSMUX, reason="native Windows + psmux required")

# The fix-tty probes assert against the `claude` binary itself — gate them on it
# being installed so they fail for a backend regression, not a host without Claude.
HAVE_CLAUDE = shutil.which("claude") is not None
HAVE_CLAUDE_EXE = shutil.which("claude.exe") is not None
_LIVE_CLAUDE = pytest.mark.skipif(
    not (HAVE_PSMUX and HAVE_CLAUDE), reason="native Windows + psmux + claude required"
)
_LIVE_CLAUDE_EXE = pytest.mark.skipif(
    not (HAVE_PSMUX and HAVE_CLAUDE_EXE), reason="native Windows + psmux + claude.exe required"
)

# Env keys the agent windows depend on; probed for child-process propagation.
ENV_KEYS = ("BMAD_AUTO_RUN_DIR", "BMAD_AUTO_TASK_ID")

_MUX_TIMEOUT_S = 60
_session_seq = 0  # deterministic unique session names (no RNG)


def _run_mux(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess:
    """Run a psmux/tmux command. Reads with ``encoding="utf-8",
    errors="backslashreplace"`` by default: Windows defaults to cp1252 and
    garbles `-F` output and multibyte separators (utf8-roundtrip hazard)."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        encoding=encoding,
        errors="backslashreplace",
        timeout=_MUX_TIMEOUT_S,
    )


def _run_mux_default_text(*args: str) -> subprocess.CompletedProcess:
    """Run tmux with Python's default text decoding, intentionally omitting
    encoding=... for the utf8-roundtrip probe."""
    return subprocess.run(
        ["tmux", *args],
        capture_output=True,
        text=True,
        errors="backslashreplace",
        timeout=_MUX_TIMEOUT_S,
    )


def _must_mux(*args: str, encoding: str = "utf-8") -> subprocess.CompletedProcess:
    cp = _run_mux(*args, encoding=encoding)
    assert cp.returncode == 0, f"tmux {' '.join(args)} failed: {cp.stderr or cp.stdout}"
    return cp


def _setopt_global_or_skip(*args: str) -> None:
    """Set a global psmux option, or *skip* the probe when this psmux build can't.

    Some psmux builds answer ``set-option -g`` with ``psmux: connection timed out``
    (the global option store isn't wired the way tmux's is), which makes the
    claude-code-fix-tty hazard unprobeable on that host. That is a skip, never a
    failure: the probe must assert where psmux honors the option and step aside
    where it can't — a hard fail here would red-flag a perfectly good backend.
    """
    try:
        cp = _run_mux("set-option", "-g", *args)
    except subprocess.TimeoutExpired:
        pytest.skip(f"psmux timed out on `set-option -g {' '.join(args)}` on this host")
    if cp.returncode != 0:
        pytest.skip(
            f"psmux can't set global option `{' '.join(args)}`: {(cp.stderr or cp.stdout).strip()}"
        )


class _Session:
    """A detached psmux session, killed on exit (plain-name kill — safe per the kill-session probe)."""

    def __init__(self) -> None:
        global _session_seq
        _session_seq += 1
        self.name = f"psmuxprobe_{os.getpid()}_{_session_seq}"

    def __enter__(self) -> str:
        _must_mux("new-session", "-d", "-s", self.name)
        return self.name

    def __exit__(self, *exc: object) -> None:
        _run_mux("kill-session", "-t", self.name)


def _enc(ps_source: str) -> str:
    """Encode a pwsh script for ``-EncodedCommand`` (base64 UTF-16LE) — the only
    form that survives the tmux -> ConPTY -> pwsh quoting layers intact."""
    return base64.b64encode(ps_source.encode("utf-16-le")).decode("ascii")


def _pwsh_argv(ps_source: str, *, profile: bool = False) -> list[str]:
    head = ["pwsh"] if profile else ["pwsh", "-NoProfile"]
    return [*head, "-EncodedCommand", _enc(ps_source)]


def _ps_single(value: str | Path) -> str:
    return str(value).replace("'", "''")


def _await_file(path: Path, deadline_s: float = 25.0) -> str | None:
    """Poll for a sentinel file written by a window command. Returns its text, or
    None if it never appeared within the deadline. No hard sleep — the poll
    interval just bounds busy-spin."""
    end = time.monotonic() + deadline_s
    while time.monotonic() < end:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="backslashreplace")
            if text.strip():
                return text
        time.sleep(0.1)
    return path.read_text(encoding="utf-8", errors="backslashreplace") if path.exists() else None


def _set_content(out: Path, value_expr: str) -> str:
    """A pwsh statement writing ``value_expr`` (a pwsh expression) to ``out``.
    The expr is wrapped in ``(...)`` so multi-term expressions bind to -Value
    instead of spilling into extra positional args."""
    return f"Set-Content -LiteralPath '{_ps_single(out)}' -Value ({value_expr})"


def _record(hazard: str, observed: str, implication: str) -> None:
    # Print the hazard → observed → implication line. `-s` prints to the real
    # console (cp1252 on Windows), which can't encode chars like Ω — sanitize so
    # progress output never crashes the probe that produced a correct finding.
    msg = f"\n[{hazard}] {observed}\n      design -> {implication}"
    enc = getattr(sys.stdout, "encoding", None) or "utf-8"
    print(msg.encode(enc, "backslashreplace").decode(enc, "backslashreplace"))


# ------------------------------------------------------------- active everywhere


def test_skip_guard_predicate_is_correct():
    """The live-probe guard must skip cleanly off-host. Deterministic on every
    platform; the anchor proving the probes degrade to skips, never errors."""
    if sys.platform != "win32":
        assert HAVE_PSMUX is False, "live probes must skip on non-Windows hosts"
    else:
        # On Windows the guard tracks real psmux, not the tmux-alias false positive.
        assert HAVE_PSMUX == all(
            shutil.which(name) is not None for name in ("psmux", "tmux", "pwsh")
        )


# -------------------------------------------------- live probes (Windows + psmux)


@_LIVE
def test_env_propagation_via_new_window_dash_e():
    """env-propagation — does `new-window -e KEY=VAL` reach the child? Compared against a pwsh
    `$env:K='v'` prelude. Measured: `-e` does NOT propagate (child sees empty);
    the prelude does. Backend must inject env via a pwsh prelude, not `-e`."""
    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        tmp = Path(td)

        # (a) the -e path: read both env keys back from the child.
        out_e = tmp / "via_e.txt"
        read_env = " + '|' + ".join(f"$env:{k}" for k in ENV_KEYS)  # $env:A + '|' + $env:B
        e_args: list[str] = []
        for k in ENV_KEYS:
            e_args += ["-e", f"{k}=VAL_{k}"]
        _must_mux("new-window", "-t", f"={s}:", *e_args, *_pwsh_argv(_set_content(out_e, read_env)))
        via_e = (_await_file(out_e) or "").strip()
        expected = "|".join(f"VAL_{k}" for k in ENV_KEYS)
        e_works = via_e == expected

        # (b) the prelude fallback: set env inside the command, then read it back.
        out_p = tmp / "via_prelude.txt"
        prelude = "; ".join(f"$env:{k}='VAL_{k}'" for k in ENV_KEYS)
        _must_mux(
            "new-window",
            "-t",
            f"={s}:",
            *_pwsh_argv(f"{prelude}; {_set_content(out_p, read_env)}"),
        )
        via_prelude = (_await_file(out_p) or "").strip()
        prelude_works = via_prelude == expected

    _record(
        "env-propagation",
        f"`-e` propagation: {'works' if e_works else 'BROKEN'} (child saw {via_e!r}); "
        f"pwsh `$env:` prelude: {'works' if prelude_works else 'BROKEN'} (saw {via_prelude!r})",
        (
            "Inject env via a pwsh `$env:K='v'` prelude in the command string"
            if not e_works
            else "`-e` is fixed on this psmux — keep `-e`"
        ),
    )
    # Green = the backend has a working env-injection strategy.
    assert prelude_works, f"pwsh env prelude must propagate env; saw {via_prelude!r}"


@_LIVE
def test_kill_session_targeting():
    """kill-session — `kill-session -t '=name'` (exact match) vs plain-name `-t name`.
    Create a session, kill each way, then `has-session`. The backend needs at
    least one form that actually removes the session."""
    exact_killed = plain_killed = False

    with _Session() as s:  # context-mgr kills on exit, so test inner extra sessions
        # exact-match form
        n1 = f"{s}_exact"
        _must_mux("new-session", "-d", "-s", n1)
        _run_mux("kill-session", "-t", f"={n1}")
        exact_killed = _run_mux("has-session", "-t", f"={n1}").returncode != 0
        if not exact_killed:
            _run_mux("kill-session", "-t", n1)  # cleanup

        # plain-name form
        n2 = f"{s}_plain"
        _must_mux("new-session", "-d", "-s", n2)
        _run_mux("kill-session", "-t", n2)
        plain_killed = _run_mux("has-session", "-t", f"={n2}").returncode != 0
        if not plain_killed:
            _run_mux("kill-session", "-t", f"={n2}")  # cleanup

    _record(
        "kill-session",
        f"kill-session exact `=name`: {'kills' if exact_killed else 'IGNORED'}; "
        f"plain `name`: {'kills' if plain_killed else 'IGNORED'}",
        (
            "Use exact-match `=name` for kill (matches reads)"
            if exact_killed
            else "Drop the `=` prefix for kill-session; use plain-name targeting"
        ),
    )
    assert exact_killed or plain_killed, "at least one kill-session form must remove the session"


@_LIVE
def test_posix_parked_trailer_non_viable():
    """posix-trailer — confirm the POSIX `sh -c "...; read -r; ..."` parked trailer is not
    viable natively, and that the pwsh replacement (`$LASTEXITCODE` capture) is.
    The backend's parked window must be re-expressed in pwsh."""
    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        tmp = Path(td)

        # (a) POSIX trailer: launch the actual sh -c shape and see if it runs.
        out_sh = tmp / "sh.txt"
        _must_mux(
            "new-window",
            "-t",
            f"={s}:",
            "sh",
            "-c",
            f"echo POSIX_RAN > '{out_sh}'; read -r x",
        )
        sh_ran = (_await_file(out_sh, deadline_s=8.0) or "").strip() == "POSIX_RAN"

        # (b) pwsh trailer: invoke a child, capture its exit code, park-equivalent.
        out_ps = tmp / "ps.txt"
        # `& cmd /c exit 7` -> $LASTEXITCODE is 7; proves exit-status capture works.
        pwsh_trailer = f"& cmd /c exit 7; {_set_content(out_ps, '$LASTEXITCODE')}"
        _must_mux("new-window", "-t", f"={s}:", *_pwsh_argv(pwsh_trailer))
        ps_exit = (_await_file(out_ps) or "").strip()

    sh_available = shutil.which("sh") is not None
    _record(
        "posix-trailer",
        f"POSIX `sh -c` trailer ran={sh_ran} (sh on PATH={sh_available}); "
        f"pwsh `$LASTEXITCODE` capture returned {ps_exit!r} (expected '7')",
        "Re-express the parked trailer in pwsh: run argv, capture `$LASTEXITCODE`, "
        "`Read-Host` to park, then the tmux switch/detach verbs",
    )
    # Green = the pwsh replacement reliably captures child exit status. The POSIX
    # check is host-gated: with Git Bash/MSYS `sh` on PATH, `sh -c` legitimately
    # runs, so only assert non-viability when `sh` is genuinely absent.
    if not sh_available:
        assert not sh_ran, "POSIX parked trailer unexpectedly ran without `sh` on PATH"
    assert ps_exit == "7", f"pwsh trailer must capture child exit code; saw {ps_exit!r}"


@_LIVE
def test_detached_pipe_pane_delivery():
    """pipe-pane — does `pipe-pane -o <sink>` deliver pane bytes while the session is
    detached (no attached client)? The pane log is debug-only, so an empty log
    is best-effort, never a launch failure. Uses a pwsh sink, not `cat`."""
    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        tmp = Path(td)
        sink = tmp / "pane.log"
        # pwsh sink: read the piped pane bytes from stdin and append to the log.
        sink_cmd = "pwsh -NoProfile -EncodedCommand " + _enc(
            "$i=[Console]::In.ReadLine(); "
            f"if ($null -ne $i) {{ Set-Content -LiteralPath '{_ps_single(sink)}' -Value $i }}"
        )
        target_win = f"={s}:"
        pipe = _must_mux("pipe-pane", "-o", "-t", target_win, sink_cmd)
        # Generate pane output from the session's own (detached) window.
        _must_mux("send-keys", "-t", target_win, "echo PIPED_OUTPUT_MARKER", "Enter")
        delivered = _await_file(sink, deadline_s=10.0) or ""

    got = "PIPED_OUTPUT_MARKER" in delivered
    _record(
        "pipe-pane",
        f"pipe-pane issued rc={pipe.returncode}; detached pane bytes delivered to sink={got}"
        f" ({delivered.strip()[:40]!r})",
        "Pane log is best-effort/debug-only — use a pwsh sink and never block a "
        "launch on it" + ("" if got else "; detached delivery is unreliable here"),
    )
    # pipe-pane is tolerated either way (debug-only); only assert it didn't error out.
    assert pipe.returncode == 0


@_LIVE
def test_utf8_roundtrip_no_garbling():
    """utf8-roundtrip — round-trip a non-ASCII window name through `list-windows -F`. Reading
    with `encoding="utf-8"` must survive; reading as cp1252 garbles it. This is
    the basis for the encoding requirement on every psmux read."""
    name = "wïndöw-café-Ω-名前"
    with _Session() as s:
        _must_mux("rename-window", "-t", f"={s}:", name)
        utf8 = _must_mux("list-windows", "-t", f"={s}", "-F", "#{window_name}").stdout.strip()
        default_cp = _run_mux_default_text("list-windows", "-t", f"={s}", "-F", "#{window_name}")
        assert default_cp.returncode == 0, default_cp.stderr or default_cp.stdout
        default_text = default_cp.stdout.strip()

    survives = name in utf8
    garbled = default_text != name
    _record(
        "utf8-roundtrip",
        f"utf-8 read round-trips name exactly={survives} ({utf8!r}); "
        f"default text read garbles it={garbled} ({default_text!r})",
        "Every psmux read must pass `encoding='utf-8', errors='backslashreplace'`",
    )
    assert survives, f"utf-8 read must preserve non-ASCII window name; saw {utf8!r}"


@_LIVE
def test_window_startup_latency():
    """window-latency — measure wall-clock from `new-window` to the command actually running,
    with and without `-NoProfile`. Must fit the hook-completion timeout budget."""

    def latency(session: str, out: Path, *, profile: bool) -> float:
        t0 = time.monotonic()
        _must_mux(
            "new-window",
            "-t",
            f"={session}:",
            *_pwsh_argv(_set_content(out, "'ready'"), profile=profile),
        )
        assert _await_file(out, deadline_s=45.0), "window command never produced readiness marker"
        return time.monotonic() - t0

    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        tmp = Path(td)
        no_profile = latency(s, tmp / "np.txt", profile=False)
        with_profile = latency(s, tmp / "p.txt", profile=True)

    _record(
        "window-latency",
        f"new-window -> command running: -NoProfile {no_profile:.1f}s, "
        f"with-profile {with_profile:.1f}s",
        f"Launch the interpreter with `-NoProfile` ({no_profile:.1f}s here, well under "
        "the 30s hook timeout); profile load is the avoidable cost",
    )
    # Green = windows start within the hook-completion timeout budget.
    assert no_profile < 30, f"-NoProfile startup must fit hook timeout; was {no_profile:.1f}s"


@_LIVE_CLAUDE_EXE
def test_claude_code_fix_tty_injection():
    """fix-tty — with `claude-code-fix-tty on` (psmux default), does psmux inject the
    agent-teams env vars / a `claude` wrapper into the pane, and does turning it
    off (or absolute-path invocation) neutralize it? bmad-auto drives `claude`
    itself, so surprise teammate panes must be avoidable."""
    teams_keys = ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "PSMUX_CLAUDE_TEAMMATE_MODE")

    def probe_pane(session: str, out: Path) -> str:
        # Profile loaded: that's where psmux injects the wrapper/env, so probe it.
        read = " + '|' + ".join(
            [
                *(f"$env:{k}" for k in teams_keys),
                "[string](Get-Command claude -ErrorAction SilentlyContinue).CommandType",
                "[string](Get-Command claude.exe -ErrorAction SilentlyContinue).CommandType",
                "[string](Get-Command claude.exe -ErrorAction SilentlyContinue).Source",
            ]
        )
        _must_mux(
            "new-window", "-t", f"={session}:", *_pwsh_argv(_set_content(out, read), profile=True)
        )
        return (_await_file(out, deadline_s=30.0) or "").strip()

    default = _run_mux("show-options", "-g", "claude-code-fix-tty").stdout.strip()
    restore = default.split()[-1] if default else None
    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        tmp = Path(td)
        on_state = probe_pane(s, tmp / "on.txt")
        _setopt_global_or_skip("claude-code-fix-tty", "off")
        try:
            off_state = probe_pane(s, tmp / "off.txt")
        finally:
            if restore is None:
                _run_mux("set-option", "-gu", "claude-code-fix-tty")
            else:
                _run_mux("set-option", "-g", "claude-code-fix-tty", restore)

    on_parts = on_state.split("|")
    injected_on = any(on_parts[:2]) or (len(on_parts) > 2 and on_parts[2] == "Function")
    neutralized = off_state != on_state
    exe_bypasses_wrapper = len(on_parts) > 3 and on_parts[3] == "Application"
    _record(
        "fix-tty",
        f"default option={default!r}; fix-tty ON pane -> {on_state!r} (injection={injected_on}); "
        f"fix-tty OFF pane -> {off_state!r} (changed={neutralized}); "
        f"`claude.exe` resolves as application={exe_bypasses_wrapper}",
        "Set `claude-code-fix-tty off` for bmad-auto sessions and/or invoke claude by "
        "absolute path, so bmad-auto's own `claude` launch is never wrapped into teammate mode",
    )
    assert injected_on, "fix-tty ON should expose the psmux Claude wrapper/env behavior"
    # `neutralized` is recorded but NOT asserted: when `claude` resolves to a plain
    # .exe (the case this test's _LIVE_CLAUDE_EXE guard requires, asserted below),
    # there is no profile-defined wrapper for `fix-tty off` to strip, so an
    # unchanged pane state is the correct observation — not a regression. The real
    # The fix-tty mitigation is `-NoProfile` + the env-strip prelude, gated by the
    # sibling test_noprofile_bypasses_fix_tty_wrapper probe.
    assert (
        exe_bypasses_wrapper
    ), "plain claude.exe should resolve to an application, not the wrapper function"


@_LIVE_CLAUDE
def test_noprofile_bypasses_fix_tty_wrapper():
    """Production's fix-tty mitigation is `-NoProfile` (psmux_backend `_pwsh_argv`),
    on the claim that skipping the profile leaves bmad-auto's own `claude` launch
    *unwrapped* (the claude-code-fix-tty wrapper is defined in the profile). The
    sibling fix-tty probe only measured `profile=True`; this measures `profile=False`
    with fix-tty ON (worst case) and asserts `claude` no longer resolves to the
    wrapper Function — the mitigation production actually relies on.

    The agent-teams env vars are *recorded, not asserted*: this suite may run inside
    a teammate session (a set value is then inherited, not psmux-injected), and psmux
    injects PSMUX_CLAUDE_TEAMMATE_MODE at the tmux-server level, which `-NoProfile`
    does NOT clear (fix-tty residue). Asserting their absence would be both flaky and
    contrary to the measured truth — so the residue is surfaced for human review."""
    teams_keys = ("CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS", "PSMUX_CLAUDE_TEAMMATE_MODE")

    def probe_noprofile_pane(session: str, out: Path) -> str:
        read = " + '|' + ".join(
            [
                *(f"$env:{k}" for k in teams_keys),
                "[string](Get-Command claude -ErrorAction SilentlyContinue).CommandType",
            ]
        )
        # profile=False == production's `-NoProfile` launch.
        _must_mux(
            "new-window", "-t", f"={session}:", *_pwsh_argv(_set_content(out, read), profile=False)
        )
        return (_await_file(out, deadline_s=30.0) or "").strip()

    default = _run_mux("show-options", "-g", "claude-code-fix-tty").stdout.strip()
    restore = default.split()[-1] if default else None
    with tempfile.TemporaryDirectory(prefix="psmuxprobe_") as td, _Session() as s:
        _setopt_global_or_skip("claude-code-fix-tty", "on")  # worst case for injection
        try:
            state = probe_noprofile_pane(s, Path(td) / "noprofile.txt")
        finally:
            if restore is None:
                _run_mux("set-option", "-gu", "claude-code-fix-tty")
            else:
                _run_mux("set-option", "-g", "claude-code-fix-tty", restore)

    parts = state.split("|")
    claude_is_wrapper = len(parts) > 2 and parts[2] == "Function"
    # Distinguish psmux injection from inheritance: a pane value that differs from
    # the launching environment was added by psmux/tmux, not inherited.
    injected_env = {
        k: parts[i]
        for i, k in enumerate(teams_keys)
        if i < len(parts) and parts[i] and parts[i] != (os.environ.get(k) or "")
    }
    _record(
        "fix-tty-noprofile",
        f"-NoProfile + fix-tty ON pane -> {state!r} "
        f"(claude wrapped={claude_is_wrapper}, psmux-injected-env={injected_env})",
        "`-NoProfile` bypasses the profile-defined claude-code-fix-tty wrapper (claude "
        "resolves as an application, not the wrapper function); process-level teammate-mode "
        "env is injected at the tmux server and is NOT cleared by -NoProfile (fix-tty residue)",
    )
    assert (
        not claude_is_wrapper
    ), "-NoProfile must leave `claude` unwrapped (application, not the wrapper Function)"
