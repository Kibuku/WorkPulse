"""
Tests for sensor dual-write to SQLite.

Verifies:
  - Each sensor's record shape lands as the right atom.
  - Dual-write is idempotent (same record twice → one row).
  - Dual-write shares ID semantics with backfill (so re-running backfill
    after dual-write produces zero new rows).
  - Hot-path safety: a broken DB connection does not raise into the caller.

Run: python -m pytest tests/test_dual_write.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


import pytest

from workpulse.core import backfill, db, dual_write


@pytest.fixture()
def fresh_db(tmp_path, monkeypatch):
    """Fresh DB + reset module state."""
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    # Reset dual_write module-level state.
    dual_write._CON = None
    dual_write._DISABLED = False
    yield tmp_path
    if dual_write._CON:
        try:
            dual_write._CON.close()
        except Exception:
            pass
    dual_write._CON = None
    dual_write._DISABLED = False


def _con(tmp_path):
    return db.connect(cfg={"paths": {}})


def test_dual_write_session(fresh_db):
    rec = {
        "start": "2026-06-09T08:00:00+03:00",
        "end":   "2026-06-09T08:01:00+03:00",
        "duration_s": 60.0,
        "app": "Code", "exe_path": "/usr/bin/code",
        "title": "atoms.py — WorkPulse",
        "stream": "dev", "idle": False,
    }
    dual_write.dual_write_session(rec, cfg={"paths": {}})
    con = _con(fresh_db)
    rows = list(con.execute("SELECT * FROM session"))
    assert len(rows) == 1
    assert rows[0]["app"] == "Code"
    assert rows[0]["stream"] == "dev"
    # Edges landed
    rels = {r["rel"] for r in con.execute(
        "SELECT DISTINCT rel FROM edge WHERE src_kind='session'"
    )}
    assert "in_app" in rels and "in_stream" in rels


def test_dual_write_idempotent(fresh_db):
    rec = {
        "start": "2026-06-09T08:00:00+03:00", "end": "2026-06-09T08:01:00+03:00",
        "duration_s": 60.0, "app": "X", "exe_path": "", "title": "t",
        "stream": None, "idle": False,
    }
    dual_write.dual_write_session(rec)
    dual_write.dual_write_session(rec)
    dual_write.dual_write_session(rec)
    con = _con(fresh_db)
    n = con.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
    assert n == 1


def test_dual_write_matches_backfill_ids(fresh_db, monkeypatch):
    """A session dual-written and then re-imported via backfill must collapse
    to one row — that's the whole point of shared content-hash IDs."""
    # Set up a tiny fake v1 source layout matching fresh_db's tmp_path.
    logs = fresh_db / "logs"; logs.mkdir()
    plans = fresh_db / "plans"; plans.mkdir()
    rec = {
        "start": "2026-06-09T08:00:00+03:00",
        "end":   "2026-06-09T08:01:00+03:00",
        "duration_s": 60.0, "app": "Code", "exe_path": "/",
        "title": "atoms.py", "stream": "dev", "idle": False,
    }
    (logs / "activity_2026-06-09.jsonl").write_text(
        json.dumps(rec) + "\n", encoding="utf-8"
    )
    from workpulse import common as wp_common
    fake_cfg = {"paths": {"logs": "logs", "db": "wp.db"},
                "streams": {"dev": {"label": "Dev"}}}
    monkeypatch.setattr(wp_common, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(backfill, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(wp_common, "ROOT", fresh_db)
    monkeypatch.setattr(backfill, "ROOT", fresh_db)
    monkeypatch.setattr(wp_common, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else fresh_db / rel))
    monkeypatch.setattr(backfill, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else fresh_db / rel))

    # First: dual-write (the live sensor path)
    dual_write.dual_write_session(rec)
    # Then: backfill the same JSONL
    counts = backfill.run(since=None, dry_run=False)
    # backfill should have inserted 0 new sessions (id collision).
    assert counts["session"] == 0
    con = _con(fresh_db)
    n = con.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
    assert n == 1


def test_dual_write_file_event_normalizes_moved(fresh_db):
    for kind in ("moved_from", "moved_to"):
        dual_write.dual_write_file_event({
            "timestamp": f"2026-06-09T08:00:00+00:00",
            "event_type": kind,
            "path": f"/p/{kind}",
            "stream": None, "size_bytes": 0,
        })
    con = _con(fresh_db)
    kinds = {r["kind"] for r in con.execute("SELECT kind FROM file_event")}
    assert kinds == {"moved"}


def test_dual_write_ai_call(fresh_db):
    dual_write.dual_write_ai_call({
        "timestamp": "2026-06-09T08:00:00+00:00",
        "session_id": "abc-123",
        "stream": "dev",
        "tool_used": "report-daily",
        "model": "claude-sonnet-4-6",
        "input_tokens": 100, "output_tokens": 50,
        "estimated_cost_usd": 0.001,
    })
    con = _con(fresh_db)
    row = con.execute("SELECT * FROM ai_call").fetchone()
    assert row["model"] == "claude-sonnet-4-6"
    assert row["prompt_slug"] == "report-daily"


def test_disabled_dual_write_is_silent(fresh_db):
    """When the module is disabled, calls must succeed silently (no DB write,
    no raise)."""
    dual_write.disable()
    dual_write.dual_write_session({
        "start": "2026-06-09T08:00:00+03:00",
        "end": "2026-06-09T08:01:00+03:00",
        "app": "X", "title": "t", "stream": None,
    })
    # Re-enable for next tests in the suite.
    dual_write._DISABLED = False


def test_hot_path_safety_bad_record(fresh_db):
    """Records missing required fields are skipped, never raise."""
    dual_write.dual_write_session({})  # empty
    dual_write.dual_write_file_event({})
    dual_write.dual_write_ai_call({})
    con = _con(fresh_db)
    for t in ("session", "file_event", "ai_call"):
        n = con.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
        assert n == 0


def test_file_event_recovers_from_dangling_transaction(fresh_db):
    """Regression for the ~31h watcher freeze: a lock left the process-wide
    connection stuck in an open transaction, so every later BEGIN threw
    'cannot start a transaction within a transaction' and was swallowed at
    debug. A dangling transaction must no longer wedge subsequent writes."""
    rec1 = {"timestamp": "2026-07-14T09:00:00+00:00", "event_type": "created",
            "path": "/Users/x/Documents/report.docx"}
    dual_write.dual_write_file_event(rec1, cfg={"paths": {}})   # caches _CON, persists 1
    con = dual_write._CON
    assert con is not None

    con.execute("BEGIN")                 # wedge it, exactly like the freeze
    assert con.in_transaction

    rec2 = {"timestamp": "2026-07-14T10:00:00+00:00", "event_type": "modified",
            "path": "/Users/x/Documents/report2.docx"}
    dual_write.dual_write_file_event(rec2, cfg={"paths": {}})   # must still land

    n = _con(fresh_db).execute("SELECT COUNT(*) FROM file_event").fetchone()[0]
    assert n == 2, f"a dangling transaction froze the write path; got {n} of 2"


def test_write_failure_resets_cached_connection(fresh_db):
    """After a write error, the cached connection is dropped so the next call
    reopens a fresh one instead of reusing a possibly-wedged handle."""
    rec = {"timestamp": "2026-07-14T09:00:00+00:00", "event_type": "created",
           "path": "/Users/x/Documents/a.docx"}
    dual_write.dual_write_file_event(rec, cfg={"paths": {}})
    con = dual_write._CON
    assert con is not None
    con.close()                          # make the cached handle unusable
    # Next write hits the dead handle, notes the failure, and resets _CON...
    dual_write.dual_write_file_event(
        {"timestamp": "2026-07-14T11:00:00+00:00", "event_type": "created",
         "path": "/Users/x/Documents/b.docx"}, cfg={"paths": {}})
    assert dual_write._CON is None       # reset happened -> self-heals next time
    # ...and the following write succeeds against a fresh connection.
    dual_write.dual_write_file_event(
        {"timestamp": "2026-07-14T12:00:00+00:00", "event_type": "created",
         "path": "/Users/x/Documents/c.docx"}, cfg={"paths": {}})
    n = _con(fresh_db).execute("SELECT COUNT(*) FROM file_event").fetchone()[0]
    assert n >= 2                        # first + recovered write both persisted
