"""
Tests for core/cleanup.prune_untracked_file_events — the file-event noise prune.

A v1 migration drags in millions of ~/Library churn events for locations v2's
watch_roots exclude. The prune drops exactly those and keeps everything under a
watched root. file_event_local must cascade, and an empty watch_roots must be a
refusal (never delete everything).

Run: python -m pytest tests/test_cleanup.py
"""

from __future__ import annotations

import os

import pytest

from workpulse.core import cleanup, db


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    return db.connect(cfg={"paths": {}})


def _fe(con, fid, path):
    con.execute("INSERT INTO file_event(id, ts, path_hash, kind) VALUES (?,?,?,?)",
                (fid, "2026-07-14T09:00:00+00:00", fid, "modified"))
    con.execute("INSERT INTO file_event_local(file_event_id, raw_path) VALUES (?,?)",
                (fid, path))
    con.commit()


CFG = {"watcher": {"watch_roots": ["~/Documents", "~/Library/CloudStorage"]}}


def test_prunes_untracked_keeps_watched(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/Clients/Acme/proposal.docx")     # keep
    _fe(con, "a2", f"{home}/Library/CloudStorage/OneDrive/report.xlsx")  # keep
    _fe(con, "b1", f"{home}/Library/Application Support/Claude/Cache/blob")  # drop
    _fe(con, "b2", f"{home}/Library/Preferences/com.apple.foo.plist")   # drop
    _fe(con, "b3", f"{home}/Library/Biome/sessions/x/bookmark")         # drop

    # Dry run: report only, nothing deleted.
    dry = cleanup.prune_untracked_file_events(con, cfg=CFG, dry_run=True)
    assert dry["deleted"] == 3 and dry["kept"] == 2
    assert con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0] == 5

    # Real run.
    res = cleanup.prune_untracked_file_events(con, cfg=CFG)
    assert res["deleted"] == 3 and res["kept"] == 2
    kept = {r[0] for r in con.execute("SELECT id FROM file_event")}
    assert kept == {"a1", "a2"}
    # file_event_local for the dropped ones is gone (cascade / explicit delete).
    assert con.execute("SELECT COUNT(*) FROM file_event_local").fetchone()[0] == 2
    assert con.execute(
        "SELECT COUNT(*) FROM file_event_local WHERE file_event_id='b1'").fetchone()[0] == 0


def test_idempotent(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/x.txt")
    _fe(con, "b1", f"{home}/Library/Caches/y")
    cleanup.prune_untracked_file_events(con, cfg=CFG)
    # Second run finds nothing more to do.
    again = cleanup.prune_untracked_file_events(con, cfg=CFG)
    assert again["deleted"] == 0 and again["kept"] == 1


def test_refuses_when_no_watch_roots(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/x.txt")
    res = cleanup.prune_untracked_file_events(con, cfg={"watcher": {"watch_roots": []}})
    assert "error" in res and res["deleted"] == 0
    assert con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0] == 1  # untouched
