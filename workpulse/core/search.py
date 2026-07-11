"""
search.py — `wp search`, the v2 retrieval verb.

Hybrid retrieval over atoms:
  - BM25 over FTS5 (always on, zero deps, fast).
  - Vector over sqlite-vec vec0 (optional; requires `pip install fastembed`
    or a configured embedding callable).
  - Fusion via Reciprocal Rank Fusion (RRF).
  - Recency boost: exponential decay, half-life 30 days.
  - Stream boost: small bump when --stream filter matches.

Returns ranked atoms. No synthesis — synthesis lives in `wp think` (step 6).

Public API:
    reindex(con, *, since=None, kinds=None) -> dict[str, int]
    search(con, query, *, limit=10, since=None, stream=None,
           with_vector=False) -> list[dict]
    index_atom(con, *, kind, id, ts, stream, content) -> None
        (called by capture and dual_write later for incremental indexing)

CLI:
    python -m workpulse.core.search "query text"
    python -m workpulse.core.search --reindex
    python -m workpulse.core.search "q" --limit 20 --stream dev --since 2026-05-01
    python -m workpulse.core.search "q" --vector   # requires fastembed (or env override)

Scope calls flagged in PLAN.md §7 step 5:
  - Vector is behind a flag and an optional dep. BM25 ships always-on.
  - No live indexing yet: reindex CLI is the path for now. Incremental
    hookup from dual_write/capture lands in step 5b after we've seen the
    recall numbers on backfilled content.
"""

from __future__ import annotations

import argparse
import json
import math
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import db
from workpulse.common import load_config


# ── tunables ─────────────────────────────────────────────────────────────────

_RRF_K = 60                     # standard RRF constant
_RECENCY_HALF_LIFE_DAYS = 30.0
_STREAM_BOOST = 0.15            # added to the final score when --stream matches
_VEC_DIM = 384                  # bge-small / fastembed default


# ── searchable content extraction ────────────────────────────────────────────
# Maps each atom kind to a SQL query that yields (atom_id, ts, stream, content)
# rows. The content is what FTS5 will tokenize — keep it short and meaningful
# (we're searching for "what was the thread", not for full-text recall on raw
# file paths).

_KINDS = ("session", "capture", "ai_call", "plan_item")


def _content_query(kind: str, since: str | None) -> tuple[str, tuple]:
    where_ts = "AND ts > ?" if since else ""
    params: tuple = (since,) if since else ()

    if kind == "session":
        # Join session_local for raw_title; the public table only has the hash.
        # Untagged sessions still get indexed — they're a first-class state.
        q = f"""
            SELECT s.id AS atom_id, s.started_at AS ts, s.stream AS stream,
                   COALESCE(NULLIF(sl.raw_title, ''), s.app) AS content
            FROM session s
            LEFT JOIN session_local sl ON sl.session_id = s.id
            WHERE 1=1 {where_ts.replace("ts", "s.started_at")}
        """
        params = (since,) if since else ()
        return q, params

    if kind == "capture":
        q = f"""
            SELECT id AS atom_id, ts, NULL AS stream, body AS content
            FROM capture
            WHERE 1=1 {where_ts}
        """
        return q, params

    if kind == "ai_call":
        # task_summary isn't on the atom; prompt_slug is the only thing we
        # stored. Useful enough for "which report ran when" type queries.
        q = f"""
            SELECT id AS atom_id, ts, NULL AS stream,
                   COALESCE(prompt_slug, model) AS content
            FROM ai_call
            WHERE 1=1 {where_ts}
        """
        return q, params

    if kind == "plan_item":
        q = f"""
            SELECT id AS atom_id, plan_date AS ts, stream, name AS content
            FROM plan_item
            WHERE 1=1 {where_ts.replace("ts", "plan_date")}
        """
        params = (since,) if since else ()
        return q, params

    raise ValueError(f"unknown atom kind {kind!r}")


# ── FTS reindex ──────────────────────────────────────────────────────────────

def reindex(con: sqlite3.Connection, *, since: str | None = None,
            kinds: list[str] | None = None) -> dict[str, int]:
    """Walk each atom kind and (re)populate search_fts. Returns per-kind
    inserted counts.

    If ``since`` is None, this is a full reindex — we DELETE FROM search_fts
    first so dupes don't accumulate. With ``since`` set we only append rows
    newer than that timestamp (caller's responsibility to pass the right one).
    """
    counts: dict[str, int] = {}
    if since is None:
        con.execute("DELETE FROM search_fts")
    target_kinds = kinds or list(_KINDS)
    con.execute("BEGIN")
    try:
        for kind in target_kinds:
            q, params = _content_query(kind, since)
            rows = list(con.execute(q, params))
            n = 0
            for r in rows:
                content = (r["content"] or "").strip()
                if not content:
                    continue
                con.execute(
                    """
                    INSERT INTO search_fts(atom_kind, atom_id, ts, stream, content)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (kind, r["atom_id"], r["ts"], r["stream"], content),
                )
                n += 1
            counts[kind] = n
            # Watermark for incremental runs later
            row = con.execute(
                f"SELECT MAX(ts) AS m FROM search_fts WHERE atom_kind = ?",
                (kind,),
            ).fetchone()
            if row and row["m"]:
                con.execute(
                    """
                    INSERT INTO search_index_state(atom_kind, last_ts, updated_at)
                    VALUES (?, ?, ?)
                    ON CONFLICT(atom_kind) DO UPDATE SET
                      last_ts = excluded.last_ts,
                      updated_at = excluded.updated_at
                    """,
                    (kind, row["m"], datetime.now(timezone.utc).isoformat()),
                )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


def index_atom(con: sqlite3.Connection, *, kind: str, id: str, ts: str,
               stream: str | None, content: str) -> None:
    """Incremental indexing hook. Step 5b will call this from dual_write and
    capture. Safe to call directly today for tests."""
    if not content or not content.strip():
        return
    con.execute(
        """
        INSERT INTO search_fts(atom_kind, atom_id, ts, stream, content)
        VALUES (?, ?, ?, ?, ?)
        """,
        (kind, id, ts, stream, content.strip()),
    )


# ── vector path (optional) ───────────────────────────────────────────────────

def _ensure_vec_schema(con: sqlite3.Connection) -> bool:
    """Create the vec0 virtual table + mapping table if sqlite-vec is loaded.
    Returns True on success, False if the extension isn't available."""
    try:
        con.execute(
            f"CREATE VIRTUAL TABLE IF NOT EXISTS search_vec USING vec0("
            f"embedding float[{_VEC_DIM}])"
        )
        con.execute(
            """
            CREATE TABLE IF NOT EXISTS search_vec_map (
              rowid     INTEGER PRIMARY KEY,
              atom_kind TEXT NOT NULL,
              atom_id   TEXT NOT NULL,
              ts        TEXT NOT NULL,
              stream    TEXT,
              UNIQUE(atom_kind, atom_id)
            )
            """
        )
        return True
    except sqlite3.OperationalError:
        return False


_EMBEDDER = None  # lazy


def _get_embedder():
    """Return a callable str|list[str] -> list[list[float]] or None."""
    global _EMBEDDER
    if _EMBEDDER is not None:
        return _EMBEDDER
    try:
        from fastembed import TextEmbedding  # type: ignore
        model = TextEmbedding(model_name="BAAI/bge-small-en-v1.5")
        def _embed(texts):
            if isinstance(texts, str):
                texts = [texts]
            return [list(v) for v in model.embed(list(texts))]
        _EMBEDDER = _embed
        return _embed
    except Exception:
        return None


# ── search (BM25 + optional vector + RRF + recency + stream) ─────────────────

def _recency_weight(ts: str | None, now: datetime) -> float:
    if not ts:
        return 1.0
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return 1.0
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    age_days = max(0.0, (now - t).total_seconds() / 86400.0)
    return math.exp(-math.log(2) * age_days / _RECENCY_HALF_LIFE_DAYS)


def _bm25_search(con: sqlite3.Connection, query: str, *, limit: int,
                 since: str | None, stream: str | None) -> list[dict]:
    """Use FTS5 MATCH + bm25() ordering. Returns raw rows with rank info."""
    # FTS5 MATCH wants a string; we let users pass natural language and rely on
    # the porter tokenizer. Quote nothing — let FTS handle it.
    sql = """
        SELECT atom_kind, atom_id, ts, stream, content,
               bm25(search_fts) AS bm25
        FROM search_fts
        WHERE search_fts MATCH ?
    """
    params: list = [query]
    if since:
        sql += " AND ts >= ?"
        params.append(since)
    if stream:
        sql += " AND stream = ?"
        params.append(stream)
    sql += " ORDER BY bm25 LIMIT ?"
    params.append(limit * 5)  # over-fetch so RRF/boosts have room to re-rank
    try:
        return [dict(r) for r in con.execute(sql, params)]
    except sqlite3.OperationalError:
        # No FTS table yet, or malformed query — return empty rather than raise
        return []


def _vec_search(con: sqlite3.Connection, query: str, *, limit: int) -> list[dict]:
    """sqlite-vec kNN search. Returns raw rows mapped back through search_vec_map.
    Empty list if vec extension or fastembed is unavailable."""
    if not _ensure_vec_schema(con):
        return []
    embedder = _get_embedder()
    if embedder is None:
        return []
    qv = embedder(query)[0]
    qv_blob = json.dumps(qv)  # sqlite-vec accepts JSON arrays
    try:
        rows = con.execute(
            """
            SELECT v.rowid AS rowid, v.distance AS dist,
                   m.atom_kind, m.atom_id, m.ts, m.stream
            FROM search_vec v
            JOIN search_vec_map m ON m.rowid = v.rowid
            WHERE v.embedding MATCH ?
              AND k = ?
            ORDER BY v.distance
            """,
            (qv_blob, limit * 5),
        ).fetchall()
    except sqlite3.OperationalError:
        return []
    return [dict(r) for r in rows]


def search(con: sqlite3.Connection, query: str, *, limit: int = 10,
           since: str | None = None, stream: str | None = None,
           with_vector: bool = False) -> list[dict]:
    """Hybrid retrieval. Returns at most `limit` ranked atom dicts:
        {atom_kind, atom_id, ts, stream, content, score, sources: [...]}
    """
    query = (query or "").strip()
    if not query:
        return []

    bm25_rows = _bm25_search(con, query, limit=limit, since=since, stream=stream)
    vec_rows = _vec_search(con, query, limit=limit) if with_vector else []

    # RRF: score(d) = sum_over_lists 1 / (k + rank(d))
    rrf: dict[tuple[str, str], float] = {}
    sources: dict[tuple[str, str], list[str]] = {}
    payload: dict[tuple[str, str], dict] = {}

    for rank, row in enumerate(bm25_rows):
        key = (row["atom_kind"], row["atom_id"])
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources.setdefault(key, []).append("bm25")
        payload.setdefault(key, row)

    for rank, row in enumerate(vec_rows):
        key = (row["atom_kind"], row["atom_id"])
        rrf[key] = rrf.get(key, 0.0) + 1.0 / (_RRF_K + rank + 1)
        sources.setdefault(key, []).append("vec")
        # Fetch content for vec-only hits (vec_rows lack the content column)
        if key not in payload:
            r = con.execute(
                "SELECT content FROM search_fts WHERE atom_kind=? AND atom_id=?",
                key,
            ).fetchone()
            row["content"] = r["content"] if r else ""
            payload[key] = row

    now = datetime.now(timezone.utc)
    results: list[dict] = []
    for key, base in rrf.items():
        p = payload[key]
        recency = _recency_weight(p.get("ts"), now)
        boost = _STREAM_BOOST if (stream and p.get("stream") == stream) else 0.0
        score = base * recency + boost
        results.append({
            "atom_kind": p["atom_kind"],
            "atom_id":   p["atom_id"],
            "ts":        p.get("ts"),
            "stream":    p.get("stream"),
            "content":   (p.get("content") or "").strip(),
            "score":     score,
            "sources":   sources[key],
        })
    results.sort(key=lambda r: r["score"], reverse=True)
    return results[:limit]


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_reindex() -> int:
    con = db.connect()
    counts = reindex(con, since=None)
    print("reindex complete")
    for k, n in counts.items():
        print(f"  {k:10s} {n:>7d}")
    return 0


def _cli_search(args: argparse.Namespace) -> int:
    con = db.connect(cfg=None, vec=args.vector)
    results = search(con, args.query, limit=args.limit, since=args.since,
                     stream=args.stream, with_vector=args.vector)
    if not results:
        print("no results")
        return 0
    for r in results:
        ts = (r["ts"] or "")[:19]
        stream = r["stream"] or "—"
        sources = ",".join(r["sources"])
        snippet = (r["content"] or "").replace("\n", " ")[:90]
        print(f"{r['score']:.4f}  [{r['atom_kind']:10s}] {ts}  {stream:10s} "
              f"({sources})  {snippet}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp search",
                                     description="Hybrid retrieval over WorkPulse atoms.")
    parser.add_argument("query", nargs="*", help="The query text.")
    parser.add_argument("--reindex", action="store_true",
                        help="Rebuild the FTS index from scratch.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="Only return atoms on/after this date.")
    parser.add_argument("--stream", help="Only return atoms in this stream "
                                          "(also applies the stream boost).")
    parser.add_argument("--vector", action="store_true",
                        help="Enable vector half of hybrid retrieval. "
                             "Requires `pip install fastembed`.")
    args = parser.parse_args(argv[1:])

    if args.reindex:
        return _cli_reindex()

    args.query = " ".join(args.query).strip()
    if not args.query:
        parser.error("missing query (or pass --reindex)")
    return _cli_search(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
