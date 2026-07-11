"""
Tests for scripts/platform_util.py — the cross-platform OS dispatcher.

The OS-touching paths (osascript/schtasks/toast) can't be meaningfully
unit-tested off their target platform, so we test the pure dispatch logic
and the process-matching, mocking subprocess/psutil where needed.

Run: python -m pytest tests/test_platform_util.py
"""

from __future__ import annotations

import sys
from pathlib import Path


import pytest

from workpulse import platform_util as pu


# ── platform predicates ─────────────────────────────────────────────────────

def test_is_mac_and_windows_are_exclusive(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    assert pu.is_mac() is True
    assert pu.is_windows() is False
    monkeypatch.setattr(sys, "platform", "win32")
    assert pu.is_mac() is False
    assert pu.is_windows() is True


# ── count_processes ─────────────────────────────────────────────────────────

class _FakeProc:
    def __init__(self, cmdline):
        self.info = {"cmdline": cmdline}


def test_count_processes_matches_both_launch_forms(monkeypatch):
    procs = [
        _FakeProc(["python", "-m", "workpulse.signals.activity"]),          # -m form
        _FakeProc(["python", "/x/scripts/activity.py"]),          # .py form
        _FakeProc(["python", "-m", "workpulse.signals.watcher"]),           # different module
        _FakeProc(["python", "-m", "workpulse.signals.activity", "status"]),# excluded (status)
        _FakeProc(["something", "else"]),                          # unrelated
    ]
    monkeypatch.setattr(pu.psutil, "process_iter", lambda attrs=None: iter(procs))
    assert pu.count_processes("activity") == 2   # -m + .py, status excluded
    assert pu.count_processes("watcher") == 1


def test_count_processes_handles_windows_path_separator(monkeypatch):
    procs = [_FakeProc(["python", "C:\\wp\\scripts\\activity.py"])]
    monkeypatch.setattr(pu.psutil, "process_iter", lambda attrs=None: iter(procs))
    assert pu.count_processes("activity") == 1


def test_count_processes_survives_access_errors(monkeypatch):
    class _Boom:
        @property
        def info(self):
            raise pu.psutil.AccessDenied()
    procs = [_Boom(), _FakeProc(["python", "-m", "workpulse.signals.watcher"])]
    monkeypatch.setattr(pu.psutil, "process_iter", lambda attrs=None: iter(procs))
    assert pu.count_processes("watcher") == 1


# ── notify dispatch ─────────────────────────────────────────────────────────

def test_notify_uses_osascript_on_mac(monkeypatch):
    calls = {}
    monkeypatch.setattr(sys, "platform", "darwin")
    def _fake_run(args, **kw):
        calls["args"] = args
        class R: returncode = 0
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    assert pu.notify("Title", "Body") is True
    assert calls["args"][0] == "osascript"
    assert "display notification" in " ".join(calls["args"])


def test_notify_uses_powershell_on_windows(monkeypatch):
    calls = {}
    monkeypatch.setattr(sys, "platform", "win32")
    def _fake_run(args, **kw):
        calls["args"] = args
        class R: returncode = 0
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    assert pu.notify("Title", "Body") is True
    assert calls["args"][0] == "powershell"
    assert "ToastNotification" in " ".join(calls["args"])


def test_notify_returns_false_on_unknown_platform(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    assert pu.notify("t", "m") is False


# ── agent_last_exit dispatch ────────────────────────────────────────────────

def test_agent_last_exit_parses_launchctl(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    def _fake_run(args, **kw):
        class R:
            returncode = 0
            stdout = ("id" if args[0] == "id" else
                      "\tstate = running\n\tlast exit code = 0\n")
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    loaded, code = pu.agent_last_exit("nightly")
    assert loaded is True
    assert code == 0


def test_agent_last_exit_launchctl_not_loaded(monkeypatch):
    monkeypatch.setattr(sys, "platform", "darwin")
    def _fake_run(args, **kw):
        class R:
            returncode = 1 if args[0] == "launchctl" else 0
            stdout = "0" if args[0] == "id" else ""
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    loaded, code = pu.agent_last_exit("nightly")
    assert loaded is False


def test_agent_last_exit_parses_schtasks(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    def _fake_run(args, **kw):
        class R:
            returncode = 0
            stdout = ("TaskName: com.workpulse.nightly\n"
                      "Last Result:  0\n"
                      "Status: Ready\n")
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    loaded, code = pu.agent_last_exit("nightly")
    assert loaded is True
    assert code == 0


def test_agent_last_exit_schtasks_nonzero(monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    def _fake_run(args, **kw):
        class R:
            returncode = 0
            stdout = "Last Result:  267011\n"
        return R()
    monkeypatch.setattr(pu.subprocess, "run", _fake_run)
    loaded, code = pu.agent_last_exit("nightly")
    assert loaded is True
    assert code == 267011
