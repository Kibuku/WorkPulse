from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, categorize, cluster, db, timeline


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    return db.connect(cfg={"paths": {}})


def _work_block(con, start, *, app="Microsoft Word",
                title="School improvement plan", minutes=20):
    for i in range(minutes):
        sid = atoms.write_session(
            con, app=app, title=title, stream=None,
            started_at=(start + timedelta(minutes=i)).isoformat())
        atoms.close_session(
            con, sid,
            ended_at=(start + timedelta(minutes=i + 1)).isoformat())
    cluster.refresh(con)
    return con.execute(
        "SELECT cluster_id FROM job_view ORDER BY started_at DESC LIMIT 1"
    ).fetchone()["cluster_id"]


def test_timeline_builds_output_first_work_block(con):
    start = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    cid = _work_block(con, start)
    categorize.correct_assignment(con, cid, "school")
    out = timeline.build_day(con, date(2026, 7, 23), cfg={"paths": {}})
    work = next(e for e in out["events"] if e["kind"] == "work")
    assert work["title"] == "School improvement plan"
    assert work["project_label"] == "school"
    assert work["attribution_method"] == "Confirmed by you"
    assert work["apps"][0]["app"] == "Microsoft Word"
    assert out["work_blocks"] == 1


def test_timeline_merges_calendar_commitment(con):
    start = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    _work_block(con, start)
    con.execute(
        """INSERT INTO calendar_event
           (id,source,started_at,ended_at,title_hash,stream,is_organizer,fetched_at)
           VALUES ('m1','ics',?,?, 'hash',NULL,0,?)""",
        ((start + timedelta(hours=1)).isoformat(),
         (start + timedelta(hours=2)).isoformat(), start.isoformat()))
    con.execute(
        """INSERT INTO calendar_event_local
           (event_id,raw_title,raw_body,raw_location,attendees)
           VALUES ('m1','Faculty review',NULL,'Room 2',NULL)""")
    out = timeline.build_day(con, date(2026, 7, 23), cfg={"paths": {}})
    meeting = next(e for e in out["events"] if e["kind"] == "meeting")
    assert meeting["title"] == "Faculty review"
    assert meeting["location"] == "Room 2"
    assert out["meetings"] == 1


def test_timeline_excludes_private_stream_by_default(con):
    start = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    cid = _work_block(con, start)
    con.execute(
        "INSERT OR REPLACE INTO stream(key,label,is_private) "
        "VALUES ('personal','Personal',1)")
    categorize.correct_assignment(con, cid, "personal")
    hidden = timeline.build_day(con, date(2026, 7, 23), cfg={"paths": {}})
    visible = timeline.build_day(
        con, date(2026, 7, 23), cfg={"paths": {}}, include_private=True)
    assert hidden["events"] == []
    assert len(visible["events"]) == 1


def test_foreground_episodes_do_not_overlap(con):
    start = datetime(2026, 7, 23, 9, 0, tzinfo=timezone.utc)
    first = _work_block(con, start, title="First output", minutes=10)
    categorize.correct_assignment(con, first, "school")
    # A later, separate attention episode.
    second = _work_block(
        con, start + timedelta(minutes=15), app="Microsoft Excel",
        title="Second output plan", minutes=10)
    categorize.correct_assignment(con, second, "finance")
    out = timeline.build_day(con, date(2026, 7, 23), cfg={"paths": {}})
    work = [e for e in out["events"] if e["kind"] == "work"]
    for before, after in zip(work, work[1:]):
        assert before["ended_at"] <= after["started_at"]
