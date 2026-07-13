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
    'acme' tags 'Fwd: Acme Q3', 'Acme kickoff notes', etc.
    A rule with stream=None (negative rule) returns None *and* prevents AI
    classification from re-firing for the same pattern.
    """
    norm = normalize_title(title)
    if not norm:
        return None
    rules = load_learned_rules(cfg)
    # Longest-pattern-first so 'acme q3' wins over 'acme'
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


# ── the tag-the-untagged loop ─────────────────────────────────────────────────
# Rules are only useful if they attribute time. retag_sessions applies them to
# already-captured sessions (so teaching one window tags every matching one,
# past and future); classify_untagged is v1's "Loop B" restored — the LLM names
# the biggest untagged window-groups and writes a rule for each.

def retag_sessions(con, cfg: dict | None = None) -> int:
    """Apply learned rules to untagged sessions, updating session.stream in place.
    Returns the number retagged. Keyless: pure rule lookup, no LLM."""
    cfg = cfg or load_config()
    rows = con.execute(
        """SELECT s.id AS id, sl.raw_title AS title
           FROM session s JOIN session_local sl ON sl.session_id = s.id
           WHERE s.stream IS NULL AND sl.raw_title IS NOT NULL AND sl.raw_title <> ''"""
    ).fetchall()
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    n = 0
    for r in rows:
        stream = match_learned(r["title"], cfg)
        if not stream:                      # None = no rule, or a negative rule
            continue
        con.execute("INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
                    (stream, stream))
        con.execute("UPDATE session SET stream = ? WHERE id = ?", (stream, r["id"]))
        con.execute(
            "INSERT OR IGNORE INTO edge(src_kind, src_id, rel, dst_kind, dst_id, created_at) "
            "VALUES ('session', ?, 'in_stream', 'stream', ?, ?)", (r["id"], stream, now))
        n += 1
    con.commit()
    return n


def retag_calendar_events(con, cfg: dict | None = None) -> int:
    """Apply learned rules to still-unattributed meetings, updating
    calendar_event.stream in place. The calendar counterpart of retag_sessions,
    so teaching a rule once files matching meetings AND work windows. Keyless."""
    cfg = cfg or load_config()
    rows = con.execute(
        """SELECT ce.id AS id, cel.raw_title AS title
           FROM calendar_event ce
           JOIN calendar_event_local cel ON cel.event_id = ce.id
           WHERE ce.stream IS NULL AND cel.raw_title IS NOT NULL
                 AND cel.raw_title <> ''"""
    ).fetchall()
    n = 0
    for r in rows:
        stream = match_learned(r["title"], cfg)
        if not stream:                      # None = no rule, or a negative rule
            continue
        con.execute("INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
                    (stream, stream))
        con.execute("UPDATE calendar_event SET stream = ? WHERE id = ?", (stream, r["id"]))
        n += 1
    con.commit()
    return n


def _ask_llm_classify(title: str, streams: dict, cfg: dict) -> tuple[str | None, str]:
    """Ask the LLM which stream a window belongs to, plus a stable anchor phrase.
    Returns (stream_key_or_None, anchor). The prompt is seeded with each stream's
    known keywords (config patterns + already-learned rules) so it self-sharpens."""
    from workpulse.core import llm
    known: dict = {k: [] for k in streams}
    for p in (cfg.get("watcher", {}) or {}).get("stream_path_patterns", []):
        if p.get("stream") in known:
            known[p["stream"]].append(p.get("path", ""))
    for r in load_learned_rules(cfg):
        if r.get("stream") in known and r.get("pattern"):
            known[r["stream"]].append(r["pattern"])
    lines = []
    for k, v in streams.items():
        label = v.get("label") if isinstance(v, dict) else str(v)
        ex = [e for e in known.get(k, []) if e][:12]
        lines.append(f"  - {k}: {label}" + (f"  (keywords: {', '.join(ex)})" if ex else ""))
    prompt = (
        "Tag this window into one of the user's work streams.\n\n"
        "Streams:\n" + "\n".join(lines) + "\n\n"
        f"Window title: {title}\n\n"
        "Prefer specific project keywords over generic app or channel names "
        "(Gmail, WhatsApp, Inbox, the browser). Use 'none' for clearly off-task or "
        "personal windows.\n"
        "Reply with ONLY one JSON object:\n"
        '  {"stream": "<key or none>", "anchor": "<2-4 word phrase taken from the title>"}'
    )
    obj, _meta = llm.ask_json(prompt, max_tokens=80, cfg=cfg)
    if not obj:
        return None, ""
    stream = obj.get("stream")
    anchor = (obj.get("anchor") or "").strip().lower()
    if (not isinstance(stream, str) or stream.lower() in ("none", "", "null")
            or stream not in streams):
        return None, anchor
    return stream, anchor


def classify_untagged(con, *, cfg: dict | None = None, max_windows: int = 10) -> dict:
    """Loop B: AI-classify the biggest untagged window-groups into streams,
    learning a rule for each (a negative rule when off-task), then retag every
    matching session. No-op (backend='none') without an LLM. Returns
    {backend, classified, rules, retagged}."""
    from collections import defaultdict
    cfg = cfg or load_config()
    from workpulse.core import llm
    backend = llm.active_backend(cfg)
    streams = cfg.get("streams") or {}
    if backend == "none" or not streams:
        return {"backend": backend, "classified": 0, "rules": 0, "retagged": 0}
    rows = con.execute(
        """SELECT sl.raw_title AS title,
                  SUM((julianday(s.ended_at) - julianday(s.started_at)) * 86400.0) AS secs
           FROM session s JOIN session_local sl ON sl.session_id = s.id
           WHERE s.stream IS NULL AND s.ended_at IS NOT NULL
                 AND sl.raw_title IS NOT NULL AND sl.raw_title <> ''
           GROUP BY sl.raw_title"""
    ).fetchall()
    groups: dict = defaultdict(float)
    sample: dict = {}
    for r in rows:
        nt = normalize_title(r["title"])
        if not nt or len(nt) < 4:
            continue
        if match_learned(r["title"], cfg) is not None or has_negative_rule(r["title"], cfg):
            continue
        groups[nt] += float(r["secs"] or 0)
        sample.setdefault(nt, r["title"])
    top = sorted(groups.items(), key=lambda x: -x[1])[:max_windows]
    for nt, _secs in top:
        stream, anchor = _ask_llm_classify(sample[nt], streams, cfg)
        pattern = anchor if (anchor and anchor in nt) else nt
        _add_rule(cfg, pattern=pattern, stream=stream, raw_title=sample[nt], source="ai")
    retagged = retag_sessions(con, cfg)
    return {"backend": backend, "classified": len(top),
            "rules": len(top), "retagged": retagged}
