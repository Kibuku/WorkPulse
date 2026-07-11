"""
Tests for PLAN.md §7 step 7c — lock-screen sensor filter + cleanup pass.

The macOS APIs (CGSessionCopyCurrentDictionary etc.) aren't exercised here
because they require PyObjC + a real macOS session. We test:
  - is_lockscreen_app() is a pure function and handles the known names.
  - prune_lockscreen() deletes the right sessions + cascades and is safe
    on an already-clean DB (idempotent).
  - rebuild_search_and_clusters() restores a consistent state after prune.

Run: python -m pytest tests/test_lockscreen_filter.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cleanup, cluster, db, search


# ── is_lockscreen_app (pure function — can run on any platform) ──────────────

@pytest.mark.parametrize("name", [
    "loginwindow", "LoginWindow", "Lock Screen", "lock screen",
    "ScreenSaverEngine", "screensaver",
])
def test_is_lockscreen_app_recognises(name):
    pytest.importorskip("workpulse.signals.activity_mac",
                        reason="activity_mac requires PyObjC")
    from workpulse.signals.activity_mac import is_lockscreen_app
    assert is_lockscreen_app(name) is True


@pytest.mark.parametrize("name", [
    "Code", "Microsoft Outlook", "Claude", "Safari", "", None, "loginwindow_legit",
])
def test_is_lockscreen_app_passes_real_apps(name):
    pytest.importorskip("workpulse.signals.activity_mac",
                        reason="activity_mac requires PyObjC")
    from workpulse.signals.activity_mac import is_lockscreen_app
    assert is_lockscreen_app(name) is False


# ── prune_lockscreen ─────────────────────────────────────────────────────────

@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


def _seed_mixed(con):
    """Three legit sessions + four contaminated ones."""
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    for i, (app, title, stream) in enumerate([
        ("Code", "atoms.py", "dev"),
        ("Word", "narrative.docx", "work"),
        ("Claude", "thinking", None),
    ]):
        sid = atoms.write_session(con, app=app, title=title, stream=stream,
                                  started_at=_iso(t0 + timedelta(minutes=i*2)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i*2 + 1)))
    for i, app in enumerate(["loginwindow", "loginwindow",
                              "ScreenSaverEngine", "Lock Screen"]):
        sid = atoms.write_session(con, app=app, title=app, stream=None,
                                  started_at=_iso(t0 + timedelta(minutes=10 + i*2)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=10 + i*2 + 1)))


def test_prune_deletes_lockscreen_sessions(env):
    con = _con()
    _seed_mixed(con)
    counts = cleanup.prune_lockscreen(con)
    assert counts["sessions"] == 4
    n_remaining = con.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
    assert n_remaining == 3
    # All cascades hit
    n_local = con.execute(
        "SELECT COUNT(*) AS n FROM session_local"
    ).fetchone()["n"]
    assert n_local == 3


def test_prune_dry_run_reports_without_deleting(env):
    con = _con()
    _seed_mixed(con)
    counts = cleanup.prune_lockscreen(con, dry_run=True)
    assert counts["sessions"] == 4
    assert counts["dry_run"] is True
    # Nothing actually removed
    n = con.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
    assert n == 7


def test_prune_idempotent(env):
    con = _con()
    _seed_mixed(con)
    cleanup.prune_lockscreen(con)
    counts2 = cleanup.prune_lockscreen(con)
    assert counts2["sessions"] == 0


def test_prune_drops_fts_rows(env):
    con = _con()
    _seed_mixed(con)
    search.reindex(con)
    fts_before = con.execute(
        "SELECT COUNT(*) AS n FROM search_fts WHERE atom_kind='session'"
    ).fetchone()["n"]
    assert fts_before == 7
    cleanup.prune_lockscreen(con)
    fts_after = con.execute(
        "SELECT COUNT(*) AS n FROM search_fts WHERE atom_kind='session'"
    ).fetchone()["n"]
    assert fts_after == 3


def test_prune_drops_edges(env):
    con = _con()
    _seed_mixed(con)
    edges_before = con.execute(
        "SELECT COUNT(*) AS n FROM edge WHERE src_kind='session'"
    ).fetchone()["n"]
    cleanup.prune_lockscreen(con)
    # All 4 loginwindow sessions had at least an in_app edge each
    edges_after = con.execute(
        "SELECT COUNT(*) AS n FROM edge WHERE src_kind='session'"
    ).fetchone()["n"]
    assert edges_after < edges_before


def test_rebuild_after_prune(env):
    con = _con()
    _seed_mixed(con)
    cleanup.prune_lockscreen(con)
    counts = cleanup.rebuild_search_and_clusters(con)
    # 3 legit sessions, 1 minute each, three different streams → 0 clusters
    # (each below 60s floor). But search index should hold all 3 atoms.
    assert counts["search_indexed"] >= 3
    # cluster.refresh ran without raising
    assert "clusters" in counts
