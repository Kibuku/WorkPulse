from __future__ import annotations

import hashlib
import io
import json
import sys

from fastapi.testclient import TestClient

from workpulse.core import updater
from workpulse.web import app as appmod


class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


def _manifest(version="9.0.0", content=b"installer"):
    suffix = "pkg" if sys.platform == "darwin" else "exe"
    key = updater.platform_key() or "macos"
    return {
        "version": version,
        "notes": "A safer update.",
        "platforms": {
            key: {
                "filename": f"WorkPulse-{version}.{suffix}",
                "url": f"https://example.test/WorkPulse-{version}.{suffix}",
                "sha256": hashlib.sha256(content).hexdigest(),
            }
        },
    }


def test_check_reports_newer_verified_release(monkeypatch):
    payload = json.dumps(_manifest()).encode()
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: _Response(payload))

    result = updater.check()

    assert result["update_available"] is True
    assert result["latest_version"] == "9.0.0"
    assert result["artifact"]["sha256"]


def test_check_does_not_offer_same_version(monkeypatch):
    payload = json.dumps(_manifest(updater.__version__)).encode()
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: _Response(payload))

    assert updater.check()["update_available"] is False


def test_download_rejects_checksum_mismatch(tmp_path, monkeypatch):
    content = b"tampered"
    offered = _manifest(content=b"expected")
    key = updater.platform_key() or "macos"
    update = {"artifact": offered["platforms"][key]}
    monkeypatch.setattr(updater, "ROOT", tmp_path)
    monkeypatch.setattr(updater.urllib.request, "urlopen",
                        lambda *a, **k: _Response(content))

    try:
        updater.download(update)
        raise AssertionError("checksum mismatch was accepted")
    except ValueError as exc:
        assert "checksum" in str(exc).lower()
    assert not list((tmp_path / "updates").glob("*.part"))


def test_install_endpoint_requires_explicit_dashboard_header(monkeypatch):
    called = []
    monkeypatch.setattr(
        updater, "download_and_launch",
        lambda: called.append(True) or {"launched": True},
    )
    client = TestClient(appmod.app)

    assert client.post("/api/update/install").status_code == 403
    assert called == []
    response = client.post(
        "/api/update/install",
        headers={"X-WorkPulse-Action": "install-update"},
    )
    assert response.status_code == 200
    assert response.json()["launched"] is True
