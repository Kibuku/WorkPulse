"""
Regression test for the "tag doesn't stick" bug.

When a user tags a window from the "Needs your attention" panel, /api/learn
writes a learned rule (learned_tags.json) and retags DB sessions. But the panel
is fed by /api/realwork, which reads the raw activity log and re-tags it at
read-time. That read-time pass used to consult only config path-patterns, never
the learned rules — so the just-tagged window kept reappearing as untagged,
looking like the tag hadn't taken. These tests lock in that a learned tag (and a
"never tag this" rule) is honored by /api/realwork immediately.

Run: python -m pytest tests/test_realwork_retag.py
"""

from __future__ import annotations

from datetime import date

from fastapi.testclient import TestClient

import workpulse.web.app as appmod
from workpulse.core import db as wp_db
from workpulse.core import learning


def _mk_session(title):
    # Shape mirrors what _load_activity_sessions yields; realwork reads these keys.
    return {
        "title": title,
        "exe_path": "",
        "app": "Word",
        "duration_s": 1800,
        "idle": False,
        "start": "2026-07-12T09:00:00+00:00",
        "stream": None,
    }


def _client(tmp_path, monkeypatch, sessions):
    cfg = {"paths": {"logs": str(tmp_path)},
           "streams": {"client-work": {"label": "Client work"}},
           "watcher": {"stream_folder_roots": {}, "stream_path_patterns": []}}
    monkeypatch.setattr(appmod, "load_config", lambda: cfg)
    monkeypatch.setattr(appmod, "_load_activity_sessions",
                        lambda c, target: [dict(s) for s in sessions])
    monkeypatch.setattr(wp_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    # rules live under paths.logs; resolve() is identity for absolute paths
    learning._RULES_CACHE["mtime"] = 0.0
    learning._RULES_CACHE["rules"] = []
    return TestClient(appmod.app)


def _untagged_titles(body):
    return [w["title"] for w in body["untagged_windows"]]


def test_learned_tag_removes_window_from_attention_panel(tmp_path, monkeypatch):
    title = "proposal_clientz_v2.docx - Word"
    client = _client(tmp_path, monkeypatch, [_mk_session(title)])

    # Before tagging: the window sits in the attention panel, untagged.
    before = client.get("/api/realwork").json()
    assert title[:70] in _untagged_titles(before)
    assert before["by_stream"] == []

    # Tag it — exactly what the "Tag as" dropdown posts.
    r = client.post("/api/learn", json={"raw_title": title, "stream": "client-work"})
    assert r.status_code == 200 and r.json()["ok"] is True

    # After tagging: gone from untagged, and its time now rolls up to the stream.
    after = client.get("/api/realwork").json()
    assert title[:70] not in _untagged_titles(after)
    assert [s["stream"] for s in after["by_stream"]] == ["client-work"]


def test_never_tag_removes_window_from_attention_panel(tmp_path, monkeypatch):
    title = "WhatsApp"
    client = _client(tmp_path, monkeypatch, [_mk_session(title)])

    assert title[:70] in _untagged_titles(client.get("/api/realwork").json())

    # "Never tag this (ignore)" posts stream: null.
    r = client.post("/api/learn", json={"raw_title": title, "stream": None})
    assert r.status_code == 200 and r.json()["ok"] is True

    after = client.get("/api/realwork").json()
    # It stays untagged time, but must not keep resurfacing for review.
    assert title[:70] not in _untagged_titles(after)
    assert after["by_stream"] == []
