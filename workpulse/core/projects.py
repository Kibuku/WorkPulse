"""
projects.py — resolve text → stream key via config/projects.yaml.

The atomic piece of Fix (i). One pure function, well-tested. Anywhere
the brain has text that might mention a project (capture body, plan
item name, window title, file path), it calls `resolve_stream(text)`
and gets back the stream key (or None) without an LLM call.

The taxonomy is in `config/projects.yaml`. Edit the YAML to change the
patterns; the resolver picks up the change on the next call (cheap
reload).

Public API:
    load_projects(cfg=None, *, path=None) -> list[dict]
    resolve_stream(text, projects=None) -> str | None
    resolve_match(text, projects=None) -> dict | None
        # richer return: {stream, label, matched_keyword, source}

CLI:
    python -m workpulse.core.projects test "today I want to work on Uganda MEMD"
    python -m workpulse.core.projects backfill            # re-route existing captures
    python -m workpulse.core.projects list                # dump the taxonomy
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from workpulse.core import db
from workpulse.common import ROOT, load_config


_DEFAULT_PATH = ROOT / "config" / "projects.yaml"


# ── load + cache ────────────────────────────────────────────────────────────

_CACHE: dict = {"mtime": 0.0, "projects": []}


def load_projects(cfg: dict | None = None,
                  *, path: Path | None = None) -> list[dict]:
    """Read config/projects.yaml; merge the `fallbacks:` list at the end.
    Cached by file mtime so consecutive calls are cheap."""
    p = path or _DEFAULT_PATH
    try:
        mtime = p.stat().st_mtime
    except FileNotFoundError:
        return []
    if mtime == _CACHE["mtime"] and _CACHE["projects"]:
        return _CACHE["projects"]
    try:
        data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError:
        return []
    out: list[dict] = []
    for entry in (data.get("projects") or []):
        if isinstance(entry, dict) and entry.get("stream"):
            out.append({
                "stream":          entry["stream"],
                "label":           entry.get("label") or entry["stream"],
                "keywords":        [str(k).lower() for k in entry.get("keywords") or []],
                "folder_patterns": [str(k).lower() for k in entry.get("folder_patterns") or []],
                "aliases":         [str(a).lower() for a in entry.get("aliases") or []],
                "source":          "project",
            })
    for entry in (data.get("fallbacks") or []):
        if isinstance(entry, dict) and entry.get("stream"):
            out.append({
                "stream":          entry["stream"],
                "label":           entry.get("label") or entry["stream"],
                "keywords":        [str(k).lower() for k in entry.get("keywords") or []],
                "folder_patterns": [],
                "aliases":         [],
                "source":          "fallback",
            })
    # Stash domain lists for browser_tracker use (separate API, not part of
    # the normal project-match flow).
    _CACHE["private_domains"] = [str(d).lower() for d in (data.get("private_domains") or [])]
    _CACHE["deny_domains"]    = [str(d).lower() for d in (data.get("deny_domains") or [])]
    _CACHE["mtime"] = mtime
    _CACHE["projects"] = out
    return out


def private_domains(*, path: Path | None = None) -> list[str]:
    """Substrings that, when found in a URL/domain, mark the visit as
    routing to the private 'personal' stream."""
    load_projects(path=path)        # make sure cache is populated
    return list(_CACHE.get("private_domains") or [])


def deny_domains(*, path: Path | None = None) -> list[str]:
    """Substrings that, when found in a URL/domain, drop the visit
    entirely — no atom written, no edge created."""
    load_projects(path=path)
    return list(_CACHE.get("deny_domains") or [])


def classify_url(url: str, *, path: Path | None = None) -> dict:
    """Classify a URL into one of three buckets:
        {'tier': 'deny',    'reason': matched_term}      → drop
        {'tier': 'private', 'reason': matched_term}      → write, route to 'personal'
        {'tier': 'project', 'stream': key, 'matched_keyword': ...}
                                                          → write, route to project
        {'tier': 'unknown'}                              → write, stream=None
    """
    if not url:
        return {"tier": "unknown"}
    low = url.lower()
    # Deny wins everything
    for d in deny_domains(path=path):
        if d and d in low:
            return {"tier": "deny", "reason": d}
    # Private next
    for d in private_domains(path=path):
        if d and d in low:
            return {"tier": "private", "reason": d}
    # Then project match — use path mode so folder patterns are checked
    m = resolve_match(url, kind="path")
    if m:
        return {"tier": "project", **m}
    return {"tier": "unknown"}


# ── resolver ────────────────────────────────────────────────────────────────

def resolve_match(text: str, projects: list[dict] | None = None,
                  *, kind: str = "text") -> dict | None:
    """Find the first project whose keyword / alias / folder_pattern matches
    a substring of `text` (case-insensitive). Returns a dict with the matched
    info, or None.

    kind="text"  → check keywords + aliases only.
    kind="path"  → check folder_patterns + keywords (paths often contain
                   project names verbatim).
    """
    if not text:
        return None
    if projects is None:
        projects = load_projects()
    low = text.lower()
    for p in projects:
        patterns: list[str] = []
        if kind == "text":
            patterns = p["keywords"] + p["aliases"]
        elif kind == "path":
            patterns = p["folder_patterns"] + p["keywords"]
        for kw in patterns:
            if kw and kw in low:
                return {
                    "stream":          p["stream"],
                    "label":           p["label"],
                    "matched_keyword": kw,
                    "source":          p["source"],
                }
    return None


def resolve_stream(text: str, projects: list[dict] | None = None,
                   *, kind: str = "text") -> str | None:
    """Convenience: just the stream key, or None."""
    m = resolve_match(text, projects, kind=kind)
    return m["stream"] if m else None


def resolve_all(text: str, projects: list[dict] | None = None,
                *, kind: str = "text") -> list[dict]:
    """Return EVERY matching project (deduped by stream key, in match order).

    Enables the "Model B" list-capture pattern: a user writes their
    morning intentions in one sentence ("Today: Uganda MEMD, Mercy Corps,
    WorkPulse v2") and the brain extracts every project mentioned. Each
    becomes a candidate stream for the day; cluster assignment picks
    among them based on signal.

    Each returned dict has the same shape as resolve_match: stream,
    label, matched_keyword, source.
    """
    if not text:
        return []
    if projects is None:
        projects = load_projects()
    low = text.lower()
    seen_streams: set[str] = set()
    out: list[dict] = []
    for p in projects:
        if p["stream"] in seen_streams:
            continue
        patterns: list[str] = []
        if kind == "text":
            patterns = p["keywords"] + p["aliases"]
        elif kind == "path":
            patterns = p["folder_patterns"] + p["keywords"]
        for kw in patterns:
            if kw and kw in low:
                out.append({
                    "stream":          p["stream"],
                    "label":           p["label"],
                    "matched_keyword": kw,
                    "source":          p["source"],
                })
                seen_streams.add(p["stream"])
                break  # one match per project is enough
    return out


# ── backfill ────────────────────────────────────────────────────────────────

def backfill_captures(con, *, dry_run: bool = False) -> dict:
    """Walk every human capture, run the resolver on its body, and create
    a typed edge capture --about_stream--> stream if a project matches.
    Idempotent (the edge table has a UNIQUE index on the 5-tuple).

    Also propagates: if the capture is pinned to a session AND the session
    has stream=NULL, set session.stream to the resolved project. Cluster
    refresh later will pick it up. Conflict (session has a different stream)
    is logged but NOT overwritten — the existing tag wins.
    """
    counts = {
        "captures_scanned":   0,
        "captures_matched":   0,
        "edges_created":      0,
        "sessions_tagged":    0,
        "sessions_conflict":  0,
    }
    projects = load_projects()
    rows = con.execute(
        "SELECT id, body, pinned_kind, pinned_id "
        "FROM capture WHERE author = 'human'"
    ).fetchall()
    if not dry_run:
        con.execute("BEGIN")
    try:
        for r in rows:
            counts["captures_scanned"] += 1
            m = resolve_match(r["body"] or "", projects)
            if m is None:
                continue
            counts["captures_matched"] += 1
            if dry_run:
                continue
            # 1) edge: capture --about_stream--> stream
            from workpulse.core.atoms import add_edge
            con.execute(
                "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
                (m["stream"], m["label"]),
            )
            before = con.execute(
                "SELECT COUNT(*) AS n FROM edge WHERE src_kind='capture' "
                "AND src_id=? AND rel='about_stream'",
                (r["id"],),
            ).fetchone()["n"]
            add_edge(con, src_kind="capture", src_id=r["id"],
                     rel="about_stream", dst_kind="stream",
                     dst_id=m["stream"])
            after = con.execute(
                "SELECT COUNT(*) AS n FROM edge WHERE src_kind='capture' "
                "AND src_id=? AND rel='about_stream'",
                (r["id"],),
            ).fetchone()["n"]
            if after > before:
                counts["edges_created"] += 1
            # 2) propagate to pinned session if it has no stream yet
            if r["pinned_kind"] == "session" and r["pinned_id"]:
                cur_stream = con.execute(
                    "SELECT stream FROM session WHERE id = ?",
                    (r["pinned_id"],),
                ).fetchone()
                if cur_stream is None:
                    continue
                cs = cur_stream["stream"]
                if cs is None:
                    con.execute(
                        "UPDATE session SET stream = ? WHERE id = ?",
                        (m["stream"], r["pinned_id"]),
                    )
                    # Also add the session --in_stream--> edge so the graph
                    # stays consistent with what dual_write writes for new
                    # sessions.
                    add_edge(con, src_kind="session", src_id=r["pinned_id"],
                             rel="in_stream", dst_kind="stream",
                             dst_id=m["stream"])
                    counts["sessions_tagged"] += 1
                elif cs != m["stream"]:
                    counts["sessions_conflict"] += 1
        if not dry_run:
            con.execute("COMMIT")
    except Exception:
        if not dry_run:
            con.execute("ROLLBACK")
        raise
    counts["dry_run"] = dry_run
    return counts


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_test(text: str) -> int:
    projects = load_projects()
    print(f"resolver loaded {len(projects)} entries")
    m = resolve_match(text, projects)
    if m:
        print(f"  matched: stream={m['stream']!r} label={m['label']!r}")
        print(f"  via keyword: {m['matched_keyword']!r} (source={m['source']})")
        return 0
    print("  no match")
    return 1


def _cli_list() -> int:
    for p in load_projects():
        kws = ", ".join(p["keywords"][:4]) + ("…" if len(p["keywords"]) > 4 else "")
        print(f"  {p['stream']:14s} {p['label']:28s} [{p['source']:8s}]  keywords: {kws}")
    return 0


def _cli_backfill(args: argparse.Namespace) -> int:
    con = db.connect(load_config())
    counts = backfill_captures(con, dry_run=args.dry_run)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}backfill_captures:")
    for k, v in counts.items():
        if k == "dry_run":
            continue
        print(f"  {k:20s} {v}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp projects",
                                     description="Project resolver.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    t = sub.add_parser("test")
    t.add_argument("text", nargs="+", help="text to test the resolver on")
    sub.add_parser("list")
    b = sub.add_parser("backfill")
    b.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv[1:])
    if args.cmd == "test":
        return _cli_test(" ".join(args.text))
    if args.cmd == "list":
        return _cli_list()
    if args.cmd == "backfill":
        return _cli_backfill(args)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
