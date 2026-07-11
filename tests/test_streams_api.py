"""
Tests for the stream-management endpoints (the taxonomy wizard's backend).

These were restored so the pilot's onboarding wizard can create/edit/delete
streams from the dashboard instead of hand-editing YAML. Config writes are
redirected to a tmp file so the real config.yaml is never touched.

Run: python -m pytest tests/test_streams_api.py
"""

from __future__ import annotations

import yaml
from fastapi.testclient import TestClient

import workpulse.web.app as app


def _client(tmp_path, monkeypatch):
    """TestClient whose config/config.yaml resolves into tmp_path."""
    cfgfile = tmp_path / "config.yaml"
    real_resolve = app.resolve

    def fake_resolve(rel):
        if rel == "config/config.yaml":
            return cfgfile
        return real_resolve(rel)

    monkeypatch.setattr(app, "resolve", fake_resolve)
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

    # Delete child, then parent.
    assert client.delete("/api/streams/acme").status_code == 200
    assert client.delete("/api/streams/work").status_code == 200
    cfg = yaml.safe_load(cfgfile.read_text())
    assert not (cfg.get("streams") or {})


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
