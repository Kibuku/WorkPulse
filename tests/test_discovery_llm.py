"""
U3 — LLM taxonomy cleanup / naming (plan R7, R12, KTD7).

refine_taxonomy merges duplicate candidates and renames noisy ones using the
model, redacting each payload first. With no provider it is a no-op (local-first
parity, R4/AE1). Confirmed/dismissed projects are never touched.

Run: .venv/bin/python -m pytest tests/test_discovery_llm.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, db, discovery, llm


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
        "VALUES (?,?,?,?,?,?)", (pid, client, name, status, 0.5, _now()))
    return pid


def _row(con, pid):
    return con.execute("SELECT name, status FROM project WHERE id=?", (pid,)).fetchone()


# ── no provider -> no-op (R4/AE1) ────────────────────────────────────────────────

def test_refine_is_noop_without_provider(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Whatsapp Secure Reliable")
    # default config: no keys -> route resolves to 'none'
    res = discovery.refine_taxonomy(con, cfg={"llm": {}})
    assert res == {"renamed": 0, "merged": 0}
    assert _row(con, "p1")["name"] == "Whatsapp Secure Reliable"


# ── renames + merges under a provider (AE4) ──────────────────────────────────────

def test_refine_renames_and_merges(env, monkeypatch):
    con = _con()
    _project(con, "m1", "Verst Carbon Madds")
    _project(con, "m2", "Madds Zambia Baseline")
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (
        {"rename": [{"id": "m1", "name": "MADDs Kenya/Zambia", "client": "Verst Carbon"}],
         "merge": [{"from": "m2", "into": "m1"}]}, {"backend": "glm"}))
    res = discovery.refine_taxonomy(con, cfg={"llm": {}})
    assert res["renamed"] == 1 and res["merged"] == 1
    assert _row(con, "m1")["name"] == "MADDs Kenya/Zambia"
    assert _row(con, "m2")["status"] == "dismissed"


# ── confirmed projects are never modified ────────────────────────────────────────

def test_refine_leaves_confirmed_alone(env, monkeypatch):
    con = _con()
    _project(con, "c1", "Confirmed Thing", status="confirmed")
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (
        {"rename": [{"id": "c1", "name": "HACKED"}], "merge": []}, {}))
    discovery.refine_taxonomy(con, cfg={"llm": {}})
    assert _row(con, "c1")["name"] == "Confirmed Thing"


# ── malformed response leaves the taxonomy intact (KTD5) ─────────────────────────

def test_refine_survives_bad_response(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Whatsapp Secure Reliable")
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (None, {}))
    res = discovery.refine_taxonomy(con, cfg={"llm": {}})
    assert res == {"renamed": 0, "merged": 0}
    assert _row(con, "p1")["name"] == "Whatsapp Secure Reliable"


# ── the payload is redacted before it leaves (R12) ───────────────────────────────

def test_refine_redacts_payload(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Inbox mail note")
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    from workpulse.core import content_capture
    monkeypatch.setattr(content_capture, "redact", lambda t, *a, **k: "REDACTED_MARK")
    captured = {}

    def spy_ask_json(prompt, **k):
        captured["prompt"] = prompt
        return ({"rename": [], "merge": []}, {})

    monkeypatch.setattr(llm, "ask_json", spy_ask_json)
    discovery.refine_taxonomy(con, cfg={"llm": {}})
    assert "REDACTED_MARK" in captured["prompt"]
