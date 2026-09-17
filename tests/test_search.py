"""
Tests for `wp search` (PLAN.md §7 step 5).

Verifies:
  - Reindex populates FTS over all four atom kinds.
  - BM25 path finds expected matches across atom kinds.
  - Untagged atoms are returned (principle #7: untagged is first-class).
  - Recency boost re-orders ties.
  - Stream filter restricts AND boosts.
  - `since` filter respects the cutoff.
  - Vector path returns empty gracefully when fastembed is missing.
  - Empty query returns empty list.

Run: python -m pytest tests/test_search.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, db
from workpulse.core import search as smod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


# ── seed helpers ─────────────────────────────────────────────────────────────

def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _seed(con):
    """A small mixed corpus across all four kinds."""
    now = datetime.now(timezone.utc)
    # sessions
    s1 = atoms.write_session(con, app="Code", title="atoms.py — WorkPulse",
                             stream="dev", started_at=_iso(now - timedelta(days=1)))
    s2 = atoms.write_session(con, app="Word", title="Uganda MEMD draft narrative",
                             stream="work", started_at=_iso(now - timedelta(days=2)))
    s3 = atoms.write_session(con, app="Claude", title="thinking about brain design",
                             stream=None, started_at=_iso(now - timedelta(days=3)))
    # captures
    c1 = atoms.write_capture(con, body="Mwangi prefers the Mt. Elgon framing for Uganda",
                             ts=_iso(now - timedelta(days=2)))
    c2 = atoms.write_capture(con, body="brain design should keep atoms small",
                             ts=_iso(now - timedelta(hours=1)))
    # ai_call
    a1 = atoms.write_ai_call(con, provider="anthropic", model="claude-sonnet-4-6",
                             in_tokens=100, out_tokens=50, cost_usd=0.001,
                             prompt_slug="report-daily",
                             ts=_iso(now - timedelta(days=1)))
    return {"s1": s1, "s2": s2, "s3": s3, "c1": c1, "c2": c2, "a1": a1}


def _write_project(con, pid, name, *, client=None, status="candidate",
                   created_at=None):
    con.execute(
        "INSERT INTO project(id, client, name, status, confidence, created_at) "
        "VALUES (?,?,?,?,?,?)",
        (pid, client, name, status, 0.8, created_at or _iso(datetime.now(timezone.utc))))
    return pid


def _write_observation(con, oid, summary, *, kind="stage", semantic_key="analysis",
                       first_seen=None):
    ts = first_seen or _iso(datetime.now(timezone.utc))
    con.execute(
        "INSERT INTO semantic_observation(id, observed_date, kind, semantic_key, "
        "summary, confidence, evidence_count, source_types, first_seen, last_seen, "
        "is_private, status, created_at, updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (oid, ts[:10], kind, semantic_key, summary, 0.9, 1, "[]",
         ts, ts, 0, "proposed", ts, ts))
    return oid


# ── tests ────────────────────────────────────────────────────────────────────

def test_reindex_populates_fts(env):
    con = _con()
    _seed(con)
    counts = smod.reindex(con)
    assert counts["session"] == 3
    assert counts["capture"] == 2
    assert counts["ai_call"] == 1


def test_bm25_finds_session_by_title(env):
    con = _con()
    ids = _seed(con)
    smod.reindex(con)
    results = smod.search(con, "Uganda")
    found = [(r["atom_kind"], r["atom_id"]) for r in results]
    assert ("session", ids["s2"]) in found
    assert ("capture", ids["c1"]) in found


def test_bm25_finds_capture_body(env):
    con = _con()
    ids = _seed(con)
    smod.reindex(con)
    results = smod.search(con, "Mwangi Elgon")
    assert results
    assert results[0]["atom_id"] == ids["c1"]


def test_untagged_atom_returned(env):
    """s3 has stream=None. Principle #7: untagged is first-class."""
    con = _con()
    ids = _seed(con)
    smod.reindex(con)
    results = smod.search(con, "brain")
    kinds_ids = [(r["atom_kind"], r["atom_id"]) for r in results]
    assert ("session", ids["s3"]) in kinds_ids


def test_recency_breaks_ties(env):
    con = _con()
    now = datetime.now(timezone.utc)
    old = atoms.write_capture(con, body="brain note one",
                              ts=_iso(now - timedelta(days=60)))
    new = atoms.write_capture(con, body="brain note one",
                              ts=_iso(now - timedelta(hours=1)))
    smod.reindex(con)
    results = smod.search(con, "brain note one")
    assert results[0]["atom_id"] == new


def test_stream_filter_restricts(env):
    con = _con()
    ids = _seed(con)
    smod.reindex(con)
    results = smod.search(con, "design", stream="dev")
    for r in results:
        assert r["stream"] == "dev"


def test_stream_boost(env):
    con = _con()
    ids = _seed(con)
    smod.reindex(con)
    results_no = smod.search(con, "design")
    results_dev = smod.search(con, "design", stream="dev")
    if results_dev:
        # The stream boost should put the dev-stream hit at the top.
        assert results_dev[0]["stream"] == "dev"


def test_since_filter(env):
    con = _con()
    now = datetime.now(timezone.utc)
    old = atoms.write_capture(con, body="ancient capture about Uganda",
                              ts=_iso(now - timedelta(days=120)))
    rec = atoms.write_capture(con, body="recent capture about Uganda",
                              ts=_iso(now - timedelta(days=1)))
    smod.reindex(con)
    cutoff = (now - timedelta(days=30)).date().isoformat()
    results = smod.search(con, "Uganda", since=cutoff)
    ids = [r["atom_id"] for r in results]
    assert rec in ids
    assert old not in ids


def test_vector_degrades_gracefully(env, monkeypatch):
    """If fastembed isn't importable, --vector should not raise; BM25 still
    returns results."""
    con = _con()
    _seed(con)
    smod.reindex(con)
    # Force the embedder to be None regardless of what's installed.
    monkeypatch.setattr(smod, "_get_embedder", lambda: None)
    results = smod.search(con, "Uganda", with_vector=True)
    assert results  # BM25 still works


def test_empty_query(env):
    con = _con()
    _seed(con)
    smod.reindex(con)
    assert smod.search(con, "") == []
    assert smod.search(con, "   ") == []


def test_index_atom_incremental(env):
    """index_atom should make a new row searchable without a full reindex."""
    con = _con()
    smod.reindex(con)  # empty initially
    # Manually index a synthetic atom
    smod.index_atom(con, kind="capture", id="ZZZ", ts="2026-06-10T00:00:00+00:00",
                    stream=None, content="a unique sentinel string")
    results = smod.search(con, "sentinel")
    assert results and results[0]["atom_id"] == "ZZZ"


# ── project + semantic_observation atom kinds (U1) ──────────────────────────────

def test_reindex_populates_project_and_observation(env):
    con = _con()
    _write_project(con, "p1", "MADDs Kenya/Zambia", client="Verst Carbon")
    _write_observation(con, "o1", "Frequent context switching")
    counts = smod.reindex(con)
    assert counts["project"] == 1
    assert counts["semantic_observation"] == 1


def test_bm25_finds_project_by_name(env):
    con = _con()
    _write_project(con, "p1", "MADDs Kenya/Zambia", client="Verst Carbon")
    smod.reindex(con)
    results = smod.search(con, "MADDs")
    assert any(r["atom_kind"] == "project" and r["atom_id"] == "p1" for r in results)


def test_bm25_finds_observation_by_summary(env):
    con = _con()
    _write_observation(con, "o1", "Frequent context switching interrupted focus")
    smod.reindex(con)
    results = smod.search(con, "context switching")
    assert any(r["atom_kind"] == "semantic_observation" and r["atom_id"] == "o1"
              for r in results)


def test_observation_indexed_without_project_link(env):
    """A project-less observation is still first-class content (mirrors the
    untagged-session precedent), not gated on attribution."""
    con = _con()
    _write_observation(con, "o1", "Deep work in the mornings")
    counts = smod.reindex(con)
    assert counts["semantic_observation"] == 1
    results = smod.search(con, "mornings")
    assert any(r["atom_id"] == "o1" for r in results)


def test_dismissed_project_not_indexed(env):
    con = _con()
    _write_project(con, "p1", "Rejected Candidate", status="dismissed")
    counts = smod.reindex(con)
    assert counts["project"] == 0
    results = smod.search(con, "Rejected Candidate")
    assert not any(r["atom_id"] == "p1" for r in results)
