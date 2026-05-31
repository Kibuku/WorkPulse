"""
lift.py — AI-lift detectors.

For a given Job's sessions, find specific points where AI would accelerate
the work the user is actually doing — grounded in observed session data,
not generic productivity advice.

Per Vision §12.2, the initial detector set:
  1. excel_cleanup        — long Excel cell-editing on a single file
  2. research_tabbing     — many-tab browser research on one topic
  3. word_long_session    — long Word writing sessions
  4. gmail_compose_burst  — multiple Gmail compose windows in quick succession
  5. long_protected_view  — long read of a Protected-View .docx/.pdf
  6. powerpoint_layout    — long PowerPoint shape-placement session

Each detector takes a list of activity sessions and returns 0+ LiftOpportunity
records. The Coach card on the dashboard renders these inline per job.

LiftOpportunity shape:
    {
        "kind":       "excel_cleanup",
        "title":      "1h 8m of Excel editing on NKCC_Q3_data.xlsx",
        "suggestion": "Describe the transform — Claude produces a one-shot script in ~10 min.",
        "evidence": {
            "duration_s": 4080,
            "session_count": 3,
            "files": ["NKCC_Q3_data.xlsx"]
        }
    }

All detectors are deterministic. Zero LLM calls in this module — pattern
matching only. Detection has to be cheap because the Coach card refreshes
on every dashboard load.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Iterable


# ── helpers ──────────────────────────────────────────────────────────────────

def _fmt_dur(seconds: float) -> str:
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h}h {mm}m" if mm else f"{h}h"


def _app_is(session: dict, *names: str) -> bool:
    app = (session.get("app") or "").lower()
    exe = (session.get("exe_path") or "").lower()
    haystack = f"{app} {exe}"
    return any(n.lower() in haystack for n in names)


def _file_from_title(title: str) -> str | None:
    """Heuristic: pull a filename out of a Word/Excel/PowerPoint title.
    Office app titles look like 'NKCC_Q3_data.xlsx - Excel'."""
    if not title:
        return None
    # Try the obvious '<filename.ext> - <app>' pattern
    m = re.match(r"^(.+?\.(?:xlsx|xlsm|xls|csv|docx|doc|pptx|ppt|pdf))\s*[-–—]\s*",
                 title, re.I)
    if m:
        return m.group(1).strip()
    return None


def _normalize_browser_title(title: str) -> str:
    """Strip browser-name suffixes and notification counts so we can compare
    titles for topic similarity."""
    t = title or ""
    t = re.sub(r"^\s*\(\d+\)\s*", "", t)
    t = re.sub(r"\s*and \d+ more pages?\s*", " ", t, flags=re.I)
    t = re.sub(r"\s*[-–—]\s*(brave|google chrome|microsoft\W*edge|firefox|opera|vivaldi)\s*$",
               "", t, flags=re.I)
    t = re.sub(r"\s*[-–—]\s*personal.*$", "", t, flags=re.I)
    return t.strip().lower()


# ── detectors ────────────────────────────────────────────────────────────────

def _detect_excel_cleanup(sessions: list[dict]) -> list[dict]:
    """≥30 minutes of Excel on a single file → script opportunity."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        if not _app_is(s, "excel.exe", "excel"):
            continue
        f = _file_from_title(s.get("title") or "") or "(unknown file)"
        by_file[f].append(s)
    out = []
    for fname, group in by_file.items():
        total = sum(float(s.get("duration_s") or 0) for s in group)
        if total < 30 * 60:
            continue
        out.append({
            "kind":       "excel_cleanup",
            "title":      f"{_fmt_dur(total)} of Excel editing on {fname}",
            "suggestion": "Describe the transform — Claude produces a one-shot Python or formula script "
                          "that runs in seconds instead of an hour of cell-by-cell work.",
            "evidence": {
                "duration_s":    int(total),
                "session_count": len(group),
                "files":         [fname],
            },
        })
    return out


def _detect_research_tabbing(sessions: list[dict]) -> list[dict]:
    """≥30 minutes total in browser sessions, ≥3 distinct page titles → research-summary prompt."""
    browser_sessions = [s for s in sessions
                        if _app_is(s, "brave", "chrome", "edge", "firefox", "opera", "vivaldi")]
    if not browser_sessions:
        return []
    total = sum(float(s.get("duration_s") or 0) for s in browser_sessions)
    if total < 30 * 60:
        return []
    titles = Counter(_normalize_browser_title(s.get("title") or "") for s in browser_sessions)
    titles.pop("", None)
    if len(titles) < 3:
        return []
    sample = [t for t, _ in titles.most_common(4)]
    return [{
        "kind":       "research_tabbing",
        "title":      f"{_fmt_dur(total)} of browser research across {len(titles)} pages",
        "suggestion": "Drop the topic + the sources you've already opened into a single Claude prompt: "
                      "'Summarize, with citations.' Five minutes instead of an hour of tabbing.",
        "evidence": {
            "duration_s":    int(total),
            "session_count": len(browser_sessions),
            "page_count":    len(titles),
            "sample_pages":  sample,
        },
    }]


def _detect_word_long_session(sessions: list[dict]) -> list[dict]:
    """Single Word file used ≥60 min in this job → first-pass draft opportunity."""
    by_file: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        if not _app_is(s, "winword.exe", "word"):
            continue
        f = _file_from_title(s.get("title") or "") or "(unknown file)"
        by_file[f].append(s)
    out = []
    for fname, group in by_file.items():
        total = sum(float(s.get("duration_s") or 0) for s in group)
        if total < 60 * 60:
            continue
        out.append({
            "kind":       "word_long_session",
            "title":      f"{_fmt_dur(total)} of Word writing on {fname}",
            "suggestion": "Have Claude produce a first-pass draft of this section from a 5-line brief — "
                          "then you edit instead of write. Faster for any content that follows a known shape.",
            "evidence": {
                "duration_s":    int(total),
                "session_count": len(group),
                "files":         [fname],
            },
        })
    return out


def _detect_gmail_compose_burst(sessions: list[dict]) -> list[dict]:
    """≥3 distinct Gmail compose windows in the same job → email templating opportunity."""
    compose_re = re.compile(r"\bcompose\b|inbox\s+\(", re.I)
    gmail_sessions = [s for s in sessions
                      if "gmail" in (s.get("title") or "").lower()]
    if len(gmail_sessions) < 3:
        return []
    total = sum(float(s.get("duration_s") or 0) for s in gmail_sessions)
    if total < 10 * 60:  # at least 10 min total to be worth flagging
        return []
    # Cheap "compose-ish" filter: titles that mention compose or that look like
    # subject lines (longer, contain a colon, etc.)
    composeish = [s for s in gmail_sessions
                  if compose_re.search(s.get("title") or "")
                  or ":" in (s.get("title") or "")
                  or len((s.get("title") or "")) > 40]
    if len(composeish) < 3:
        return []
    return [{
        "kind":       "gmail_compose_burst",
        "title":      f"{len(composeish)} email-composition windows ({_fmt_dur(total)} total)",
        "suggestion": "If these are variations on the same email, draft a Claude prompt "
                      "with the core message + recipient list. One prompt = N personalised drafts.",
        "evidence": {
            "duration_s":    int(total),
            "session_count": len(gmail_sessions),
            "composeish":    len(composeish),
        },
    }]


def _detect_long_protected_view(sessions: list[dict]) -> list[dict]:
    """≥15 min in a Protected-View .docx/.pdf → summarise + Q&A via Claude."""
    pv_sessions = [s for s in sessions
                   if "protected view" in (s.get("title") or "").lower()
                   or "read-only" in (s.get("title") or "").lower()]
    if not pv_sessions:
        return []
    total = sum(float(s.get("duration_s") or 0) for s in pv_sessions)
    if total < 15 * 60:
        return []
    files = []
    for s in pv_sessions:
        f = _file_from_title(s.get("title") or "")
        if f and f not in files:
            files.append(f)
    label = ", ".join(files[:3]) if files else f"{len(pv_sessions)} document(s)"
    return [{
        "kind":       "long_protected_view",
        "title":      f"{_fmt_dur(total)} reading {label}",
        "suggestion": "Hand the file to Claude: 'Summarise, extract the action items, "
                      "and let me ask follow-up questions.' Faster than skim-reading.",
        "evidence": {
            "duration_s":    int(total),
            "session_count": len(pv_sessions),
            "files":         files,
        },
    }]


def _detect_powerpoint_layout(sessions: list[dict]) -> list[dict]:
    """≥45 min in PowerPoint → diagram/SmartArt-via-Claude opportunity."""
    ppt_sessions = [s for s in sessions if _app_is(s, "powerpnt", "powerpoint")]
    if not ppt_sessions:
        return []
    total = sum(float(s.get("duration_s") or 0) for s in ppt_sessions)
    if total < 45 * 60:
        return []
    return [{
        "kind":       "powerpoint_layout",
        "title":      f"{_fmt_dur(total)} in PowerPoint",
        "suggestion": "If you're placing shapes manually, ask Claude to produce a Mermaid diagram "
                      "or describe the SmartArt — paste into the slide instead of hand-laying it out.",
        "evidence": {
            "duration_s":    int(total),
            "session_count": len(ppt_sessions),
        },
    }]


# ── public entry point ───────────────────────────────────────────────────────

_DETECTORS = (
    _detect_excel_cleanup,
    _detect_research_tabbing,
    _detect_word_long_session,
    _detect_gmail_compose_burst,
    _detect_long_protected_view,
    _detect_powerpoint_layout,
)


def detect_lift(sessions: list[dict], *,
                job: dict | None = None,
                cfg: dict | None = None) -> list[dict]:
    """Run all detectors against the session list for a job. Returns a flat
    list of LiftOpportunity dicts, sorted by duration_s descending (biggest
    opportunity first)."""
    if not sessions:
        return []
    out: list[dict] = []
    for det in _DETECTORS:
        try:
            out.extend(det(sessions))
        except Exception:
            # A broken detector should not break the rest of the card
            continue
    out.sort(key=lambda x: -(x.get("evidence") or {}).get("duration_s", 0))
    return out


if __name__ == "__main__":
    # Quick CLI to see what lift would be detected for an existing job
    import json, sys as _sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from scripts.common import load_config
    from scripts.jobs import sessions_for_job
    if len(_sys.argv) < 2:
        print("usage: lift.py <job-id>")
        _sys.exit(1)
    cfg = load_config()
    sessions = sessions_for_job(_sys.argv[1], cfg=cfg)
    print(f"sessions for job: {len(sessions)}")
    for lift in detect_lift(sessions, cfg=cfg):
        print(f"\n[{lift['kind']}]  {lift['title']}")
        print(f"  → {lift['suggestion']}")
