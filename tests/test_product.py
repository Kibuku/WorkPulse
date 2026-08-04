from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from workpulse import product
from workpulse.web import app as appmod


def test_configure_persists_release_channel(tmp_path):
    target = tmp_path / "product.json"
    selected = product.configure("learning", "facilitator", path=target)

    assert selected.channel == "learning-facilitator"
    assert selected.entry_path == "/learning"
    assert selected.has("learning.facilitator")
    assert json.loads(target.read_text())["role"] == "facilitator"


def test_learning_device_does_not_gain_facilitator_capability(tmp_path, monkeypatch):
    target = tmp_path / "product.json"
    product.configure("learning", "device", path=target)
    monkeypatch.setattr(product, "MANIFEST_PATH", target)

    selected = product.current()
    assert selected.entry_path == "/learning?role=device"
    assert selected.has("learning.device")
    assert not selected.has("learning.facilitator")


def test_invalid_product_role_is_rejected(tmp_path):
    with pytest.raises(ValueError):
        product.configure("learning", "individual", path=tmp_path / "product.json")


def test_institution_is_the_commercial_workpulse_product(tmp_path, monkeypatch):
    target = tmp_path / "product.json"
    product.configure("institution", "member", path=target)
    monkeypatch.setattr(product, "MANIFEST_PATH", target)

    selected = product.current()
    assert selected.channel == "workpulse-institution"
    assert selected.entry_path == "/institution"
    assert selected.has("institution.member")
    assert not selected.has("learning.facilitator")


def test_legacy_personal_maps_to_private_developer_lab():
    selected = product._make("personal", "individual")
    assert selected.channel == "developer-lab"
    assert selected.has("developer.lab")


def test_device_url_cannot_grant_facilitator_api(monkeypatch):
    device = product._make("learning", "device")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: device)
    monkeypatch.setattr(appmod.product_identity, "has", device.has)
    client = TestClient(appmod.app)

    assert client.get("/learning").history
    assert client.get("/learning/device").status_code == 200
    assert client.get("/api/v2/classroom/status").status_code == 403


def test_facilitator_cannot_open_local_device_enrolment(monkeypatch):
    facilitator = product._make("learning", "facilitator")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: facilitator)
    monkeypatch.setattr(appmod.product_identity, "has", facilitator.has)
    client = TestClient(appmod.app)

    assert client.get("/learning").status_code == 200
    assert client.get("/learning/device").status_code == 403
    assert client.get("/api/v2/classroom/local-agent").status_code == 403
