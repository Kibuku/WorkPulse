"""
llm.py — provider façade for WorkPulse's AI calls (Ask WorkPulse, Phase 0).

Three backends, picked automatically from what's configured:

  1. "anthropic" — Cloud Claude via API key. Best quality. Costs money.
  2. "ollama"    — Local LLM via Ollama (https://ollama.com). Free. Slower.
  3. "none"      — Neither available. Callers fall back to templated output.

Routing (config `llm.backend`, default "auto"):
    "auto"       -> Anthropic if a key is set, else Ollama if up + model pulled, else none
    "anthropic"  -> Anthropic if a key is set, else none
    "ollama"     -> Ollama if up + model pulled, else none
    "none"       -> none

Public API:
    active_backend(cfg) -> "anthropic" | "ollama" | "none"
    backend_status(cfg) -> dict            # for /api/system + Settings page
    ask_text(prompt, *, max_tokens, cfg, model) -> (text|None, meta)
    ask_json(prompt, *, max_tokens, cfg, model) -> (dict|None, meta)
        meta = {backend, model, input_tokens, output_tokens, duration_s}

Callers use ask_text / ask_json rather than a specific SDK, so switching
backend is one config line. Ported from WorkPulse v1's scripts/llm.py.
"""

from __future__ import annotations

import json
import logging
import os
import re
import time

from workpulse.common import load_config

log = logging.getLogger("workpulse.llm")

_DEFAULT_OLLAMA_URL   = "http://127.0.0.1:11434"
_DEFAULT_LOCAL_MODEL  = "llama3.2:3b"          # ~2GB, fast on CPU, good at JSON
_DEFAULT_CLOUD_MODEL  = "claude-haiku-4-5"
_DEFAULT_GEMINI_MODEL = "gemini-2.5-flash-lite"
_DEFAULT_INTERACTIVE_TIMEOUT_S = 35
_OLLAMA_PROBE_CACHE_S = 60
_ollama_probe = {"ts": 0.0, "up": False, "models": [], "error": None}


# ── config helpers ────────────────────────────────────────────────────────────

def _llm_cfg(cfg: dict | None) -> dict:
    return ((cfg or {}).get("llm") or {})


def _ollama_url(cfg: dict | None) -> str:
    return (_llm_cfg(cfg).get("ollama") or {}).get("url") or _DEFAULT_OLLAMA_URL


def _ollama_model(cfg: dict | None) -> str:
    return (_llm_cfg(cfg).get("ollama") or {}).get("model") or _DEFAULT_LOCAL_MODEL


def _cloud_model(cfg: dict | None, override: str | None = None) -> str:
    return override or _llm_cfg(cfg).get("model") or _DEFAULT_CLOUD_MODEL


def _anthropic_key(cfg: dict | None = None) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from workpulse.wp_secrets import get as get_secret  # type: ignore
        return get_secret("anthropic_key") or None
    except Exception:
        return None


def _gemini_key(cfg: dict | None = None) -> str | None:
    key = os.environ.get("GEMINI_API_KEY")
    if key:
        return key
    try:
        from workpulse.wp_secrets import get as get_secret
        return get_secret("gemini_key") or None
    except Exception:
        return None


def _gemini_model(cfg: dict | None, override: str | None = None) -> str:
    return override or ((_llm_cfg(cfg).get("gemini") or {}).get("model")) or _DEFAULT_GEMINI_MODEL


# ── ollama probe ──────────────────────────────────────────────────────────────

def _probe_ollama(cfg: dict | None = None, *, force: bool = False) -> dict:
    """Return {up, models, error}, cached for 60s. Fails fast + quietly when
    Ollama isn't running (connection refused is immediate, no hang)."""
    now = time.monotonic()
    if not force and (now - _ollama_probe["ts"]) < _OLLAMA_PROBE_CACHE_S:
        return _ollama_probe
    try:
        import urllib.request
        req = urllib.request.Request(f"{_ollama_url(cfg)}/api/tags")
        with urllib.request.urlopen(req, timeout=1.5) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        models = [m.get("name", "") for m in data.get("models", [])]
        _ollama_probe.update(ts=now, up=True, models=models, error=None)
    except Exception as e:  # noqa: BLE001 — any failure means "not available"
        _ollama_probe.update(ts=now, up=False, models=[], error=str(e)[:120])
    return _ollama_probe


def _ollama_ready(cfg: dict | None) -> bool:
    probe = _probe_ollama(cfg)
    if not probe["up"]:
        return False
    wanted = _ollama_model(cfg)
    return any(m.startswith(wanted) for m in probe["models"])


# ── backend selection ─────────────────────────────────────────────────────────

def active_backend(cfg: dict | None = None) -> str:
    """The backend ask_* would actually use right now."""
    if cfg is None:
        cfg = load_config()
    setting = (_llm_cfg(cfg).get("backend") or "auto").lower()
    if setting == "gemini":
        return "gemini" if _gemini_key(cfg) else "none"
    if setting == "anthropic":
        return "anthropic" if _anthropic_key(cfg) else "none"
    if setting == "ollama":
        return "ollama" if _ollama_ready(cfg) else "none"
    if setting == "none":
        return "none"
    # auto
    if _gemini_key(cfg):
        return "gemini"
    if _anthropic_key(cfg):
        return "anthropic"
    if _ollama_ready(cfg):
        return "ollama"
    return "none"


def backend_status(cfg: dict | None = None) -> dict:
    """Diagnostic snapshot for /api/system and the Settings page. Shape kept
    back-compatible with the dashboard JS (reads llm.ollama.{installed,
    model_ready,want_model} behind null-guards)."""
    if cfg is None:
        cfg = load_config()
    probe = _probe_ollama(cfg)
    wanted = _ollama_model(cfg)
    return {
        "active": active_backend(cfg),
        "gemini": {
            "available": bool(_gemini_key(cfg)),
            "model": _gemini_model(cfg),
            "privacy_mode": "free-redacted-pilot",
        },
        "anthropic": {
            "available": bool(_anthropic_key(cfg)),
        },
        "ollama": {
            "installed":   probe["up"],
            "models":      probe["models"],
            "want_model":  wanted,
            "model_ready": any(m.startswith(wanted) for m in probe["models"]),
            "url":         _ollama_url(cfg),
            "error":       probe.get("error"),
        },
    }


# ── the two call sites ─────────────────────────────────────────────────────────

def _new_meta(backend: str) -> dict:
    return {"backend": backend, "model": None,
            "input_tokens": 0, "output_tokens": 0, "duration_s": 0.0}


def ask_text(prompt: str, *, max_tokens: int = 600, cfg: dict | None = None,
             model: str | None = None) -> tuple[str | None, dict]:
    """Single-shot prompt expecting a plain-text reply. (text|None, meta)."""
    if cfg is None:
        cfg = load_config()
    backend = active_backend(cfg)
    meta = _new_meta(backend)
    if backend == "gemini":
        return _gemini_text(prompt, max_tokens, cfg, meta, model)
    if backend == "anthropic":
        return _anthropic_text(prompt, max_tokens, cfg, meta, model)
    if backend == "ollama":
        return _ollama_text(prompt, max_tokens, cfg, meta)
    return None, meta


def ask_json(prompt: str, *, max_tokens: int = 256, cfg: dict | None = None,
             model: str | None = None) -> tuple[dict | None, dict]:
    """Single-shot prompt expecting a JSON object reply. (dict|None, meta)."""
    if cfg is None:
        cfg = load_config()
    backend = active_backend(cfg)
    meta = _new_meta(backend)
    if backend == "gemini":
        text, meta = _gemini_generate(prompt, max_tokens, cfg, meta, model, as_json=True)
        return _parse_json_loose(text), meta
    if backend == "anthropic":
        text, meta = _anthropic_text(prompt, max_tokens, cfg, meta, model)
        return _parse_json_loose(text), meta
    if backend == "ollama":
        return _ollama_json(prompt, max_tokens, cfg, meta)
    return None, meta


def _gemini_generate(prompt: str, max_tokens: int, cfg: dict, meta: dict,
                     model: str | None, *, as_json: bool):
    import urllib.error
    import urllib.request
    key = _gemini_key(cfg)
    if not key:
        return None, meta
    chosen = _gemini_model(cfg, model)
    meta["model"] = chosen
    generation = {"maxOutputTokens": max_tokens}
    if as_json:
        generation["responseMimeType"] = "application/json"
    body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": generation}
    req = urllib.request.Request(
        f"https://generativelanguage.googleapis.com/v1beta/models/{chosen}:generateContent",
        data=json.dumps(body).encode("utf-8"), method="POST",
        headers={"Content-Type": "application/json", "x-goog-api-key": key})
    try:
        t0 = time.monotonic()
        timeout = float(((_llm_cfg(cfg).get("gemini") or {}).get("timeout_s")) or 35)
        with urllib.request.urlopen(req, timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8"))
        meta["duration_s"] = round(time.monotonic() - t0, 3)
        usage = payload.get("usageMetadata") or {}
        meta["input_tokens"] = int(usage.get("promptTokenCount", 0) or 0)
        meta["output_tokens"] = int(usage.get("candidatesTokenCount", 0) or 0)
        parts = (((payload.get("candidates") or [{}])[0].get("content") or {}).get("parts") or [])
        return "".join(str(part.get("text") or "") for part in parts).strip(), meta
    except Exception as exc:
        log.warning("gemini call failed: %s", exc)
        return None, meta


def _gemini_text(prompt: str, max_tokens: int, cfg: dict, meta: dict,
                  model: str | None):
    return _gemini_generate(prompt, max_tokens, cfg, meta, model, as_json=False)


# ── anthropic backend ─────────────────────────────────────────────────────────

def _anthropic_text(prompt: str, max_tokens: int, cfg: dict, meta: dict,
                    model: str | None):
    key = _anthropic_key(cfg)
    if not key:
        return None, meta
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None, meta
    m = _cloud_model(cfg, model)
    meta["model"] = m
    try:
        client = anthropic.Anthropic(api_key=key)
        t0 = time.monotonic()
        resp = client.messages.create(
            model=m, max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        meta["duration_s"]    = round(time.monotonic() - t0, 3)
        meta["input_tokens"]  = int(getattr(resp.usage, "input_tokens", 0))
        meta["output_tokens"] = int(getattr(resp.usage, "output_tokens", 0))
        text = "".join(b.text for b in resp.content
                       if getattr(b, "type", None) == "text")
    except Exception as e:  # noqa: BLE001
        log.warning("anthropic call failed: %s", e)
        return None, meta
    return text.strip(), meta


# ── ollama backend ────────────────────────────────────────────────────────────

def _ollama_post(prompt: str, max_tokens: int, cfg: dict, meta: dict,
                 *, as_json: bool):
    import urllib.request
    model = _ollama_model(cfg)
    meta["model"] = model
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "stream": False,
        "options": {"num_predict": max_tokens,
                    "temperature": 0.2 if as_json else 0.3},
    }
    if as_json:
        body["format"] = "json"
    req = urllib.request.Request(
        f"{_ollama_url(cfg)}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        t0 = time.monotonic()
        timeout_s = float(
            ((_llm_cfg(cfg).get("ollama") or {}).get("timeout_s"))
            or _DEFAULT_INTERACTIVE_TIMEOUT_S
        )
        with urllib.request.urlopen(req, timeout=timeout_s) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        meta["duration_s"]    = round(time.monotonic() - t0, 3)
        meta["input_tokens"]  = int(payload.get("prompt_eval_count", 0) or 0)
        meta["output_tokens"] = int(payload.get("eval_count", 0) or 0)
        return ((payload.get("message") or {}).get("content") or "").strip()
    except Exception as e:  # noqa: BLE001
        log.warning("ollama call failed: %s", e)
        return None


def _ollama_text(prompt: str, max_tokens: int, cfg: dict, meta: dict):
    text = _ollama_post(prompt, max_tokens, cfg, meta, as_json=False)
    return text, meta


def _ollama_json(prompt: str, max_tokens: int, cfg: dict, meta: dict):
    text = _ollama_post(prompt, max_tokens, cfg, meta, as_json=True)
    return _parse_json_loose(text), meta


# ── forgiving JSON extraction (local models wrap JSON in prose/fences) ─────────

def _parse_json_loose(raw: str | None) -> dict | None:
    if not raw:
        return None
    s = raw.strip().strip("`")
    if s.lower().startswith("json"):
        s = s[4:].strip()
    m = re.search(r"\{.*\}", s, re.DOTALL)
    if m:
        s = m.group(0)
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None


# ── CLI diagnostic ────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="WorkPulse LLM backend diagnostic")
    ap.add_argument("--ask", help="Send a quick JSON test prompt")
    args = ap.parse_args()
    cfg = load_config()
    print(json.dumps(backend_status(cfg), indent=2))
    if args.ask:
        obj, meta = ask_json(args.ask, cfg=cfg)
        print(f"meta: {meta}")
        print(f"reply: {json.dumps(obj, indent=2) if obj else '(no parse)'}")
