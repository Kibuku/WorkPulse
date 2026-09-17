"""
U1 — OpenAI-compatible provider adapter (GLM + DeepSeek).

GLM and DeepSeek expose /chat/completions; one shared adapter serves both.
Tested with mocked HTTP only — no network, no real keys (plan R1, KTD2/KTD6).

Run: .venv/bin/python -m pytest tests/test_llm_providers.py
"""

from __future__ import annotations

import io
import json

import pytest

from workpulse.core import llm


def _fake_openai_response(text="HELLO", pt=11, ct=3):
    body = json.dumps({
        "choices": [{"message": {"content": text}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }).encode()

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

    return _Resp(body)


@pytest.fixture()
def mock_http(monkeypatch):
    calls = {}

    def fake_urlopen(req, timeout=None):
        calls["url"] = req.full_url if hasattr(req, "full_url") else req
        calls["headers"] = dict(getattr(req, "headers", {}))
        calls["body"] = getattr(req, "data", None)
        return _fake_openai_response()

    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return calls


# ── GLM ─────────────────────────────────────────────────────────────────────────

def test_glm_text_returns_completion(monkeypatch, mock_http):
    monkeypatch.setattr(llm, "_glm_key", lambda cfg=None: "fake-key")
    cfg = {"llm": {"backend": "glm"}}
    text, meta = llm.ask_text("hi", cfg=cfg)
    assert text == "HELLO"
    assert meta["backend"] == "glm"


# ── DeepSeek ──────────────────────────────────────────────────────────────────

def test_deepseek_text_returns_completion(monkeypatch, mock_http):
    monkeypatch.setattr(llm, "_deepseek_key", lambda cfg=None: "fake-key")
    cfg = {"llm": {"backend": "deepseek"}}
    text, meta = llm.ask_text("hi", cfg=cfg)
    assert text == "HELLO"
    assert meta["backend"] == "deepseek"


def test_deepseek_json_parses(monkeypatch, monkeypatch2=None):
    monkeypatch.setattr(llm, "_deepseek_key", lambda cfg=None: "fake-key")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda req, timeout=None: _fake_openai_response('{"ok": true}'))
    obj, meta = llm.ask_json("hi", cfg={"llm": {"backend": "deepseek"}})
    assert obj == {"ok": True}


# ── no key -> none (local-first floor, KTD5) ─────────────────────────────────────

def test_glm_no_key_resolves_none(monkeypatch):
    monkeypatch.setattr(llm, "_glm_key", lambda cfg=None: None)
    cfg = {"llm": {"backend": "glm"}}
    assert llm.active_backend(cfg) == "none"
    text, meta = llm.ask_text("hi", cfg=cfg)
    assert text is None


# ── backend_status reports the new providers ─────────────────────────────────────

def test_backend_status_reports_glm_deepseek(monkeypatch):
    monkeypatch.setattr(llm, "_glm_key", lambda cfg=None: "k")
    monkeypatch.setattr(llm, "_deepseek_key", lambda cfg=None: None)
    st = llm.backend_status({"llm": {"backend": "glm"}})
    assert st["glm"]["available"] is True
    assert st["deepseek"]["available"] is False
