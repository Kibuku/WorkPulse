"""
U1 — project attribution schema (migration 0011).

Proves the migration is additive and idempotent and that a session and a
semantic_observation can be linked to a project and read back. Robustness is
first-class (plan R8): re-running migrate() must not lose or change data.

Run: .venv/bin/python -m pytest tests/test_project_schema.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _cols(con, table):
    return {r["name"] for r in con.execute(f"SELECT name FROM pragma_table_info('{table}')")}


def _tables(con):
    return {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _insert_project(con, pid="p1", client="Verst Carbon", name="MADDs"):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pid, client, name, "candidate", 0.8, _now()),
    )
    return pid


def _insert_observation(con, oid="o1", project_id=None):
    con.execute(
        "INSERT INTO semantic_observation(id, observed_date, kind, semantic_key, "
        "summary, confidence, evidence_count, source_types, first_seen, last_seen, "
        "is_private, status, created_at, updated_at, project_id) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, "2026-09-16", "stage", "analysis", "Analysing", 0.9, 3, "[]",
         _now(), _now(), 0, "proposed", _now(), _now(), project_id),
    )
    return oid


# ── additive schema ───────────────────────────────────────────────────────────

def test_migration_creates_project_and_correction_tables(env):
    con = _con()
    tables = _tables(con)
    assert "project" in tables
    assert "project_correction" in tables


def test_session_and_observation_gain_project_id(env):
    con = _con()
    assert "project_id" in _cols(con, "session")
    assert "project_id" in _cols(con, "semantic_observation")


# ── idempotency (plan R8 / AE3) ────────────────────────────────────────────────

def test_remigration_is_a_noop(env):
    con = _con()  # applies through 0011
    assert db.migrate(con) == 0  # nothing new to apply on a second call


def test_remigration_preserves_existing_rows(env):
    con = _con()
    atoms.write_session(con, app="Word", title="MADDs proposal",
                        started_at=_now())
    _insert_project(con)
    _insert_observation(con)
    before = (
        con.execute("SELECT COUNT(*) c FROM session").fetchone()["c"],
        con.execute("SELECT COUNT(*) c FROM project").fetchone()["c"],
        con.execute("SELECT COUNT(*) c FROM semantic_observation").fetchone()["c"],
    )
    db.migrate(con)  # re-run
    after = (
        con.execute("SELECT COUNT(*) c FROM session").fetchone()["c"],
        con.execute("SELECT COUNT(*) c FROM project").fetchone()["c"],
        con.execute("SELECT COUNT(*) c FROM semantic_observation").fetchone()["c"],
    )
    assert before == after == (1, 1, 1)


# ── linking round-trips ─────────────────────────────────────────────────────────

def test_session_links_to_project_and_reads_back(env):
    con = _con()
    sid = atoms.write_session(con, app="Word", title="MADDs proposal",
                              started_at=_now())
    pid = _insert_project(con)
    con.execute("UPDATE session SET project_id=? WHERE id=?", (pid, sid))
    row = con.execute(
        "SELECT p.client, p.name FROM session s JOIN project p "
        "ON p.id = s.project_id WHERE s.id=?", (sid,)).fetchone()
    assert (row["client"], row["name"]) == ("Verst Carbon", "MADDs")


def test_observation_links_to_project_and_reads_back(env):
    con = _con()
    pid = _insert_project(con)
    oid = _insert_observation(con, project_id=pid)
    row = con.execute(
        "SELECT p.name FROM semantic_observation o JOIN project p "
        "ON p.id = o.project_id WHERE o.id=?", (oid,)).fetchone()
    assert row["name"] == "MADDs"
