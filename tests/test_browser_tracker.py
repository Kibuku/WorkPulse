"""
Tests for scripts/browser_tracker.py + scripts/personal.py.

AppleScript / NSWorkspace probes are monkeypatched. We test:
  - URL classification: deny / private / project / unknown
  - sample_once writes browser_visit + browser_visit_local correctly
  - deny domains drop the visit entirely (no row written)
  - private domains route to stream='personal' with is_private=1
  - personal auth: set / verify / token lifecycle
  - personal stream is marked private after migration 0007

Run: python -m pytest tests/test_browser_tracker.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.signals import browser_tracker as bt

from workpulse.core import personal as wp_personal
from workpulse.core import db
from workpulse.core import projects as wp_projects


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    # Reset projects cache so per-test loads pick up the real YAML
    wp_projects._CACHE["mtime"] = 0
    wp_projects._CACHE["projects"] = []
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


# ── migration: personal stream is marked private ───────────────────────────

def test_personal_stream_is_private_after_migration(env):
    con = _con()
    row = con.execute(
        "SELECT is_private FROM stream WHERE key='personal'"
    ).fetchone()
    assert row is not None
    assert row["is_private"] == 1


def test_other_streams_default_not_private(env):
    con = _con()
    # The projects.yaml lazy-upserts streams later; create one directly
    con.execute(
        "INSERT INTO stream(key, label, parent_key) VALUES ('uganda', 'Uganda', NULL)"
    )
    row = con.execute(
        "SELECT is_private FROM stream WHERE key='uganda'"
    ).fetchone()
    assert row["is_private"] == 0


# ── URL classification ─────────────────────────────────────────────────────

def test_classify_url_project_match():
    c = wp_projects.classify_url(
        "https://verst.sharepoint.com/sites/uganda-memd/Shared%20Documents/x.docx"
    )
    assert c["tier"] == "project"
    assert c["stream"] == "uganda"


def test_classify_url_private_domain():
    c = wp_projects.classify_url("https://www.chase.com/personal/banking")
    assert c["tier"] == "private"
    assert c["reason"] == "chase.com"


def test_classify_url_deny_domain():
    c = wp_projects.classify_url("https://login.microsoftonline.com/auth?...")
    assert c["tier"] == "deny"


def test_classify_url_deny_beats_project_match():
    """Even if a deny domain contains a project keyword, deny wins."""
    c = wp_projects.classify_url("https://appleid.apple.com/auth?")
    assert c["tier"] == "deny"


def test_classify_url_unknown():
    c = wp_projects.classify_url("https://example.com/random")
    assert c["tier"] == "unknown"


def test_classify_url_empty():
    assert wp_projects.classify_url("")["tier"] == "unknown"


# ── is_browser / read_tab_for / record_visit (the activity-bound path) ──────

def test_is_browser_recognises_known_browsers():
    assert bt.is_browser("Safari") is True
    assert bt.is_browser("Google Chrome") is True
    assert bt.is_browser("Brave Browser") is True
    assert bt.is_browser("Microsoft Edge") is True


def test_is_browser_rejects_non_browsers():
    assert bt.is_browser("Claude") is False
    assert bt.is_browser("Microsoft Word") is False
    assert bt.is_browser("") is False
    assert bt.is_browser(None) is False


def test_read_tab_for_non_browser_returns_none():
    # Non-browser app name → no AppleScript attempted, None returned.
    assert bt.read_tab_for("Claude") is None
    assert bt.read_tab_for("") is None


def test_record_visit_binds_to_supplied_ts(env):
    con = _con()
    fixed_ts = "2026-07-02T09:15:00+00:00"
    res = bt.record_visit(
        con,
        {"app": "Safari", "url": "https://verst.sharepoint.com/sites/uganda-memd/x",
         "title": "x"},
        ts=fixed_ts,
    )
    assert res["tier"] == "project"
    assert res["stream"] == "uganda"
    row = con.execute("SELECT ts FROM browser_visit WHERE id = ?",
                      (res["id"],)).fetchone()
    assert row["ts"] == fixed_ts  # visit bound to the session's clock


def test_record_visit_empty_tab_returns_none(env):
    con = _con()
    assert bt.record_visit(con, {}) is None
    assert bt.record_visit(con, {"app": "Safari", "url": ""}) is None


def test_record_visit_deny_writes_nothing(env):
    con = _con()
    res = bt.record_visit(
        con, {"app": "Safari", "url": "https://login.microsoftonline.com/x", "title": "t"})
    assert res["tier"] == "deny"
    n = con.execute("SELECT COUNT(*) AS n FROM browser_visit").fetchone()["n"]
    assert n == 0


# ── sample_once writes ──────────────────────────────────────────────────────

def _fake_tab(monkeypatch, *, url, title="t", app="Safari"):
    monkeypatch.setattr(
        bt, "current_browser_tab",
        lambda: {"app": app, "url": url, "title": title},
    )


def test_sample_writes_project_visit(env, monkeypatch):
    con = _con()
    _fake_tab(monkeypatch,
              url="https://verst.sharepoint.com/sites/Uganda-MEMD/x.docx",
              title="x.docx - Uganda MEMD")
    res = bt.sample_once(con)
    assert res["tier"] == "project"
    assert res["stream"] == "uganda"
    assert res["is_private"] is False
    n = con.execute("SELECT COUNT(*) AS n FROM browser_visit").fetchone()["n"]
    assert n == 1


def test_sample_writes_private_visit(env, monkeypatch):
    con = _con()
    _fake_tab(monkeypatch,
              url="https://chase.com/personal/banking",
              title="My Chase Account")
    res = bt.sample_once(con)
    assert res["tier"] == "private"
    assert res["stream"] == "personal"
    assert res["is_private"] is True
    row = con.execute("SELECT stream, is_private FROM browser_visit").fetchone()
    assert row["stream"] == "personal"
    assert row["is_private"] == 1


def test_sample_drops_deny_visit(env, monkeypatch):
    con = _con()
    _fake_tab(monkeypatch, url="https://login.microsoftonline.com/auth")
    res = bt.sample_once(con)
    assert res["tier"] == "deny"
    # NO row written
    n = con.execute("SELECT COUNT(*) AS n FROM browser_visit").fetchone()["n"]
    assert n == 0


def test_sample_returns_none_when_no_browser_focused(env, monkeypatch):
    monkeypatch.setattr(bt, "current_browser_tab", lambda: None)
    con = _con()
    assert bt.sample_once(con) is None


def test_sample_unknown_url_writes_with_null_stream(env, monkeypatch):
    con = _con()
    _fake_tab(monkeypatch, url="https://example.com/random")
    res = bt.sample_once(con)
    assert res["tier"] == "unknown"
    assert res["stream"] is None
    row = con.execute("SELECT stream, is_private FROM browser_visit").fetchone()
    assert row["stream"] is None
    assert row["is_private"] == 0


def test_sample_writes_raw_to_local_table(env, monkeypatch):
    """The public table only holds hashes; raw url + title live in
    browser_visit_local, parallel to session_local / file_event_local."""
    con = _con()
    _fake_tab(monkeypatch,
              url="https://chatgpt.com/c/some-conversation-id",
              title="Some Conversation")
    bt.sample_once(con)
    pub = con.execute("SELECT url_hash, title_hash FROM browser_visit").fetchone()
    loc = con.execute(
        "SELECT raw_url, raw_title FROM browser_visit_local"
    ).fetchone()
    assert "chatgpt" not in pub["url_hash"]   # de-identified
    assert loc["raw_url"] == "https://chatgpt.com/c/some-conversation-id"
    assert loc["raw_title"] == "Some Conversation"


# ── personal auth ───────────────────────────────────────────────────────────

def test_password_initially_unset(env):
    con = _con()
    assert wp_personal.is_password_set(con) is False


def test_set_and_verify_password(env):
    con = _con()
    wp_personal.set_password(con, "test-1234")
    assert wp_personal.is_password_set(con)
    assert wp_personal.verify_password(con, "test-1234") is True
    assert wp_personal.verify_password(con, "wrong") is False


def test_password_too_short_refused(env):
    con = _con()
    with pytest.raises(ValueError):
        wp_personal.set_password(con, "abc")


def test_password_change_invalidates_token(env):
    con = _con()
    wp_personal.set_password(con, "test-1234")
    token = wp_personal.create_unlock_token(con)
    assert wp_personal.validate_token(con, token) is True
    # Change password → outstanding tokens invalidated
    wp_personal.set_password(con, "new-password")
    assert wp_personal.validate_token(con, token) is False


def test_token_expires(env, monkeypatch):
    con = _con()
    wp_personal.set_password(con, "test-1234")
    token = wp_personal.create_unlock_token(con, ttl_minutes=30)
    assert wp_personal.validate_token(con, token) is True
    # Manually expire it
    con.execute(
        "UPDATE personal_unlock_token SET expires_at = ? WHERE token = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(), token),
    )
    assert wp_personal.validate_token(con, token) is False


def test_purge_expired_drops_old_tokens(env):
    con = _con()
    wp_personal.set_password(con, "test-1234")
    wp_personal.create_unlock_token(con)
    # Force-expire it
    con.execute(
        "UPDATE personal_unlock_token SET expires_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat(),),
    )
    n = wp_personal.purge_expired_tokens(con)
    assert n == 1
    row = con.execute("SELECT COUNT(*) AS n FROM personal_unlock_token").fetchone()
    assert row["n"] == 0


def test_validate_token_rejects_garbage(env):
    con = _con()
    wp_personal.set_password(con, "test-1234")
    assert wp_personal.validate_token(con, None) is False
    assert wp_personal.validate_token(con, "") is False
    assert wp_personal.validate_token(con, "not-a-real-token") is False


def test_create_token_fails_with_no_password(env):
    con = _con()
    with pytest.raises(RuntimeError):
        wp_personal.create_unlock_token(con)
