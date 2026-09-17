"""
U7 — by-project surfacing (plan R13, surfaces R4/R5).

project_time aggregates time/activity by project; project_detail drills down to
deliverable level and evidence. The web layer exposes these plus the correction
action.

Run: .venv/bin/python -m pytest tests/test_projects_api.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, attribution, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _project(con, pid, name, client=None):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)", (pid, client, name, "candidate", 0.8,
                                datetime.now(timezone.utc).isoformat()))
    return pid


def _session(con, title, project_id, *, start, minutes):
    started = start.isoformat()
    ended = (start + timedelta(minutes=minutes)).isoformat()
    sid = atoms.write_session(con, app="Word", title=title, started_at=started)
    con.execute("UPDATE session SET ended_at=?, project_id=? WHERE id=?",
                (ended, project_id, sid))
    return sid


# ── project_time aggregates hours + sessions by project (R13) ─────────────────────

def test_project_time_groups_by_project(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _project(con, "madds", "Verst Carbon Madds", client="Verst Carbon")
    _project(con, "diss", "Dissertation")
    _session(con, "MADDs a", "madds", start=now, minutes=60)
    _session(con, "MADDs b", "madds", start=now, minutes=30)
    _session(con, "Diss ch3", "diss", start=now, minutes=45)

    rows = attribution.project_time(con)
    by_id = {r["project_id"]: r for r in rows}
    assert by_id["madds"]["sessions"] == 2
    assert round(by_id["madds"]["minutes"]) == 90
    assert by_id["diss"]["sessions"] == 1
    # ordered by minutes desc
    assert rows[0]["project_id"] == "madds"


def test_project_time_excludes_unattributed(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _project(con, "madds", "Verst Carbon Madds")
    _session(con, "MADDs a", "madds", start=now, minutes=60)
    atoms.write_session(con, app="Claude", title="Claude", started_at=now.isoformat())  # unattributed
    rows = attribution.project_time(con)
    assert len(rows) == 1 and rows[0]["project_id"] == "madds"


# ── project_time honours the day window ──────────────────────────────────────────

def test_project_time_respects_days_window(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _project(con, "madds", "Verst Carbon Madds")
    _session(con, "recent", "madds", start=now - timedelta(days=1), minutes=30)
    _session(con, "old", "madds", start=now - timedelta(days=40), minutes=30)
    rows = attribution.project_time(con, days=7)
    assert rows[0]["sessions"] == 1  # only the recent one


# ── project_detail drills to deliverable level (R4) ──────────────────────────────

def test_project_detail_lists_deliverables(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _project(con, "madds", "Verst Carbon Madds")
    _session(con, "MADDs Engagement Letter", "madds", start=now, minutes=60)
    _session(con, "MADDs Engagement Letter", "madds", start=now, minutes=20)
    _session(con, "MADDs Methodology", "madds", start=now, minutes=15)
    detail = attribution.project_detail(con, "madds")
    titles = {d["title"]: d for d in detail["deliverables"]}
    assert titles["MADDs Engagement Letter"]["sessions"] == 2
    assert detail["project"]["name"] == "Verst Carbon Madds"


# ── the web layer exposes the by-project endpoint ────────────────────────────────

def test_projects_endpoint_returns_json(env, monkeypatch):
    con = _con()
    now = datetime.now(timezone.utc)
    _project(con, "madds", "Verst Carbon Madds")
    _session(con, "MADDs a", "madds", start=now, minutes=60)

    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    from fastapi.testclient import TestClient
    from workpulse.web import app as webapp
    monkeypatch.setattr(webapp, "load_config", lambda: {"paths": {}}, raising=False)
    client = TestClient(webapp.app)
    resp = client.get("/api/projects")
    assert resp.status_code == 200
    data = resp.json()
    assert any(p["project_id"] == "madds" for p in data["projects"])
