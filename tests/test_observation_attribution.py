"""
U4 — attribute existing semantic observations to projects (plan R11).

An observation inherits the dominant project of its evidence sessions. When its
evidence is itself unattributed, the observation stays unattributed (R7). A
re-run does not overwrite an already-attributed observation.

Run: .venv/bin/python -m pytest tests/test_observation_attribution.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, attribution, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _project(con, pid, name):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)", (pid, None, name, "candidate", 0.8, _now()))
    return pid


def _session(con, title, project_id=None):
    sid = atoms.write_session(con, app="Word", title=title, started_at=_now())
    if project_id:
        con.execute("UPDATE session SET project_id=? WHERE id=?", (project_id, sid))
    return sid


def _observation(con, oid):
    con.execute(
        "INSERT INTO semantic_observation(id, observed_date, kind, semantic_key, "
        "summary, confidence, evidence_count, source_types, first_seen, last_seen, "
        "is_private, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "2026-09-16", "stage", "analysis", "Analysing", 0.9, 1, '["session"]',
         _now(), _now(), 0, "proposed", _now(), _now()))
    return oid


def _evidence(con, oid, sid):
    con.execute(
        "INSERT INTO semantic_evidence_local(observation_id, source_kind, source_id) "
        "VALUES (?,?,?)", (oid, "session", sid))


def _obs_project(con, oid):
    return con.execute(
        "SELECT project_id FROM semantic_observation WHERE id=?", (oid,)).fetchone()["project_id"]


# ── observation inherits its evidence sessions' project (R11) ────────────────────

def test_observation_inherits_project_from_evidence(env):
    con = _con()
    pid = _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "MADDs methodology", project_id=pid)
    oid = _observation(con, "o1")
    _evidence(con, oid, sid)
    stats = attribution.attribute_observations(con)
    assert stats["attributed"] == 1
    assert _obs_project(con, "o1") == pid


# ── unattributed evidence -> observation stays unattributed (R7) ─────────────────

def test_observation_unattributed_when_evidence_unattributed(env):
    con = _con()
    sid = _session(con, "Claude")  # no project_id
    oid = _observation(con, "o2")
    _evidence(con, oid, sid)
    stats = attribution.attribute_observations(con)
    assert stats["unattributed"] == 1
    assert _obs_project(con, "o2") is None


# ── re-run does not overwrite an already-attributed observation ──────────────────

def test_rerun_preserves_existing_observation_project(env):
    con = _con()
    pid = _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "MADDs methodology", project_id=pid)
    oid = _observation(con, "o3")
    _evidence(con, oid, sid)
    attribution.attribute_observations(con)
    # a different project now dominates evidence, but the obs is already set
    pid2 = _project(con, "p2", "Other Project")
    con.execute("UPDATE session SET project_id=? WHERE id=?", (pid2, sid))
    attribution.attribute_observations(con)  # only_unattributed=True default
    assert _obs_project(con, "o3") == pid
