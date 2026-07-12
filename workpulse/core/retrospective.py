"""
retrospective.py — "how did I work on <X>?" / "what have I worked on this week?"

Turns raw sessions into an *analysis*, not a dump. Every window and file is
assumed to mean something, so the rollup groups time into **areas of work**:

  - Tagged areas (a stream): what was worked on, grounded in the window titles
    and files, so the answer can say what the work actually was.
  - Unclassified time: sessions with no stream. Surfaced honestly with the apps
    and windows involved, so an LLM (or the reader) can infer, probabilistically,
    what it likely was and where it fits.

Keyless-first: summarize() is pure SQL. to_sop_markdown() renders a readable
analysis with no backend, and upgrades to a thoughtful narrative via
skills/sop.md when Anthropic or Ollama is available.

Descriptive, never evaluative (Vision §12.2): it explains what happened, it does
not judge it.

Public API:
    summarize(con, *, stream=None, query=None, since=None, until=None, cfg=None) -> dict
    to_sop_markdown(rollup, *, cfg=None) -> str
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from workpulse.core import db
from workpulse.common import load_config, PKG

_SKILL_PATH = PKG / "skills" / "sop.md"

# Friendly names for the exe soup so "brave.exe" reads as "Brave".
_APP_NAMES = {
    "brave.exe": "Brave", "chrome.exe": "Chrome", "msedge.exe": "Edge",
    "firefox.exe": "Firefox", "opera.exe": "Opera", "arc.exe": "Arc",
    "winword.exe": "Word", "excel.exe": "Excel", "powerpnt.exe": "PowerPoint",
    "onenote.exe": "OneNote", "outlook.exe": "Outlook", "acrobat.exe": "Acrobat",
    "acrord32.exe": "Acrobat Reader", "code.exe": "VS Code",
    "windowsterminal.exe": "Windows Terminal", "powershell.exe": "PowerShell",
    "cmd.exe": "Command Prompt", "explorer.exe": "File Explorer",
    "teams.exe": "Teams", "slack.exe": "Slack", "zoom.exe": "Zoom",
    "notepad.exe": "Notepad", "notepad++.exe": "Notepad++",
}
# Apps that carry no work meaning on their own — folded into time, never a "what".
_SYSTEM_APPS = {
    "applicationframehost.exe", "shellexperiencehost.exe", "shellhost.exe",
    "lockapp.exe", "searchhost.exe", "searchui.exe", "startmenuexperiencehost.exe",
    "textinputhost.exe", "dwm.exe", "sihost.exe", "ctfmon.exe", "runtimebroker.exe",
    "", "unknown", "(unknown)",
}
_SKIP_TITLES = {"", "(no window)", "program manager", "task switching",
                "windows shell experience host", "settings", "search"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _default_window() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=7)).isoformat(), today.isoformat()


def _basename(path: str) -> str:
    return (path or "").replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _fmt_dur(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h}h {mm}m" if mm else f"{h}h"


def _friendly_app(exe: str | None) -> str:
    e = (exe or "").strip().lower()
    if e in _APP_NAMES:
        return _APP_NAMES[e]
    if e.endswith(".exe"):
        e = e[:-4]
    return e.replace("-", " ").replace("_", " ").title() if e else "unknown app"


def _is_system_app(exe: str | None) -> bool:
    return (exe or "").strip().lower() in _SYSTEM_APPS


_BROWSER_SUFFIX = re.compile(
    r"\s*[-–—]\s*(brave|google chrome|chrome|microsoft\W*edge|edge|firefox|opera|arc|vivaldi)\s*$",
    re.I)
_PERSONAL_SUFFIX = re.compile(r"\s*[-–—]\s*personal.*$", re.I)


def _norm_title(raw: str | None) -> str:
    t = (raw or "").strip()
    t = re.sub(r"^\s*\(\d+\)\s*", "", t)                  # leading "(66)" count
    t = re.sub(r"\s*and \d+ more pages?\s*", " ", t, flags=re.I)
    t = _BROWSER_SUFFIX.sub("", t)
    t = _PERSONAL_SUFFIX.sub("", t)
    return re.sub(r"\s+", " ", t).strip()


def _resolve_stream(query: str | None, cfg: dict | None) -> str | None:
    streams = (cfg or {}).get("streams") or {}
    if not query or not streams:
        return None
    q = query.lower()
    for key, val in streams.items():
        label = (val.get("label") if isinstance(val, dict) else str(val)) or key
        if key.replace("-", " ").lower() in q or str(label).lower() in q:
            return key
    return None


def _stream_label(key: str | None, cfg: dict | None) -> str:
    if key is None:
        return "Unclassified time"
    streams = (cfg or {}).get("streams") or {}
    val = streams.get(key)
    if isinstance(val, dict) and val.get("label"):
        return val["label"]
    return str(val) if val else key


# ── the rollup ────────────────────────────────────────────────────────────────

def summarize(con: sqlite3.Connection, *, stream: str | None = None,
              query: str | None = None, since: str | None = None,
              until: str | None = None, cfg: dict | None = None) -> dict:
    """Analysis-oriented rollup of the work in [since, until]."""
    cfg = cfg or {}
    d_since, d_until = _default_window()
    since = since or d_since
    until = until or d_until
    if stream is None:
        stream = _resolve_stream(query, cfg)

    where = ("WHERE substr(s.started_at,1,10) BETWEEN ? AND ? "
             "AND s.ended_at IS NOT NULL")
    params: list = [since, until]
    if stream:
        where += " AND s.stream = ?"
        params.append(stream)
    rows = con.execute(
        f"""SELECT s.stream AS stream, s.app AS app, sl.raw_title AS title,
                   substr(s.started_at,1,10) AS day,
                   (julianday(s.ended_at) - julianday(s.started_at)) * 86400.0 AS secs
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id = s.id
            {where}""", params).fetchall()

    total = 0.0
    days: dict[str, float] = defaultdict(float)
    # per-area accumulators, keyed by stream (None = unclassified)
    area_secs: dict = defaultdict(float)
    area_apps: dict = defaultdict(lambda: defaultdict(float))
    area_titles: dict = defaultdict(lambda: defaultdict(lambda: [0.0, ""]))
    for r in rows:
        secs = float(r["secs"] or 0)
        if secs <= 0:
            continue
        total += secs
        days[r["day"]] += secs
        key = r["stream"]
        area_secs[key] += secs
        area_apps[key][(r["app"] or "").lower()] += secs
        nt = _norm_title(r["title"])
        if nt and nt.lower() not in _SKIP_TITLES and not _is_system_app(r["app"]):
            slot = area_titles[key][nt.lower()]
            slot[0] += secs
            slot[1] = slot[1] or nt[:90]

    areas = []
    for key, secs in sorted(area_secs.items(), key=lambda x: -x[1]):
        apps = sorted(area_apps[key].items(), key=lambda x: -x[1])
        app_list = [{"app": a, "label": _friendly_app(a), "seconds": round(v)}
                    for a, v in apps if not _is_system_app(a)][:5]
        titles = sorted(area_titles[key].values(), key=lambda x: -x[0])
        highlights = [{"title": raw, "seconds": round(sec)}
                      for sec, raw in titles if raw][:4]
        areas.append({
            "stream":     key,
            "label":      _stream_label(key, cfg),
            "tagged":     key is not None,
            "seconds":    round(secs),
            "share":      round(secs / total, 3) if total else 0.0,
            "apps":       app_list,
            "highlights": highlights,
        })

    files = con.execute(
        """SELECT fl.raw_path AS path, MAX(fe.ts) AS last_ts, COUNT(*) AS n
           FROM file_event_local fl
           JOIN file_event fe ON fe.id = fl.file_event_id
           WHERE substr(fe.ts,1,10) BETWEEN ? AND ?
           GROUP BY fl.raw_path
           ORDER BY n DESC, last_ts DESC LIMIT 40""", [since, until]).fetchall()
    from workpulse.core.files import _is_noise as _noise_path
    files = [r for r in files if not _noise_path(r["path"])][:20]

    return {
        "stream":        stream,
        "window":        {"since": since, "until": until},
        "total_seconds": round(total),
        "session_count": len(rows),
        "active_days":   len(days),
        "day_breakdown": [{"date": k, "seconds": round(v)}
                          for k, v in sorted(days.items())],
        "areas":         areas,
        "files": [{"basename": _basename(r["path"]), "path": r["path"],
                   "last_touched": r["last_ts"], "count": r["n"]}
                  for r in files],
    }


# ── rendering ─────────────────────────────────────────────────────────────────

def _load_skill() -> str:
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("Explain what the person worked on. For tagged areas, say what the "
                "work was from the window titles and files. For unclassified time, "
                "infer probabilistically what it likely was. Descriptive, never "
                "evaluative. End with a Gap section on what the data cannot show.")


def _deterministic(rollup: dict) -> str:
    stream = rollup.get("stream")
    w = rollup["window"]
    scope = _stream_label(stream, None) if stream else "your work"
    lines = [f"# What you worked on ({scope}): {w['since']} to {w['until']}", "",
             f"{_fmt_dur(rollup['total_seconds'])} across "
             f"{rollup['session_count']} sessions and "
             f"{rollup['active_days']} active day"
             f"{'s' if rollup['active_days'] != 1 else ''}."]

    tagged = [a for a in rollup["areas"] if a["tagged"] and a["seconds"] >= 60]
    if tagged:
        lines += ["", "## What you worked on"]
        for a in tagged:
            pct = round(a["share"] * 100)
            lines += ["", f"### {a['label']}: {_fmt_dur(a['seconds'])} ({pct}%)"]
            if a["highlights"]:
                lines.append("You were on: " +
                             "; ".join(h["title"] for h in a["highlights"]) + ".")
            if a["apps"]:
                lines.append("Mostly in " +
                             ", ".join(x["label"] for x in a["apps"][:3]) + ".")

    untagged = next((a for a in rollup["areas"] if not a["tagged"]), None)
    if untagged and untagged["seconds"] >= 60:
        pct = round(untagged["share"] * 100)
        lines += ["", f"## Unclassified time: {_fmt_dur(untagged['seconds'])} ({pct}%)",
                  "This time isn't tagged to a project yet."]
        if untagged["apps"]:
            lines.append("It was mostly " +
                         ", ".join(x["label"] for x in untagged["apps"][:3]) +
                         ", so it likely relates to those.")
        if untagged["highlights"]:
            lines.append("Windows seen: " +
                         "; ".join(h["title"] for h in untagged["highlights"]) + ".")
        lines.append("Tag a few of these in the dashboard and it stops being a mystery.")

    if rollup["files"]:
        lines += ["", "## Files touched"]
        for f in rollup["files"][:10]:
            when = (f["last_touched"] or "")[:10]
            lines.append(f"- {f['basename']} ({f['count']}x, last {when})")

    lines += ["", "## Gap",
              "This is what the sensors saw, not what you were thinking. It shows "
              "which projects and windows the time went to, but not the decisions "
              "behind them. Tagging the unclassified time sharpens every answer."]
    return "\n".join(lines)


def to_sop_markdown(rollup: dict, *, cfg: dict | None = None) -> str:
    """A thoughtful analysis of the rollup. Deterministic without a backend;
    a richer narrative (with probabilistic inference for untagged time) when
    Anthropic or Ollama is available."""
    deterministic = _deterministic(rollup)
    try:
        from workpulse.core import llm
        skill = _load_skill()
        prompt = (f"{skill}\n\n---\n\nWORK DATA (JSON):\n"
                  f"{json.dumps(rollup, default=str)}\n\n"
                  f"A plain reference rendering you can improve on:\n{deterministic}")
        text, meta = llm.ask_text(prompt, max_tokens=1100, cfg=cfg)
        if text and meta.get("backend") != "none":
            return text
    except Exception:
        pass
    return deterministic


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="wp retro",
        description="Analyze what you worked on in a window.")
    ap.add_argument("query", nargs="*", help="Stream / project words (optional).")
    ap.add_argument("--stream")
    ap.add_argument("--since", metavar="YYYY-MM-DD")
    ap.add_argument("--until", metavar="YYYY-MM-DD")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    query = " ".join(args.query).strip() or None
    cfg = load_config()
    con = db.connect(cfg=cfg)
    rollup = summarize(con, stream=args.stream, query=query,
                       since=args.since, until=args.until, cfg=cfg)
    if args.json:
        print(json.dumps(rollup, indent=2, default=str))
    else:
        print(to_sop_markdown(rollup, cfg=cfg))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
