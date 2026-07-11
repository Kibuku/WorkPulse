"""
Smoke tests for the v2 substrate.

Verifies:
  - Schema applies cleanly on an empty file.
  - Each atom type writes + extracts typed edges.
  - The institutional/private split: public tables never hold raw strings.
  - Idempotent edge inserts.

Run:  python -m pytest tests/test_atoms.py
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path


import pytest

from workpulse.core import atoms, db


@pytest.fixture()
def con(tmp_path, monkeypatch):
    """Fresh DB per test, isolated under tmp_path."""
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    c = db.connect(cfg={"paths": {}})
    yield c
    c.close()


def test_schema_applies(con):
    v = db.current_version(con)
    assert v >= 1
    tables = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    for t in ("session", "session_local", "file_event", "file_event_local",
              "ai_call", "capture", "edge", "plan_item", "skill_run",
              "app", "stream", "schema_migration"):
        assert t in tables, f"missing table {t!r}"


def test_write_session_and_edges(con):
    sid = atoms.write_session(
        con,
        app="Code.exe",
        title="workpulse.py — VS Code",
        stream="dev",
        raw_path="/Users/g/Documents/WorkPulse",
        raw_files=["scripts/db.py", "scripts/atoms.py"],
    )
    # Public row: no raw title.
    row = con.execute("SELECT * FROM session WHERE id = ?", (sid,)).fetchone()
    assert row["app"] == "Code.exe"
    assert row["stream"] == "dev"
    assert row["title_hash"]
    assert "workpulse" not in row["title_hash"].lower()  # de-identified

    # Private projection holds the raw.
    loc = con.execute("SELECT * FROM session_local WHERE session_id = ?", (sid,)).fetchone()
    assert loc["raw_title"].startswith("workpulse.py")
    assert "WorkPulse" in loc["raw_path"]

    # Edges: in_app, in_stream, touched_file (x2).
    edges = list(con.execute(
        "SELECT rel, dst_kind, dst_id FROM edge WHERE src_kind='session' AND src_id = ?",
        (sid,),
    ))
    rels = [e["rel"] for e in edges]
    assert "in_app" in rels
    assert "in_stream" in rels
    assert rels.count("touched_file") == 2


def test_edge_idempotent(con):
    atoms.add_edge(con, src_kind="x", src_id="a", rel="r", dst_kind="y", dst_id="b")
    atoms.add_edge(con, src_kind="x", src_id="a", rel="r", dst_kind="y", dst_id="b")
    n = con.execute(
        "SELECT COUNT(*) AS n FROM edge WHERE src_id='a' AND dst_id='b'"
    ).fetchone()["n"]
    assert n == 1


def test_close_session(con):
    sid = atoms.write_session(con, app="X", title="hi")
    atoms.close_session(con, sid)
    row = con.execute("SELECT ended_at FROM session WHERE id = ?", (sid,)).fetchone()
    assert row["ended_at"] is not None


def test_file_event_split(con):
    fid = atoms.write_file_event(con, raw_path="/secret/path/foo.docx", kind="modified")
    pub = con.execute("SELECT * FROM file_event WHERE id = ?", (fid,)).fetchone()
    assert "secret" not in (pub["path_hash"] or "")
    loc = con.execute(
        "SELECT raw_path FROM file_event_local WHERE file_event_id = ?", (fid,),
    ).fetchone()
    assert loc["raw_path"] == "/secret/path/foo.docx"


def test_ai_call(con):
    cid = atoms.write_ai_call(
        con, provider="anthropic", model="claude-sonnet-4-6",
        in_tokens=1234, out_tokens=567, cost_usd=0.012,
        prompt_slug="report-daily",
    )
    row = con.execute("SELECT * FROM ai_call WHERE id = ?", (cid,)).fetchone()
    assert row["in_tokens"] == 1234
    assert row["prompt_slug"] == "report-daily"


def test_capture_pinned_edge(con):
    sid = atoms.write_session(con, app="X", title="t")
    cap_id = atoms.write_capture(
        con, body="Mwangi prefers the Mt. Elgon framing.",
        pinned_kind="session", pinned_id=sid,
    )
    edge = con.execute(
        "SELECT * FROM edge WHERE src_kind='capture' AND src_id = ?", (cap_id,),
    ).fetchone()
    assert edge["rel"] == "about"
    assert edge["dst_id"] == sid


def test_capture_pin_validation(con):
    with pytest.raises(ValueError):
        atoms.write_capture(con, body="x", pinned_kind="session", pinned_id=None)
    with pytest.raises(ValueError):
        atoms.write_capture(con, body="x", author="robot")


def test_untagged_is_first_class(con):
    """Per principle #7: untagged is a normal state."""
    sid = atoms.write_session(con, app="X", title="unknown", stream=None)
    row = con.execute("SELECT stream FROM session WHERE id = ?", (sid,)).fetchone()
    assert row["stream"] is None  # not an error; not a sentinel; just None
