from __future__ import annotations

from pathlib import Path

from workpulse import desktop


def test_install_arguments_select_learning_device_home(monkeypatch):
    monkeypatch.setattr(desktop.sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", r"C:\Users\Student\AppData\Local")
    flavour = desktop._runtime_flavour([
        "--cli", "install", "--product", "learning", "--role", "device",
    ])
    assert flavour == ("learning", "device")
    assert desktop._default_home(flavour).parts[-2:] == ("Pulse", "LearningDevice")


def test_legacy_runtime_defaults_to_private_developer_lab(monkeypatch):
    monkeypatch.setattr(desktop.sys, "executable", "/Applications/WorkPulse.app/x")
    assert desktop._runtime_flavour([]) == ("developer", "lab")


def test_institution_runtime_has_isolated_home(monkeypatch):
    monkeypatch.setattr(
        desktop.sys, "executable",
        "/Applications/WorkPulse Institution.app/Contents/MacOS/WorkPulse",
    )
    assert desktop._runtime_flavour([]) == ("institution", "member")
