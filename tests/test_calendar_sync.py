"""
Tests for scripts/calendar_sync.py.

The actual HTTP fetch is monkeypatched in every test — we test parsing,
DB writes, project resolution, and the Categorizer signal hook-up. No
network in CI.

Run: python -m pytest tests/test_calendar_sync.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cluster, db
from workpulse.signals import calendar_sync as cs
from workpulse.core import categorize as cat


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


# ── synthetic ICS bodies ────────────────────────────────────────────────────

def _ics(events):
    """Build a minimal ICS document from a list of dicts."""
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//Test//Test//EN"]
    for i, e in enumerate(events):
        lines += ["BEGIN:VEVENT",
                  f"UID:{e.get('uid', f'uid-{i}')}@test",
                  f"SUMMARY:{e['title']}",
                  f"DTSTART:{e['start']}",
                  f"DTEND:{e['end']}"]
        if e.get("location"):
            lines.append(f"LOCATION:{e['location']}")
        if e.get("body"):
            lines.append(f"DESCRIPTION:{e['body']}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return ("\r\n".join(lines) + "\r\n").encode("utf-8")


# ── parse ───────────────────────────────────────────────────────────────────

def test_parse_returns_events_in_window():
    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = _ics([
        {"title": "Uganda MEMD technical review",
         "start": now.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")},
        {"title": "Old meeting",
         "start": (now - timedelta(days=60)).strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now - timedelta(days=60, hours=-1)).strftime("%Y%m%dT%H%M%SZ")},
    ])
    events = cs.parse_events(
        body,
        since=now - timedelta(days=14),
        until=now + timedelta(days=30),
    )
    titles = [e["title"] for e in events]
    assert "Uganda MEMD technical review" in titles
    assert "Old meeting" not in titles


def test_parse_extracts_location_and_body():
    now = datetime.now(timezone.utc)
    body = _ics([
        {"title": "Mercy Corps stakeholder call",
         "start": now.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
         "location": "Teams",
         "body":  "Quarterly review with Mwangi"},
    ])
    events = cs.parse_events(body, since=now - timedelta(days=1),
                             until=now + timedelta(days=1))
    assert events[0]["location"] == "Teams"
    assert "Mwangi" in events[0]["body"]


# ── sync ────────────────────────────────────────────────────────────────────

def test_sync_writes_events_and_resolves_stream(env, monkeypatch):
    con = _con()
    now = datetime.now(timezone.utc).replace(microsecond=0)
    body = _ics([
        {"title": "Uganda MEMD methodology review",
         "start": now.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ"),
         "uid":   "u1"},
        {"title": "Mercy Corps stakeholder mapping",
         "start": (now + timedelta(hours=2)).strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=3)).strftime("%Y%m%dT%H%M%SZ"),
         "uid":   "u2"},
    ])
    monkeypatch.setattr(cs, "fetch_ics", lambda url, **kw: body)
    counts = cs.sync(con, url="https://example.com/cal.ics", cfg={"paths": {}})
    assert counts["fetched"] == 2
    assert counts["inserted"] == 2
    assert counts["with_stream"] == 2
    rows = con.execute(
        "SELECT stream FROM calendar_event ORDER BY started_at"
    ).fetchall()
    streams = [r["stream"] for r in rows]
    assert "uganda" in streams
    assert "uganda2" in streams


def test_sync_no_url_returns_error(env, monkeypatch):
    """When no URL is configured anywhere, sync should report it cleanly
    and not blow up. We monkeypatch _calendar_url so the test isn't
    sensitive to a config/calendar.url file existing on the dev box."""
    monkeypatch.setattr(cs, "_calendar_url", lambda cfg=None: None)
    con = _con()
    counts = cs.sync(con, url=None, cfg={"paths": {}})
    assert counts["errors"]
    assert "no calendar URL" in counts["errors"][0]


def test_sync_event_with_no_project_match_has_null_stream(env, monkeypatch):
    con = _con()
    now = datetime.now(timezone.utc)
    body = _ics([
        {"title": "Lunch", "uid": "u1",
         "start": now.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")},
    ])
    monkeypatch.setattr(cs, "fetch_ics", lambda url, **kw: body)
    cs.sync(con, url="x", cfg={"paths": {}})
    row = con.execute("SELECT stream FROM calendar_event").fetchone()
    assert row["stream"] is None


def test_sync_is_idempotent(env, monkeypatch):
    con = _con()
    now = datetime.now(timezone.utc)
    body = _ics([
        {"title": "Uganda meeting", "uid": "stable-uid",
         "start": now.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (now + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")},
    ])
    monkeypatch.setattr(cs, "fetch_ics", lambda url, **kw: body)
    first = cs.sync(con, url="x", cfg={"paths": {}})
    second = cs.sync(con, url="x", cfg={"paths": {}})
    assert first["inserted"] == 1
    # Re-sync of the same UID is an UPDATE (rowcount=1 still); count of rows
    # in the table should remain 1
    n = con.execute("SELECT COUNT(*) AS n FROM calendar_event").fetchone()["n"]
    assert n == 1


# ── Categorizer integration ─────────────────────────────────────────────────

def test_categorizer_uses_calendar_event_signal(env, monkeypatch):
    con = _con()
    # Seed a cluster with NO project signal
    t0 = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(30):
        sid = atoms.write_session(
            con, app="Teams", title="Microsoft Teams", stream=None,
            started_at=_iso(t0 + timedelta(minutes=i)),
        )
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    cluster.refresh(con)
    cid = con.execute(
        "SELECT cluster_id FROM job_view WHERE stream IS NULL LIMIT 1"
    ).fetchone()["cluster_id"]

    # Now sync a calendar event that OVERLAPS the cluster window
    body = _ics([
        {"title": "Uganda MEMD technical review", "uid": "u1",
         "start": t0.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (t0 + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")},
    ])
    monkeypatch.setattr(cs, "fetch_ics", lambda url, **kw: body)
    cs.sync(con, url="x", cfg={"paths": {}})

    # Categorizer should now assign the cluster to uganda
    res = cat.assign_cluster(con, cid)
    assert res["stream"] == "uganda"
    assert res["source"] == "agent"
    signals_used = [e["signal"] for e in res["evidence"]]
    assert "calendar_event" in signals_used


def test_calendar_event_outside_window_is_ignored(env, monkeypatch):
    con = _con()
    t0 = (datetime.now(timezone.utc) - timedelta(days=2)).replace(hour=10, minute=0, second=0, microsecond=0)
    for i in range(30):
        sid = atoms.write_session(
            con, app="Teams", title="x", stream=None,
            started_at=_iso(t0 + timedelta(minutes=i)),
        )
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    cluster.refresh(con)
    cid = con.execute(
        "SELECT cluster_id FROM job_view WHERE stream IS NULL LIMIT 1"
    ).fetchone()["cluster_id"]

    # Calendar event 5 HOURS BEFORE the cluster — must not influence it
    far = t0 - timedelta(hours=5)
    body = _ics([
        {"title": "Uganda MEMD review", "uid": "u1",
         "start": far.strftime("%Y%m%dT%H%M%SZ"),
         "end":   (far + timedelta(hours=1)).strftime("%Y%m%dT%H%M%SZ")},
    ])
    monkeypatch.setattr(cs, "fetch_ics", lambda url, **kw: body)
    cs.sync(con, url="x", cfg={"paths": {}})

    res = cat.assign_cluster(con, cid)
    assert res["stream"] != "uganda"  # no signal, so misc or other
