"""
U4 — LLM-assist attribution on hard cases (plan R6/R8/R9/R12, KTD4/KTD5/KTD7).

After the deterministic floor, the model resolves residual unattributed sessions
using the discovered taxonomy. Titles are redacted before they leave; results
persist so re-runs make no new calls; corrections and clear sessions are never
sent; a provider error degrades to the unattributed bucket.

Run: .venv/bin/python -m pytest tests/test_attribution_llm.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, attribution, db, llm


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _project(con, pid, name, client=None):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)", (pid, client, name, "candidate", 0.5, _now()))
    return pid


def _session(con, title):
    return atoms.write_session(con, app="Word", title=title, started_at=_now())


def _pid(con, sid):
    return con.execute("SELECT project_id FROM session WHERE id=?", (sid,)).fetchone()["project_id"]


def _provider(monkeypatch, response):
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    calls = []

    def ask(prompt, **k):
        calls.append(prompt)
        return (response(len(calls)) if callable(response) else response), {"backend": "glm"}

    monkeypatch.setattr(llm, "ask_json", ask)
    return calls


# ── no provider -> no-op, residual stays unattributed (R4/AE1) ───────────────────

def test_assist_noop_without_provider(env):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "something the floor cannot place xyzzy")
    res = attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert res == {"attributed": 0, "llm_calls": 0}
    assert _pid(con, sid) is None


# ── resolves a hard case (R6) ────────────────────────────────────────────────────

def test_assist_resolves_hard_case(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "unplaceable jargon token qqq")  # deterministic can't place
    _provider(monkeypatch, {"project_id": "p1"})
    res = attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert res["attributed"] == 1 and res["llm_calls"] == 1
    assert _pid(con, sid) == "p1"


# ── already-attributed sessions are never sent (AE2) ─────────────────────────────

def test_assist_skips_already_attributed(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "already done")
    con.execute("UPDATE session SET project_id='p1' WHERE id=?", (sid,))
    calls = _provider(monkeypatch, {"project_id": "p1"})
    attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert calls == []  # nothing residual to ask about


# ── caching: duplicate titles cost one call (R8) ─────────────────────────────────

def test_assist_caches_by_title(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    _session(con, "same weird title")
    _session(con, "same weird title")
    calls = _provider(monkeypatch, {"project_id": "p1"})
    res = attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert res["attributed"] == 2 and len(calls) == 1  # one call, both attributed


# ── provider error -> unattributed, no raise (AE3/R5) ────────────────────────────

def test_assist_degrades_on_error(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "hard case here")
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))

    def boom(prompt, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(llm, "ask_json", boom)
    res = attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert res["attributed"] == 0
    assert _pid(con, sid) is None


# ── corrected sessions are never sent (R9) ───────────────────────────────────────

def test_assist_skips_corrected_sessions(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    sid = _session(con, "corrected but null")
    con.execute(
        "INSERT INTO project_correction(target_kind, target_id, to_project, corrected_at) "
        "VALUES ('session', ?, 'p1', ?)", (sid, _now()))
    calls = _provider(monkeypatch, {"project_id": "p1"})
    attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert calls == []


# ── payload is redacted before it leaves (R12) ───────────────────────────────────

def test_assist_redacts_title(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    _session(con, "Inbox - secret@example.com")
    from workpulse.core import content_capture
    monkeypatch.setattr(content_capture, "redact", lambda t, *a, **k: "REDACTED_MARK")
    calls = _provider(monkeypatch, {"project_id": None})
    attribution.llm_assist_attribution(con, cfg={"llm": {}})
    assert calls and "secret@example.com" not in calls[0]
    assert "REDACTED_MARK" in calls[0]


# ── the per-pass call cap bounds cost (R11) ──────────────────────────────────────

def test_assist_respects_max_calls(env, monkeypatch):
    con = _con()
    _project(con, "p1", "Verst Carbon Madds")
    _session(con, "distinct hard one")
    _session(con, "distinct hard two")
    _session(con, "distinct hard three")
    _provider(monkeypatch, {"project_id": "p1"})
    res = attribution.llm_assist_attribution(con, cfg={"llm": {}}, max_calls=1)
    assert res["llm_calls"] == 1
    assert res["attributed"] == 1  # only the one within the cap
