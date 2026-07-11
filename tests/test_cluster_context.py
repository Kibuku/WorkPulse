"""
Tests for scripts/cluster_context.py — Fix B in the dashboard saga.

The point of cluster_context is to convert "Claude 38 min" into "Claude 38 min —
building WorkPulse v2 substrate (4 commits, 12 captures)" by joining sessions
× git × captures × skill_runs deterministically.

Git interaction is monkeypatched per-test so the suite doesn't depend on this
project's commit history.

Run: python -m pytest tests/test_cluster_context.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cluster, db
from workpulse.core import cluster_context as cc


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


def _seed_cluster(con, start, end_offset_min=30):
    """One simple cluster: 30 min of Code on dev."""
    end = start + timedelta(minutes=end_offset_min)
    for i in range(end_offset_min):
        sid = atoms.write_session(
            con, app="Code", title="atoms.py", stream="dev",
            started_at=_iso(start + timedelta(minutes=i)),
        )
        atoms.close_session(con, sid,
                            ended_at=_iso(start + timedelta(minutes=i + 1)))
    cluster.refresh(con)
    row = con.execute(
        "SELECT cluster_id, started_at, ended_at FROM job_view"
    ).fetchone()
    return row


# ── range resolution ────────────────────────────────────────────────────────

def test_unknown_cluster_returns_empty(env):
    con = _con()
    out = cc.cluster_context(con, cluster_id="NOPE")
    assert out["captures"] == []
    assert out["git_commits"] == []
    assert out["summary"] == ""


# ── captures joining ────────────────────────────────────────────────────────

def test_captures_in_range_surface(env):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    atoms.write_capture(con, body="thought during the cluster",
                        ts=_iso(t0 + timedelta(minutes=5)))
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=[])
    bodies = [c["body"] for c in out["captures"]]
    assert "thought during the cluster" in bodies


def test_captures_pinned_to_session_surface_even_if_outside_range(env):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    session_id = con.execute(
        "SELECT id FROM session WHERE cluster_id = ? LIMIT 1",
        (row["cluster_id"],),
    ).fetchone()["id"]
    # Capture written OUTSIDE the time window but PINNED to the session
    atoms.write_capture(
        con, body="pinned thought outside window",
        pinned_kind="session", pinned_id=session_id,
        ts=_iso(t0 + timedelta(hours=10)),
    )
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=[])
    bodies = [c["body"] for c in out["captures"]]
    assert "pinned thought outside window" in bodies


# ── git ─────────────────────────────────────────────────────────────────────

def test_git_commits_between_empty_when_not_a_repo(tmp_path):
    fake = tmp_path / "fake-repo"
    fake.mkdir()
    assert cc.git_commits_between("2026-01-01T00:00:00+00:00",
                                  "2026-12-31T00:00:00+00:00", fake) == []


def test_git_commits_are_joined_into_context(env, monkeypatch):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    fake_commits = [
        {"repo": "WorkPulse", "sha": "abc1234567",
         "ts": _iso(t0 + timedelta(minutes=10)),
         "author": "George", "subject": "v2 step 8: dream cycle"},
        {"repo": "WorkPulse", "sha": "def8901234",
         "ts": _iso(t0 + timedelta(minutes=20)),
         "author": "George", "subject": "v2 step 8b: launchd wiring"},
    ]
    monkeypatch.setattr(cc, "git_commits_between",
                        lambda s, e, r: fake_commits)
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=["x"])
    assert len(out["git_commits"]) == 2
    subjects = [c["subject"] for c in out["git_commits"]]
    assert "v2 step 8: dream cycle" in subjects


# ── skill runs ──────────────────────────────────────────────────────────────

def test_skill_runs_in_range(env):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    # Drop 3 think runs + 1 report run in the range
    for i in range(3):
        con.execute(
            """
            INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                                  in_tokens, out_tokens, cost_usd, input, output, status)
            VALUES (?, ?, 'think', NULL, 'claude-x', 100, 50, 0.001, 'q', 'a', 'ok')
            """,
            (atoms.new_id(), _iso(t0 + timedelta(minutes=i))),
        )
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd, input, output, status)
        VALUES (?, ?, 'report-daily', NULL, 'claude-x', 200, 100, 0.002, 'q', 'a', 'ok')
        """,
        (atoms.new_id(), _iso(t0 + timedelta(minutes=15))),
    )
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=[])
    slugs = {s["slug"] for s in out["skill_runs"]}
    assert slugs == {"think", "report-daily"}
    think = next(s for s in out["skill_runs"] if s["slug"] == "think")
    assert think["count"] == 3
    assert think["total_in_tokens"] == 300


# ── file events ─────────────────────────────────────────────────────────────

def test_file_events_in_range_grouped_by_extension(env):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    for i, path in enumerate(["/x/a.py", "/x/b.py", "/x/c.md", "/x/d.py"]):
        atoms.write_file_event(con, raw_path=path, kind="modified",
                               ts=_iso(t0 + timedelta(minutes=i)))
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=[])
    assert out["file_events"]["total"] == 4
    ext_map = dict(out["file_events"]["by_extension"])
    assert ext_map.get("py") == 3
    assert ext_map.get("md") == 1


# ── summary line synthesis ──────────────────────────────────────────────────

def test_summary_empty_when_no_signal(env):
    line = cc.summary_line({"captures": [], "git_commits": [], "skill_runs": [],
                            "file_events": {"total": 0, "by_extension": []}})
    assert line == ""


def test_summary_leads_with_commit_subject():
    line = cc.summary_line({
        "captures": [],
        "git_commits": [{"subject": "Build the dream cycle", "sha": "x"}],
        "skill_runs": [], "file_events": {"total": 0, "by_extension": []},
    })
    assert line == "Build the dream cycle"


def test_summary_appends_capture_count():
    line = cc.summary_line({
        "captures": [
            {"author": "human", "body": "x"},
            {"author": "human", "body": "y"},
        ],
        "git_commits": [{"subject": "Fix N", "sha": "z"}],
        "skill_runs": [], "file_events": {"total": 0, "by_extension": []},
    })
    assert "2 captures" in line
    assert line.startswith("Fix N")


def test_summary_system_captures_only_when_alone():
    line = cc.summary_line({
        "captures": [{"author": "system", "body": "x"}],
        "git_commits": [], "skill_runs": [],
        "file_events": {"total": 0, "by_extension": []},
    })
    assert "1 system trace" in line


def test_summary_includes_file_extensions_when_many_events():
    line = cc.summary_line({
        "captures": [], "git_commits": [], "skill_runs": [],
        "file_events": {"total": 25,
                        "by_extension": [("py", 18), ("md", 7)]},
    })
    assert "25 file events" in line
    assert "py" in line


# ── end-to-end ──────────────────────────────────────────────────────────────

def test_end_to_end_enrichment(env, monkeypatch):
    con = _con()
    t0 = datetime(2026, 6, 16, 21, 0, tzinfo=timezone.utc)
    row = _seed_cluster(con, t0)
    atoms.write_capture(con, body="captured during work",
                        ts=_iso(t0 + timedelta(minutes=5)))
    monkeypatch.setattr(cc, "git_commits_between",
                        lambda s, e, r: [{
                            "repo": "WorkPulse", "sha": "abc1234567",
                            "ts": _iso(t0 + timedelta(minutes=3)),
                            "author": "G", "subject": "Hook up the brain"}])
    out = cc.cluster_context(con, cluster_id=row["cluster_id"], repos=["x"])
    # Summary should lead with the commit subject + mention the human capture
    assert out["summary"].startswith("Hook up the brain")
    assert "1 capture" in out["summary"]
