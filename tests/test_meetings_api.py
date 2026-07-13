"""
Tests for the meeting-tagging loop — the calendar side of tag-the-untagged.

GET /api/meetings/untagged surfaces meetings with no stream; tagging one via
/api/learn (which now also retags calendar events) removes it from the list and
reports meetings_retagged. Config + DB are redirected to a tmp sandbox.

Run: python -m pytest tests/test_meetings_api.py
"""

from __future__ import annotations

from fastapi.testclient import TestClient

import workpulse.web.app as appmod
from workpulse.core import db as wp_db
from workpulse.core import learning


def _seed_meeting(con, event_id, title):
    con.execute(
        "INSERT INTO calendar_event(id,source,started_at,ended_at,title_hash,"
        "stream,is_organizer,fetched_at) VALUES (?,'ics',"
        "'2026-07-01T09:00:00+00:00','2026-07-01T10:00:00+00:00','h',NULL,0,"
        "'2026-07-01T00:00:00+00:00')", (event_id,))
    con.execute(
        "INSERT INTO calendar_event_local(event_id,raw_title,raw_body,"
        "raw_location,attendees) VALUES (?,?,NULL,NULL,NULL)", (event_id, title))
    con.commit()


def _client(tmp_path, monkeypatch):
    cfg = {"paths": {"logs": str(tmp_path)},
           "streams": {"clientwork": {"label": "Client work"}}}
    monkeypatch.setattr(appmod, "load_config", lambda: cfg)
    monkeypatch.setattr(wp_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    learning._RULES_CACHE["mtime"] = 0.0
    learning._RULES_CACHE["rules"] = []
    return TestClient(appmod.app), cfg


def _titles(body):
    return [m["title"] for m in body["meetings"]]


def test_untagged_meetings_surface_then_leave_on_tag(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    con = wp_db.connect(cfg={"paths": {}})
    _seed_meeting(con, "e1", "Mercy Corps kickoff")
    _seed_meeting(con, "e2", "Enersave review")

    # Both meetings are unattributed, so both surface for filing.
    before = client.get("/api/meetings/untagged").json()
    assert set(_titles(before)) == {"Mercy Corps kickoff", "Enersave review"}

    # Tag the Mercy Corps one — same action the "Tag as" dropdown fires.
    r = client.post("/api/learn",
                    json={"raw_title": "Mercy Corps kickoff", "stream": "clientwork"})
    body = r.json()
    assert body["ok"] is True and body["meetings_retagged"] == 1

    # It leaves the list; the unrelated meeting stays.
    after = client.get("/api/meetings/untagged").json()
    assert _titles(after) == ["Enersave review"]
    # And it's now attributed in the store.
    assert wp_db.connect(cfg={"paths": {}}).execute(
        "SELECT stream FROM calendar_event WHERE id='e1'").fetchone()[0] == "clientwork"


def test_recurring_meeting_grouped_and_counted(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    con = wp_db.connect(cfg={"paths": {}})
    _seed_meeting(con, "w1", "CPD Weekly Meeting")
    _seed_meeting(con, "w2", "CPD Weekly Meeting")
    _seed_meeting(con, "w3", "CPD Weekly Meeting")

    body = client.get("/api/meetings/untagged").json()
    # One row for the recurring meeting, counted — tag once covers all.
    assert body["count"] == 1
    assert body["meetings"][0]["title"] == "CPD Weekly Meeting"
    assert body["meetings"][0]["count"] == 3


def test_no_calendar_data_is_empty_not_error(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    wp_db.connect(cfg={"paths": {}})  # schema only, no meetings
    r = client.get("/api/meetings/untagged")
    assert r.status_code == 200 and r.json() == {"meetings": [], "count": 0}
