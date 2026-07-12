"""
Tests for the LLM provider façade (core/llm.py, Ask WorkPulse Phase 0).

No real network or API keys: the anthropic-key check, the Ollama probe, and
readiness are monkeypatched so routing is tested deterministically offline.

Run: python -m pytest tests/test_llm_backend.py
"""

from __future__ import annotations

from workpulse.core import llm


# ── loose JSON extraction (matters most for local models) ─────────────────────

def test_parse_json_loose_variants():
    assert llm._parse_json_loose('{"a": 1}') == {"a": 1}
    assert llm._parse_json_loose('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm._parse_json_loose('sure, here it is: {"a": 1}. done') == {"a": 1}
    assert llm._parse_json_loose("not json at all") is None
    assert llm._parse_json_loose("") is None
    assert llm._parse_json_loose("[1, 2, 3]") is None  # array is not an object


# ── backend selection ─────────────────────────────────────────────────────────

def test_auto_prefers_anthropic_when_key(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: "sk-test")
    monkeypatch.setattr(llm, "_ollama_ready", lambda cfg: True)
    assert llm.active_backend({"llm": {"backend": "auto"}}) == "anthropic"


def test_auto_falls_to_ollama_without_key(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: None)
    monkeypatch.setattr(llm, "_ollama_ready", lambda cfg: True)
    assert llm.active_backend({"llm": {"backend": "auto"}}) == "ollama"


def test_auto_none_when_nothing_available(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: None)
    monkeypatch.setattr(llm, "_ollama_ready", lambda cfg: False)
    assert llm.active_backend({"llm": {"backend": "auto"}}) == "none"


def test_explicit_ollama_ignores_key(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: "sk-test")
    monkeypatch.setattr(llm, "_ollama_ready", lambda cfg: True)
    assert llm.active_backend({"llm": {"backend": "ollama"}}) == "ollama"


def test_explicit_anthropic_without_key_is_none(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: None)
    assert llm.active_backend({"llm": {"backend": "anthropic"}}) == "none"


def test_explicit_none(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: "sk-test")
    assert llm.active_backend({"llm": {"backend": "none"}}) == "none"


# ── ask_* with no backend degrades gracefully ─────────────────────────────────

def test_ask_text_none_backend_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "active_backend", lambda cfg=None: "none")
    text, meta = llm.ask_text("hello", cfg={})
    assert text is None
    assert meta["backend"] == "none"


def test_ask_json_none_backend_returns_none(monkeypatch):
    monkeypatch.setattr(llm, "active_backend", lambda cfg=None: "none")
    obj, meta = llm.ask_json("hello", cfg={})
    assert obj is None
    assert meta["backend"] == "none"


# ── status shape stays back-compatible with the dashboard ─────────────────────

def test_backend_status_shape(monkeypatch):
    monkeypatch.setattr(llm, "_anthropic_key", lambda cfg=None: None)
    monkeypatch.setattr(llm, "_probe_ollama",
                        lambda cfg=None, force=False:
                        {"ts": 0.0, "up": False, "models": [], "error": None})
    st = llm.backend_status({"llm": {}})
    assert set(st) == {"active", "anthropic", "ollama"}
    assert st["active"] == "none"
    assert set(st["ollama"]) == {"installed", "models", "want_model",
                                 "model_ready", "url", "error"}
    assert st["anthropic"]["available"] is False
