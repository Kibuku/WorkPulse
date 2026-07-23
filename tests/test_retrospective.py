"""
Tests for core/retrospective.py — "how did I work on <X>?" rollup + SOP.

Deterministic + keyless (no LLM configured in tests), so these run offline.

Run: python -m pytest tests/test_retrospective.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, categorize, cluster, db
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
    area = next(a for a in roll["areas"] if a["stream"] == "client-work")
    apps = {x["label"]: x["seconds"] for x in area["apps"]}
    assert apps["Word"] == 3600 and apps["Excel"] == 1800
    assert area["apps"][0]["label"] == "Word"  # sorted by time desc


def test_summarize_prefers_current_cluster_assignment(env):
    con = _con()
    now = datetime.now(timezone.utc)
    _session(con, app="Word", title="School demo", stream="old-project",
             start=now - timedelta(days=1), dur_minutes=60)
    cluster.refresh(con)
    cid = con.execute("SELECT cluster_id FROM job_view LIMIT 1").fetchone()[0]
    categorize.correct_assignment(con, cid, "client-work")
    roll = rmod.summarize(con, cfg=CFG)
    streams = {a["stream"] for a in roll["areas"]}
    assert "client-work" in streams
    assert "old-project" not in streams


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
    assert "What you worked on" in md
    assert "Client work" in md               # the stream label, not the raw key
    assert "## Gap" in md
    assert "→" not in md and "—" not in md  # house voice: no arrows / em dashes


# ── Natural-language date windows ────────────────────────────────────────────
# _parse_window turns "in June" / "last month" / "last 30 days" into concrete
# (since, until) ISO dates. Anchored to a fixed 'today' so the tests are stable.
from datetime import date


_TODAY = date(2026, 7, 12)  # a Sunday in July, fixed anchor


def test_parse_window_named_month_current_year():
    # "in June" -> the whole of the most-recent June (this year, since June < July)
    assert rmod._parse_window("what happened in June", today=_TODAY) == \
        ("2026-06-01", "2026-06-30")


def test_parse_window_named_month_rolls_back_a_year():
    # "in December" while it's July -> last December, not a future one
    assert rmod._parse_window("recap of December", today=_TODAY) == \
        ("2025-12-01", "2025-12-31")


def test_parse_window_last_n_days():
    assert rmod._parse_window("what did i do the last 30 days", today=_TODAY) == \
        ("2026-06-12", "2026-07-12")


def test_parse_window_last_month():
    assert rmod._parse_window("summarise last month", today=_TODAY) == \
        ("2026-06-01", "2026-06-30")


def test_parse_window_yesterday():
    assert rmod._parse_window("what happened yesterday", today=_TODAY) == \
        ("2026-07-11", "2026-07-11")


def test_parse_window_this_month():
    assert rmod._parse_window("this month", today=_TODAY) == \
        ("2026-07-01", "2026-07-12")


def test_parse_window_may_is_not_the_verb():
    # bare "may" (the verb) must NOT be read as the month of May
    assert rmod._parse_window("how may i improve", today=_TODAY) is None
    # but "in May" is a real month reference
    assert rmod._parse_window("what happened in May", today=_TODAY) == \
        ("2026-05-01", "2026-05-31")


def test_parse_window_none_when_no_date():
    assert rmod._parse_window("how did i work on the proposal", today=_TODAY) is None
    assert rmod._parse_window("", today=_TODAY) is None
    assert rmod._parse_window(None, today=_TODAY) is None
