"""
files.py — "where is my <file>?" locate over captured file activity.

The first Ask WorkPulse capability, and deliberately the simplest: given a
loose description ("client z proposal", "the budget spreadsheet"), find the
file on disk the user most likely means, ranked by name match, project,
recency, and how often they've touched it.

Sources (both are the *private* projection — see the lock note below):
  - file_event_local.raw_path  — files created / modified / deleted
  - session_local.raw_path + raw_files — files open in a tracked window

Deterministic and keyless by design: this returns a useful answer with no
API key and no Ollama. The LLM only makes the phrasing conversational
(that lives in the /api/ask layer, Phase 1c), never the retrieval.

PRIVACY: raw_path lives in the private tier. Callers reached through the web
layer MUST check the personal unlock (core.personal) before invoking locate()
and surfacing paths. The core function itself performs the local read; access
control is the caller's responsibility (mirrors the rest of WorkPulse).

Public API:
    locate(con, query, *, limit=5, since=None, cfg=None) -> list[FileHit]
    format_hits(hits, query) -> str          # keyless markdown answer

FileHit = {path, basename, project, last_touched, touch_count, score}

CLI:
    python -m workpulse.core.files "client z proposal"
    python -m workpulse.core.files "budget spreadsheet" --since 2026-07-01
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import datetime, timezone

from workpulse.core import db
from workpulse.common import load_config, ROOT

# WorkPulse's own files (its install dir, packaged copies, virtualenvs, caches)
# get captured by the watcher but are never what a user is looking for. Drop them
# so a query like "report" surfaces your documents, not WorkPulse internals.
_NOISE_MARKERS = ("workpulse-pkg", "site-packages", "__pycache__",
                  "node_modules", "/.venv/", "appdata/local/temp",
                  "appdata/local/packages")
_WP_ROOT_KEY = str(ROOT).replace("\\", "/").lower().rstrip("/") + "/"


def _is_noise(path: str) -> bool:
    p = (path or "").replace("\\", "/").lower()
    if len(_WP_ROOT_KEY) > 1 and p.startswith(_WP_ROOT_KEY):
        return True
    return any(m in p for m in _NOISE_MARKERS)

# Words that carry no signal for locating a file.
_STOPWORDS = {
    "where", "is", "are", "the", "a", "an", "my", "our", "find", "locate",
    "open", "show", "me", "file", "files", "document", "doc", "for", "of",
    "to", "on", "in", "please", "can", "you", "i", "was", "were", "that",
    "this", "it", "and", "with", "get", "grab", "pull", "up", "last",
}

# Common document extensions we let a bare word ("spreadsheet", "pdf") hint at.
_EXT_ALIASES = {
    "spreadsheet": {"xlsx", "xls", "xlsm", "csv"},
    "sheet":       {"xlsx", "xls", "xlsm", "csv"},
    "excel":       {"xlsx", "xls", "xlsm", "csv"},
    "presentation":{"pptx", "ppt"},
    "slides":      {"pptx", "ppt"},
    "powerpoint":  {"pptx", "ppt"},
    "word":        {"docx", "doc"},
    "pdf":         {"pdf"},
}


# ── path helpers ──────────────────────────────────────────────────────────────

def _basename(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]


def _extension(path: str) -> str:
    base = _basename(path)
    return base.rsplit(".", 1)[-1].lower() if "." in base else ""


def _norm_key(path: str) -> str:
    return path.replace("\\", "/").rstrip("/").lower()


def _tokenize(query: str) -> list[str]:
    toks = [t for t in "".join(
        c.lower() if (c.isalnum()) else " " for c in query
    ).split() if t and t not in _STOPWORDS]
    # de-dup, preserve order
    seen, out = set(), []
    for t in toks:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


# ── candidate gathering ───────────────────────────────────────────────────────

class _Cand:
    __slots__ = ("path", "last_touched", "touch_count", "stream")

    def __init__(self, path: str):
        self.path = path
        self.last_touched = ""
        self.touch_count = 0
        self.stream: str | None = None

    def bump(self, ts: str | None, stream: str | None):
        self.touch_count += 1
        if ts and ts > self.last_touched:
            self.last_touched = ts
        if stream and not self.stream:
            self.stream = stream


def _gather(con: sqlite3.Connection, since: str | None) -> dict[str, _Cand]:
    cands: dict[str, _Cand] = {}

    def add(path: str | None, ts: str | None, stream: str | None):
        if not path or _is_noise(path):
            return
        key = _norm_key(path)
        c = cands.get(key)
        if c is None:
            c = _Cand(path)
            cands[key] = c
        # Keep the most recent spelling of the path for display
        if ts and ts >= c.last_touched:
            c.path = path
        c.bump(ts, stream)

    # file events
    where = "WHERE fe.ts >= ?" if since else ""
    params = (since,) if since else ()
    for r in con.execute(
        f"""SELECT fl.raw_path AS path, fe.ts AS ts
            FROM file_event_local fl
            JOIN file_event fe ON fe.id = fl.file_event_id
            {where}""", params):
        add(r["path"], r["ts"], None)

    # sessions: single raw_path + raw_files JSON array
    where_s = "WHERE s.started_at >= ?" if since else ""
    for r in con.execute(
        f"""SELECT sl.raw_path AS path, sl.raw_files AS files,
                   s.started_at AS ts, s.stream AS stream
            FROM session_local sl
            JOIN session s ON s.id = sl.session_id
            {where_s}""", params):
        add(r["path"], r["ts"], r["stream"])
        if r["files"]:
            try:
                for f in json.loads(r["files"]):
                    add(f, r["ts"], r["stream"])
            except (json.JSONDecodeError, TypeError):
                pass
    return cands


# ── scoring ───────────────────────────────────────────────────────────────────

def _recency_bonus(last_touched: str, now: datetime) -> float:
    if not last_touched:
        return 0.0
    try:
        dt = datetime.fromisoformat(last_touched.replace("Z", "+00:00"))
    except ValueError:
        return 0.0
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    days = (now - dt).total_seconds() / 86400.0
    if days <= 1:   return 3.0
    if days <= 7:   return 2.0
    if days <= 30:  return 1.0
    return 0.0


def _score(cand: _Cand, tokens: list[str], want_exts: set[str],
           projects: list[dict], now: datetime) -> tuple[float, dict]:
    base = _basename(cand.path).lower()
    full = cand.path.replace("\\", "/").lower()
    name_score = 0.0
    for t in tokens:
        if t in base:
            name_score += 3.0
        elif t in full:
            name_score += 1.0
    ext_score = 2.0 if (want_exts and _extension(cand.path) in want_exts) else 0.0

    # project match: resolve the path to a stream, bonus if a query token
    # matches that stream key/label or the session's own stream.
    project = cand.stream
    if project is None and projects:
        try:
            from workpulse.core import projects as wp_projects
            m = wp_projects.resolve_match(cand.path, projects, kind="path")
            if m:
                project = m.get("stream")
        except Exception:
            project = None
    project_score = 0.0
    if project:
        pl = str(project).replace("-", " ").lower()
        if any(t in pl for t in tokens):
            project_score = 2.0

    freq_score = min(cand.touch_count, 5) * 0.2
    rec_score = _recency_bonus(cand.last_touched, now)
    total = name_score + ext_score + project_score + freq_score + rec_score
    return total, {"name": name_score, "ext": ext_score, "project": project_score,
                   "freq": freq_score, "recency": rec_score, "resolved_project": project}


# ── public API ────────────────────────────────────────────────────────────────

def locate(con: sqlite3.Connection, query: str, *, limit: int = 5,
           since: str | None = None, cfg: dict | None = None) -> list[dict]:
    """Return up to `limit` FileHit dicts for a loose file description,
    best match first. Deterministic; no LLM. See module docstring re: privacy."""
    tokens = _tokenize(query)
    want_exts: set[str] = set()
    for t in tokens:
        if t in _EXT_ALIASES:
            want_exts |= _EXT_ALIASES[t]
        elif len(t) <= 4 and t.isalpha() and t in {
            "docx", "doc", "xlsx", "xls", "pdf", "pptx", "ppt", "csv", "txt", "md"}:
            want_exts.add(t)

    projects: list[dict] = []
    try:
        from workpulse.core import projects as wp_projects
        projects = wp_projects.load_projects()
    except Exception:
        projects = []

    now = datetime.now(timezone.utc)
    hits: list[dict] = []
    for cand in _gather(con, since).values():
        score, parts = _score(cand, tokens, want_exts, projects, now)
        # A hit must actually match the query (name or project), unless the
        # query had no usable tokens (then rank purely by recency).
        if tokens and parts["name"] == 0.0 and parts["project"] == 0.0:
            continue
        hits.append({
            "path":         cand.path,
            "basename":     _basename(cand.path),
            "project":      parts["resolved_project"],
            "last_touched": cand.last_touched or None,
            "touch_count":  cand.touch_count,
            "score":        round(score, 2),
        })
    hits.sort(key=lambda h: (h["score"], h["last_touched"] or ""), reverse=True)
    return hits[:limit]


def _human_when(iso: str | None) -> str:
    if not iso:
        return "at an unknown time"
    try:
        dt = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return f"on {iso[:10]}"
    return dt.strftime("on %a %b %d at %H:%M")


def format_hits(hits: list[dict], query: str) -> str:
    """A warm, keyless answer. No em dashes or arrows (house voice)."""
    if not hits:
        return (f"I couldn't find a file matching \"{query}\" in what I've seen "
                f"you work on. If you've opened it since I started watching, try "
                f"a word from its name, or a different phrasing.")
    top = hits[0]
    lines = [f"Your best match for \"{query}\" is **{top['basename']}**, "
             f"last touched {_human_when(top['last_touched'])}.",
             "",
             f"`{top['path']}`"]
    if len(hits) > 1:
        lines += ["", "Other files that matched:"]
        for h in hits[1:]:
            lines.append(f"- {h['basename']}  ({_human_when(h['last_touched'])})")
    return "\n".join(lines)


# ── CLI ───────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(
        prog="wp files", description="Locate a file from your work history.")
    ap.add_argument("query", nargs="+", help="Loose description of the file.")
    ap.add_argument("--since", metavar="YYYY-MM-DD")
    ap.add_argument("--limit", type=int, default=5)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv[1:])
    query = " ".join(args.query).strip()
    con = db.connect(cfg=load_config())
    hits = locate(con, query, limit=args.limit, since=args.since,
                  cfg=load_config())
    if args.json:
        print(json.dumps(hits, indent=2, default=str))
    else:
        print(format_hits(hits, query))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
