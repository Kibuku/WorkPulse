"""
Tests for `wp think` (PLAN.md §7 step 6).

The real LLM call is monkeypatched in every test so the suite runs offline
and deterministically. We test the contract — retrieval → prompt → output
parsing → skill_run recording — and verify the fallback path on its own.

Run: python -m pytest tests/test_think.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, db, search
from workpulse.core import think as tmod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _seed_uganda(con):
    now = datetime.now(timezone.utc)
    atoms.write_capture(con,
        body="Mwangi prefers the Mt. Elgon framing for the Uganda MEMD work.",
        ts=(now - timedelta(days=1)).isoformat())
    atoms.write_session(con, app="Word",
        title="Uganda MEMD draft narrative", stream="work",
        started_at=(now - timedelta(days=2)).isoformat())
    search.reindex(con)


# ── fallback path (no LLM) ───────────────────────────────────────────────────

def test_fallback_no_atoms(env):
    con = _con()
    search.reindex(con)
    r = tmod.think(con, "what about a topic nobody captured?", force_fallback=True)
    assert r["fallback"] is True
    assert "## Answer" in r["raw"]
    assert "## Gap" in r["raw"]
    assert "doesn't have what you're asking about" in r["raw"]
    assert r["atoms"] == []


def test_fallback_with_atoms_lists_ids(env):
    con = _con()
    _seed_uganda(con)
    r = tmod.think(con, "Uganda Elgon", force_fallback=True, limit=5)
    assert r["fallback"] is True
    # Each retrieved atom id appears in the rendered output
    for a in r["atoms"]:
        assert a["atom_id"] in r["raw"]
    assert "## Answer" in r["raw"]
    assert "## Gap" in r["raw"]


def test_fallback_records_skill_run(env):
    con = _con()
    _seed_uganda(con)
    r = tmod.think(con, "Uganda", force_fallback=True)
    row = con.execute("SELECT * FROM skill_run WHERE id = ?", (r["skill_run"],)).fetchone()
    assert row is not None
    assert row["skill_slug"] == "think"
    assert row["status"] == "fallback"
    assert row["model"] is None
    assert row["in_tokens"] == 0


def test_fallback_does_not_write_ai_call(env):
    con = _con()
    _seed_uganda(con)
    tmod.think(con, "Uganda", force_fallback=True)
    n = con.execute("SELECT COUNT(*) AS n FROM ai_call").fetchone()["n"]
    assert n == 0


def test_fallback_flags_missing_captures(env):
    """When the result set has zero captures, the fallback gap says so."""
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_session(con, app="X", title="Acme review", stream="work",
                       started_at=now.isoformat())
    search.reindex(con)
    r = tmod.think(con, "Acme", force_fallback=True)
    assert "No captures" in r["raw"]


def test_fallback_flags_untagged_atoms(env):
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_session(con, app="X", title="orphan thought",
                       stream=None, started_at=now.isoformat())
    atoms.write_capture(con, body="orphan thought again", ts=now.isoformat())
    search.reindex(con)
    r = tmod.think(con, "orphan", force_fallback=True)
    assert "untagged" in r["raw"].lower()


# ── LLM path (monkeypatched) ────────────────────────────────────────────────

def _fake_ask_text(text, *, backend="anthropic", model="claude-x",
                   in_tok=123, out_tok=45):
    def _ask(prompt, *, max_tokens=1024, cfg=None, model=None, feature=None):
        return text, {"backend": backend, "model": model or "claude-x",
                      "input_tokens": in_tok, "output_tokens": out_tok,
                      "duration_s": 1.5}
    return _ask


def test_llm_path_parses_sections(env, monkeypatch):
    con = _con()
    _seed_uganda(con)
    canned = (
        "## Answer\n\n"
        "You're working on the Uganda MEMD narrative [ABC]. Mwangi prefers "
        "the Mt. Elgon framing [DEF].\n\n"
        "## Gap\n\n"
        "No update since 2 days ago. The brain hasn't seen what you wrote "
        "yesterday.\n"
    )
    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text", _fake_ask_text(canned))
    r = tmod.think(con, "what's the state of Uganda?", model="claude-x")
    assert r["fallback"] is False
    assert r["model"] == "claude-x"
    assert "Uganda MEMD narrative" in r["answer"]
    assert "No update since" in r["gap"]


def test_llm_path_records_ai_call(env, monkeypatch):
    con = _con()
    _seed_uganda(con)
    canned = "## Answer\n\nx\n\n## Gap\n\ny\n"
    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text",
                        _fake_ask_text(canned, in_tok=100, out_tok=50))
    r = tmod.think(con, "ping", model="claude-x")
    ai = con.execute("SELECT * FROM ai_call ORDER BY ts DESC LIMIT 1").fetchone()
    assert ai["prompt_slug"] == "skill:think"


# ── provider routing (U3, AE4 routing half) ─────────────────────────────────────

def test_think_routes_through_qa_feature(env, monkeypatch):
    """think() calls llm.ask_text with feature='qa' and records the mocked
    provider's backend/model/token counts via _record."""
    con = _con()
    _seed_uganda(con)
    canned = "## Answer\n\nx\n\n## Gap\n\ny\n"
    captured = {}

    def spy_ask_text(prompt, *, max_tokens=1024, cfg=None, model=None, feature=None):
        captured["feature"] = feature
        return canned, {"backend": "glm", "model": "glm-4-flash",
                        "input_tokens": 10, "output_tokens": 5, "duration_s": 0.5}

    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text", spy_ask_text)
    r = tmod.think(con, "what's the state of Uganda?")
    assert captured["feature"] == "qa"
    assert r["fallback"] is False
    ai = con.execute("SELECT * FROM ai_call ORDER BY ts DESC LIMIT 1").fetchone()
    assert ai["provider"] == "glm" and ai["model"] == "glm-4-flash"
    assert ai["in_tokens"] == 10 and ai["out_tokens"] == 5
    sr = con.execute("SELECT status, model FROM skill_run WHERE id = ?",
                     (r["skill_run"],)).fetchone()
    assert sr["status"] == "ok"
    assert sr["model"] == "glm-4-flash"


def test_llm_failure_falls_back(env, monkeypatch):
    """If ask_text returns (None, meta) (no key, network down, all providers
    failed), think() must not raise — it falls back (KTD4)."""
    con = _con()
    _seed_uganda(con)
    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text",
                        lambda prompt, **k: (None, {"backend": "none"}))
    r = tmod.think(con, "Uganda")
    assert r["fallback"] is True
    assert "## Answer" in r["raw"] and "## Gap" in r["raw"]


def test_skill_file_is_in_prompt(env, monkeypatch):
    """The skill file must be loaded and prepended to the LLM prompt — the
    procedure lives in markdown, not in Python."""
    con = _con()
    _seed_uganda(con)
    captured = {}
    def _fake(prompt, *, max_tokens=1024, cfg=None, model=None, feature=None):
        captured["prompt"] = prompt
        return "## Answer\n\nok\n\n## Gap\n\n-\n", {"backend": "anthropic",
               "model": "claude-x", "input_tokens": 1, "output_tokens": 1,
               "duration_s": 0.1}
    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text", _fake)
    tmod.think(con, "Uganda", model="claude-x")
    p = captured["prompt"]
    assert "## Output contract" in p or "Answer" in p  # the skill content
    assert "USER QUESTION:" in p
    assert "ATOMS" in p


# ── redaction before any cloud call (U4, R7) ─────────────────────────────────────

def test_prompt_to_provider_is_redacted(env, monkeypatch):
    """Covers AE5: a sensitive substring in retrieved content is redacted in
    the prompt sent to the provider, but the returned atoms stay raw."""
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_capture(con, body="ping secret@example.com about the draft",
                        ts=now.isoformat())
    search.reindex(con)
    captured = {}

    def spy(prompt, *, max_tokens=1024, cfg=None, model=None, feature=None):
        captured["prompt"] = prompt
        return "## Answer\n\nok\n\n## Gap\n\n-\n", {"backend": "anthropic",
               "model": "claude-x", "input_tokens": 1, "output_tokens": 1,
               "duration_s": 0.1}

    import workpulse.core.llm as llm_mod
    monkeypatch.setattr(llm_mod, "ask_text", spy)
    r = tmod.think(con, "secret", model="claude-x")
    assert "secret@example.com" not in captured["prompt"]
    assert any("secret@example.com" in (a.get("content") or "")
              for a in r["atoms"])


def test_fallback_atoms_are_not_redacted(env):
    """The zero-key fallback never calls ask_text, so it must see raw
    content -- redaction only applies on the cloud-call path."""
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_capture(con, body="ping secret@example.com about the draft",
                        ts=now.isoformat())
    search.reindex(con)
    r = tmod.think(con, "secret", force_fallback=True)
    assert "secret@example.com" in r["raw"]
