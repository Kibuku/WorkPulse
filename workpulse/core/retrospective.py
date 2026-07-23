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
                "windows shell experience host", "settings", "search",
                "chatgpt", "claude", "whatsapp", "safari", "google chrome",
                "microsoft teams", "microsoft outlook", "dashboard",
                "getting started", "computer use controls", "activities"}


# ── helpers ───────────────────────────────────────────────────────────────────

def _default_window() -> tuple[str, str]:
    today = datetime.now(timezone.utc).date()
    return (today - timedelta(days=7)).isoformat(), today.isoformat()


_MONTHS = {"january": 1, "february": 2, "march": 3, "april": 4, "may": 5,
           "june": 6, "july": 7, "august": 8, "september": 9, "october": 10,
           "november": 11, "december": 12, "jan": 1, "feb": 2, "mar": 3,
           "apr": 4, "jun": 6, "jul": 7, "aug": 8, "sep": 9, "sept": 9,
           "oct": 10, "nov": 11, "dec": 12}


def _parse_window(query: str | None, today=None) -> tuple[str, str] | None:
    """Extract a date window from a natural-language question ("in June",
    "last month", "last 30 days", "yesterday"). Returns (since, until) ISO dates,
    or None to fall back to the default (last 7 days)."""
    import calendar
    q = (query or "").lower()
    if not q:
        return None
    today = today or datetime.now(timezone.utc).date()
    m = re.search(r"last (\d+)\s+(day|week|month)s?", q)
    if m:
        days = int(m.group(1)) * {"day": 1, "week": 7, "month": 30}[m.group(2)]
        return ((today - timedelta(days=days)).isoformat(), today.isoformat())
    if "yesterday" in q:
        y = today - timedelta(days=1)
        return (y.isoformat(), y.isoformat())
    if "today" in q:
        return (today.isoformat(), today.isoformat())
    if "last week" in q or "past week" in q or "this week" in q:
        return ((today - timedelta(days=7)).isoformat(), today.isoformat())
    if "this month" in q:
        return (today.replace(day=1).isoformat(), today.isoformat())
    if "last month" in q:
        last_prev = today.replace(day=1) - timedelta(days=1)
        return (last_prev.replace(day=1).isoformat(), last_prev.isoformat())
    for name, mo in _MONTHS.items():
        # "may" collides with the verb; only treat it as a month in a date context
        if name == "may" and not re.search(
                r"(in|of|during|for|since|from|through|throughout)\s+may\b|\bmay\s+20\d\d", q):
            continue
        if re.search(r"\b" + name + r"\b", q):
            year = today.year if mo <= today.month else today.year - 1
            last_day = calendar.monthrange(year, mo)[1]
            return (f"{year}-{mo:02d}-01", f"{year}-{mo:02d}-{last_day:02d}")
    return None


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
    t = (raw or "").replace("\u200e", "").replace("\u200f", "").strip()
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
    if val:
        return str(val)
    return key.replace("-", " ").replace("_", " ").title()


# ── the rollup ────────────────────────────────────────────────────────────────

def summarize(con: sqlite3.Connection, *, stream: str | None = None,
              query: str | None = None, since: str | None = None,
              until: str | None = None, cfg: dict | None = None) -> dict:
    """Analysis-oriented rollup of the work in [since, until]."""
    cfg = cfg or {}
    if since is None and until is None:
        parsed = _parse_window(query)     # "in June", "last month", etc.
        if parsed:
            since, until = parsed
    d_since, d_until = _default_window()
    since = since or d_since
    until = until or d_until
    if stream is None:
        stream = _resolve_stream(query, cfg)

    effective_stream = (
        "CASE WHEN ca.source = 'fallback' THEN NULL "
        "ELSE COALESCE(ca.stream, s.stream) END"
    )
    where = ("WHERE substr(s.started_at,1,10) BETWEEN ? AND ? "
             "AND s.ended_at IS NOT NULL")
    params: list = [since, until]
    if stream:
        where += f" AND ({effective_stream}) = ?"
        params.append(stream)
    rows = con.execute(
        f"""SELECT ({effective_stream}) AS stream, s.cluster_id,
                   s.app AS app, sl.raw_title AS title,
                   substr(s.started_at,1,10) AS day,
                   (julianday(s.ended_at) - julianday(s.started_at)) * 86400.0 AS secs
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id = s.id
            LEFT JOIN cluster_assignment ca ON ca.cluster_id = s.cluster_id
            {where}""", params).fetchall()

    total = 0.0
    days: dict[str, float] = defaultdict(float)
    # per-area accumulators, keyed by stream (None = unclassified)
    area_secs: dict = defaultdict(float)
    area_apps: dict = defaultdict(lambda: defaultdict(float))
    area_titles: dict = defaultdict(lambda: defaultdict(lambda: [0.0, ""]))
    area_clusters: dict = defaultdict(set)
    for r in rows:
        secs = float(r["secs"] or 0)
        if secs <= 0:
            continue
        total += secs
        days[r["day"]] += secs
        key = r["stream"]
        if r["cluster_id"]:
            area_clusters[key].add(r["cluster_id"])
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
        outputs = []
        conflicts = []
        try:
            from workpulse.core import cluster_context
            from workpulse.core import projects as wp_projects
            taxonomy = wp_projects.load_projects()
            for cluster_id in list(area_clusters[key])[:12]:
                ctx = cluster_context.cluster_context(
                    con, cluster_id=cluster_id, repos=[], cfg=cfg)
                inferred = cluster_context.infer_output(
                    ctx, _stream_label(key, cfg))
                if (inferred["specific"]
                        and inferred["title"] not in outputs):
                    outputs.append(inferred["title"])
                    match = wp_projects.resolve_match(
                        inferred["title"], taxonomy, kind="text")
                    if match and key and match["key"] != key:
                        conflicts.append({
                            "output": inferred["title"],
                            "filed_as": key,
                            "suggested": match["key"],
                        })
                if len(outputs) >= 5:
                    break
        except Exception:
            outputs = []
            conflicts = []
        areas.append({
            "stream":     key,
            "label":      _stream_label(key, cfg),
            "tagged":     key is not None,
            "seconds":    round(secs),
            "share":      round(secs / total, 3) if total else 0.0,
            "apps":       app_list,
            "highlights": highlights,
            "outputs":    outputs,
            "conflicts":  conflicts,
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
            if a.get("outputs"):
                lines.append("Outputs and work observed: " +
                             "; ".join(a["outputs"]) + ".")
            elif a["highlights"]:
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
                         ". The apps alone do not establish its purpose.")
        if untagged["highlights"]:
            lines.append("Windows seen: " +
                         "; ".join(h["title"] for h in untagged["highlights"]) + ".")
        lines.append("Tag a few of these in the dashboard and it stops being a mystery.")

    conflicts = [c for a in rollup["areas"] for c in a.get("conflicts", [])]
    if conflicts:
        lines += ["", "## Needs review"]
        for c in conflicts[:5]:
            lines.append(
                f"- {c['output']} is filed as {_stream_label(c['filed_as'], cfg)}, "
                f"but its title matches {_stream_label(c['suggested'], cfg)}.")

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


def to_sop_markdown(rollup: dict, *, cfg: dict | None = None,
                    with_meta: bool = False,
                    use_backend: bool = True) -> str | tuple[str, dict]:
    """A thoughtful analysis of the rollup. Deterministic without a backend;
    a richer narrative (with probabilistic inference for untagged time) when
    Anthropic or Ollama is available."""
    deterministic = _deterministic(rollup)
    try:
        if not use_backend:
            raise RuntimeError("backend intentionally skipped")
        from workpulse.core import llm
        skill = _load_skill()
        prompt = (f"{skill}\n\n---\n\nWORK DATA (JSON):\n"
                  f"{json.dumps(rollup, default=str)}\n\n"
                  f"A plain reference rendering you can improve on:\n{deterministic}")
        text, meta = llm.ask_text(prompt, max_tokens=140, cfg=cfg)
        if text and meta.get("backend") != "none":
            return (text, meta) if with_meta else text
    except Exception:
        pass
    meta = {"backend": "none", "model": None, "fallback": True}
    return (deterministic, meta) if with_meta else deterministic


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
