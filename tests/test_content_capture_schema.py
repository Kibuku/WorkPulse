"""
U2 — content_capture source-reference columns (migration 0012).

Additive + idempotent: existing Tesseract rows are untouched; a LlamaParse row
can record which file it came from (source_path_hash/source_mtime) for dedup.

Run: .venv/bin/python -m pytest tests/test_content_capture_schema.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _cols(con, table):
    return {r["name"] for r in con.execute(f"SELECT name FROM pragma_table_info('{table}')")}


def _now():
    return datetime.now(timezone.utc).isoformat()


def test_migration_adds_source_columns(env):
    con = _con()
    cols = _cols(con, "content_capture")
    assert "source_path_hash" in cols
    assert "source_mtime" in cols


def test_migration_is_idempotent(env):
    con = _con()
    assert db.migrate(con) == 0  # nothing new on a second call


def test_existing_tesseract_row_shape_preserved(env):
    con = _con()
    # A Tesseract-style row (no source) still inserts and reads back.
    con.execute(
        "INSERT INTO content_capture(id, ts, app, stage, redacted_text, ocr_engine) "
        "VALUES ('c1', ?, 'Word', 'drafting', 'hello', 'tesseract-local')", (_now(),))
    row = con.execute(
        "SELECT source_path_hash, source_mtime FROM content_capture WHERE id='c1'").fetchone()
    assert row["source_path_hash"] is None and row["source_mtime"] is None


def test_llamaparse_row_records_source(env):
    con = _con()
    con.execute(
        "INSERT INTO content_capture(id, ts, app, stage, redacted_text, ocr_engine, "
        "source_path_hash, source_mtime) "
        "VALUES ('c2', ?, 'PDF', NULL, 'parsed text', 'llamaparse', 'abc123', ?)",
        (_now(), "2026-09-20T10:00:00+00:00"))
    row = con.execute(
        "SELECT ocr_engine, source_path_hash FROM content_capture WHERE id='c2'").fetchone()
    assert row["ocr_engine"] == "llamaparse" and row["source_path_hash"] == "abc123"
