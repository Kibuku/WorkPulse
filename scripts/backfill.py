"""
backfill.py — one-shot migration: v1 JSONL/JSON/Markdown → v2 SQLite atoms.

Per PLAN.md §7 step 2. Reads every v1 source on disk and writes v2 atoms,
preserving timestamps and typed edges. Idempotent — re-runs produce the
same DB state (uses content-hash IDs + INSERT OR IGNORE).

Sources read:
    logs/activity_YYYY-MM-DD.jsonl   → session + session_local + edges
    logs/file_events_YYYY-MM-DD.jsonl → file_event + file_event_local
    logs/ai_sessions.jsonl           → ai_call
    plans/YYYY-MM-DD.md              → plan_item
    config/config.yaml streams       → stream reference rows

Sources NOT read (by design):
    logs/jobs.jsonl     — Jobs are derived in v2 (a view over sessions);
                          step 7 materializes them via skills/cluster.md.
                          The underlying time data is already captured in
                          activity_*.jsonl.
    logs/learned_tags.json — Reference cache, not source-of-truth. The v2
                          equivalent is captured by `classify.md` skill +
                          deterministic fallback in step 4/5; v1 cache
                          stays on disk for the transition release.

v1 JSONL files stay on disk read-only after this runs (PLAN.md §7 says
"keep JSONL on disk read-only as a fallback for one release"). We do not
delete anything.

CLI:
    python -m scripts.backfill              # full run
    python -m scripts.backfill --dry-run    # report only, no writes
    python -m scripts.backfill --since YYYY-MM-DD
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sqlite3
import sys
from datetime import date, datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import atoms, db, plans
from scripts.common import ROOT, load_config, resolve


# ── id derivation (content-hash, so re-runs are idempotent) ─────────────────

def _stable_id(*parts: str) -> str:
    """Deterministic 20-char id from the given parts. Stable across runs so
    INSERT OR IGNORE dedupes naturally."""
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    # 20 chars, uppercase, matches the shape of atoms.new_id()
    return h[:20].upper()


# ── helpers ─────────────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_app(con: sqlite3.Connection, name: str, ts: str, *, counts: dict) -> None:
    # INSERT OR IGNORE so the counter only reflects NEW apps. last_seen is
    # widened by a separate UPDATE that doesn't pollute the count.
    cur = con.execute(
        "INSERT OR IGNORE INTO app(name, category, first_seen, last_seen) VALUES (?, NULL, ?, ?)",
        (name, ts, ts),
    )
    if cur.rowcount:
        counts["app"] += 1
    con.execute(
        "UPDATE app SET last_seen = ? WHERE name = ? AND last_seen < ?",
        (ts, name, ts),
    )


def _ensure_stream(con: sqlite3.Connection, key: str | None, label: str | None = None,
                   parent: str | None = None, *, counts: dict) -> None:
    if not key:
        return
    cur = con.execute(
        "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, ?)",
        (key, label or key, parent),
    )
    if cur.rowcount:
        counts["stream"] += 1


def _add_edge(con: sqlite3.Connection, *, src_kind, src_id, rel, dst_kind, dst_id,
              counts: dict) -> None:
    cur = con.execute(
        """
        INSERT OR IGNORE INTO edge(src_kind, src_id, rel, dst_kind, dst_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (src_kind, src_id, rel, dst_kind, dst_id, _now_iso()),
    )
    if cur.rowcount:
        counts["edge"] += 1


# ── source readers ──────────────────────────────────────────────────────────

def _iter_jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _date_from_filename(p: Path, prefix: str) -> str | None:
    """activity_2026-06-01.jsonl → '2026-06-01'."""
    stem = p.stem
    if not stem.startswith(prefix):
        return None
    return stem[len(prefix):]


# ── source: activity_*.jsonl → session ──────────────────────────────────────

def backfill_sessions(con: sqlite3.Connection, logs_dir: Path, *,
                      since: str | None, counts: dict, dry_run: bool) -> None:
    files = sorted(logs_dir.glob("activity_*.jsonl"))
    for f in files:
        d = _date_from_filename(f, "activity_")
        if since and d and d < since:
            continue
        for row in _iter_jsonl(f):
            start = row.get("start")
            end = row.get("end")
            app = row.get("app") or "unknown"
            title = row.get("title") or ""
            stream = row.get("stream")
            exe_path = row.get("exe_path")
            if not start:
                continue
            sid = _stable_id("session", start, app, title)

            if dry_run:
                counts["session"] += 1
                continue

            _ensure_app(con, app, start, counts=counts)
            _ensure_stream(con, stream, counts=counts)

            norm = atoms._normalize_title(title)
            title_hash = atoms.sha256_short(norm or "__empty__")

            cur = con.execute(
                """
                INSERT OR IGNORE INTO session
                  (id, started_at, ended_at, app, title_hash, stream, cluster_id)
                VALUES (?, ?, ?, ?, ?, ?, NULL)
                """,
                (sid, start, end, app, title_hash, stream),
            )
            if cur.rowcount == 0:
                continue  # already imported
            counts["session"] += 1

            con.execute(
                """
                INSERT OR IGNORE INTO session_local
                  (session_id, raw_title, raw_path, raw_files)
                VALUES (?, ?, ?, ?)
                """,
                (sid, title, exe_path, json.dumps([])),
            )

            # Typed edges, zero LLM
            _add_edge(con, src_kind="session", src_id=sid, rel="in_app",
                      dst_kind="app", dst_id=app, counts=counts)
            if stream:
                _add_edge(con, src_kind="session", src_id=sid, rel="in_stream",
                          dst_kind="stream", dst_id=stream, counts=counts)


# ── source: file_events_*.jsonl → file_event ────────────────────────────────

def backfill_file_events(con: sqlite3.Connection, logs_dir: Path, *,
                         since: str | None, counts: dict, dry_run: bool) -> None:
    files = sorted(logs_dir.glob("file_events_*.jsonl"))
    for f in files:
        d = _date_from_filename(f, "file_events_")
        if since and d and d < since:
            continue
        for row in _iter_jsonl(f):
            ts = row.get("timestamp")
            kind = row.get("event_type")
            path = row.get("path")
            if not ts or not kind or not path:
                continue
            if kind not in ("created", "modified", "deleted", "moved"):
                # v1 used a wider vocabulary in places; normalize.
                if kind == "created_or_modified":
                    kind = "modified"
                else:
                    continue
            fid = _stable_id("file_event", ts, kind, path)
            if dry_run:
                counts["file_event"] += 1
                continue
            path_hash = atoms.sha256_short(path)
            cur = con.execute(
                "INSERT OR IGNORE INTO file_event(id, ts, path_hash, kind) VALUES (?, ?, ?, ?)",
                (fid, ts, path_hash, kind),
            )
            if cur.rowcount == 0:
                continue
            counts["file_event"] += 1
            con.execute(
                "INSERT OR IGNORE INTO file_event_local(file_event_id, raw_path) VALUES (?, ?)",
                (fid, path),
            )


# ── source: ai_sessions.jsonl → ai_call ─────────────────────────────────────

def backfill_ai_calls(con: sqlite3.Connection, logs_dir: Path, *,
                      since: str | None, counts: dict, dry_run: bool) -> None:
    f = logs_dir / "ai_sessions.jsonl"
    if not f.exists():
        return
    for row in _iter_jsonl(f):
        ts = row.get("timestamp")
        if not ts:
            continue
        if since and ts[:10] < since:
            continue
        provider = "anthropic"  # v1 only used Anthropic
        model = row.get("model") or "unknown"
        in_tok = int(row.get("input_tokens") or 0)
        out_tok = int(row.get("output_tokens") or 0)
        cost = float(row.get("estimated_cost_usd") or 0.0)
        slug = row.get("tool_used") or row.get("task_summary") or None
        sid = row.get("session_id") or _stable_id("ai_call", ts, slug or "")
        # use the v1 session_id if it looks like a uuid; otherwise stable hash
        cid = _stable_id("ai_call", sid, ts, model)
        if dry_run:
            counts["ai_call"] += 1
            continue
        cur = con.execute(
            """
            INSERT OR IGNORE INTO ai_call
              (id, ts, provider, model, in_tokens, out_tokens, cost_usd, prompt_slug)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, ts, provider, model, in_tok, out_tok, cost, slug),
        )
        if cur.rowcount:
            counts["ai_call"] += 1


# ── source: plans/YYYY-MM-DD.md → plan_item ─────────────────────────────────

def backfill_plans(con: sqlite3.Connection, *, since: str | None,
                   counts: dict, dry_run: bool) -> None:
    plans_dir = ROOT / "plans"
    if not plans_dir.exists():
        return
    for f in sorted(plans_dir.glob("*.md")):
        try:
            d = date.fromisoformat(f.stem)
        except ValueError:
            continue
        if since and f.stem < since:
            continue
        parsed = plans.parse(f.read_text(encoding="utf-8"))
        for it in parsed.get("items", []):
            iid = _stable_id("plan_item", f.stem, it.get("name") or "",
                             it.get("section") or "new")
            if dry_run:
                counts["plan_item"] += 1
                continue
            _ensure_stream(con, it.get("stream"), counts=counts)
            cur = con.execute(
                """
                INSERT OR IGNORE INTO plan_item
                  (id, plan_date, name, planned_minutes, done, job_id, stream, section, raw)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (iid, f.stem, it.get("name") or "",
                 it.get("planned_minutes"),
                 1 if it.get("done") else 0,
                 it.get("job_id"), it.get("stream"),
                 it.get("section") or "new",
                 it.get("raw")),
            )
            if cur.rowcount:
                counts["plan_item"] += 1


# ── source: config.yaml streams → stream ────────────────────────────────────

def backfill_streams(con: sqlite3.Connection, *, counts: dict, dry_run: bool) -> None:
    cfg = load_config()
    streams = cfg.get("streams") or {}
    for key, val in streams.items():
        if isinstance(val, str):
            label, parent = val, None
        elif isinstance(val, dict):
            label = val.get("label") or key
            parent = val.get("parent")
        else:
            continue
        if dry_run:
            counts["stream"] += 1
            continue
        _ensure_stream(con, key, label=label, parent=parent, counts=counts)


# ── driver ──────────────────────────────────────────────────────────────────

def run(*, since: str | None, dry_run: bool) -> dict:
    cfg = load_config()
    logs_dir = resolve(cfg.get("paths", {}).get("logs", "logs"))
    con = db.connect(cfg)

    counts = {k: 0 for k in
              ("session", "file_event", "ai_call", "capture",
               "plan_item", "app", "stream", "edge")}

    # Each phase runs in one transaction so 674k file_events don't autocommit
    # 674k times. Streams first so FKs on later inserts have somewhere to point.
    def _tx(fn, *args, **kwargs):
        if dry_run:
            return fn(con, *args, counts=counts, dry_run=dry_run, **kwargs)
        con.execute("BEGIN")
        try:
            fn(con, *args, counts=counts, dry_run=dry_run, **kwargs)
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise

    _tx(backfill_streams)
    _tx(backfill_sessions, logs_dir, since=since)
    _tx(backfill_file_events, logs_dir, since=since)
    _tx(backfill_ai_calls, logs_dir, since=since)
    _tx(backfill_plans, since=since)
    return counts


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp-backfill")
    parser.add_argument("--dry-run", action="store_true",
                        help="report counts but write nothing")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only backfill records on/after this date")
    args = parser.parse_args(argv[1:])

    counts = run(since=args.since, dry_run=args.dry_run)
    print(("DRY-RUN " if args.dry_run else "") + "backfill summary")
    for k in ("session", "file_event", "ai_call", "plan_item", "edge", "app", "stream"):
        print(f"  {k:12s} {counts[k]:>6d}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
