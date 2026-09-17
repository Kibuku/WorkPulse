"""
U2 — per-feature provider routing (plan R3, KTD1).

Each smart feature picks its provider+model via cfg.llm.features.<feature>,
falling back to the global backend, then the local floor. Callers that pass no
feature keep today's behavior exactly.

Run: .venv/bin/python -m pytest tests/test_llm_routing.py
"""

from __future__ import annotations

import io
import json

import pytest

from workpulse.core import llm


@pytest.fixture()
def keys(monkeypatch):
    # All provider keys absent unless a test opts one in.
    for fn in ("_gemini_key", "_anthropic_key", "_glm_key", "_deepseek_key"):
        monkeypatch.setattr(llm, fn, lambda cfg=None: None)
    monkeypatch.setattr(llm, "_ollama_ready", lambda cfg=None: False)
    return monkeypatch


# ── resolver ──────────────────────────────────────────────────────────────────

def test_feature_backend_and_model_win(keys):
    keys.setattr(llm, "_deepseek_key", lambda cfg=None: "k")
    cfg = {"llm": {"features": {"attribution": {"backend": "deepseek", "model": "deepseek-chat"}}}}
    assert llm._resolve_route(cfg, "attribution") == ("deepseek", "deepseek-chat")


def test_feature_falls_back_to_global_when_unset(keys):
    keys.setattr(llm, "_glm_key", lambda cfg=None: "k")
    cfg = {"llm": {"backend": "glm"}}  # no features block
    assert llm._resolve_route(cfg, "attribution") == ("glm", None)


def test_feature_falls_back_to_global_when_its_backend_has_no_key(keys):
    keys.setattr(llm, "_glm_key", lambda cfg=None: "k")  # global glm available
    cfg = {"llm": {"backend": "glm",
                   "features": {"attribution": {"backend": "deepseek"}}}}  # deepseek has no key
    assert llm._resolve_route(cfg, "attribution") == ("glm", None)


def test_no_feature_uses_global(keys):
    keys.setattr(llm, "_anthropic_key", lambda cfg=None: "k")
    cfg = {"llm": {"backend": "anthropic"}}
    assert llm._resolve_route(cfg, None) == ("anthropic", None)


# ── ask_text honours the feature route ──────────────────────────────────────────

def _fake_openai(text="HELLO"):
    body = json.dumps({"choices": [{"message": {"content": text}}],
                       "usage": {}}).encode()

    class _R(io.BytesIO):
        def __enter__(self): return self
        def __exit__(self, *a): return False
    return _R(body)


def test_ask_text_routes_by_feature(keys, monkeypatch):
    keys.setattr(llm, "_glm_key", lambda cfg=None: "k")
    import urllib.request
    monkeypatch.setattr(urllib.request, "urlopen", lambda req, timeout=None: _fake_openai())
    cfg = {"llm": {"backend": "none",
                   "features": {"discovery": {"backend": "glm"}}}}
    text, meta = llm.ask_text("hi", cfg=cfg, feature="discovery")
    assert text == "HELLO" and meta["backend"] == "glm"


def test_ask_text_no_feature_backcompat(keys):
    # No feature, global none -> unchanged behavior (None).
    text, meta = llm.ask_text("hi", cfg={"llm": {"backend": "none"}})
    assert text is None
