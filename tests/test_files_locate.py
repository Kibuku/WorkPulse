"""
Tests for core/files.py locate() — the "where is my <file>?" retrieval.

Deterministic + keyless: no LLM is involved, so these run fully offline.

Run: python -m pytest tests/test_files_locate.py
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from workpulse.core import atoms, db
from workpulse.core import files as fmod


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def test_locate_finds_by_name(env):
    con = _con()
    now = datetime.now(timezone.utc).isoformat()
    atoms.write_file_event(con, raw_path=r"C:\Users\pc\ClientZ\Proposal_v3.docx",
                           kind="modified", ts=now)
    atoms.write_file_event(con, raw_path=r"C:\Users\pc\Misc\notes.txt",
                           kind="modified", ts=now)
    hits = fmod.locate(con, "client z proposal")
    assert hits, "expected at least one hit"
    assert hits[0]["basename"] == "Proposal_v3.docx"


def test_recency_breaks_ties(env):
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_file_event(con, raw_path="/home/u/budget_2025.xlsx",
                           kind="modified", ts=(now - timedelta(days=20)).isoformat())
    atoms.write_file_event(con, raw_path="/home/u/budget_2026.xlsx",
                           kind="modified", ts=now.isoformat())
    hits = fmod.locate(con, "budget spreadsheet")
    assert hits[0]["basename"] == "budget_2026.xlsx"


def test_since_filter_excludes_old(env):
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_file_event(con, raw_path="/x/ancient_report.pdf",
                           kind="modified", ts=(now - timedelta(days=40)).isoformat())
    since = (now - timedelta(days=7)).date().isoformat()
    assert fmod.locate(con, "report", since=since) == []


def test_no_match_returns_empty(env):
    con = _con()
    now = datetime.now(timezone.utc).isoformat()
    atoms.write_file_event(con, raw_path="/x/unrelated.bin", kind="modified", ts=now)
    assert fmod.locate(con, "quarterly tax filing") == []


def test_touch_count_aggregates(env):
    con = _con()
    now = datetime.now(timezone.utc)
    p = "/home/u/thesis/chapter_three.docx"
    for i in range(3):
        atoms.write_file_event(con, raw_path=p, kind="modified",
                               ts=(now - timedelta(hours=i)).isoformat())
    hits = fmod.locate(con, "chapter three")
    assert len(hits) == 1
    assert hits[0]["touch_count"] == 3


def test_format_hits_no_match_is_friendly():
    out = fmod.format_hits([], "budget")
    assert "couldn't find" in out.lower()
    assert "→" not in out and "—" not in out  # no arrows / em dashes
