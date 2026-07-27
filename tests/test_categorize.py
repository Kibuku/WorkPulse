"""
Tests for scripts/categorize.py (Fix 1.7).

Run: python -m pytest tests/test_categorize.py
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cluster, db
from workpulse.core import capture as capmod, categorize as cat


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    monkeypatch.setattr(capmod, "ROOT", tmp_path)
    def _cap_dir():
        p = tmp_path / "captures"; p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(capmod, "_captures_dir", _cap_dir)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


def _seed_cluster_in_stream(con, *, stream, title="atoms.py", app="Code",
                            start=None):
    """Make a 30-minute cluster in `stream`. Returns its cluster_id."""
    start = start or datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    for i in range(30):
        sid = atoms.write_session(
            con, app=app, title=title, stream=stream,
            started_at=_iso(start + timedelta(minutes=i)),
        )
        atoms.close_session(con, sid,
                            ended_at=_iso(start + timedelta(minutes=i + 1)))
    cluster.refresh(con)
    if stream is None:
        row = con.execute(
            "SELECT cluster_id FROM job_view WHERE stream IS NULL "
            "ORDER BY started_at DESC LIMIT 1",
        ).fetchone()
    else:
        row = con.execute(
            "SELECT cluster_id FROM job_view WHERE stream = ? "
            "ORDER BY started_at DESC LIMIT 1", (stream,),
        ).fetchone()
    assert row is not None, f"no cluster materialized for stream={stream!r}"
    return row["cluster_id"]


# ── daily candidates ────────────────────────────────────────────────────────

def test_extract_daily_candidates_from_capture_edges(env):
    con = _con()
    # Capture mentions Uganda AND Mercy Corps — both get edges via the
    # routing hook in capture.capture()
    capmod.capture(
        body="Today: Uganda MEMD ER schedule, Mercy Corps stakeholder mapping",
        author="human", cfg={"paths": {}},
    )
    n = cat.extract_daily_candidates(con)
    assert n >= 2
    today = datetime.now(timezone.utc).date().isoformat()
    streams = cat.daily_candidates_for(con, today)
    assert "uganda" in streams
    assert "uganda2" in streams


def test_extract_daily_candidates_is_idempotent(env):
    con = _con()
    capmod.capture(body="Uganda MEMD work", author="human", cfg={"paths": {}})
    first = cat.extract_daily_candidates(con)
    second = cat.extract_daily_candidates(con)
    assert first >= 1
    assert second == 0


# ── signal gathering ────────────────────────────────────────────────────────

def test_signals_pick_up_existing_session_stream(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream="dev")
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    signals = cat.gather_signals(con, cid, meta["started_at"], meta["ended_at"])
    assert "dev" in signals
    assert any(sig[0] == "existing_stream" for sig in signals["dev"])


def test_signals_pick_up_file_path_match(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    # File event with a Uganda path during the cluster window
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    ts = (datetime.fromisoformat(meta["started_at"]) + timedelta(minutes=5)).isoformat()
    atoms.write_file_event(
        con, raw_path="/Users/g/Documents/Uganda-MEMD/narrative.docx",
        kind="modified", ts=ts,
    )
    signals = cat.gather_signals(con, cid, meta["started_at"], meta["ended_at"])
    assert "uganda" in signals
    assert any(s[0] == "file_path_match" for s in signals["uganda"])


def test_signals_pick_up_capture_about_stream(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    # Get one of the cluster's sessions and pin a capture to it
    sid = con.execute(
        "SELECT id FROM session WHERE cluster_id = ? LIMIT 1", (cid,),
    ).fetchone()["id"]
    capmod.capture(
        body="Mercy Corps stakeholder review", author="human",
        pinned_kind="session", pinned_id=sid, cfg={"paths": {}},
    )
    signals = cat.gather_signals(con, cid, meta["started_at"], meta["ended_at"])
    assert "uganda2" in signals
    assert any(s[0] == "capture_about" for s in signals["uganda2"])


# ── assignment ──────────────────────────────────────────────────────────────

def test_assign_leaves_competing_file_paths_unclassified(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    # Repeated saves are not independent proof. Competing project paths make
    # this interval ambiguous, so it must remain unclassified.
    base = datetime.fromisoformat(meta["started_at"])
    for i, path in enumerate([
        "/Documents/Uganda-MEMD/a.docx",
        "/Documents/Uganda-MEMD/b.docx",
        "/Documents/Mercy-Corps/c.docx",
    ]):
        atoms.write_file_event(
            con, raw_path=path, kind="modified",
            ts=_iso(base + timedelta(minutes=i + 1)),
        )
    res = cat.assign_cluster(con, cid)
    assert not res["skipped"]
    assert res["stream"] == "misc"
    assert res["source"] == "fallback"
    assert res["confidence"] < 0.75


def test_assigns_single_unambiguous_file_path(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    meta = con.execute(
        "SELECT started_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    base = datetime.fromisoformat(meta["started_at"])
    for minute in (1, 2, 3):
        atoms.write_file_event(
            con, raw_path="/Documents/Uganda-MEMD/a.docx", kind="modified",
            ts=_iso(base + timedelta(minutes=minute)),
        )

    res = cat.assign_cluster(con, cid, allow_ai=False)

    assert res["stream"] == "uganda"
    assert res["source"] == "agent"
    assert len([e for e in res["evidence"]
                if e["signal"] == "file_path_match"]) == 1


def test_assign_falls_back_to_misc_with_no_signal(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None, title="generic chrome",
                                  app="App")
    res = cat.assign_cluster(con, cid)
    assert res["stream"] == "misc"
    assert res["source"] == "fallback"


def test_assign_uses_ai_for_ambiguous_cluster_when_confident(env, monkeypatch):
    con = _con()
    cid = _seed_cluster_in_stream(
        con, stream=None, title="methodology review", app="Microsoft Word",
        start=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    monkeypatch.setattr(cat.llm, "active_backend",
                        lambda cfg=None: "ollama")
    monkeypatch.setattr(
        cat.llm, "ask_json",
        lambda *args, **kwargs: (
            {"stream": "uganda", "confidence": 0.91,
             "reason": "Methodology work matches this project."},
            {"backend": "ollama", "model": "llama3.2:3b"},
        ),
    )
    cfg = {"llm": {"auto_tagging": {
        "enabled": True,
        "ambiguity_threshold": 0.75,
        "auto_assign_threshold": 0.85,
    }}}
    res = cat.assign_cluster(con, cid, cfg=cfg)
    assert res["stream"] == "uganda"
    assert res["source"] == "agent"
    assert res["confidence"] == 0.91
    assert res["evidence"][0]["backend"] == "ollama"


def test_assign_rejects_low_confidence_ai_suggestion(env, monkeypatch):
    con = _con()
    cid = _seed_cluster_in_stream(
        con, stream=None, title="generic chrome", app="App",
        start=datetime.now(timezone.utc) - timedelta(hours=1),
    )
    monkeypatch.setattr(cat.llm, "active_backend",
                        lambda cfg=None: "ollama")
    monkeypatch.setattr(
        cat.llm, "ask_json",
        lambda *args, **kwargs: (
            {"stream": "uganda", "confidence": 0.61,
             "reason": "Weak match."},
            {"backend": "ollama", "model": "llama3.2:3b"},
        ),
    )
    cfg = {"llm": {"auto_tagging": {
        "enabled": True,
        "auto_assign_threshold": 0.85,
    }}}
    res = cat.assign_cluster(con, cid, cfg=cfg)
    assert res["stream"] == "misc"
    assert res["source"] == "fallback"


def test_assign_respects_user_override(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    cat.correct_assignment(con, cid, to_stream="masters", by="user")
    # Agent re-run shouldn't touch it
    res = cat.assign_cluster(con, cid)
    assert res["skipped"] is True
    assert res["stream"] == "masters"
    row = con.execute(
        "SELECT source FROM cluster_assignment WHERE cluster_id = ?", (cid,),
    ).fetchone()
    assert row["source"] == "user"


def test_assign_force_overrides_agent_but_not_user(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream="dev")
    res1 = cat.assign_cluster(con, cid)
    assert res1["source"] == "agent"
    # User now corrects
    cat.correct_assignment(con, cid, to_stream="uganda")
    # Force re-run — still user (force-protected)
    res2 = cat.assign_cluster(con, cid, force=True)
    assert res2["skipped"] is True


def test_assign_writes_skill_run_with_agent_slug(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream="dev")
    res = cat.assign_cluster(con, cid)
    sr = con.execute(
        "SELECT skill_slug FROM skill_run WHERE id = ?", (res["skill_run"],),
    ).fetchone()
    assert sr["skill_slug"] == "categorize"


# ── teach-correct: corrections ──────────────────────────────────────────────

def test_correct_records_from_and_to(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream="dev")
    cat.assign_cluster(con, cid)  # agent first
    res = cat.correct_assignment(con, cid, to_stream="uganda")
    assert res["from_stream"] == "dev"
    assert res["to_stream"] == "uganda"
    row = con.execute(
        "SELECT * FROM cluster_correction WHERE cluster_id = ?", (cid,),
    ).fetchone()
    assert row["from_stream"] == "dev"
    assert row["to_stream"] == "uganda"
    assert row["signals_snapshot"]  # JSON evidence preserved for Teacher


def test_correct_overwrites_assignment_to_user_source(env):
    con = _con()
    cid = _seed_cluster_in_stream(con, stream="dev")
    cat.correct_assignment(con, cid, to_stream="masters")
    row = con.execute(
        "SELECT stream, source, confidence FROM cluster_assignment "
        "WHERE cluster_id = ?", (cid,),
    ).fetchone()
    assert row["stream"] == "masters"
    assert row["source"] == "user"
    assert row["confidence"] == 1.0


# ── daily_candidate bonus ───────────────────────────────────────────────────

def test_assign_uses_daily_candidate_as_tie_breaker(env):
    con = _con()
    # Seed a cluster TODAY with no other signal
    today = datetime.now(timezone.utc).replace(hour=10, minute=0, second=0, microsecond=0)
    cid = _seed_cluster_in_stream(con, stream=None, start=today,
                                  title="generic", app="App")
    # Declare today: Uganda + Mercy Corps via the capture pattern
    capmod.capture(
        body="Today: Uganda MEMD and Mercy Corps",
        author="human", cfg={"paths": {}},
    )
    res = cat.assign_cluster(con, cid)
    # A plan containing two projects says both are possible, not which one owns
    # this generic interval. Taxonomy order must not become a false fact.
    assert res["stream"] == "misc"
    assert res["source"] == "fallback"
    assert res["confidence"] == 0.5


# ── assign_all ──────────────────────────────────────────────────────────────

def test_signals_pick_up_browser_visit(env):
    """A browser visit to a SharePoint URL during the cluster window should
    contribute a browser_visit signal to the matching stream."""
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    # Write a browser_visit overlapping the cluster, routed to uganda
    base = datetime.fromisoformat(meta["started_at"])
    con.execute(
        "INSERT OR IGNORE INTO stream(key, label) VALUES ('uganda', 'Uganda MEMD')"
    )
    con.execute(
        """
        INSERT INTO browser_visit(id, ts, app, domain, url_hash, title_hash,
                                   stream, is_private)
        VALUES ('v1', ?, 'Safari', 'verst.sharepoint.com', 'h1', 'h2',
                'uganda', 0)
        """,
        (_iso(base + timedelta(minutes=5)),),
    )
    signals = cat.gather_signals(con, cid, meta["started_at"], meta["ended_at"])
    assert "uganda" in signals
    assert any(sig[0] == "browser_visit" for sig in signals["uganda"])


def test_signals_ignore_private_browser_visits(env):
    """A private-tier browser visit (is_private=1) must NOT contribute to
    work cluster signals — even if it overlaps."""
    con = _con()
    cid = _seed_cluster_in_stream(con, stream=None)
    meta = con.execute(
        "SELECT started_at, ended_at FROM job_view WHERE cluster_id = ?", (cid,),
    ).fetchone()
    base = datetime.fromisoformat(meta["started_at"])
    # 'personal' stream exists from migration 0007
    con.execute(
        """
        INSERT INTO browser_visit(id, ts, app, domain, url_hash, title_hash,
                                   stream, is_private)
        VALUES ('v2', ?, 'Safari', 'chase.com', 'h1', 'h2', 'personal', 1)
        """,
        (_iso(base + timedelta(minutes=5)),),
    )
    signals = cat.gather_signals(con, cid, meta["started_at"], meta["ended_at"])
    # personal should NOT appear in signals (private visit filtered out)
    assert "personal" not in signals


def test_assign_all_processes_every_cluster(env):
    con = _con()
    base = datetime(2026, 6, 26, 10, 0, tzinfo=timezone.utc)
    _seed_cluster_in_stream(con, stream="dev", start=base)
    _seed_cluster_in_stream(con, stream="work",
                            start=base + timedelta(hours=2))
    counts = cat.assign_all(con)
    assert counts["total"] >= 2
    assert counts["assigned_agent"] + counts["fallback"] == counts["total"]
