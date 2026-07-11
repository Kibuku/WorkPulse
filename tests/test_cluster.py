"""
Tests for scripts/cluster.py and skills/cluster.md.

Verifies:
  - YAML frontmatter parses; defaults fill missing keys.
  - Sessions within max_gap form one cluster; gaps split clusters.
  - respect_stream=True keeps streams separate; False merges them.
  - cluster_untagged=False excludes stream IS NULL sessions.
  - min_cluster_seconds drops sub-threshold clusters.
  - Re-running refresh is idempotent (stable cluster ids).
  - job_view row has accurate totals + distinct apps JSON.

Run: python -m pytest tests/test_cluster.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cluster, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _add(con, *, app, title, stream, start, dur_min=5):
    sid = atoms.write_session(con, app=app, title=title, stream=stream,
                              started_at=_iso(start))
    end = start + timedelta(minutes=dur_min)
    atoms.close_session(con, sid, ended_at=_iso(end))
    return sid


# ── skill frontmatter ────────────────────────────────────────────────────────

def test_load_params_reads_frontmatter():
    p = cluster.load_params()
    assert p["max_gap_minutes"] == 30
    assert p["min_cluster_seconds"] == 60
    assert p["respect_stream"] is True
    assert p["cluster_untagged"] is True


def test_load_params_falls_back_on_missing(tmp_path):
    fake = tmp_path / "nope.md"
    p = cluster.load_params(skill_path=fake)
    assert p == cluster._DEFAULTS


# ── clustering algorithm ─────────────────────────────────────────────────────

def test_two_sessions_within_gap_cluster(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="Code", title="atoms.py", stream="dev", start=t0, dur_min=10)
    _add(con, app="Code", title="db.py",    stream="dev",
         start=t0 + timedelta(minutes=15), dur_min=10)
    s = cluster.refresh(con)
    assert s["clusters"] == 1
    assert s["sessions_assigned"] == 2


def test_large_gap_splits_cluster(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="Code", title="a", stream="dev", start=t0, dur_min=10)
    _add(con, app="Code", title="b", stream="dev",
         start=t0 + timedelta(hours=2), dur_min=10)
    s = cluster.refresh(con)
    assert s["clusters"] == 2


def test_different_streams_never_cluster(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="X", title="a", stream="dev",  start=t0, dur_min=10)
    _add(con, app="X", title="b", stream="work",
         start=t0 + timedelta(minutes=5), dur_min=10)
    s = cluster.refresh(con)
    assert s["clusters"] == 2


def test_respect_stream_false_merges_across_streams(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="X", title="a", stream="dev",  start=t0, dur_min=10)
    _add(con, app="X", title="b", stream="work",
         start=t0 + timedelta(minutes=5), dur_min=10)
    params = dict(cluster.load_params())
    params["respect_stream"] = False
    s = cluster.refresh(con, params=params)
    assert s["clusters"] == 1


def test_untagged_clusters_when_enabled(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="Claude", title="t1", stream=None, start=t0, dur_min=10)
    _add(con, app="Claude", title="t2", stream=None,
         start=t0 + timedelta(minutes=5), dur_min=10)
    s = cluster.refresh(con)
    assert s["untagged_clusters"] == 1


def test_cluster_untagged_false_excludes_them(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="X", title="a", stream=None, start=t0, dur_min=10)
    params = dict(cluster.load_params())
    params["cluster_untagged"] = False
    s = cluster.refresh(con, params=params)
    assert s["untagged_clusters"] == 0
    assert s["clusters"] == 0


def test_short_clusters_dropped(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    # 10-second session — under default 60s floor
    sid = atoms.write_session(con, app="X", title="blip", stream="dev",
                              started_at=_iso(t0))
    atoms.close_session(con, sid, ended_at=_iso(t0 + timedelta(seconds=10)))
    s = cluster.refresh(con)
    assert s["clusters"] == 0
    assert s["dropped_short"] == 1


def test_refresh_idempotent_stable_ids(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="X", title="a", stream="dev", start=t0, dur_min=10)
    _add(con, app="X", title="b", stream="dev",
         start=t0 + timedelta(minutes=5), dur_min=10)
    cluster.refresh(con)
    ids_1 = [r["cluster_id"] for r in con.execute(
        "SELECT cluster_id FROM job_view ORDER BY cluster_id")]
    cluster.refresh(con)
    ids_2 = [r["cluster_id"] for r in con.execute(
        "SELECT cluster_id FROM job_view ORDER BY cluster_id")]
    assert ids_1 == ids_2


def test_job_view_totals_and_apps(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="Code", title="a", stream="dev", start=t0, dur_min=10)
    _add(con, app="Word", title="b", stream="dev",
         start=t0 + timedelta(minutes=12), dur_min=10)
    cluster.refresh(con)
    row = con.execute("SELECT * FROM job_view").fetchone()
    assert row["session_count"] == 2
    # 10 + 10 = 20 minutes
    assert 1180 <= row["total_seconds"] <= 1220
    apps = sorted(["Code", "Word"])
    assert row["apps_seen"] == f'["{apps[0]}", "{apps[1]}"]'.replace('"', '"') or \
           sorted(eval(row["apps_seen"])) == apps


def test_jobs_for_stream(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _add(con, app="X", title="a", stream="dev",  start=t0, dur_min=10)
    _add(con, app="X", title="b", stream="work", start=t0 + timedelta(hours=2), dur_min=10)
    cluster.refresh(con)
    dev_jobs = cluster.jobs_for_stream(con, "dev")
    work_jobs = cluster.jobs_for_stream(con, "work")
    assert len(dev_jobs) == 1
    assert len(work_jobs) == 1
    assert dev_jobs[0]["stream"] == "dev"
