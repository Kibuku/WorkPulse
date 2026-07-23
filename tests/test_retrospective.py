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


def test_project_name_beats_generic_work_and_scopes_files(env):
    con = _con()
    now = datetime.now(timezone.utc)
    cfg = {
        "streams": {
            "work": {"label": "Work"},
            "dev": {"label": "WorkPulse build"},
        }
    }
    _session(con, app="Code", title="WorkPulse workflow learner",
             stream="dev", start=now - timedelta(days=1), dur_minutes=60)
    _session(con, app="Word", title="Unrelated client TOR",
             stream="work", start=now - timedelta(days=1, hours=2),
             dur_minutes=90)
    atoms.write_file_event(
        con, raw_path="/Projects/WorkPulse/dashboard.js",
        kind="modified", ts=(now - timedelta(days=1)).isoformat())
    atoms.write_file_event(
        con, raw_path="/Projects/Client/TOR.docx",
        kind="modified", ts=(now - timedelta(days=1)).isoformat())

    roll = rmod.summarize(
        con, query="How have I worked on WorkPulse recently?", cfg=cfg)

    assert roll["stream"] == "dev"
    assert roll["total_seconds"] == 60 * 60
    assert roll["session_count"] == 1
    assert [f["basename"] for f in roll["files"]] == ["dashboard.js"]


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
    assert "Client work: recent work" in md
    assert "Client work" in md               # the stream label, not the raw key
    assert "## Gap" in md
    assert "→" not in md and "—" not in md  # house voice: no arrows / em dashes


def test_deterministic_renderer_handles_assignment_conflict():
    rollup = {
        "stream": "workpulse",
        "window": {"since": "2026-07-16", "until": "2026-07-23"},
        "total_seconds": 3600,
        "session_count": 1,
        "active_days": 1,
        "areas": [{
            "stream": "workpulse", "label": "WorkPulse", "tagged": True,
            "seconds": 3600, "share": 1.0, "apps": [], "highlights": [],
            "outputs": ["Client proposal"],
            "conflicts": [{
                "output": "Client proposal",
                "filed_as": "workpulse",
                "suggested": "client-work",
            }],
        }],
        "files": [],
    }
    cfg = {"streams": {
        "workpulse": {"label": "WorkPulse"},
        "client-work": {"label": "Client work"},
    }}

    md = rmod.to_sop_markdown(rollup, cfg=cfg, use_backend=False)

    assert "Needs review" not in md
    assert "filed as WorkPulse" not in md
    assert "matches Client work" not in md


def test_scoped_summary_does_not_promote_conflicting_output(monkeypatch, env):
    con = _con()
    now = datetime.now(timezone.utc)
    sid = _session(
        con, app="Word", title="Uganda MEMD",
        stream="workpulse", start=now - timedelta(days=1), dur_minutes=30)
    con.execute("UPDATE session SET cluster_id='cluster-1' WHERE id=?", (sid,))

    from workpulse.core import cluster_context
    monkeypatch.setattr(
        cluster_context, "cluster_context",
        lambda *args, **kwargs: {"titles": [], "files": []})
    monkeypatch.setattr(
        cluster_context, "infer_output",
        lambda *args, **kwargs: {"specific": True, "title": "Uganda MEMD"})

    roll = rmod.summarize(
        con, stream="workpulse",
        cfg={"streams": {"workpulse": {"label": "WorkPulse build"}}})

    area = roll["areas"][0]
    assert area["outputs"] == []
    assert area["conflicts"][0]["suggested"] == "uganda"
    md = rmod.to_sop_markdown(
        roll,
        cfg={"streams": {"workpulse": {"label": "WorkPulse build"}}},
        use_backend=False,
    )
    assert "WorkPulse build: recent work" in md


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
