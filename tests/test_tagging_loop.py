"""
Tests for the tag-the-untagged loop (core/learning.py).

retag_sessions is keyless (pure rule lookup); classify_untagged is the restored
"Loop B" and is tested with a monkeypatched LLM so it runs offline.

Run: python -m pytest tests/test_tagging_loop.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, db
from workpulse.core import learning


@pytest.fixture()
def env(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    cfg = {"paths": {"logs": str(tmp_path)},
           "streams": {"client-work": {"label": "Client work"}}}
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: cfg)
    # reset the module-level learned-rules cache so tests don't leak into each other
    learning._RULES_CACHE["mtime"] = 0.0
    learning._RULES_CACHE["rules"] = []
    return cfg


def _con():
    return db.connect(cfg={"paths": {}})


def _seed_untagged(con, title, minutes=30):
    now = datetime.now(timezone.utc)
    sid = atoms.write_session(con, app="Brave", title=title, stream=None,
                              started_at=now.isoformat())
    atoms.close_session(con, sid,
                        ended_at=(now + timedelta(minutes=minutes)).isoformat())
    return sid


def test_retag_applies_rules_retroactively(env):
    con = _con()
    _seed_untagged(con, "NKCC Q3 report - Word")
    _seed_untagged(con, "Random reddit thread")
    learning._add_rule(env, pattern="nkcc", stream="client-work",
                       raw_title="NKCC Q3 report", source="user")
    n = learning.retag_sessions(con, env)
    assert n == 1
    streams = sorted((r[0] or "∅") for r in
                     con.execute("SELECT stream FROM session").fetchall())
    assert streams == ["client-work", "∅"]   # NKCC tagged, reddit still untagged


def test_negative_rule_leaves_untagged(env):
    con = _con()
    _seed_untagged(con, "WhatsApp")
    learning._add_rule(env, pattern="whatsapp", stream=None,
                       raw_title="WhatsApp", source="user")
    assert learning.retag_sessions(con, env) == 0   # negative rule tags nothing


def test_classify_untagged_no_backend_is_noop(env, monkeypatch):
    con = _con()
    _seed_untagged(con, "NKCC Q3 report - Word")
    from workpulse.core import llm
    monkeypatch.setattr(llm, "active_backend", lambda cfg=None: "none")
    res = learning.classify_untagged(con, cfg=env)
    assert res == {"backend": "none", "classified": 0, "rules": 0, "retagged": 0}


def test_classify_untagged_learns_and_retags(env, monkeypatch):
    con = _con()
    _seed_untagged(con, "NKCC Q3 report - Word", minutes=60)
    from workpulse.core import llm
    monkeypatch.setattr(llm, "active_backend", lambda cfg=None: "anthropic")
    monkeypatch.setattr(
        llm, "ask_json",
        lambda prompt, **k: ({"stream": "client-work", "anchor": "nkcc"},
                             {"backend": "anthropic"}))
    res = learning.classify_untagged(con, cfg=env)
    assert res["classified"] >= 1
    assert res["retagged"] == 1
    assert any(r["stream"] == "client-work" for r in learning.load_learned_rules(env))
    streams = [r[0] for r in con.execute("SELECT stream FROM session").fetchall()]
    assert "client-work" in streams
