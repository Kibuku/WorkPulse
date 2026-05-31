"""
learning.py — Loop B of WorkPulse stream tagging.

When the activity tracker sees a window it can't classify with config rules,
folder roots, stream-name keywords, or .docx content, it asks Claude:

    Given (a) the user's defined streams, (b) what they've been working on
    in the last 30 minutes, and (c) the title + app of the window in front
    of them right now — which stream does this belong to, and what 2-4
    word anchor phrase should we remember it by?

The verdict + anchor get written to logs/learned_tags.json. Next time any
window title contains that anchor, the local rule fires — no API call.

This means the system is supervised by *use*: as you live in WorkPulse
across the day, the rule set self-fills. After a couple of weeks it should
auto-tag almost everything you do.

Public API:
    normalize_title(raw)               -> str
    match_learned(title, cfg)          -> str | None      # local rule lookup
    classify_async(session_dict, cfg)  -> None            # fire-and-forget AI classify
    recent_context_summary(cfg)        -> str             # used by classifier prompt
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
import threading
import time
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config, resolve

log = logging.getLogger("learning")

# ── title normalization ───────────────────────────────────────────────────────
# Strip away the bits of a window title that change run-to-run (notification
# counts, tab counts, browser branding) so different windows referring to the
# same content normalize to the same string.

_NORM_PATTERNS = [
    re.compile(r"^\s*\(\d+\)\s*"),                         # leading "(3)" notification count
    re.compile(r"\s*and \d+ more pages?\s*", re.I),        # browser tab counts
    re.compile(r"\s*\[(protected view|read-only|compatibility mode)\]\s*", re.I),
    re.compile(r"\s*-\s*(brave|google chrome|microsoft\W*edge|mozilla firefox|firefox|opera|vivaldi)\s*$", re.I),
    re.compile(r"\s*-\s*personal(\s*-\s*microsoft.*)?$", re.I),  # Edge "Personal" profile suffix
    re.compile(r"\s*—\s*personal(\s*—\s*microsoft.*)?$", re.I),  # em-dash variant
]


def normalize_title(raw: str) -> str:
    """Reduce a window title to a stable comparable form."""
    if not raw:
        return ""
    t = raw
    for p in _NORM_PATTERNS:
        t = p.sub(" ", t)
    t = re.sub(r"\s+", " ", t).strip().lower()
    return t


# ── learned-rules store (logs/learned_tags.json) ──────────────────────────────
# Persisted as a JSON array of {pattern, stream, source, raw_title, created_at, hit_count}.
# Loaded once and reloaded if the file mtime changes — cheap enough to do per call.

_RULES_LOCK = threading.RLock()  # reentrant — _add_rule re-enters via load_learned_rules
_RULES_CACHE: dict = {"mtime": 0.0, "rules": []}


def _rules_path(cfg: dict) -> Path:
    return resolve(cfg["paths"]["logs"]) / "learned_tags.json"


def load_learned_rules(cfg: dict) -> list[dict]:
    """Read the learned-rules file, with mtime-based caching for hot reload."""
    p = _rules_path(cfg)
    try:
        m = p.stat().st_mtime
    except FileNotFoundError:
        return []
    with _RULES_LOCK:
        if m == _RULES_CACHE["mtime"]:
            return _RULES_CACHE["rules"]
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            if not isinstance(data, list):
                data = []
        except (json.JSONDecodeError, OSError):
            data = []
        _RULES_CACHE["mtime"] = m
        _RULES_CACHE["rules"] = data
        return data


def _save_learned_rules(cfg: dict, rules: list[dict]) -> None:
    p = _rules_path(cfg)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(rules, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(p)
    # Force reload next call
    with _RULES_LOCK:
        _RULES_CACHE["mtime"] = 0.0


def _add_rule(cfg: dict, pattern: str, stream: str | None, raw_title: str, source: str) -> None:
    """Append (or update) a rule. stream=None means 'permanently untagged' (a negative rule)."""
    pattern = (pattern or "").strip().lower()
    if not pattern or len(pattern) < 2:
        return
    with _RULES_LOCK:
        rules = list(load_learned_rules(cfg))  # copy
    # If the same pattern already exists, just bump its hit count
    for r in rules:
        if r.get("pattern") == pattern:
            if r.get("stream") != stream:
                r["stream"] = stream  # newer verdict wins
            r["hit_count"] = int(r.get("hit_count", 0)) + 1
            r["source"] = source
            _save_learned_rules(cfg, rules)
            return
    rules.append({
        "pattern": pattern,
        "stream": stream,
        "source": source,
        "raw_title": raw_title,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "hit_count": 1,
    })
    _save_learned_rules(cfg, rules)
    log.info("learned: %r -> %s (source=%s)", pattern, stream, source)


def match_learned(title: str, cfg: dict) -> str | None:
    """Return the learned stream for a window title, or None if no rule matches.

    Matches by substring on the normalized title — so a rule with pattern
    'nkcc' tags 'Fwd: NKCC Q3', 'NKCC kickoff notes', etc.
    A rule with stream=None (negative rule) returns None *and* prevents AI
    classification from re-firing for the same pattern.
    """
    norm = normalize_title(title)
    if not norm:
        return None
    rules = load_learned_rules(cfg)
    # Longest-pattern-first so 'nkcc q3' wins over 'nkcc'
    for r in sorted(rules, key=lambda x: -len(x.get("pattern", ""))):
        pat = r.get("pattern", "")
        if pat and pat in norm:
            return r.get("stream")  # may be None for negative rules
    return None


def has_negative_rule(title: str, cfg: dict) -> bool:
    """True if a learned rule covers this title with stream=None — skip AI classify."""
    norm = normalize_title(title)
    if not norm:
        return False
    for r in load_learned_rules(cfg):
        pat = r.get("pattern", "")
        if pat and pat in norm and r.get("stream") is None:
            return True
    return False


# ── recent-context summary (used in the classifier prompt) ────────────────────

def recent_context_summary(cfg: dict, minutes: int = 30) -> str:
    """One-line summary of which streams the user worked in over the last N minutes."""
    log_dir = resolve(cfg["paths"]["logs"])
    today_log = log_dir / f"activity_{datetime.now().date().isoformat()}.jsonl"
    if not today_log.exists():
        return "(no recent activity)"
    cutoff = datetime.now().astimezone() - timedelta(minutes=minutes)
    by_stream: dict[str, float] = defaultdict(float)
    try:
        with today_log.open("r", encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("idle"):
                    continue
                try:
                    end = datetime.fromisoformat(rec["end"])
                except (KeyError, ValueError):
                    continue
                if end < cutoff:
                    continue
                stream = rec.get("stream") or "untagged"
                by_stream[stream] += float(rec.get("duration_s", 0))
    except OSError:
        return "(no recent activity)"
    if not by_stream:
        return "(no recent activity)"
    parts = []
    for s, secs in sorted(by_stream.items(), key=lambda x: -x[1]):
        if s == "untagged":
            continue
        mins = round(secs / 60)
        if mins >= 1:
            parts.append(f"{s} ({mins} min)")
    return ", ".join(parts) if parts else "(only untagged activity)"


# ── AI classification (fire-and-forget) ───────────────────────────────────────

# In-flight set: don't queue duplicate classify jobs for the same normalized title
_PENDING: set[str] = set()
_PENDING_LOCK = threading.Lock()


def _ask_llm(title: str, app: str, duration_s: float, cfg: dict) -> tuple[str | None, str]:
    """Classify one window via whichever LLM backend is configured.
    Returns (stream_key_or_None, anchor_phrase). Backend selection (cloud
    Claude vs local Ollama vs no-op) is handled by scripts.llm."""
    streams = cfg["streams"]
    # Enrich each stream's description with the config patterns mapped to it
    # AND the patterns the system has already learned. Massively improves
    # accuracy — the model sees that "NKCC" maps to consulting, etc.
    pat_by_stream: dict[str, list[str]] = {k: [] for k in streams}
    for p in cfg["watcher"].get("stream_path_patterns", []):
        s = p.get("stream")
        if s in pat_by_stream:
            pat_by_stream[s].append(p.get("path", ""))
    for r in load_learned_rules(cfg):
        s = r.get("stream")
        if s in pat_by_stream and r.get("pattern"):
            pat_by_stream[s].append(r["pattern"])
    stream_lines_parts = []
    for k, label in streams.items():
        examples = [e for e in pat_by_stream.get(k, []) if e][:14]
        ex_txt = f" — known keywords: {', '.join(examples)}" if examples else ""
        stream_lines_parts.append(f"  - {k}: {label}{ex_txt}")
    stream_lines = "\n".join(stream_lines_parts)
    context = recent_context_summary(cfg, minutes=30)

    prompt = (
        "You are tagging an active window into one of the user's work streams.\n\n"
        f"Available streams:\n{stream_lines}\n\n"
        f"Recent context (last 30 min of focused work): {context}\n\n"
        "Window the user has just been focused on:\n"
        f"  app:      {app}\n"
        f"  title:    {title}\n"
        f"  duration: {round(duration_s)} seconds\n\n"
        "Tagging rules (important):\n"
        "  - Prefer project-specific keywords (e.g. NKCC, GIZ, Verst Carbon,\n"
        "    Kenya Power, dissertation chapter names) over generic channel\n"
        "    keywords (Gmail, WhatsApp, the user's email address).\n"
        "  - Use the recent-context summary as a tiebreaker — if the user has\n"
        "    been deep in one stream and the title is ambiguous, lean that way.\n"
        "  - 'none' is the right answer for clearly off-task windows: random\n"
        "    web reading, system dialogs, settings, blank documents.\n\n"
        "Decide:\n"
        "  1) Which stream key best fits, or 'none'.\n"
        "  2) A short 2-4 word stable anchor phrase taken from the title that\n"
        "     future similar windows will also contain. Pick the most specific\n"
        "     project signal (e.g. 'nkcc', 'verst carbon dashboard',\n"
        "     'energy transition report'), NOT generic words like 'gmail' or\n"
        "     'inbox'.\n\n"
        'Reply with ONLY a JSON object on one line, no other text:\n'
        '  {"stream": "<key or none>", "anchor": "<phrase>"}'
    )

    from scripts.llm import ask_json
    obj, meta = ask_json(prompt, max_tokens=120, cfg=cfg)

    # Log every call regardless of outcome so the dashboard reflects what
    # WorkPulse is actually doing on the user's behalf.
    try:
        from scripts.ai_logger import log_session
        log_session(
            stream=None,
            task_summary=f"Classify window: {title[:140]}",
            input_tokens=meta["input_tokens"],
            output_tokens=meta["output_tokens"],
            tool_used=f"workpulse-loop-b-{meta['backend']}",
            duration_minutes=round(meta["duration_s"] / 60, 3),
            cfg=cfg,
        )
    except Exception:
        pass

    if not obj:
        return None, ""

    stream = obj.get("stream")
    anchor = (obj.get("anchor") or "").strip().lower()
    if not isinstance(stream, str):
        return None, anchor
    if stream.lower() in ("none", "", "null"):
        return None, anchor
    if stream not in streams:
        log.warning("LLM returned unknown stream: %r", stream)
        return None, anchor
    return stream, anchor


# Backward-compat alias — older code paths called _ask_claude
_ask_claude = _ask_llm


# Skip generic system / placeholder titles that should never reach the API
_SKIP_TITLES = {
    "(no window)", "program manager", "task switching",
    "windows shell experience host", "settings", "search",
}


def _should_classify(title: str, app: str, duration_s: float, cfg: dict) -> bool:
    if duration_s < 30:
        return False
    if not title:
        return False
    norm = normalize_title(title)
    if not norm or norm in _SKIP_TITLES or len(norm) < 4:
        return False
    if has_negative_rule(title, cfg):
        return False
    return True


def _classify_worker(title: str, app: str, duration_s: float, cfg: dict) -> None:
    norm = normalize_title(title)
    try:
        # Re-check inside worker — another sample may have classified meanwhile
        with _PENDING_LOCK:
            if norm not in _PENDING:
                return
        if match_learned(title, cfg) is not None or has_negative_rule(title, cfg):
            return  # someone else got there first

        stream, anchor = _ask_claude(title, app, duration_s, cfg)

        # Choose the rule pattern: prefer the AI's anchor, fall back to full
        # normalized title. Anchor must be a substring of the normalized title
        # so it actually matches future windows.
        pattern = anchor if (anchor and anchor in norm) else norm

        # Even when stream is None, store a negative rule so we don't keep
        # re-asking about the same kind of window forever.
        _add_rule(
            cfg=cfg,
            pattern=pattern,
            stream=stream,
            raw_title=title,
            source="ai",
        )
    except Exception:
        log.exception("classify_worker failed")
    finally:
        with _PENDING_LOCK:
            _PENDING.discard(norm)


def classify_async(title: str, app: str, duration_s: float, cfg: dict | None = None) -> None:
    """Fire-and-forget AI classification of a window. No-op if gated out."""
    if cfg is None:
        cfg = load_config()
    if not _should_classify(title, app, duration_s, cfg):
        return
    norm = normalize_title(title)
    with _PENDING_LOCK:
        if norm in _PENDING:
            return
        _PENDING.add(norm)
    t = threading.Thread(
        target=_classify_worker,
        args=(title, app, duration_s, cfg),
        daemon=True,
    )
    t.start()


# ── CLI for manual testing ────────────────────────────────────────────────────

if __name__ == "__main__":
    cfg = load_config()
    if len(sys.argv) >= 2:
        title = sys.argv[1]
        app = sys.argv[2] if len(sys.argv) >= 3 else "msedge.exe"
        print(f"normalized: {normalize_title(title)!r}")
        print(f"learned match: {match_learned(title, cfg)}")
        print(f"recent context: {recent_context_summary(cfg)}")
        print("classifying with Claude (60s)…")
        stream, anchor = _ask_claude(title, app, 60.0, cfg)
        print(f"  stream: {stream}")
        print(f"  anchor: {anchor!r}")
    else:
        print("usage: learning.py \"<window title>\" [app.exe]")
