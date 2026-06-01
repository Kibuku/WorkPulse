"""
doctag.py — content-aware stream tagging for Word documents.

When the activity tracker sees a Word window with no matching folder/title rule,
it asks Claude to read the first ~1500 chars of the open .docx and decide which
stream the content belongs to.

Result is cached by (path, mtime) so we only pay for classification once per
file revision. Cache lives at logs/doctag_cache.json.

Public:
    classify_doc(path: str, cfg: dict) -> str | None
        Returns a stream key (one of cfg["streams"]) or None if undecidable.
"""

from __future__ import annotations

import json
import os
import sys
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config, resolve

# ── .docx text extraction (stdlib only) ───────────────────────────────────────

_DOCX_NS = {"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"}


def _extract_docx_text(path: str, max_chars: int = 1500) -> str:
    """Pull plain text out of a .docx by reading word/document.xml."""
    try:
        with zipfile.ZipFile(path) as z:
            with z.open("word/document.xml") as f:
                xml = f.read()
    except (zipfile.BadZipFile, KeyError, FileNotFoundError, PermissionError):
        return ""
    try:
        root = ET.fromstring(xml)
    except ET.ParseError:
        return ""
    parts: list[str] = []
    chars = 0
    for t in root.iter("{http://schemas.openxmlformats.org/wordprocessingml/2006/main}t"):
        if t.text:
            parts.append(t.text)
            chars += len(t.text)
            if chars >= max_chars:
                break
    return " ".join(parts)[:max_chars]


# ── classification cache ──────────────────────────────────────────────────────

def _cache_path(cfg: dict) -> Path:
    return resolve(cfg["paths"]["logs"]) / "doctag_cache.json"


def _load_cache(cfg: dict) -> dict:
    p = _cache_path(cfg)
    if not p.exists():
        return {}
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cache(cfg: dict, cache: dict) -> None:
    p = _cache_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        p.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError:
        pass


# ── LLM classification ────────────────────────────────────────────────────────

def _classify_with_llm(text: str, cfg: dict, source_path: str = "") -> str | None:
    """Classify a document's content into one stream. Backend (cloud/local/none)
    chosen by scripts.llm. `source_path` is informational, used in the
    AI-session log task summary."""
    from scripts.tree import labels as _stream_labels
    streams = _stream_labels(cfg)
    stream_lines = "\n".join(f"  - {key}: {label}" for key, label in streams.items())

    prompt = (
        "You are classifying a personal work document into one stream.\n\n"
        f"Available streams:\n{stream_lines}\n\n"
        "Document excerpt:\n---\n"
        f"{text}\n"
        "---\n\n"
        'Reply with ONLY a JSON object on one line, no other text:\n'
        '  {"stream": "<stream key, or none>"}'
    )

    from scripts.llm import ask_json
    obj, meta = ask_json(prompt, max_tokens=40, cfg=cfg)

    answer = ""
    if obj and isinstance(obj.get("stream"), str):
        answer = obj["stream"].strip().lower().strip("`. \n")
    verdict = answer if answer in streams else None

    # Log every call regardless so AI Sessions panel stays honest.
    try:
        from pathlib import Path as _P
        from scripts.ai_logger import log_session
        fname = _P(source_path).name if source_path else "(content)"
        log_session(
            stream=verdict,
            task_summary=f"Classify .docx: {fname}",
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            tool_used=f"workpulse-doctag-{meta['backend']}",
            duration_minutes=round(meta["duration_s"] / 60, 3),
            cfg=cfg,
        )
    except Exception:
        pass

    return verdict


# ── public API ────────────────────────────────────────────────────────────────

def classify_doc(path: str, cfg: dict | None = None) -> str | None:
    """Tag a .docx by content. Cached by (path, mtime).
    Returns stream key or None."""
    if not path or not path.lower().endswith(".docx"):
        return None
    if cfg is None:
        cfg = load_config()
    try:
        mtime = int(os.path.getmtime(path))
    except OSError:
        return None
    norm = path.replace("\\", "/").lower()
    key = f"{norm}|{mtime}"

    cache = _load_cache(cfg)
    if key in cache:
        v = cache[key]
        return v if v else None

    text = _extract_docx_text(path)
    if len(text) < 40:
        # Not enough content to bother classifying; cache as miss
        cache[key] = ""
        _save_cache(cfg, cache)
        return None

    stream = _classify_with_llm(text, cfg, source_path=path)
    cache[key] = stream or ""
    _save_cache(cfg, cache)
    return stream


if __name__ == "__main__":
    # Quick CLI: python scripts/doctag.py "C:/path/to/doc.docx"
    if len(sys.argv) < 2:
        print("usage: doctag.py <docx-path>")
        sys.exit(1)
    cfg = load_config()
    print(classify_doc(sys.argv[1], cfg))
