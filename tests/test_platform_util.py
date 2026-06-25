"""Tests for the cross-platform process primitives."""

from __future__ import annotations

import os

from automator import platform_util


def test_pid_alive_true_for_self():
    assert platform_util.pid_alive(os.getpid()) is True


def test_pid_alive_rejects_non_positive(monkeypatch):
    # 0/negative would target a process group via os.kill — the guard must short
    # out before os.kill is ever reached.
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(platform_util.os, "kill", _boom)
    assert platform_util.pid_alive(0) is False
    assert platform_util.pid_alive(-1) is False


def test_terminate_pid_rejects_non_positive(monkeypatch):
    def _boom(*_a, **_k):  # pragma: no cover - must not run
        raise AssertionError("os.kill must not be called for pid <= 0")

    monkeypatch.setattr(platform_util.os, "kill", _boom)
    platform_util.terminate_pid(0)  # no raise, no signal
    platform_util.terminate_pid(-42)
