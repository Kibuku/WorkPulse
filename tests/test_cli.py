"""
Tests for the `workpulse` CLI (workpulse/cli.py) — the install entry point.

The system-mutating step (scheduler.install_all → launchd/schtasks) is mocked;
we verify the safe parts: config is seeded from the template, the DB is
initialised, install_all is invoked, and dispatch/exit codes behave.

Run: python -m pytest tests/test_cli.py
"""

from __future__ import annotations

import workpulse.cli as cli
import workpulse.core.db as db
import workpulse.ops.scheduler as scheduler


def test_install_seeds_config_and_invokes_agents(tmp_path, monkeypatch, capsys):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.example.yaml").write_text(
        "streams: {}\npaths:\n  logs: logs\n", encoding="utf-8")

    monkeypatch.setattr(cli, "ROOT", tmp_path)                 # config lands in tmp
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    called = {"install_all": 0}
    monkeypatch.setattr(scheduler, "install_all",
                        lambda: called.__setitem__("install_all", called["install_all"] + 1) or 0)

    rc = cli.main(["install"])

    assert rc == 0
    assert (cfgdir / "config.yaml").exists()                   # seeded from template
    assert called["install_all"] == 1                          # agents install attempted
    out = capsys.readouterr().out
    assert "created" in out and "workpulse web" in out         # user gets next steps


def test_install_uses_existing_config(tmp_path, monkeypatch, capsys):
    cfgdir = tmp_path / "config"
    cfgdir.mkdir()
    (cfgdir / "config.yaml").write_text("streams: {}\npaths:\n  logs: logs\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(scheduler, "install_all", lambda: 0)

    assert cli.main(["install"]) == 0
    assert "using existing" in capsys.readouterr().out


def test_no_command_prints_help_ok(capsys):
    assert cli.main([]) == 0
    assert "usage: workpulse" in capsys.readouterr().out


def test_install_propagates_agent_failure(tmp_path, monkeypatch):
    cfgdir = tmp_path / "config"; cfgdir.mkdir()
    (cfgdir / "config.example.yaml").write_text("streams: {}\npaths:\n  logs: logs\n", encoding="utf-8")
    monkeypatch.setattr(cli, "ROOT", tmp_path)
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(scheduler, "install_all", lambda: 1)   # an agent failed
    assert cli.main(["install"]) == 1                          # non-zero surfaces to caller
