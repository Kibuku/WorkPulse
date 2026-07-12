"""
Tests for core/retrospective.py — "how did I work on <X>?" rollup + SOP.

Deterministic + keyless (no LLM configured in tests), so these run offline.

Run: python -m pytest tests/test_retrospective.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, db
from workpulse.core import retrospective as rmod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _session(con, *, app, title, stream, start, dur_minutes):
    sid = atoms.write_session(con, app=app, title=title, stream=stream,
                              started_at=start.isoformat())
    atoms.close_session(con, sid,
                        ended_at=(start + timedelta(minutes=dur_minutes)).isoformat())
    return sid


CFG = {"streams": {"client-work": {"label": "Client work"}}}


def test_summarize_totals_and_apps(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _session(con, app="Word", title="NKCC report", stream="client-work",
             start=now - timedelta(days=1), dur_minutes=60)
    _session(con, app="Excel", title="NKCC figures", stream="client-work",
             start=now - timedelta(days=1, hours=2), dur_minutes=30)
    roll = rmod.summarize(con, stream="client-work", cfg=CFG)
    assert roll["session_count"] == 2
    assert roll["total_seconds"] == 90 * 60
    apps = {a["app"]: a["seconds"] for a in roll["apps"]}
    assert apps["Word"] == 3600 and apps["Excel"] == 1800
    assert roll["apps"][0]["app"] == "Word"  # sorted by time desc


def test_resolves_stream_from_query(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _session(con, app="Word", title="x", stream="client-work",
             start=now - timedelta(days=1), dur_minutes=45)
    roll = rmod.summarize(con, query="how did I do the client work last week",
                          cfg=CFG)
    assert roll["stream"] == "client-work"
    assert roll["session_count"] == 1


def test_window_excludes_out_of_range(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _session(con, app="Word", title="old", stream="client-work",
             start=now - timedelta(days=40), dur_minutes=60)
    since = (now - timedelta(days=7)).date().isoformat()
    until = now.date().isoformat()
    roll = rmod.summarize(con, stream="client-work", since=since, until=until,
                          cfg=CFG)
    assert roll["session_count"] == 0
    assert roll["total_seconds"] == 0


def test_open_session_excluded(env):
    con = _con()
    now = datetime.now(timezone.utc)
    # open session (no ended_at) has no measurable duration
    atoms.write_session(con, app="Word", title="in progress", stream="client-work",
                        started_at=(now - timedelta(hours=1)).isoformat())
    roll = rmod.summarize(con, stream="client-work", cfg=CFG)
    assert roll["session_count"] == 0


def test_sop_markdown_deterministic_without_backend(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _session(con, app="Word", title="NKCC report", stream="client-work",
             start=now - timedelta(days=1), dur_minutes=90)
    roll = rmod.summarize(con, stream="client-work", cfg=CFG)
    md = rmod.to_sop_markdown(roll, cfg=CFG)  # no API key in tests -> deterministic
    assert "How you worked on client-work" in md
    assert "Day by day" in md
    assert "## Gap" in md
    assert "→" not in md and "—" not in md  # house voice: no arrows / em dashes
