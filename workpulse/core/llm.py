"""
llm.py — backend status for the dashboard chrome.

v2 standardises on Anthropic (see ``core.think._call_anthropic``); the v1
Ollama/local path was dropped. This module exists purely so ``/api/system``
can report *which* backend is live and whether the AI features are usable.

The return shape is kept back-compatible with the v1 dashboard JS, which
reads ``llm.ollama.{installed,model_ready,want_model}`` behind null-guards —
so a stubbed, always-absent ``ollama`` block renders cleanly (the Ollama
hints simply never show). The v1 dashboard panels get replaced in Phase 3b.

Public API:
    active_backend(cfg) -> "anthropic" | "none"
    backend_status(cfg) -> dict
"""

from __future__ import annotations

from workpulse.common import load_config
from workpulse.wp_secrets import get as get_secret


def active_backend(cfg: dict | None = None) -> str:
    """Return the backend the AI features would actually use right now.

    v2 is Anthropic-only: available iff an Anthropic key is configured."""
    if cfg is None:
        cfg = load_config()
    return "anthropic" if get_secret("anthropic_key") else "none"


def backend_status(cfg: dict | None = None) -> dict:
    """Diagnostic snapshot for /api/system and the Settings page.

    The ``ollama`` block is a permanent absent-stub for back-compat with the
    v1 dashboard JS; v2 does not talk to Ollama."""
    if cfg is None:
        cfg = load_config()
    return {
        "active": active_backend(cfg),
        "anthropic": {
            "available": bool(get_secret("anthropic_key")),
        },
        "ollama": {
            "installed":   False,
            "models":      [],
            "want_model":  "",
            "model_ready": False,
            "url":         None,
            "error":       None,
        },
    }
