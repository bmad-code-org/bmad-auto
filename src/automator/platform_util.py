"""Cross-platform process primitives.

Quarantines the handful of POSIX-only process operations the orchestrator and
its plugins rely on, so a future native-Windows backend can slot in without
re-auditing every call site. On Linux/macOS — and WSL, which *is* Linux — these
preserve today's exact behavior; the Windows branches degrade gracefully and are
not yet exercised (no Windows backend ships in this pass).
"""

from __future__ import annotations

import os
import signal
import subprocess
import sys

# SIGKILL is absent on Windows; fall back to SIGTERM so attribute access never
# raises. Callers wanting a hard kill should reference this rather than
# signal.SIGKILL directly.
SIGKILL = getattr(signal, "SIGKILL", signal.SIGTERM)  # portability: SIGKILL absent on Windows


def terminate_pid(pid: int) -> None:
    """Politely terminate ``pid``.

    POSIX: ``os.kill(pid, SIGTERM)`` — raises the same ``OSError`` family
    (``ProcessLookupError``/``PermissionError``) as before, so callers keep their
    existing "already gone / not ours" handling. Windows: degrades to ``taskkill``
    (the closest analogue; not exercised yet — kept guarded for the future
    native-Windows backend)."""
    if sys.platform == "win32":
        # portability: no os.kill(SIGTERM) on Windows — taskkill is the analogue.
        subprocess.run([_taskkill(), "/PID", str(pid)], check=False, capture_output=True)
        return
    os.kill(pid, signal.SIGTERM)


def _taskkill() -> str:
    """Absolute path to the Windows ``taskkill`` binary. Resolving it from
    ``%SystemRoot%\\System32`` rather than invoking ``taskkill`` by name keeps the
    Windows process-search order from picking up a same-named executable planted on
    PATH or in the working directory."""
    return os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "taskkill.exe")


def detach_kwargs() -> dict[str, object]:
    """``Popen`` kwargs that detach a child so it outlives its launcher.

    POSIX uses ``start_new_session``; Windows uses a new process group via
    ``creationflags`` (not exercised yet)."""
    if sys.platform == "win32":
        # portability: start_new_session is POSIX-only; CREATE_NEW_PROCESS_GROUP
        # is the Windows analogue.
        return {"creationflags": getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)}
    return {"start_new_session": True}  # portability: POSIX detach kwarg; Windows branch above
