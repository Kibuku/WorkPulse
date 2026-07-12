"""
Tests for the stream-management endpoints (the taxonomy wizard's backend).

These were restored so the pilot's onboarding wizard can create/edit/delete
streams from the dashboard instead of hand-editing YAML. Config writes are
redirected to a tmp file so the real config.yaml is never touched.

Run: python -m pytest tests/test_streams_api.py
"""

from __future__ import annotations

import copy

import yaml
from fastapi.testclient import TestClient

import workpulse.web.app as app
from workpulse.common import ROOT

# Hermetic seed: the bundled template, NOT the developer's real config.yaml
# (which may exist locally and would otherwise leak streams into these tests).
_EXAMPLE_CFG = yaml.safe_load(
    (ROOT / "config" / "config.example.yaml").read_text(encoding="utf-8")
) or {}


def _client(tmp_path, monkeypatch):
    """TestClient whose config resolves + loads from tmp_path (never the real
    config.yaml). Both the /api/streams writers (resolve) and readers like
    /api/system (load_config) are redirected so read-back is consistent."""
    cfgfile = tmp_path / "config.yaml"
    real_resolve = app.resolve

    def fake_resolve(rel):
        if rel == "config/config.yaml":
            return cfgfile
        return real_resolve(rel)

    def fake_load_config():
        if cfgfile.exists():
            return yaml.safe_load(cfgfile.read_text(encoding="utf-8")) or {}
        return copy.deepcopy(_EXAMPLE_CFG)

    monkeypatch.setattr(app, "resolve", fake_resolve)
    monkeypatch.setattr(app, "load_config", fake_load_config)
    return TestClient(app.app), cfgfile


def test_first_add_materialises_config(tmp_path, monkeypatch):
    """On a fresh install (no config.yaml), the first add creates a complete,
    working config.yaml seeded from the template — not a 500."""
    client, cfgfile = _client(tmp_path, monkeypatch)
    assert not cfgfile.exists()

    r = client.post("/api/streams", json={"key": "acme-web", "label": "Acme Web"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["key"] == "acme-web"
    assert body["breadcrumb"] == "Acme Web"

    cfg = yaml.safe_load(cfgfile.read_text())
    assert cfg["streams"]["acme-web"] == {"label": "Acme Web"}
    # Seeded from the template, so real config blocks are present + editable.
    assert "watcher" in cfg and "paths" in cfg


def test_add_child_edit_and_delete_flow(tmp_path, monkeypatch):
    client, cfgfile = _client(tmp_path, monkeypatch)

    assert client.post("/api/streams", json={"key": "work", "label": "Work"}).status_code == 200
    r = client.post("/api/streams", json={"key": "acme", "label": "Acme", "parent": "work"})
    assert r.status_code == 200
    assert r.json()["breadcrumb"] == "Work › Acme"

    # Rename the child.
    r = client.patch("/api/streams/acme", json={"label": "Acme Web"})
    assert r.status_code == 200 and r.json()["label"] == "Acme Web"

    # Can't delete the parent while it has a child.
    r = client.delete("/api/streams/work")
    assert r.status_code == 400 and "children" in r.json()["error"]

    # Delete requires explicit confirmation (surfaces impact first).
    prev = client.delete("/api/streams/acme")
    assert prev.status_code == 200 and prev.json().get("needs_confirm") is True
    # Delete child, then parent, with confirm.
    assert client.delete("/api/streams/acme?confirm=true").json()["ok"] is True
    assert client.delete("/api/streams/work?confirm=true").json()["ok"] is True
    cfg = yaml.safe_load(cfgfile.read_text())
    assert not (cfg.get("streams") or {})


def test_recognize_routes_folder_vs_keyword(tmp_path, monkeypatch):
    """A folder-like hint lands in stream_folder_roots; a keyword lands in
    stream_path_patterns — the two structures activity.py actually reads."""
    client, cfgfile = _client(tmp_path, monkeypatch)

    r = client.post("/api/streams", json={"key": "acme-web", "label": "Acme Web",
                                          "recognize": "~/Clients/Acme"})
    assert r.status_code == 200 and r.json()["recognize"] == "~/Clients/Acme"
    r = client.post("/api/streams", json={"key": "contoso", "label": "Contoso",
                                          "recognize": "contoso"})
    assert r.status_code == 200

    cfg = yaml.safe_load(cfgfile.read_text())
    w = cfg["watcher"]
    assert w["stream_folder_roots"]["acme-web"] == ["~/Clients/Acme"]   # folder → roots
    assert {"path": "contoso", "stream": "contoso"} in w["stream_path_patterns"]  # keyword


def test_recognize_readback_and_patch_replace(tmp_path, monkeypatch):
    """/api/system exposes each stream's recognize hint, and PATCH replaces it
    (no accumulation), including switching keyword→folder."""
    client, cfgfile = _client(tmp_path, monkeypatch)
    client.post("/api/streams", json={"key": "acme", "label": "Acme", "recognize": "acme"})

    streams = {s["key"]: s for s in client.get("/api/system").json()["streams"]}
    assert streams["acme"]["recognize"] == "acme"

    # Replace keyword with a folder — old keyword pattern must be gone.
    r = client.patch("/api/streams/acme", json={"recognize": "~/Work/Acme"})
    assert r.status_code == 200 and r.json()["recognize"] == "~/Work/Acme"
    cfg = yaml.safe_load(cfgfile.read_text())
    assert cfg["watcher"]["stream_folder_roots"]["acme"] == ["~/Work/Acme"]
    assert all(p.get("stream") != "acme" for p in cfg["watcher"].get("stream_path_patterns", []))

    # Clearing it empties both.
    client.patch("/api/streams/acme", json={"recognize": ""})
    cfg = yaml.safe_load(cfgfile.read_text())
    assert "acme" not in (cfg["watcher"].get("stream_folder_roots") or {})


def test_validation_and_cycle_guard(tmp_path, monkeypatch):
    client, cfgfile = _client(tmp_path, monkeypatch)

    # Bad key.
    assert client.post("/api/streams", json={"key": "Bad Key!", "label": "x"}).status_code == 400
    # Missing label.
    assert client.post("/api/streams", json={"key": "ok", "label": ""}).status_code == 400

    assert client.post("/api/streams", json={"key": "alpha", "label": "A"}).status_code == 200
    assert client.post("/api/streams", json={"key": "beta", "label": "B", "parent": "alpha"}).status_code == 200
    # Duplicate.
    assert client.post("/api/streams", json={"key": "alpha", "label": "A2"}).status_code == 409
    # Cycle: making 'alpha' a child of its own descendant 'beta' must be refused.
    r = client.patch("/api/streams/alpha", json={"parent": "beta"})
    assert r.status_code == 400 and "descendant" in r.json()["error"]


def test_delete_migrated_db_only_stream(tmp_path, monkeypatch):
    """A stream that exists only in the DB (migrated from v1, config streams
    null) must be deletable, not 'stream not found'."""
    from fastapi.testclient import TestClient
    import workpulse.web.app as appmod
    from workpulse.core import db as _db
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setattr(_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    real_resolve = appmod.resolve
    monkeypatch.setattr(appmod, "resolve",
                        lambda rel: cfgfile if rel == "config/config.yaml" else real_resolve(rel))
    monkeypatch.setattr(appmod, "load_config",
                        lambda: {"paths": {"logs": str(tmp_path)}, "streams": None})
    from workpulse.core import atoms
    from datetime import datetime, timedelta, timezone
    con = _db.connect(cfg={"paths": {}})
    con.execute("INSERT OR IGNORE INTO stream(key,label,parent_key) VALUES ('oldproj','Old Proj',NULL)")
    con.commit()
    # a session tagged to it — the FK that used to silently block the delete
    now = datetime.now(timezone.utc)
    sid = atoms.write_session(con, app="Word", title="old work", stream="oldproj",
                              started_at=now.isoformat())
    atoms.close_session(con, sid, ended_at=(now + timedelta(minutes=30)).isoformat())
    client = TestClient(appmod.app)

    # Phase 1: preview the impact, delete nothing.
    prev = client.delete("/api/streams/oldproj")
    assert prev.status_code == 200 and prev.json().get("needs_confirm") is True
    assert prev.json()["impact"]["sessions"] == 1
    assert _db.connect(cfg={"paths": {}}).execute(
        "SELECT 1 FROM stream WHERE key='oldproj'").fetchone() is not None  # still there

    # Phase 2: confirm -> deletes + untags the session.
    r = client.delete("/api/streams/oldproj?confirm=true")
    assert r.status_code == 200, r.text
    assert r.json().get("deleted") == "oldproj"
    check = _db.connect(cfg={"paths": {}})
    assert check.execute("SELECT 1 FROM stream WHERE key='oldproj'").fetchone() is None
    assert check.execute("SELECT stream FROM session WHERE id=?", (sid,)).fetchone()[0] is None


def test_migrated_db_streams_are_not_flagged_as_unset(tmp_path, monkeypatch):
    """A user who migrated from v1 has streams in the DB but a null config
    `streams:`. /api/system must NOT nag them to set up streams
    (taxonomy_trivial is False once >= 2 real streams exist in the DB)."""
    import workpulse.web.app as appmod
    from workpulse.core import db as _db
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setattr(_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    real_resolve = appmod.resolve
    monkeypatch.setattr(appmod, "resolve",
                        lambda rel: cfgfile if rel == "config/config.yaml" else real_resolve(rel))
    monkeypatch.setattr(appmod, "load_config",
                        lambda: {"paths": {"logs": str(tmp_path)}, "streams": None})
    con = _db.connect(cfg={"paths": {}})
    con.execute("INSERT OR IGNORE INTO stream(key,label,parent_key) VALUES ('majicom','Majicom',NULL)")
    con.execute("INSERT OR IGNORE INTO stream(key,label,parent_key) VALUES ('consulting','Consulting',NULL)")
    con.commit()

    client = TestClient(appmod.app)
    body = client.get("/api/system").json()
    assert body["taxonomy_trivial"] is False


def test_empty_config_and_db_is_flagged_as_unset(tmp_path, monkeypatch):
    """Conversely, a genuinely fresh install (no config streams, no DB streams)
    still gets the setup nudge."""
    import workpulse.web.app as appmod
    from workpulse.core import db as _db
    cfgfile = tmp_path / "config.yaml"
    monkeypatch.setattr(_db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    real_resolve = appmod.resolve
    monkeypatch.setattr(appmod, "resolve",
                        lambda rel: cfgfile if rel == "config/config.yaml" else real_resolve(rel))
    monkeypatch.setattr(appmod, "load_config",
                        lambda: {"paths": {"logs": str(tmp_path)}, "streams": None})
    _db.connect(cfg={"paths": {}})  # create schema, no streams
    client = TestClient(appmod.app)
    assert client.get("/api/system").json()["taxonomy_trivial"] is True
