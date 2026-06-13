"""
Tests for scripts/name_clusters.py and skills/name-cluster.md.

LLM calls are monkeypatched; the real call is only smoke-tested by hand.

Verifies:
  - Fallback derives a non-empty name + oneliner from window titles.
  - User names are preserved (never overwritten by automated passes).
  - LLM names are preserved without --force; overwritten with --force.
  - LLM response parser handles the NAME/ONELINER line format.
  - skill_run is recorded for every call; ai_call only when LLM used.
  - --since filters by cluster ended_at.

Run: python -m pytest tests/test_name_clusters.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts import atoms, cluster, db, name_clusters as nc, think


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from scripts import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed_cluster(con, *, stream="dev", titles=None, when=None):
    when = when or datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    titles = titles or [("atoms.py — WorkPulse", 3),
                        ("db.py — WorkPulse",    2)]
    for i, (title, n) in enumerate(titles):
        for j in range(n):
            sid = atoms.write_session(con, app="Code", title=title,
                                      stream=stream,
                                      started_at=_iso(when + timedelta(minutes=i*5+j)))
            atoms.close_session(con, sid,
                                ended_at=_iso(when + timedelta(minutes=i*5+j+1)))
    cluster.refresh(con)
    row = con.execute("SELECT cluster_id FROM job_view LIMIT 1").fetchone()
    return row["cluster_id"]


# ── parser ───────────────────────────────────────────────────────────────────

def test_parse_response_extracts_both_lines():
    text = "NAME: WorkPulse Substrate Build\nONELINER: Mostly VS Code on atoms.py."
    n, o = nc._parse_response(text)
    assert n == "WorkPulse Substrate Build"
    assert o == "Mostly VS Code on atoms.py."


def test_parse_response_robust_to_extra_whitespace():
    text = "  NAME:   Foo Bar   \n  ONELINER:   sentence one   \nextra junk"
    n, o = nc._parse_response(text)
    assert n == "Foo Bar"
    assert o == "sentence one"


def test_parse_response_missing_oneliner():
    n, o = nc._parse_response("NAME: only name here")
    assert n == "only name here"
    assert o is None


# ── fallback ────────────────────────────────────────────────────────────────

def test_fallback_produces_non_empty_name(env):
    con = _con()
    cid = _seed_cluster(con, titles=[("Uganda MEMD draft narrative", 3),
                                     ("Uganda contact reports", 2)])
    r = nc.name_one(con, cid, force_fallback=True)
    assert not r["skipped"]
    assert r["source"] == "fallback"
    assert r["name"]
    # Top tokens were Uganda + memd + narrative; expect one of them to surface
    assert any(w.lower() in r["name"].lower()
               for w in ("uganda", "memd", "narrative", "contact"))


def test_fallback_records_skill_run_no_ai_call(env):
    con = _con()
    cid = _seed_cluster(con)
    r = nc.name_one(con, cid, force_fallback=True)
    sr = con.execute("SELECT * FROM skill_run WHERE id = ?",
                     (r["skill_run"],)).fetchone()
    assert sr is not None
    assert sr["skill_slug"] == "name-cluster"
    assert sr["status"] == "fallback"
    n_ai = con.execute("SELECT COUNT(*) AS n FROM ai_call").fetchone()["n"]
    assert n_ai == 0


# ── LLM path ────────────────────────────────────────────────────────────────

def test_llm_path_writes_name(env, monkeypatch):
    con = _con()
    cid = _seed_cluster(con)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg:
            ("NAME: WorkPulse Substrate Build\nONELINER: Mostly VS Code on atoms.py.",
             120, 22, 0.5),
    )
    r = nc.name_one(con, cid)
    assert r["source"] == "llm"
    assert r["name"] == "WorkPulse Substrate Build"
    row = con.execute(
        "SELECT * FROM cluster_name WHERE cluster_id = ?", (cid,)
    ).fetchone()
    assert row["source"] == "llm"
    assert row["confidence"] >= 0.5


def test_llm_path_records_ai_call(env, monkeypatch):
    con = _con()
    cid = _seed_cluster(con)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: X\nONELINER: y", 10, 5, 0.1),
    )
    nc.name_one(con, cid)
    ai = con.execute(
        "SELECT prompt_slug, in_tokens FROM ai_call ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert ai["prompt_slug"] == "skill:name-cluster"
    assert ai["in_tokens"] == 10


# ── precedence ──────────────────────────────────────────────────────────────

def test_user_name_is_preserved(env, monkeypatch):
    con = _con()
    cid = _seed_cluster(con)
    nc.set_user_name(con, cid, "My Custom Name", one_liner="my words")
    # LLM tries to override — should be skipped
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: AI Name\nONELINER: ai words", 1, 1, 0.0),
    )
    r = nc.name_one(con, cid)
    assert r["skipped"] is True
    row = con.execute(
        "SELECT name, source FROM cluster_name WHERE cluster_id = ?", (cid,)
    ).fetchone()
    assert row["name"] == "My Custom Name"
    assert row["source"] == "user"


def test_llm_name_kept_without_force(env, monkeypatch):
    con = _con()
    cid = _seed_cluster(con)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: First Pass\nONELINER: x", 1, 1, 0.0),
    )
    nc.name_one(con, cid)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: Second Pass\nONELINER: y", 1, 1, 0.0),
    )
    r = nc.name_one(con, cid)
    assert r["skipped"] is True
    row = con.execute(
        "SELECT name FROM cluster_name WHERE cluster_id = ?", (cid,)
    ).fetchone()
    assert row["name"] == "First Pass"


def test_llm_name_replaced_with_force(env, monkeypatch):
    con = _con()
    cid = _seed_cluster(con)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: First Pass\nONELINER: x", 1, 1, 0.0),
    )
    nc.name_one(con, cid)
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: ("NAME: Second Pass\nONELINER: y", 1, 1, 0.0),
    )
    r = nc.name_one(con, cid, force=True)
    assert not r["skipped"]
    row = con.execute(
        "SELECT name FROM cluster_name WHERE cluster_id = ?", (cid,)
    ).fetchone()
    assert row["name"] == "Second Pass"


# ── name_all ────────────────────────────────────────────────────────────────

def test_name_all_iterates_clusters(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _seed_cluster(con, stream="dev",  titles=[("dev work title", 3)], when=t0)
    _seed_cluster(con, stream="work", titles=[("work title here", 3)],
                  when=t0 + timedelta(hours=4))
    results = nc.name_all(con, force_fallback=True)
    named = [r for r in results if not r.get("skipped")]
    assert len(named) >= 2


def test_name_all_since_filter(env):
    con = _con()
    t0 = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)
    _seed_cluster(con, stream="dev", titles=[("a", 3)], when=t0)
    t1 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    _seed_cluster(con, stream="work", titles=[("b", 3)], when=t1)
    results = nc.name_all(con, since="2026-06-05", force_fallback=True)
    # Only the t1 cluster should be in scope
    named = [r for r in results if not r.get("skipped")]
    assert len(named) == 1
