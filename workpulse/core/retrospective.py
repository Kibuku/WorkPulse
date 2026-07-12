"""
retrospective.py — "how did I work on <X> last week?"

Rolls up the sessions, clusters (job_view), and files touched in a time window
(optionally scoped to one stream) into a factual summary, and turns that into a
repeatable SOP via skills/sop.md. This is the second Ask WorkPulse capability.

Descriptive, never evaluative (Vision §12.2): it observes the shape of the work,
it does not grade it.

Keyless-first: summarize() is pure SQL and always returns a useful rollup.
to_sop_markdown() renders a deterministic day-by-day digest with no backend, and
upgrades to a flowing SOP when a backend (Anthropic or Ollama) is available.

PRIVACY: files/paths come from the private tier. Callers reached through the web
layer must gate on the personal unlock before surfacing them (see core.files).

Public API:
    summarize(con, *, stream=None, query=None, since=None, until=None, cfg=None) -> dict
    to_sop_markdown(rollup, *, cfg=None) -> str

CLI:
    python -m workpulse.core.retrospective "client work" --since 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timedelta, timezone

from workpulse.core import db
from workpulse.common import load_config, PKG

_SKILL_PATH = PKG / "skills" / "sop.md"


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


# ── the rollup ────────────────────────────────────────────────────────────────

def summarize(con: sqlite3.Connection, *, stream: str | None = None,
              query: str | None = None, since: str | None = None,
              until: str | None = None, cfg: dict | None = None) -> dict:
    """Factual rollup of the work in [since, until], optionally one stream."""
    cfg = cfg or {}
    d_since, d_until = _default_window()
    since = since or d_since
    until = until or d_until
    if stream is None:
        stream = _resolve_stream(query, cfg)

    # sessions in window (closed sessions only — need a duration)
    swhere = ("WHERE substr(started_at,1,10) BETWEEN ? AND ? "
              "AND ended_at IS NOT NULL")
    sparams: list = [since, until]
    if stream:
        swhere += " AND stream = ?"
        sparams.append(stream)
    sessions = con.execute(
        f"""SELECT started_at, ended_at, app,
                   (julianday(ended_at) - julianday(started_at)) * 86400.0 AS secs
            FROM session {swhere}""", sparams).fetchall()

    total = sum(float(r["secs"] or 0) for r in sessions)
    apps: dict[str, float] = defaultdict(float)
    days: dict[str, float] = defaultdict(float)
    for r in sessions:
        apps[(r["app"] or "(unknown)")] += float(r["secs"] or 0)
        days[(r["started_at"] or "")[:10]] += float(r["secs"] or 0)

    # clusters (job_view) overlapping the window
    cwhere = "WHERE substr(jv.started_at,1,10) <= ? AND substr(jv.ended_at,1,10) >= ?"
    cparams: list = [until, since]
    if stream:
        cwhere += " AND jv.stream = ?"
        cparams.append(stream)
    clusters = con.execute(
        f"""SELECT jv.cluster_id, jv.total_seconds, jv.started_at, jv.ended_at,
                   cn.name AS name, cn.one_liner AS one_liner
            FROM job_view jv
            LEFT JOIN cluster_name cn ON cn.cluster_id = jv.cluster_id
            {cwhere}
            ORDER BY jv.total_seconds DESC LIMIT 12""", cparams).fetchall()

    # files touched in window (private paths)
    files = con.execute(
        """SELECT fl.raw_path AS path, MAX(fe.ts) AS last_ts, COUNT(*) AS n
           FROM file_event_local fl
           JOIN file_event fe ON fe.id = fl.file_event_id
           WHERE substr(fe.ts,1,10) BETWEEN ? AND ?
           GROUP BY fl.raw_path
           ORDER BY n DESC, last_ts DESC LIMIT 20""", [since, until]).fetchall()

    return {
        "stream":        stream,
        "window":        {"since": since, "until": until},
        "total_seconds": round(total),
        "session_count": len(sessions),
        "active_days":   len(days),
        "apps": [{"app": k, "seconds": round(v)}
                 for k, v in sorted(apps.items(), key=lambda x: -x[1])],
        "day_breakdown": [{"date": k, "seconds": round(v)}
                          for k, v in sorted(days.items())],
        "clusters": [{"name": r["name"], "one_liner": r["one_liner"],
                      "hours": round((r["total_seconds"] or 0) / 3600.0, 1),
                      "started_at": r["started_at"], "ended_at": r["ended_at"]}
                     for r in clusters],
        "files": [{"basename": _basename(r["path"]), "path": r["path"],
                   "last_touched": r["last_ts"], "count": r["n"]}
                  for r in files],
    }


# ── rendering ─────────────────────────────────────────────────────────────────

def _load_skill() -> str:
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("Turn the work data into a short, repeatable SOP. Be factual and "
                "descriptive, never evaluative. End with a Gap section listing what "
                "the data cannot show.")


def _deterministic(rollup: dict) -> str:
    stream = rollup.get("stream") or "your work"
    w = rollup["window"]
    lines = [f"# How you worked on {stream}: {w['since']} to {w['until']}", "",
             f"{_fmt_dur(rollup['total_seconds'])} across "
             f"{rollup['session_count']} sessions and "
             f"{rollup['active_days']} active day"
             f"{'s' if rollup['active_days'] != 1 else ''}.", ""]

    if rollup["day_breakdown"]:
        lines += ["## Day by day", ""]
        for d in rollup["day_breakdown"]:
            lines.append(f"- {d['date']}: {_fmt_dur(d['seconds'])}")
        lines.append("")

    if rollup["clusters"]:
        lines += ["## Pieces of work", ""]
        for c in rollup["clusters"]:
            name = c["name"] or "(unnamed cluster)"
            note = f" ({c['one_liner']})" if c.get("one_liner") else ""
            lines.append(f"- {name}, {c['hours']}h{note}")
        lines.append("")

    if rollup["apps"]:
        lines += ["## Where the time went", ""]
        for a in rollup["apps"][:6]:
            lines.append(f"- {a['app']}: {_fmt_dur(a['seconds'])}")
        lines.append("")

    if rollup["files"]:
        lines += ["## Files touched", ""]
        for f in rollup["files"][:12]:
            when = (f["last_touched"] or "")[:10]
            lines.append(f"- {f['basename']} ({f['count']}x, last {when})")
        lines.append("")

    lines += ["## Gap", "",
              "This is what the sensors saw, not what you were thinking. It shows "
              "the shape of the work and the files involved, but not the decisions "
              "or the why. Add a note if a step here needs its reasoning captured."]
    return "\n".join(lines)


def to_sop_markdown(rollup: dict, *, cfg: dict | None = None) -> str:
    """A repeatable SOP from the rollup. Deterministic without a backend;
    a flowing SOP when Anthropic or Ollama is available."""
    deterministic = _deterministic(rollup)
    try:
        from workpulse.core import llm
        skill = _load_skill()
        prompt = (f"{skill}\n\n---\n\nWORK DATA (JSON):\n"
                  f"{json.dumps(rollup, default=str)}\n\n"
                  f"Reference summary you can build on:\n{deterministic}")
        text, meta = llm.ask_text(prompt, max_tokens=900, cfg=cfg)
        if text and meta.get("backend") != "none":
            return text
    except Exception:
        pass
    return deterministic


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="wp retro",
        description="Summarize how you worked on something, as an SOP.")
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
