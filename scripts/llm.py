"""
llm.py — provider abstraction for WorkPulse's AI calls.

Three backends, picked automatically based on what's configured:

  1. "anthropic" — Cloud Claude via API key. Best quality. Costs money.
  2. "ollama"    — Local LLM via Ollama (https://ollama.com). Free. Slower.
  3. "none"      — Neither configured. Classifier silently no-ops.

Routing rule (default):
    Anthropic key configured     -> "anthropic"
  ELIF Ollama daemon + model up  -> "ollama"
  ELSE                            -> "none"

Public API:
    active_backend() -> "anthropic" | "ollama" | "none"
    backend_status() -> dict  (for /api/system)
    ask_json(prompt, max_tokens) -> (parsed_json_or_None, meta)
        meta = {backend, input_tokens, output_tokens, duration_s}

All callers (learning.py, doctag.py) should use ask_json() rather than
talking to a specific SDK. This way switching backends is one line.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config
from scripts.wp_secrets import get as get_secret

log = logging.getLogger("llm")

OLLAMA_URL = "http://127.0.0.1:11434"
DEFAULT_LOCAL_MODEL = "llama3.2:3b"   # ~2GB, fast on CPU, good at JSON
_OLLAMA_PROBE_CACHE_S = 60            # cache "is Ollama up?" for this long
_ollama_probe = {"ts": 0.0, "up": False, "models": []}


# ── status / detection ────────────────────────────────────────────────────────

def _probe_ollama(force: bool = False) -> dict:
    """Return {up: bool, models: [str], error: str|None}, cached for 60s."""
    now = time.monotonic()
    if not force and (now - _ollama_probe["ts"]) < _OLLAMA_PROBE_CACHE_S:
        return _ollama_probe
    try:
        import urllib.request
        req = urllib.request.Request(f"{OLLAMA_URL}/api/tags")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        _ollama_probe.update(ts=now, up=True, models=models, error=None)
    except Exception as e:
        _ollama_probe.update(ts=now, up=False, models=[], error=str(e)[:120])
    return _ollama_probe


def _local_model(cfg: dict) -> str:
    return (cfg.get("llm", {}) or {}).get("local_model") or DEFAULT_LOCAL_MODEL


def active_backend(cfg: dict | None = None) -> str:
    """Return the backend that ask_json() would actually use right now."""
    if cfg is None:
        cfg = load_config()
    # Allow explicit override via secrets.json — useful for "always local"
    forced = get_secret("prefer_backend")
    if forced in ("anthropic", "ollama", "none"):
        # Honor explicit choice unless it points at something unavailable
        if forced == "anthropic" and get_secret("anthropic_key"):
            return "anthropic"
        if forced == "ollama":
            probe = _probe_ollama()
            wanted = _local_model(cfg)
            if probe["up"] and any(m.startswith(wanted) for m in probe["models"]):
                return "ollama"
        if forced == "none":
            return "none"
        # else fall through to automatic
    # Automatic: Anthropic if key, else Ollama if up+model pulled, else none
    if get_secret("anthropic_key"):
        return "anthropic"
    probe = _probe_ollama()
    wanted = _local_model(cfg)
    if probe["up"] and any(m.startswith(wanted) for m in probe["models"]):
        return "ollama"
    return "none"


def backend_status(cfg: dict | None = None) -> dict:
    """Diagnostic snapshot for /api/system and the Settings page."""
    if cfg is None:
        cfg = load_config()
    probe = _probe_ollama()
    wanted = _local_model(cfg)
    has_model = any(m.startswith(wanted) for m in probe["models"])
    return {
        "active":  active_backend(cfg),
        "anthropic": {
            "available": bool(get_secret("anthropic_key")),
        },
        "ollama": {
            "installed":   probe["up"],
            "models":      probe["models"],
            "want_model":  wanted,
            "model_ready": has_model,
            "url":         OLLAMA_URL,
            "error":       probe.get("error"),
        },
    }


# ── ask_json — the only call sites used by classifiers ────────────────────────

def ask_json(prompt: str, *, max_tokens: int = 128, cfg: dict | None = None) -> tuple[dict | None, dict]:
    """Send a single-shot prompt expecting a JSON object reply.

    Returns (parsed_dict_or_None, meta). The classifier callers handle None
    by skipping the rule write. meta always populated:
        {backend, input_tokens, output_tokens, duration_s}
    """
    if cfg is None:
        cfg = load_config()
    backend = active_backend(cfg)
    meta = {"backend": backend, "input_tokens": 0, "output_tokens": 0, "duration_s": 0.0}

    if backend == "anthropic":
        return _ask_anthropic_json(prompt, max_tokens, cfg, meta)
    if backend == "ollama":
        return _ask_ollama_json(prompt, max_tokens, cfg, meta)
    return None, meta  # backend == "none"


# ── Anthropic backend ─────────────────────────────────────────────────────────

def _ask_anthropic_json(prompt: str, max_tokens: int, cfg: dict, meta: dict):
    try:
        import anthropic
    except ImportError:
        return None, meta
    api_key = get_secret("anthropic_key")
    if not api_key:
        return None, meta
    try:
        client = anthropic.Anthropic(api_key=api_key)
        t0 = time.monotonic()
        msg = client.messages.create(
            model=cfg["llm"]["model"],
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        meta["duration_s"] = round(time.monotonic() - t0, 3)
        meta["input_tokens"]  = getattr(msg.usage, "input_tokens", 0)
        meta["output_tokens"] = getattr(msg.usage, "output_tokens", 0)
        raw = (msg.content[0].text or "").strip()
    except Exception as e:
        log.warning("anthropic call failed: %s", e)
        return None, meta
    return _parse_json_loose(raw), meta


# ── Ollama backend ────────────────────────────────────────────────────────────

def _ask_ollama_json(prompt: str, max_tokens: int, cfg: dict, meta: dict):
    """Call Ollama's /api/chat with format=json. Returns parsed dict or None."""
    import urllib.request, urllib.error
    model = _local_model(cfg)
    body = json.dumps({
        "model":   model,
        "messages":[{"role": "user", "content": prompt}],
        "stream":  False,
        "format":  "json",
        "options": {"num_predict": max_tokens, "temperature": 0.2},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=body, method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        t0 = time.monotonic()
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        meta["duration_s"]    = round(time.monotonic() - t0, 3)
        meta["input_tokens"]  = int(payload.get("prompt_eval_count", 0) or 0)
        meta["output_tokens"] = int(payload.get("eval_count", 0) or 0)
        raw = ((payload.get("message") or {}).get("content") or "").strip()
    except Exception as e:
        log.warning("ollama call failed: %s", e)
        return None, meta
    return _parse_json_loose(raw), meta


# ── helper: forgiving JSON extraction ─────────────────────────────────────────

def _parse_json_loose(raw: str) -> dict | None:
    """Try hard to extract a JSON object from an LLM reply. Some local models
    wrap the JSON in ```json fences or prose — strip those before parsing."""
    if not raw:
        return None
    raw = raw.strip().strip("`")
    if raw.lower().startswith("json"):
        raw = raw[4:].strip()
    # If there's surrounding prose, grab the first {...} block
    m = re.search(r"\{.*\}", raw, re.DOTALL)
    if m:
        raw = m.group(0)
    try:
        obj = json.loads(raw)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ── CLI for diagnostic use ────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="WorkPulse LLM provider diagnostic")
    ap.add_argument("--ask", help="Send a quick test prompt expecting JSON")
    args = ap.parse_args()
    cfg = load_config()
    st = backend_status(cfg)
    print(json.dumps(st, indent=2))
    if args.ask:
        print(f"\n→ active backend: {st['active']}")
        obj, meta = ask_json(args.ask, cfg=cfg)
        print(f"   meta: {meta}")
        print(f"   reply: {json.dumps(obj, indent=2) if obj else '(no parse)'}")
