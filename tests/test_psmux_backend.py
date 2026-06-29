"""Backend tests for the native-Windows psmux multiplexer and platform selection.

Two tiers live here, both deterministic — subprocess is mocked, so no real
tmux/psmux binary or Windows host is needed:

  * **POSIX regression pins** — lock the byte-for-byte tmux command strings so the
    additive Windows backend cannot perturb them. Green on every OS; must stay green.
  * **psmux backend contract** — the Windows-specific command construction and the
    platform-selection branch.

The pwsh construction asserted below mirrors psmux's real behavior: it runs a
window command as an argv with an explicit interpreter (``pwsh …``), never a bare
expression string, and a ``-Command`` string is mangled by the tmux->ConPTY->pwsh
quoting layers (use ``-EncodedCommand``).
"""

from __future__ import annotations

import base64
import json
import shlex
import subprocess
import sys

import pytest

from automator.adapters import psmux_backend, tmux_base
from automator.adapters.base import SessionSpec
from automator.adapters.generic import GenericAdapter
from automator.adapters.multiplexer import TerminalMultiplexer, get_multiplexer
from automator.adapters.profile import get_profile
from automator.adapters.psmux_backend import PsmuxError, PsmuxMultiplexer
from automator.adapters.tmux_backend import PARKED_RETURN_DETACH, TmuxMultiplexer
from automator.policy import LimitsPolicy, Policy


def _decoded_pwsh(args: list) -> str:
    """Decode the ``-EncodedCommand`` (base64 UTF-16LE) payload of a psmux argv
    back to its pwsh source, so a test can assert the real script shape rather
    than the opaque base64 blob."""
    flat = [str(a) for a in args]
    payload = flat[flat.index("-EncodedCommand") + 1]
    return base64.b64decode(payload).decode("utf-16-le")


class _Recorder:
    """Captures the (argv, kwargs) of every subprocess.run call and returns a
    successful, parseable result so the backend method completes."""

    def __init__(self) -> None:
        self.calls: list[tuple[list, dict]] = []

    def __call__(self, args, **kwargs):
        self.calls.append((list(args), kwargs))
        return subprocess.CompletedProcess(args, 0, stdout="@win0", stderr="")


@pytest.fixture
def rec(monkeypatch):
    """Mock the subprocess seam of both backends so no real mux is invoked and the
    exact argv/kwargs are inspectable. Patches psmux's seam too once it exists, to
    catch a reused-tmux-base implementation either way."""
    recorder = _Recorder()
    monkeypatch.setattr(tmux_base.subprocess, "run", recorder)
    monkeypatch.setattr(psmux_backend.subprocess, "run", recorder)
    return recorder


# --------------------------------------------------------------- POSIX pins (green)


def test_posix_parked_trailer_is_byte_identical(rec, tmp_path):
    TmuxMultiplexer().new_parked_window(
        "sess", "win", tmp_path, ["echo", "hi"], PARKED_RETURN_DETACH
    )
    args, _ = rec.calls[-1]
    assert args[:2] == ["tmux", "new-window"]
    assert args[-3:-1] == ["sh", "-c"]

    return_trailer = (
        f"ret=$(tmux show-options -wqv {PARKED_RETURN_DETACH} 2>/dev/null); "
        f'if [ "$ret" = "{PARKED_RETURN_DETACH}" ]; then tmux detach-client 2>/dev/null; '
        'elif [ -n "$ret" ]; then '
        'tmux switch-client -t "$ret" 2>/dev/null || tmux switch-client -l 2>/dev/null; '
        "fi"
    )
    inner = shlex.join(["echo", "hi"])
    expected = (
        f'{inner}; ec=$?; echo "[bmad-auto exited $ec — press enter]"; '
        f"read -r; {return_trailer}"
    )
    assert args[-1] == expected


def test_posix_new_window_env_arg_shape(rec, tmp_path):
    TmuxMultiplexer().new_window("sess", "win", tmp_path, {"K": "V", "K2": "V2"}, "mycmd")
    args, _ = rec.calls[-1]
    assert args == [
        "tmux",
        "new-window",
        "-t",
        "=sess:",
        "-n",
        "win",
        "-c",
        str(tmp_path),
        "-P",
        "-F",
        "#{window_id}",
        "-e",
        "K=V",
        "-e",
        "K2=V2",
        "mycmd",
    ]


def test_posix_pipe_pane_sink_unchanged(rec, tmp_path):
    log = tmp_path / "pane.log"
    TmuxMultiplexer().pipe_pane("@1", log)
    args, _ = rec.calls[-1]
    assert args == ["tmux", "pipe-pane", "-t", "@1", "-o", f"cat >> {shlex.quote(str(log))}"]


# ----------------------------------------------------------- platform selection


def test_selection_picks_psmux_on_win32(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    get_multiplexer.cache_clear()
    try:
        assert isinstance(get_multiplexer(), PsmuxMultiplexer)
    finally:
        get_multiplexer.cache_clear()


@pytest.mark.parametrize("plat", ["linux", "darwin"])
def test_selection_keeps_tmux_off_windows(monkeypatch, plat):
    monkeypatch.setattr(sys, "platform", plat)
    get_multiplexer.cache_clear()
    try:
        assert isinstance(get_multiplexer(), TmuxMultiplexer)
    finally:
        get_multiplexer.cache_clear()


def test_selection_wsl_stays_tmux(monkeypatch):
    # WSL reports sys.platform == "linux"; it must keep the POSIX tmux backend,
    # never the Windows psmux one.
    monkeypatch.setattr(sys, "platform", "linux")
    get_multiplexer.cache_clear()
    try:
        assert isinstance(get_multiplexer(), TmuxMultiplexer)
    finally:
        get_multiplexer.cache_clear()


# --------------------------------------------------- psmux backend contract (red)


def test_psmux_satisfies_multiplexer_contract():
    # Instantiation raises TypeError if any abstract method is unimplemented.
    assert isinstance(PsmuxMultiplexer(), TerminalMultiplexer)


def test_win_parked_trailer_uses_pwsh_not_posix(rec, tmp_path):
    PsmuxMultiplexer().new_parked_window("sess", "win", tmp_path, ["claude"], PARKED_RETURN_DETACH)
    args, _ = rec.calls[-1]
    flat = " ".join(map(str, args))
    # interpreter is pwsh via -EncodedCommand; no POSIX-shell trailer survives
    assert args[-4:-1] == ["pwsh", "-NoProfile", "-EncodedCommand"]
    assert "sh -c" not in flat
    assert "read -r" not in flat
    assert "$?" not in flat
    # the real pwsh trailer: run argv, capture exit, park on Read-Host, then the
    # same tmux return verbs keyed by the per-window return option.
    source = _decoded_pwsh(args)
    assert "& 'claude'" in source
    assert "$LASTEXITCODE" in source
    assert "Read-Host" in source
    assert (
        f"show-options -wqv '{PARKED_RETURN_DETACH}'" in source
    )  # return_opt is _ps_single-quoted
    assert "detach-client" in source


def test_win_env_propagates_and_validates_key_names(rec, tmp_path):
    mux = PsmuxMultiplexer()
    # a safe env var is injected as a pwsh $env: prelude ahead of the command
    # (decode the -EncodedCommand payload — the key isn't literal in the argv).
    command = shlex.join(["claude", "say it's ok", "semi;colon", "$HOME"])
    mux.new_window("sess", "win", tmp_path, {"BMAD_AUTO_RUN_DIR": "C:/r"}, command)
    source = _decoded_pwsh(rec.calls[-1][0])
    assert "$env:BMAD_AUTO_RUN_DIR='C:/r'" in source
    assert source.rstrip().endswith("& 'claude' 'say it''s ok' 'semi;colon' '$HOME'")
    # an unsafe env-var name is rejected, not interpolated into the prelude
    with pytest.raises(PsmuxError):
        mux.new_window("sess", "win", tmp_path, {"BAD NAME; rm -rf /": "x"}, "claude")


def test_win_pipe_pane_uses_pwsh_sink(rec, tmp_path):
    log = tmp_path / "pane.log"
    PsmuxMultiplexer().pipe_pane("@1", log)
    args, _ = rec.calls[-1]
    assert args[:5] == ["tmux", "pipe-pane", "-t", "@1", "-o"]
    sink = args[-1].split()
    assert sink[:3] == ["pwsh", "-NoProfile", "-EncodedCommand"]
    source = base64.b64decode(sink[-1]).decode("utf-16-le")
    assert "Add-Content" in source
    assert str(log) in source


def test_win_pipe_pane_tolerates_dead_window(monkeypatch, tmp_path):
    def fail(args, **kwargs):
        raise psmux_backend.subprocess.SubprocessError("window already gone")

    monkeypatch.setattr(psmux_backend.subprocess, "run", fail)
    # the pane log is debug-only; a failed attach must not bring a launch down
    PsmuxMultiplexer().pipe_pane("@dead", tmp_path / "pane.log")


def test_win_psmux_reads_pass_utf8_encoding(rec):
    PsmuxMultiplexer().has_session("sess")
    reads = [kw for _, kw in rec.calls if kw.get("capture_output")]
    assert reads, "expected at least one psmux read"
    assert all(kw.get("encoding") == "utf-8" for kw in reads)


def test_win_kill_and_has_session_reach_the_named_session(rec, monkeypatch):
    # psmux silently ignores the `=name` exact-match form for kill-session;
    # plain-name targeting actually removes it. has-session reads keep `=name`
    # (which works). Pin both exact forms.
    monkeypatch.setattr(psmux_backend.shutil, "which", lambda _name: "tmux")
    mux = PsmuxMultiplexer()
    mux.has_session("sess")
    mux.kill_session("sess")
    has_args = next(a for a, _ in rec.calls if "has-session" in a)
    kill_args = next(a for a, _ in rec.calls if "kill-session" in a)
    assert has_args == ["tmux", "has-session", "-t", "=sess"]
    assert kill_args == ["tmux", "kill-session", "-t", "sess"]


def test_win_new_session_strips_nesting_guard_env(rec, monkeypatch, tmp_path):
    # The engine creates per-run sessions from inside the bmad-auto-ctl pane, where
    # psmux's nesting guard would no-op new-session (psmux #424). The create call
    # must run with PSMUX_SESSION/PSMUX_ACTIVE/TMUX stripped — and only those.
    monkeypatch.setenv("PSMUX_SESSION", "bmad-auto-ctl")
    monkeypatch.setenv("PSMUX_ACTIVE", "1")
    monkeypatch.setenv("TMUX", "/tmp/psmux-1/default,1,0")
    monkeypatch.setenv("BMAD_KEEP", "keepme")
    PsmuxMultiplexer().new_session("sess", tmp_path, 80, 24)
    argv, kwargs = rec.calls[-1]
    assert argv[:6] == ["tmux", "new-session", "-d", "-s", "sess", "-c"]
    assert argv[-4:] == ["-x", "80", "-y", "24"]
    env = kwargs["env"]
    assert not ({"PSMUX_SESSION", "PSMUX_ACTIVE", "TMUX"} & env.keys())
    assert env.get("BMAD_KEEP") == "keepme"  # unrelated env survives


def test_win_new_session_raises_on_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(
        psmux_backend.subprocess,
        "run",
        lambda args, **k: subprocess.CompletedProcess(args, 1, stdout="", stderr="boom"),
    )
    with pytest.raises(PsmuxError, match="new-session failed: boom"):
        PsmuxMultiplexer().new_session("sess", tmp_path, 80, 24)


def test_win_available_requires_psmux_tmux_and_pwsh(monkeypatch):
    present = {"psmux", "tmux", "pwsh"}
    monkeypatch.setattr(
        psmux_backend.shutil, "which", lambda name: name if name in present else None
    )
    assert PsmuxMultiplexer().available() is True
    present.remove("pwsh")
    assert PsmuxMultiplexer().available() is False


def test_generic_adapter_drives_psmux_backend_with_mocked_subprocess(monkeypatch, tmp_path):
    calls: list[tuple[list, dict]] = []

    def fake_run(args, **kwargs):
        calls.append((list(args), kwargs))
        rc = 1 if args[1] == "has-session" else 0
        return subprocess.CompletedProcess(args, rc, stdout="@win0", stderr="")

    monkeypatch.setattr(psmux_backend.subprocess, "run", fake_run)
    monkeypatch.setattr(psmux_backend.shutil, "which", lambda name: name)
    adapter = GenericAdapter(
        run_dir=tmp_path / "run",
        policy=Policy(limits=LimitsPolicy()),
        profile=get_profile("claude"),
        mux=PsmuxMultiplexer(),
    )
    task_id = "1-2-dev-1"
    spec = SessionSpec(
        task_id=task_id,
        role="dev",
        prompt="/bmad-dev-auto 1-2",
        cwd=tmp_path,
        env={"BMAD_AUTO_RUN_DIR": str(tmp_path / "run"), "BMAD_AUTO_TASK_ID": task_id},
        timeout_s=10.0,
    )

    handle = adapter.start_session(spec)
    assert handle.native_id == "@win0"
    verbs = [args[1] for args, _ in calls]
    assert verbs == ["has-session", "new-session", "set-option", "new-window", "pipe-pane"]

    ts = handle.launched_ns + 1
    events_dir = adapter.watcher.events_dir
    events_dir.mkdir(parents=True, exist_ok=True)
    (events_dir / f"{ts}-{task_id}-Stop.json").write_text(
        json.dumps({"ts": ts, "event": "Stop", "task_id": task_id, "session_id": "s1"}),
        encoding="utf-8",
    )
    (adapter.tasks_dir / task_id / "result.json").write_text(
        json.dumps({"workflow": "auto-dev"}), encoding="utf-8"
    )
    assert adapter.wait_for_completion(handle, spec).status == "completed"


# ------------------------------------------------ negative paths & injection defense
# Close the defensive branches the earlier tests left unexercised: the three guard
# `raise`s, the env-value pwsh-escape, and kill-session's no-tmux early return.
# Deterministic — subprocess mocked, no real host.


def test_win_new_window_empty_command_raises(tmp_path):
    # A blank command shlex-splits to [] → empty-argv guard fires before any
    # subprocess call (the source is built eagerly).
    with pytest.raises(PsmuxError, match="empty command"):
        PsmuxMultiplexer().new_window("sess", "win", tmp_path, {}, "   ")


def test_win_new_window_invalid_quoting_raises(tmp_path):
    # Malformed quoting makes shlex.split raise ValueError, which the backend wraps
    # as PsmuxError rather than letting it escape raw.
    with pytest.raises(PsmuxError, match="invalid command quoting"):
        PsmuxMultiplexer().new_window("sess", "win", tmp_path, {}, "claude 'unterminated")


def test_win_parked_window_empty_argv_raises(tmp_path):
    # A parked window with no argv has nothing to run → guard raise.
    with pytest.raises(PsmuxError, match="empty argv"):
        PsmuxMultiplexer().new_parked_window("sess", "win", tmp_path, [], PARKED_RETURN_DETACH)


def test_win_env_value_with_quote_is_escaped_in_prelude(rec, tmp_path):
    # A single quote in an env *value* must be doubled ('') inside the pwsh
    # single-quoted literal so the value can't break out of the $env: prelude
    # (only key-name rejection and arg escaping were pinned before).
    PsmuxMultiplexer().new_window("sess", "win", tmp_path, {"BMAD_X": "o'brien'; rm"}, "claude")
    source = _decoded_pwsh(rec.calls[-1][0])
    assert "$env:BMAD_X='o''brien''; rm'" in source


def test_win_kill_session_noops_without_tmux(rec, monkeypatch):
    # With no tmux on PATH, kill_session returns early — no raise, no kill-session
    # subprocess attempted.
    monkeypatch.setattr(psmux_backend.shutil, "which", lambda _name: None)
    PsmuxMultiplexer().kill_session("sess")
    assert not any("kill-session" in a for a, _ in rec.calls)


def test_win_new_window_clears_psmux_teammate_env_before_launch(rec, tmp_path):
    # psmux injects agent-teams env at the server level; clear it in-window before
    # the command runs so bmad-auto's `claude` never inherits teammate mode.
    PsmuxMultiplexer().new_window("sess", "win", tmp_path, {}, "claude")
    source = _decoded_pwsh(rec.calls[-1][0])
    assert "Remove-Item Env:CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in source
    assert "Remove-Item Env:PSMUX_CLAUDE_TEAMMATE_MODE" in source
    # the clear precedes the command launch
    assert source.index("PSMUX_CLAUDE_TEAMMATE_MODE") < source.index("& 'claude'")


def test_win_parked_window_also_clears_teammate_env(rec, tmp_path):
    # Same teammate-env clear on the parked launch path — no sibling gap.
    PsmuxMultiplexer().new_parked_window("sess", "win", tmp_path, ["claude"], PARKED_RETURN_DETACH)
    source = _decoded_pwsh(rec.calls[-1][0])
    assert "Remove-Item Env:CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS" in source
    assert "Remove-Item Env:PSMUX_CLAUDE_TEAMMATE_MODE" in source
    assert source.index("PSMUX_CLAUDE_TEAMMATE_MODE") < source.index("& 'claude'")
