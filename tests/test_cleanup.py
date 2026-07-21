"""
Tests for core/cleanup.prune_untracked_file_events — the file-event noise prune.

A v1 migration drags in millions of ~/Library churn events for locations v2's
watch_roots exclude. The prune drops exactly those and keeps everything under a
watched root. file_event_local must cascade, and an empty watch_roots must be a
refusal (never delete everything).

Run: python -m pytest tests/test_cleanup.py
"""

from __future__ import annotations

import os
import json
from datetime import datetime, timezone

import pytest

from workpulse.core import cleanup, db


@pytest.fixture()
def con(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    return db.connect(cfg={"paths": {}})


def _fe(con, fid, path):
    con.execute("INSERT INTO file_event(id, ts, path_hash, kind) VALUES (?,?,?,?)",
                (fid, "2026-07-14T09:00:00+00:00", fid, "modified"))
    con.execute("INSERT INTO file_event_local(file_event_id, raw_path) VALUES (?,?)",
                (fid, path))
    con.commit()


CFG = {"watcher": {"watch_roots": ["~/Documents", "~/Library/CloudStorage"]}}


def test_prunes_untracked_keeps_watched(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/Clients/Acme/proposal.docx")     # keep
    _fe(con, "a2", f"{home}/Library/CloudStorage/OneDrive/report.xlsx")  # keep
    _fe(con, "b1", f"{home}/Library/Application Support/Claude/Cache/blob")  # drop
    _fe(con, "b2", f"{home}/Library/Preferences/com.apple.foo.plist")   # drop
    _fe(con, "b3", f"{home}/Library/Biome/sessions/x/bookmark")         # drop

    # Dry run: report only, nothing deleted.
    dry = cleanup.prune_untracked_file_events(con, cfg=CFG, dry_run=True)
    assert dry["deleted"] == 3 and dry["kept"] == 2
    assert con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0] == 5

    # Real run.
    res = cleanup.prune_untracked_file_events(con, cfg=CFG)
    assert res["deleted"] == 3 and res["kept"] == 2
    kept = {r[0] for r in con.execute("SELECT id FROM file_event")}
    assert kept == {"a1", "a2"}
    # file_event_local for the dropped ones is gone (cascade / explicit delete).
    assert con.execute("SELECT COUNT(*) FROM file_event_local").fetchone()[0] == 2
    assert con.execute(
        "SELECT COUNT(*) FROM file_event_local WHERE file_event_id='b1'").fetchone()[0] == 0


def test_idempotent(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/x.txt")
    _fe(con, "b1", f"{home}/Library/Caches/y")
    cleanup.prune_untracked_file_events(con, cfg=CFG)
    # Second run finds nothing more to do.
    again = cleanup.prune_untracked_file_events(con, cfg=CFG)
    assert again["deleted"] == 0 and again["kept"] == 1


def test_refuses_when_no_watch_roots(con):
    home = os.path.expanduser("~")
    _fe(con, "a1", f"{home}/Documents/x.txt")
    res = cleanup.prune_untracked_file_events(con, cfg={"watcher": {"watch_roots": []}})
    assert "error" in res and res["deleted"] == 0
    assert con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0] == 1  # untouched


def test_retention_removes_raw_evidence_but_keeps_memory_markers(con, tmp_path,
                                                                 monkeypatch):
    old = "2026-01-01T09:00:00+00:00"
    recent = "2026-07-20T09:00:00+00:00"
    con.execute("INSERT INTO app(name,first_seen,last_seen) VALUES ('Word',?,?)",
                (old, recent))
    for sid, stamp in (("s-old", old), ("s-new", recent)):
        con.execute("INSERT INTO session(id,started_at,ended_at,app,title_hash) VALUES (?,?,?,?,?)",
                    (sid, stamp, stamp, "Word", sid))
        con.execute("INSERT INTO session_local(session_id,raw_title) VALUES (?,?)",
                    (sid, "Project document"))
    for fid, stamp in (("f-old", old), ("f-new", recent)):
        con.execute("INSERT INTO file_event(id,ts,path_hash,kind) VALUES (?,?,?,'modified')",
                    (fid, stamp, fid))
        con.execute("INSERT INTO file_event_local(file_event_id,raw_path) VALUES (?,?)",
                    (fid, f"/Documents/{fid}.docx"))
    for bid, stamp in (("b-old", old), ("b-new", recent)):
        con.execute("INSERT INTO browser_visit(id,ts,app,domain,url_hash,title_hash) VALUES (?,?,?,?,?,?)",
                    (bid, stamp, "Safari", "example.com", bid, bid))
        con.execute("INSERT INTO browser_visit_local(visit_id,raw_url,raw_title) VALUES (?,?,?)",
                    (bid, "https://example.com/work", "Work"))
    for cid, stamp in (("c-old", old), ("c-new", recent)):
        con.execute("INSERT INTO calendar_event(id,source,started_at,ended_at,title_hash,fetched_at) VALUES (?,'ics',?,?,?,?)",
                    (cid, stamp, stamp, cid, stamp))
        con.execute("INSERT INTO calendar_event_local(event_id,raw_title) VALUES (?,?)",
                    (cid, "Planning meeting"))
    con.execute("INSERT INTO skill_run(id,ts,skill_slug,input,output,status) VALUES ('r-old',?,'think','large prompt','large output','ok')", (old,))
    con.execute("INSERT INTO capture(id,ts,author,body) VALUES ('memory',?,'human','Keep this forever')", (old,))

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "activity_2026-01-01.jsonl").write_text("old\n")
    (logs / "activity_2026-07-20.jsonl").write_text("new\n")
    (logs / "learned_tags.json").write_text("{}")
    (logs / "ai_sessions.jsonl").write_text(
        json.dumps({"ts": old, "value": "old"}) + "\n" +
        json.dumps({"ts": recent, "value": "new"}) + "\n")
    monkeypatch.setattr(cleanup, "resolve", lambda _p: logs)
    cfg = {"paths": {"logs": "logs"}, "retention": {"raw_days": 100}}
    now = datetime(2026, 7, 21, tzinfo=timezone.utc)

    dry = cleanup.apply_retention(con, cfg=cfg, now=now, dry_run=True)
    assert dry["file_events"] == 1
    assert dry["session_details"] == 1
    assert dry["log_files"] == 1
    assert (logs / "activity_2026-01-01.jsonl").exists()

    result = cleanup.apply_retention(con, cfg=cfg, now=now)
    assert result["cutoff"] == "2026-04-12"
    assert con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM session").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM session_local").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM browser_visit").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM browser_visit_local").fetchone()[0] == 1
    assert con.execute("SELECT COUNT(*) FROM calendar_event").fetchone()[0] == 2
    assert con.execute("SELECT COUNT(*) FROM calendar_event_local").fetchone()[0] == 1
    skill = con.execute("SELECT input,output FROM skill_run WHERE id='r-old'").fetchone()
    assert skill["input"] is None and skill["output"] is None
    assert con.execute("SELECT body FROM capture WHERE id='memory'").fetchone()[0] == "Keep this forever"
    assert not (logs / "activity_2026-01-01.jsonl").exists()
    assert (logs / "activity_2026-07-20.jsonl").exists()
    assert (logs / "learned_tags.json").exists()
    ai_rows = [json.loads(line) for line in (logs / "ai_sessions.jsonl").read_text().splitlines()]
    assert [row["value"] for row in ai_rows] == ["new"]


def test_retention_rejects_non_positive_window(con):
    result = cleanup.apply_retention(
        con, cfg={"retention": {"raw_days": 0}}, dry_run=True)
    assert "error" in result
