"""
Tests for POST /api/ask — the Ask WorkPulse endpoint (Phase 1c).

Covers the deterministic router and each branch (locate / retrospective /
general), keyless behaviour, and the personal-lock gating. Config + DB are
redirected to a tmp sandbox so the real config/DB are never touched.

Run: python -m pytest tests/test_ask_api.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import workpulse.web.app as appmod
from workpulse.core import atoms, db

CFG = {"streams": {"client-work": {"label": "Client work"}},
       "paths": {}, "llm": {"backend": "auto"}}


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(appmod, "load_config", lambda: CFG)
    con = db.connect(cfg={"paths": {}})
    now = datetime.now(timezone.utc)
    atoms.write_file_event(con, raw_path=r"C:\Users\pc\ClientZ\Proposal_v3.docx",
                           kind="modified", ts=now.isoformat())
    sid = atoms.write_session(con, app="Word", title="NKCC report",
                              stream="client-work",
                              started_at=(now - timedelta(days=1)).isoformat())
    atoms.close_session(
        con, sid,
        ended_at=(now - timedelta(days=1) + timedelta(minutes=60)).isoformat())
    return TestClient(appmod.app)


# ── router ────────────────────────────────────────────────────────────────────

def test_route_classification():
    assert appmod._ask_route("How do I make proposals") == "workflow"
    assert appmod._ask_route("how do i do proposals?") == "workflow"
    assert appmod._ask_route("How do I handle a concept note?") == "workflow"
    assert appmod._ask_route(
        "Tell me how I tend to work on proposals") == "workflow"
    assert appmod._ask_route("What is my proposal workflow?") == "workflow"
    assert appmod._ask_route(
        "What did I do on the proposal last week?") == "retrospective"
    assert appmod._ask_route("where is my client z proposal") == "locate"
    assert appmod._ask_route("find the budget spreadsheet") == "locate"
    assert appmod._ask_route("how did I work on the report last week") == "retrospective"
    assert appmod._ask_route("summarize what I did this week") == "retrospective"
    assert appmod._ask_route("How did I approach WorkPulse?") == "general"
    # month / "what happened" phrasings must reach the retrospective, not general
    assert appmod._ask_route("what happened in June") == "retrospective"
    assert appmod._ask_route("what was I doing in December") == "retrospective"
    assert appmod._ask_route("what is the state of the acme project") == "general"


# ── endpoint branches ─────────────────────────────────────────────────────────

def test_ask_locate_finds_file(client):
    r = client.post("/api/ask", json={"question": "where is my client z proposal"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "locate"
    assert any("Proposal_v3.docx" == e["basename"] for e in body["evidence"])


def test_ask_retrospective(client):
    r = client.post("/api/ask",
                    json={"question": "how did I do the client work last week"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "retrospective"
    assert "What you worked on" in body["answer"]
    assert "Client work" in body["answer"]


def test_ask_general_uses_think(client):
    r = client.post("/api/ask",
                    json={"question": "what is the state of the acme project"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "general"


def test_ask_proposal_method_uses_workflow_memory(client):
    con = db.connect(cfg={"paths": {}})
    now = datetime.now(timezone.utc)
    paths = [
        (8, "/Private/A/Financial Proposal.docx"),
        (7, "/Private/A/Financial Proposal v2.docx"),
        (6, "/Private/A/Financial Proposal FINAL.pdf"),
        (4, "/Private/B/Concept Note.docx"),
        (3, "/Private/B/Concept Note REVISED.docx"),
    ]
    for days, path in paths:
        atoms.write_file_event(
            con,
            raw_path=path,
            kind="modified",
            ts=(now - timedelta(days=days)).isoformat(),
        )

    r = client.post(
        "/api/ask",
        json={"question": "How do I make proposals", "use_ai": True},
    )
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "workflow"
    assert body["backend"] == "workflow-memory"
    assert "candidate pattern" in body["answer"]
    assert "proposal journeys" in body["answer"]
    assert "56 hours" not in body["answer"]
    assert body["evidence"]

    natural = client.post(
        "/api/ask",
        json={"question": "how do i do proposals?", "use_ai": True},
    )
    assert natural.status_code == 200, natural.text
    assert natural.json()["kind"] == "workflow"
    assert natural.json()["backend"] == "workflow-memory"


def test_ask_missing_question_is_400(client):
    r = client.post("/api/ask", json={})
    assert r.status_code == 400


def test_ask_keyless_backend_is_none(client):
    # No API key + no Ollama in tests -> backend reported as none, still answers.
    r = client.post("/api/ask", json={"question": "where is the proposal"})
    assert r.status_code == 200
    assert r.json()["backend"] == "none"


# ── personal lock ─────────────────────────────────────────────────────────────

def test_locate_locked_only_when_password_set(client):
    # Before a password is set, locate works (no lock).
    r = client.post("/api/ask", json={"question": "where is the proposal"})
    assert r.status_code == 200

    # After a password is set, private search requires unlock -> 401.
    from workpulse.core import personal
    con = db.connect(cfg={"paths": {}})
    personal.set_password(con, "1234")
    r2 = client.post("/api/ask", json={"question": "where is the proposal"})
    assert r2.status_code == 401
    assert r2.json().get("locked") is True

    # General questions are not path reads, so they stay open.
    r3 = client.post("/api/ask", json={"question": "what is the acme project state"})
    assert r3.status_code == 200


# ── "what have I worked on this week" must be a detailed, evidenced answer ─────

def test_learn_survives_null_streams_config(tmp_path, monkeypatch):
    # A real YAML gotcha: `streams:` with no value parses to None, so
    # cfg.get("streams", {}) returns None and `x not in None` used to 500.
    from fastapi.testclient import TestClient
    from workpulse.core import db as _db
    monkeypatch.setattr(_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    cfg = {"paths": {"logs": str(tmp_path)}, "streams": None, "llm": {}}
    monkeypatch.setattr(appmod, "load_config", lambda: cfg)
    con = _db.connect(cfg={"paths": {}})
    con.execute("INSERT OR IGNORE INTO stream(key,label,parent_key) "
                "VALUES ('consulting','consulting',NULL)")
    con.commit()
    client = TestClient(appmod.app)
    r = client.post("/api/learn",
                    json={"raw_title": "NKCC notes", "stream": "consulting"})
    assert r.status_code == 200, r.text
    assert r.json().get("ok") is True


def test_route_what_have_i_worked_on():
    assert appmod._ask_route("what have I worked on this week") == "retrospective"
    assert appmod._ask_route("what have i worked on recently") == "retrospective"
    assert appmod._ask_route("what have i been up to this week") == "retrospective"


def test_this_week_is_detailed_with_evidence(client):
    r = client.post("/api/ask",
                    json={"question": "what have I worked on this week"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["kind"] == "retrospective"
    assert body["total_seconds"] > 0            # the seeded session counts
    assert "What you worked on" in body["answer"]   # analysis, not a raw dump
    assert len(body["evidence"]) >= 1           # areas / files as evidence
