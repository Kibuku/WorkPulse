"""
remembrance.py — cross-Job remembrance, deterministic v1.

Given an active Job, find prior Jobs whose content (name + window titles)
semantically overlaps. The intent is to make the user's own past work
visible while it's still useful — "you've thought about this before; here's
what you produced".

Per Vision §9.2, the entire match runs locally; nothing leaves the machine.
This is the personal-scope precursor to the §4.3 Push Engine. The same
match logic will later run inside the Institution Brain over fingerprints
(§7) — but here it runs on the individual's own Job rollups, on their own
disk, with their own privacy boundary.

The detector is intentionally deterministic — no API key required, works
fully offline. An LLM-coached overlay can be layered on top later (Slice B);
this module's job is to surface the *candidates* with a defensible reason.

Public API:
    remembrance_for(job_id, *, limit=3, cfg=None) -> list[dict]
"""

from __future__ import annotations

import re
import sys
from collections import Counter
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config


# ── tokenisation ─────────────────────────────────────────────────────────────

# Minimal English stopwords + UI/file noise that pollutes window titles.
# Deliberately conservative — better to keep a weak match-word than risk
# dropping a real domain term.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "you", "are", "was", "this", "that",
    "from", "have", "has", "had", "but", "not", "all", "any", "can",
    "into", "out", "out of", "off", "off of", "via", "use", "using", "used",
    "your", "our", "their", "his", "her", "its",
    # WorkPulse / app chrome
    "untitled", "document", "new", "draft",
    # Common app suffixes leaking into titles
    "word", "excel", "powerpoint", "outlook", "safari", "chrome", "firefox",
    "edge", "brave", "claude", "code", "vscode",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


def _tokens(text: str) -> list[str]:
    """Lowercased, stopword-stripped tokens of length ≥3."""
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def _job_bag(job: dict, sessions: list[dict]) -> Counter:
    """Build a token bag for a Job: the name plus the distinct window titles
    seen during its sessions. Window title counts are de-duplicated per
    distinct title so a session that ran for hours doesn't drown out variety."""
    bag: Counter = Counter()
    bag.update(_tokens(job.get("name") or ""))
    bag.update(_tokens(job.get("note") or ""))
    seen_titles: set[str] = set()
    for s in sessions:
        t = (s.get("title") or "").strip()
        if not t or t in seen_titles:
            continue
        seen_titles.add(t)
        bag.update(_tokens(t))
    return bag


def _jaccard(a: Counter, b: Counter) -> tuple[float, list[str]]:
    """Set-based Jaccard on token presence, plus the intersection terms
    (sorted by combined frequency, top 5) for surfacing as the 'reason'."""
    ak = set(a.keys())
    bk = set(b.keys())
    inter = ak & bk
    union = ak | bk
    if not union:
        return 0.0, []
    score = len(inter) / len(union)
    shared = sorted(inter, key=lambda t: -(a[t] + b[t]))[:5]
    return score, shared


# ── public API ───────────────────────────────────────────────────────────────

# Match-quality floor. Below this the overlap is mostly noise.
_MIN_SCORE = 0.10
# Same-stream pairs get a small boost — overlap matters more inside a stream.
_SAME_STREAM_BOOST = 0.08
# Don't let a stream boost alone clear the floor — guard against false positives.
_BARE_OVERLAP_MIN = 2
# Look back this far for candidate prior Jobs.
_LOOKBACK_DAYS = 90


def remembrance_for(job_id: str, *, limit: int = 3,
                    cfg: dict | None = None) -> list[dict]:
    """Find up to `limit` prior Jobs that overlap meaningfully with `job_id`.

    Returns a list of dicts ordered by descending score:
        {
          "job_id":         str,    # the prior Job
          "name":           str,
          "stream":         str | None,
          "total_minutes":  float,  # how much time the prior Job accumulated
          "last_active":    str | None,  # ISO; when the prior Job last had sessions
          "ended_at":       str | None,
          "score":          float,  # final, boost-adjusted
          "shared_terms":   list[str],
          "reason":         str,    # one-line plain-language synthesis
        }

    Returns [] if the source Job doesn't exist, has no token signal, or no
    prior Job clears the floor.
    """
    if cfg is None:
        cfg = load_config()
    from scripts.jobs import get_job, list_all, sessions_for_job

    src = get_job(job_id, cfg=cfg)
    if src is None:
        return []

    src_sessions = sessions_for_job(job_id, cfg=cfg)
    src_bag = _job_bag(src, src_sessions)
    if sum(src_bag.values()) == 0:
        return []

    cutoff = (date.today() - timedelta(days=_LOOKBACK_DAYS)).isoformat()
    matches: list[dict] = []

    for cand in list_all(days_back=_LOOKBACK_DAYS, cfg=cfg):
        if cand["id"] == job_id:
            continue
        # Skip candidates with no date signal (defensive — shouldn't happen)
        if (cand.get("created_at") or "")[:10] < cutoff:
            continue
        cand_sessions = sessions_for_job(cand["id"], cfg=cfg)
        cand_bag = _job_bag(cand, cand_sessions)
        # Keep candidates with either logged activity or a substantive name.
        # An auto-created placeholder like "Item A" has 1 token and zero
        # sessions — that's noise; skip it. A Job named "Carbon methodology
        # research" with no sessions still carries thinking, and is exactly
        # what remembrance should surface.
        if not cand_sessions and len(set(cand_bag.keys())) < 3:
            continue
        if sum(cand_bag.values()) == 0:
            continue

        score, shared = _jaccard(src_bag, cand_bag)
        same_stream = bool(src.get("stream")
                           and src.get("stream") == cand.get("stream"))
        if same_stream:
            score = min(1.0, score + _SAME_STREAM_BOOST)
        if score < _MIN_SCORE:
            continue
        if len(shared) < _BARE_OVERLAP_MIN:
            # A boost shouldn't carry a match with one shared word.
            continue

        # Pull a summary of the prior Job for the card.
        total_s = sum(float(s.get("duration_s") or 0)
                      for s in cand_sessions if not s.get("idle"))
        last_active = None
        for s in cand_sessions:
            end = s.get("end") or s.get("start")
            if end and (last_active is None or end > last_active):
                last_active = end

        # Reason: "shared terms: X, Y, Z" — keep it short and honest.
        reason = "Shared themes: " + ", ".join(shared[:3])
        if same_stream:
            reason += f" · same stream"

        matches.append({
            "job_id":        cand["id"],
            "name":          cand["name"],
            "stream":        cand.get("stream"),
            "total_minutes": round(total_s / 60, 1),
            "last_active":   last_active,
            "ended_at":      cand.get("ended_at"),
            "score":         round(score, 3),
            "shared_terms":  shared,
            "reason":        reason,
        })

    matches.sort(key=lambda m: (-m["score"],
                                # Tiebreak: more total time = more substantive prior work
                                -m["total_minutes"]))
    return matches[:max(1, int(limit))]


# ── CLI: smoke + diagnostic ──────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="Cross-Job remembrance smoke")
    ap.add_argument("job_id", help="Job to find remembrance candidates for")
    ap.add_argument("--limit", type=int, default=5)
    args = ap.parse_args()
    out = remembrance_for(args.job_id, limit=args.limit)
    print(json.dumps(out, indent=2, default=str))
