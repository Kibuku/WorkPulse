"""
U3/U4 — LlamaParse candidate selection, parse+persist, search indexing.

Real files on disk (so existence + mtime dedup work); LlamaParse itself is
always mocked (no network, no key).

Run: .venv/bin/python -m pytest tests/test_document_parse.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, content_capture, db, search


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _make_file(tmp_path, name, body=b"doc bytes"):
    p = tmp_path / name
    p.write_bytes(body)
    return str(p)


def _touch_event(con, path, kind="created"):
    return atoms.write_file_event(con, raw_path=path, kind=kind, ts=_now())


CFG = {"content_capture": {}}


# ── candidate selection (R1, R3) ────────────────────────────────────────────────

def test_touched_document_is_a_candidate(env):
    con = _con()
    path = _make_file(env, "concept-note.pdf")
    _touch_event(con, path)
    cands = content_capture.find_parse_candidates(con, CFG)
    assert any(c["raw_path"] == path for c in cands)


def test_denied_file_is_not_a_candidate(env):
    con = _con()
    path = _make_file(env, "secret.pdf")
    _touch_event(con, path)
    cfg = {"content_capture": {"deny_paths": ["secret"]}}
    cands = content_capture.find_parse_candidates(con, cfg)
    assert not any(c["raw_path"] == path for c in cands)


def test_deleted_event_is_not_a_candidate(env):
    con = _con()
    path = _make_file(env, "gone.pdf")
    _touch_event(con, path, kind="deleted")
    cands = content_capture.find_parse_candidates(con, CFG)
    assert not any(c["raw_path"] == path for c in cands)


def test_nonparseable_extension_skipped(env):
    con = _con()
    path = _make_file(env, "screenshot.png")
    _touch_event(con, path)
    cands = content_capture.find_parse_candidates(con, CFG)
    assert not any(c["raw_path"] == path for c in cands)


# ── parse + persist (R5, R6) ────────────────────────────────────────────────────

def test_parse_and_persist_redacts_and_records_source(env, monkeypatch):
    con = _con()
    path = _make_file(env, "note.pdf")
    _touch_event(con, path)
    monkeypatch.setattr(content_capture, "_llamaparse_parse",
                        lambda p, cfg: ("Contact owner@example.com re MADDs.",
                                        {"engine": "llamaparse"}))
    stats = content_capture.parse_and_persist(con, CFG)
    assert stats["parsed"] == 1
    row = con.execute(
        "SELECT redacted_text, ocr_engine, source_path_hash FROM content_capture "
        "WHERE ocr_engine='llamaparse'").fetchone()
    assert "owner@example.com" not in row["redacted_text"]  # R6
    assert row["source_path_hash"] is not None


def test_dedup_second_run_makes_no_new_call(env, monkeypatch):
    con = _con()
    path = _make_file(env, "note.pdf")
    _touch_event(con, path)
    calls = {"n": 0}

    def fake_parse(p, cfg):
        calls["n"] += 1
        return "some parsed text long enough to persist", {"engine": "llamaparse"}

    monkeypatch.setattr(content_capture, "_llamaparse_parse", fake_parse)
    content_capture.parse_and_persist(con, CFG)
    content_capture.parse_and_persist(con, CFG)  # unchanged file
    assert calls["n"] == 1


def test_one_failure_does_not_stop_the_pass(env, monkeypatch):
    con = _con()
    p1 = _make_file(env, "bad.pdf")
    p2 = _make_file(env, "good.pdf")
    _touch_event(con, p1)
    _touch_event(con, p2)

    def flaky(p, cfg):
        if p == p1:
            return None, {"engine": "llamaparse"}  # degraded (R7)
        return "good content that is long enough to persist", {"engine": "llamaparse"}

    monkeypatch.setattr(content_capture, "_llamaparse_parse", flaky)
    stats = content_capture.parse_and_persist(con, CFG)
    assert stats["parsed"] == 1
    texts = [r["redacted_text"] for r in
             con.execute("SELECT redacted_text FROM content_capture WHERE ocr_engine='llamaparse'")]
    assert any("good content" in t for t in texts)


# ── search indexing (U4, R8) ────────────────────────────────────────────────────

def test_parsed_content_is_searchable(env, monkeypatch):
    con = _con()
    path = _make_file(env, "solar.pdf")
    _touch_event(con, path)
    monkeypatch.setattr(content_capture, "_llamaparse_parse",
                        lambda p, cfg: ("Solar water treatment concept for Zambia.",
                                        {"engine": "llamaparse"}))
    content_capture.parse_and_persist(con, CFG)
    search.reindex(con)
    results = search.search(con, "Zambia")
    assert any(r["atom_kind"] == "content_capture" for r in results)
