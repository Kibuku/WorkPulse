"""
U3 — semantic attribution engine.

Assigns each session to a project with confidence + evidence, or to the
unattributed bucket when signal is insufficient (plan R1/R3/R5/R7, KTD4).
Deterministic scorer is the local-first floor: it must attribute clear matches
with no LLM and no network.

Run: .venv/bin/python -m pytest tests/test_attribution.py
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


def _project(con, pid, client, name):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pid, client, name, "candidate", 0.8, _now()))
    return pid


def _session(con, title, app="Microsoft Word"):
    return atoms.write_session(con, app=app, title=title, started_at=_now())


# ── attributes a clear match (AE1, R1) ──────────────────────────────────────────

def test_attributes_session_to_matching_project(env):
    con = _con()
    pid = _project(con, "p-madds", "Verst Carbon", "Verst Carbon Madds")
    sid = _session(con, "Verst Carbon_Development of MADDs in Kenya_Engagement")
    decision = attribution.attribute_session(con, sid)
    assert decision is not None
    assert decision["project_id"] == pid
    assert 0.0 <= decision["confidence"] <= 1.0
    assert decision["evidence"]  # non-empty (R5)


# ── generic-only title -> unattributed (AE2, R7) ────────────────────────────────

def test_generic_title_is_unattributed(env):
    con = _con()
    _project(con, "p-madds", "Verst Carbon", "Verst Carbon Madds")
    sid = _session(con, "Claude", app="Claude")
    assert attribution.attribute_session(con, sid) is None


# ── token-variant match still resolves (R3 floor) ───────────────────────────────

def test_partial_token_match_resolves(env):
    con = _con()
    pid = _project(con, "p-carta", "Verst Carbon", "Kenya Carta Esia")
    sid = _session(con, "CARTA ESIA financial proposal draft")
    decision = attribution.attribute_session(con, sid)
    assert decision is not None and decision["project_id"] == pid


# ── the best of several projects wins ────────────────────────────────────────────

def test_picks_best_scoring_project(env):
    con = _con()
    _project(con, "p-madds", "Verst Carbon", "Verst Carbon Madds")
    pid = _project(con, "p-diss", None, "Dissertation Dispatch Analysis")
    sid = _session(con, "Dissertation chapter 3 dispatch analysis")
    decision = attribution.attribute_session(con, sid)
    assert decision["project_id"] == pid


# ── apply writes project_id; unattributed leaves it NULL ─────────────────────────

def test_attribute_all_writes_links_and_bucket(env):
    con = _con()
    _project(con, "p-madds", "Verst Carbon", "Verst Carbon Madds")
    good = _session(con, "MADDs Kenya methodology")
    generic = _session(con, "Claude", app="Claude")
    stats = attribution.attribute_all(con)
    assert stats["attributed"] == 1
    assert stats["unattributed"] == 1
    gp = con.execute("SELECT project_id FROM session WHERE id=?", (good,)).fetchone()["project_id"]
    up = con.execute("SELECT project_id FROM session WHERE id=?", (generic,)).fetchone()["project_id"]
    assert gp == "p-madds"
    assert up is None


# ── deterministic floor works with no semantic backend (KTD4/R9) ────────────────

def test_baseline_attributes_without_backend(env):
    con = _con()
    _project(con, "p-wp", None, "Workpulse Njiani")
    sid = _session(con, "index.html — Njiani", app="Code")
    # conftest disables ollama and no key is set: pure deterministic path.
    decision = attribution.attribute_session(con, sid)
    assert decision is not None and decision["project_id"] == "p-wp"
