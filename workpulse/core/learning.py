"""
learning.py — the learned-rules store for stream tagging.

A learned rule maps a normalised window-title substring to a stream (or to
``None`` for "permanently untagged"). Rules are written from the dashboard's
manual retag action and read by the tagger as a fast, local, no-API lookup.

v2 note: the v1 "Loop B" that asked an LLM to classify unknown windows on the
sensor's hot path has been removed — attribution now happens downstream in the
Categorizer, which scores whole clusters. What remains here is the
deterministic rule store: normalise, read, write, and match. No LLM calls.

Public API:
    normalize_title(raw)            -> str
    load_learned_rules(cfg)         -> list[dict]
    _add_rule(cfg, pattern, stream, raw_title, source) -> None
    match_learned(title, cfg)       -> str | None
    has_negative_rule(title, cfg)   -> bool
"""

from __future__ import annotations

import json
import logging
import re
import threading
from datetime import datetime
from pathlib import Path

from workpulse.common import load_config, resolve

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
