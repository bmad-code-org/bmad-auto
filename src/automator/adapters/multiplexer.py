"""Terminal-multiplexer seam.

The coding-CLI adapter (:class:`~.base.CodingCLIAdapter`) abstracts *which CLI*
to drive and how its prompts/hooks work. This module abstracts the orthogonal
**transport** axis: how sessions, windows, and panes are created, observed, and
torn down. Today the only backend is tmux (:class:`~.tmux_backend.TmuxMultiplexer`);
the seam exists so a future non-POSIX backend (an eventual native-Windows "psmux")
can slot in without the rest of the codebase shelling out to ``tmux`` directly.

``TerminalMultiplexer`` is the contract a backend author implements. Operation
names mirror today's call sites verbatim so the migration is mechanical. Get the
process-wide backend through :func:`get_multiplexer`.
"""

from __future__ import annotations

import functools
from abc import ABC, abstractmethod
from pathlib import Path


class MultiplexerError(Exception):
    """A transport-backend operation failed. Backends raise a subclass (e.g.
    :class:`~.tmux_backend.TmuxError`) so call sites can catch the seam-level type
    without importing a backend."""


class TerminalMultiplexer(ABC):
    """Transport backend for agent sessions: sessions, windows, and clients.

    A backend must shell out to (or otherwise drive) exactly one multiplexer and
    nothing else — it is the single place POSIX-shell / tmux knowledge is allowed
    to live. The full surface below is the contract; Phase 1 wired only the subset
    the generic adapter needs, and Phase 2 fills in the rest as ``runs.py``,
    ``tui/launch.py``, ``probe.py``, and ``tui/data.py`` migrate onto it.
    """

    # ----------------------------------------------------------- sessions

    @abstractmethod
    def has_session(self, name: str) -> bool:
        """True iff a session named exactly ``name`` exists."""

    @abstractmethod
    def new_session(
        self, name: str, cwd: Path, cols: int | None = None, lines: int | None = None
    ) -> None:
        """Create a detached session with a single shell window rooted at ``cwd``.
        When ``cols``/``lines`` are given the session is pinned to that geometry
        (agent sessions are observed detached, so their pane size must be fixed);
        omit both for a session whose size is irrelevant (e.g. the control session,
        which is only ever attached, and an attaching client resizes it anyway)."""

    @abstractmethod
    def kill_session(self, name: str) -> None:
        """Kill the named session (tolerant of it already being gone)."""

    @abstractmethod
    def list_sessions(self) -> list[str]:
        """Names of all live sessions."""

    @abstractmethod
    def session_options(self, option: str) -> dict[str, str]:
        """Map of session name -> value of ``option`` across all sessions."""

    @abstractmethod
    def set_session_option(self, name: str, option: str, value: str) -> None:
        """Set a user option on the named session."""

    # ------------------------------------------------------------ windows

    @abstractmethod
    def new_window(
        self, session: str, name: str, cwd: Path, env: dict[str, str], command: str
    ) -> str:
        """Create a window running ``command`` (with ``env`` layered on) in
        ``session``, rooted at ``cwd``. Returns the backend-native window id."""

    @abstractmethod
    def new_parked_window(
        self, session: str, name: str, cwd: Path, argv: list[str], return_opt: str
    ) -> str:
        """Create a window that runs ``argv`` then *parks* — waiting on a key so
        the exit status stays inspectable instead of the window closing the moment
        the process exits — and finally returns an attached client to its origin
        (keyed by the per-window ``return_opt``). Returns the native window id."""

    @abstractmethod
    def list_window_ids(self, session: str) -> list[str]:
        """Native ids of every window in ``session`` (empty if it is gone)."""

    @abstractmethod
    def list_windows(self, session: str, fields: list[str]) -> list[tuple[str, ...]]:
        """One tuple per window in ``session``, each holding the requested
        backend fields in order."""

    @abstractmethod
    def window_alive(self, session: str, window_id: str) -> bool:
        """True iff ``window_id`` is still a window of ``session``."""

    @abstractmethod
    def kill_window(self, target: str) -> None:
        """Kill the targeted window (tolerant of it already being gone)."""

    @abstractmethod
    def select_window(self, target: str) -> None:
        """Make ``target`` the current window of its session."""

    @abstractmethod
    def set_window_option(self, target: str, option: str, value: str) -> None:
        """Set a user option on the targeted window."""

    @abstractmethod
    def unset_window_option(self, target: str, option: str) -> None:
        """Remove a user option from the targeted window (so a later read sees it
        as unset, not as an empty value)."""

    @abstractmethod
    def show_window_option(self, target: str, option: str) -> str:
        """Value of a user option on the targeted window ('' if unset)."""

    @abstractmethod
    def pipe_pane(self, window_id: str, log_file: Path) -> None:
        """Tee the window's pane output to ``log_file`` (tolerant of the window
        having already died)."""

    @abstractmethod
    def send_text(self, window_id: str, text: str) -> None:
        """Send ``text`` literally to the window, then submit it (Enter)."""

    # ----------------------------------------------------- client / attach

    @abstractmethod
    def attach_target_argv(self, target: str) -> list[str]:
        """argv that attaches the caller's terminal to ``target``."""

    @abstractmethod
    def current_pane_id(self) -> str | None:
        """Native id of the pane this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_window_id(self) -> str | None:
        """Native id of the window this process runs in, or None when not inside
        the multiplexer."""

    @abstractmethod
    def current_session(self) -> str | None:
        """Name of the session this process runs in, or None when not inside the
        multiplexer."""

    @abstractmethod
    def detach_client(self) -> None:
        """Detach the client viewing the current session."""

    @abstractmethod
    def switch_client(self, target: str, last_fallback: bool = False) -> bool:
        """Switch the current client to ``target`` (optionally falling back to
        the last client on failure). Returns True iff a switch happened."""

    @abstractmethod
    def available(self) -> bool:
        """True iff this backend can run on the current host (e.g. its binary is
        on PATH)."""


@functools.lru_cache(maxsize=1)
def get_multiplexer() -> TerminalMultiplexer:
    """Return the process-wide terminal multiplexer. The seam where a backend
    would later be selected by policy; today it is always tmux."""
    from .tmux_backend import TmuxMultiplexer  # lazy import: avoid a cycle

    return TmuxMultiplexer()
