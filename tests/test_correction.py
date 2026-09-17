"""
U5 — correction loop (plan R6, KTD5).

A user reassignment persists to project_correction, confirms the target project,
sets the session, generalizes to later similar sessions, and is never reverted
by an automatic pass.

Run: .venv/bin/python -m pytest tests/test_correction.py
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


def _project(con, pid, name, client=None, status="candidate"):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)", (pid, client, name, status, 0.8, _now()))
    return pid


def _session(con, title):
    return atoms.write_session(con, app="Word", title=title, started_at=_now())


def _sess_project(con, sid):
    return con.execute("SELECT project_id FROM session WHERE id=?", (sid,)).fetchone()["project_id"]


# ── correction sets the session, confirms the project, records the tuple ─────────

def test_correction_sets_session_and_confirms_project(env):
    con = _con()
    _project(con, "bd", "Business Dev")
    pid = _project(con, "carta", "Kenya Carta Esia", client="Verst Carbon")
    sid = _session(con, "CARTA financial proposal")
    con.execute("UPDATE session SET project_id='bd' WHERE id=?", (sid,))

    attribution.correct_session(con, sid, project_id=pid)

    assert _sess_project(con, sid) == pid
    status = con.execute("SELECT status FROM project WHERE id=?", (pid,)).fetchone()["status"]
    assert status == "confirmed"
    corr = con.execute(
        "SELECT from_project, to_project, target_kind FROM project_correction "
        "WHERE target_id=?", (sid,)).fetchone()
    assert corr["from_project"] == "bd"
    assert corr["to_project"] == pid
    assert corr["target_kind"] == "session"


# ── correcting to a new name creates a confirmed project ─────────────────────────

def test_correction_creates_new_confirmed_project(env):
    con = _con()
    sid = _session(con, "Solar water treatment concept note")
    pid = attribution.correct_session(con, sid, client="Verst Carbon",
                                      name="Solar Water Treatment")
    row = con.execute("SELECT client, name, status FROM project WHERE id=?", (pid,)).fetchone()
    assert (row["client"], row["name"], row["status"]) == (
        "Verst Carbon", "Solar Water Treatment", "confirmed")
    assert _sess_project(con, sid) == pid


# ── the correction generalizes to later similar sessions (AE4) ───────────────────

def test_correction_generalizes_to_similar_sessions(env):
    con = _con()
    sid = _session(con, "CARTA ESIA screening")
    attribution.correct_session(con, sid, client="Verst Carbon", name="Kenya Carta Esia")
    later = _session(con, "CARTA ESIA financial proposal draft")
    attribution.attribute_all(con)
    corrected_pid = _sess_project(con, sid)
    assert _sess_project(con, later) == corrected_pid


# ── an automatic re-run never reverts a corrected session (KTD5, R8) ─────────────

def test_full_rerun_does_not_revert_correction(env):
    con = _con()
    _project(con, "decoy", "Decoy Project With Many Tokens Here")
    sid = _session(con, "Decoy project tokens")  # would score to decoy
    pid = attribution.correct_session(con, sid, client="Client", name="Real Project")
    attribution.attribute_all(con, only_unattributed=False)  # aggressive re-run
    assert _sess_project(con, sid) == pid
