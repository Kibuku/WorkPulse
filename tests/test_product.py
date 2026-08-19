from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from workpulse import product
from workpulse.web import app as appmod


ROOT = Path(__file__).resolve().parents[1]


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


def test_personal_is_the_public_individual_product():
    selected = product._make("personal", "individual")
    assert selected.channel == "personal-workpulse"
    assert selected.entry_path == "/personal"
    assert selected.has("personal.view")
    assert not selected.has("developer.lab")


def test_public_installer_workflow_builds_only_personal():
    workflow = (ROOT / ".github" / "workflows" / "desktop-installers.yml").read_text()
    assert "build-windows.ps1 -Flavor personal" in workflow
    assert "build-macos.sh personal" in workflow
    assert "PersonalWorkPulseSetup-$Version-Windows.exe" in workflow
    assert "PersonalWorkPulse-$VERSION-macOS.pkg" in workflow
    assert "WorkPulseInstitution" not in workflow
    assert "LearningPulseFacilitator" not in workflow
    assert "LearningPulseDevice" not in workflow


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


def test_personal_html_omits_institution_and_classroom_surfaces(monkeypatch):
    personal = product._make("personal", "individual")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: personal)
    monkeypatch.setattr(appmod.product_identity, "has", personal.has)
    client = TestClient(appmod.app)

    response = client.get("/personal")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert 'data-workspace-target="organization"' not in response.text
    assert 'data-workspace-target="classroom"' not in response.text
    assert 'id="organization-workspace"' not in response.text
    assert 'id="classroom-workspace"' not in response.text
    assert "WorkPulse spaces" not in response.text
    assert "__WORKPULSE_ASSET_VERSION__" not in response.text
    assert f"dashboard.js?v={appmod.__version__}" in response.text
    assert client.get("/static/index.html").status_code == 404


def test_personal_rejects_institution_and_learning_apis(monkeypatch):
    personal = product._make("personal", "individual")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: personal)
    monkeypatch.setattr(appmod.product_identity, "has", personal.has)
    client = TestClient(appmod.app)

    assert client.get("/api/v2/organization/preview").status_code == 403
    assert client.get("/api/v2/organization/context").status_code == 403
    assert client.get("/api/v2/classroom/status").status_code == 403
    assert client.post("/api/v2/classroom/agent/enroll", json={}).status_code == 403


def test_learning_roles_receive_shared_learning_surface(monkeypatch):
    for role, path in (("facilitator", "/learning"), ("device", "/learning/device")):
        selected = product._make("learning", role)
        monkeypatch.setattr(appmod.product_identity, "current", lambda: selected)
        monkeypatch.setattr(appmod.product_identity, "has", selected.has)
        response = TestClient(appmod.app).get(path)
        assert response.status_code == 200
        assert 'id="classroom-workspace"' in response.text


def test_institution_and_learning_navigation_do_not_collide(monkeypatch):
    institution = product._make("institution", "member")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: institution)
    monkeypatch.setattr(appmod.product_identity, "has", institution.has)
    institution_html = TestClient(appmod.app).get("/institution").text
    assert 'data-workspace-target="organization"' in institution_html
    assert 'data-workspace-target="classroom"' not in institution_html
    assert 'id="classroom-workspace"' not in institution_html

    facilitator = product._make("learning", "facilitator")
    monkeypatch.setattr(appmod.product_identity, "current", lambda: facilitator)
    monkeypatch.setattr(appmod.product_identity, "has", facilitator.has)
    learning_html = TestClient(appmod.app).get("/learning").text
    assert 'data-workspace-target="organization"' not in learning_html
    assert 'data-workspace-target="classroom"' in learning_html
    assert 'id="organization-workspace"' not in learning_html
