"""
Tests for `wp capture` (PLAN.md §7 step 4).

Verifies:
  - Positional, --file, and --stdin body modes.
  - --pin with kind:id and validation of unknown kinds.
  - --auto-pin grabs the latest session id.
  - Pinned captures land a typed 'about' edge.
  - Markdown sidecar is appended under captures/YYYY-MM-DD.md.
  - Empty body refuses to write.
  - --quiet prints only the id.

Run: python -m pytest tests/test_capture.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts import atoms, capture as capmod, db


@pytest.fixture()
def env(tmp_path, monkeypatch, capsys):
    """Fresh DB + redirected captures dir per test."""
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    monkeypatch.setattr(capmod, "ROOT", tmp_path)
    def _fake_captures_dir():
        p = tmp_path / "captures"
        p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(capmod, "_captures_dir", _fake_captures_dir)
    from scripts import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con(env):
    return db.connect(cfg={"paths": {}})


def test_positional_capture(env, capsys):
    rc = capmod.main(["wp-capture", "Mwangi", "prefers", "Mt.", "Elgon", "framing"])
    assert rc == 0
    con = _con(env)
    row = con.execute("SELECT * FROM capture").fetchone()
    assert row["body"] == "Mwangi prefers Mt. Elgon framing"
    assert row["author"] == "human"


def test_file_mode(env, tmp_path):
    body_file = tmp_path / "note.md"
    body_file.write_text("from a file", encoding="utf-8")
    capmod.main(["wp-capture", "--file", str(body_file)])
    con = _con(env)
    row = con.execute("SELECT body FROM capture").fetchone()
    assert row["body"] == "from a file"


def test_stdin_mode(env, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("piped thought\n"))
    capmod.main(["wp-capture", "--stdin"])
    con = _con(env)
    row = con.execute("SELECT body FROM capture").fetchone()
    assert row["body"] == "piped thought"


def test_pin_creates_edge(env):
    con = _con(env)
    sid = atoms.write_session(con, app="X", title="t")
    capmod.main(["wp-capture", "ship-relevant", "--pin", f"session:{sid}"])
    edge = con.execute(
        "SELECT rel, dst_kind, dst_id FROM edge WHERE rel='about'"
    ).fetchone()
    assert edge["dst_kind"] == "session"
    assert edge["dst_id"] == sid


def test_pin_validates_kind(env):
    with pytest.raises(SystemExit):
        capmod.main(["wp-capture", "x", "--pin", "wrong:abc"])


def test_pin_requires_colon(env):
    with pytest.raises(SystemExit):
        capmod.main(["wp-capture", "x", "--pin", "session-abc"])


def test_auto_pin_grabs_latest_session(env):
    con = _con(env)
    atoms.write_session(con, app="X", title="old")
    sid_latest = atoms.write_session(con, app="Y", title="new")
    capmod.main(["wp-capture", "thought", "--auto-pin"])
    edge = con.execute(
        "SELECT dst_id FROM edge WHERE rel='about'"
    ).fetchone()
    assert edge["dst_id"] == sid_latest


def test_auto_pin_with_no_sessions_is_unpinned(env):
    capmod.main(["wp-capture", "first thought ever", "--auto-pin"])
    con = _con(env)
    row = con.execute("SELECT pinned_kind, pinned_id FROM capture").fetchone()
    assert row["pinned_kind"] is None and row["pinned_id"] is None


def test_markdown_sidecar(env):
    capmod.main(["wp-capture", "sidecar test"])
    md_files = list((env / "captures").glob("*.md"))
    assert len(md_files) == 1
    text = md_files[0].read_text(encoding="utf-8")
    assert "sidecar test" in text
    assert text.startswith("# Captures")


def test_empty_body_refused(env):
    with pytest.raises(SystemExit):
        capmod.main(["wp-capture", "   "])


def test_quiet_prints_only_id(env, capsys):
    capmod.main(["wp-capture", "x", "--quiet"])
    out = capsys.readouterr().out.strip().splitlines()
    assert len(out) == 1
    # Capture ids are 26 chars (10 time + 16 random, Crockford base32)
    assert len(out[0]) == 26


def test_system_author(env):
    capmod.main(["wp-capture", "dream cycle notes", "--author", "system"])
    con = _con(env)
    row = con.execute("SELECT author FROM capture").fetchone()
    assert row["author"] == "system"
