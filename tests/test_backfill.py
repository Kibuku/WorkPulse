"""
Tests for the v1 → v2 backfill.

Verifies:
  - Each source format parses + writes the expected atoms.
  - Re-running the backfill produces no new rows (idempotency).
  - Typed edges land per principle #6.
  - Streams from config land in the stream table.

Run: python -m pytest tests/test_backfill.py
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


import pytest

from workpulse.core import backfill, db


@pytest.fixture()
def fake_v1(tmp_path, monkeypatch):
    """A tiny synthetic v1 layout under tmp_path."""
    logs = tmp_path / "logs"
    plans = tmp_path / "plans"
    logs.mkdir()
    plans.mkdir()

    (logs / "activity_2026-06-01.jsonl").write_text("\n".join([
        json.dumps({"start": "2026-06-01T08:00:00+03:00",
                    "end":   "2026-06-01T08:01:00+03:00",
                    "duration_s": 60.0, "app": "Code",
                    "title": "atoms.py — WorkPulse", "stream": "dev"}),
        json.dumps({"start": "2026-06-01T08:01:00+03:00",
                    "end":   "2026-06-01T08:02:00+03:00",
                    "duration_s": 60.0, "app": "Code",
                    "title": "db.py — WorkPulse", "stream": None}),  # untagged
    ]) + "\n", encoding="utf-8")

    (logs / "file_events_2026-06-01.jsonl").write_text(json.dumps({
        "timestamp": "2026-06-01T08:00:30+00:00",
        "event_type": "modified",
        "path": "/Users/g/Documents/WorkPulse/scripts/atoms.py",
        "stream": "dev",
    }) + "\n", encoding="utf-8")

    (logs / "ai_sessions.jsonl").write_text(json.dumps({
        "timestamp": "2026-06-01T09:00:00+00:00",
        "session_id": "9d10-aaaa", "stream": "dev",
        "task_summary": "test",
        "tool_used": "report-daily",
        "model": "claude-sonnet-4-6",
        "input_tokens": 100, "output_tokens": 50,
        "estimated_cost_usd": 0.001,
        "duration_minutes": 0.1,
    }) + "\n", encoding="utf-8")

    (plans / "2026-06-01.md").write_text(
        "# Plan — 2026-06-01\n\n## New\n\n- [ ] Ship backfill (~60 min) — stream:dev\n",
        encoding="utf-8",
    )

    # Re-point common.ROOT and the resolver at our fake tree.
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "ROOT", tmp_path)
    monkeypatch.setattr(backfill, "ROOT", tmp_path)
    fake_cfg = {
        "paths": {"logs": "logs", "db": "wp.db"},
        "streams": {"dev": {"label": "Dev"}, "misc": "Miscellaneous"},
    }
    monkeypatch.setattr(wp_common, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(backfill, "load_config", lambda: fake_cfg)
    monkeypatch.setattr(wp_common, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else tmp_path / rel))
    monkeypatch.setattr(backfill, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else tmp_path / rel))
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    return tmp_path


def test_backfill_first_run_counts(fake_v1):
    counts = backfill.run(since=None, dry_run=False)
    assert counts["session"] == 2
    assert counts["file_event"] == 1
    assert counts["ai_call"] == 1
    assert counts["stream"] >= 2  # both 'dev' and 'misc' from config
    assert counts["app"] == 1     # only 'Code'


def test_backfill_idempotent(fake_v1):
    backfill.run(since=None, dry_run=False)
    counts2 = backfill.run(since=None, dry_run=False)
    # Re-run inserts zero new atoms.
    assert counts2["session"] == 0
    assert counts2["file_event"] == 0
    assert counts2["ai_call"] == 0
    assert counts2["app"] == 0


def test_backfill_writes_typed_edges(fake_v1):
    backfill.run(since=None, dry_run=False)
    con = db.connect(cfg={"paths": {}})
    rels = {r["rel"] for r in con.execute(
        "SELECT DISTINCT rel FROM edge WHERE src_kind='session'"
    )}
    assert "in_app" in rels
    assert "in_stream" in rels


def test_backfill_preserves_untagged(fake_v1):
    """The second session has stream=None and must stay untagged
    (principle #7: untagged is first-class)."""
    backfill.run(since=None, dry_run=False)
    con = db.connect(cfg={"paths": {}})
    nulls = con.execute(
        "SELECT COUNT(*) AS n FROM session WHERE stream IS NULL"
    ).fetchone()["n"]
    assert nulls == 1


def test_backfill_private_split(fake_v1):
    """Raw titles never land in the public session table."""
    backfill.run(since=None, dry_run=False)
    con = db.connect(cfg={"paths": {}})
    pub_cols = [r["name"] for r in con.execute("PRAGMA table_info(session)")]
    assert "raw_title" not in pub_cols
    loc = con.execute(
        "SELECT raw_title FROM session_local LIMIT 1"
    ).fetchone()
    assert loc is not None and loc["raw_title"]
