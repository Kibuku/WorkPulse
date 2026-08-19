from __future__ import annotations

import sqlite3
from pathlib import Path

from workpulse import cli


def _database(path: Path, sessions: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(path)
    try:
        con.execute("CREATE TABLE session (id INTEGER PRIMARY KEY)")
        con.executemany("INSERT INTO session DEFAULT VALUES", [()] * sessions)
        con.commit()
    finally:
        con.close()


def test_personal_install_adopts_richer_legacy_mac_home(monkeypatch, tmp_path):
    home = tmp_path / "user"
    legacy = home / "WorkPulse"
    personal = home / "Library" / "Application Support" / "Pulse" / "Personal"
    _database(legacy / "workpulse.db", 4)
    _database(personal / "workpulse.db", 1)
    (legacy / "config").mkdir()
    (legacy / "config" / "projects.yaml").write_text("streams: [client]\n")

    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))

    assert cli._adopt_legacy_home(personal) is True
    assert cli._session_count(personal / "workpulse.db") == 4
    assert (personal / "config" / "projects.yaml").read_text() == "streams: [client]\n"
    assert (legacy / ".workpulse-adopted").exists()
    assert next((personal / "backups").glob("pre-adopt-*"))


def test_mac_legacy_candidates_include_previous_desktop_home(monkeypatch, tmp_path):
    home = tmp_path / "user"
    old_source = home / "WorkPulse"
    old_desktop = home / "Library" / "Application Support" / "Pulse" / "DeveloperLab"
    old_source.mkdir(parents=True)
    old_desktop.mkdir(parents=True)
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))

    assert cli._legacy_homes(home / "new-personal") == [old_source, old_desktop]


def test_stale_marker_for_another_destination_does_not_block_adoption(monkeypatch, tmp_path):
    home = tmp_path / "user"
    legacy = home / "WorkPulse"
    personal = home / "Library" / "Application Support" / "Pulse" / "Personal"
    _database(legacy / "workpulse.db", 7)
    (legacy / ".workpulse-adopted").write_text(
        "Adopted into /tmp/a-test-folder on 2026-01-01 00:00:00.\n"
    )
    monkeypatch.setattr(cli.sys, "platform", "darwin")
    monkeypatch.setattr(cli.Path, "home", classmethod(lambda cls: home))

    assert cli._adopt_legacy_home(personal) is True
    assert cli._session_count(personal / "workpulse.db") == 7
