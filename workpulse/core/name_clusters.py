"""
name_clusters.py — `wp name-clusters`, the step-7b naming pass.

For each cluster in job_view that doesn't have a 'user' or 'llm' name yet,
propose one. The judgment lives in skills/name-cluster.md (markdown is
code). The harness does I/O + LLM dispatch + parsing + write.

Two precedence rules (encoded both here and in the cluster_name table):
    user > llm > fallback
A user-set name is never overwritten by an automated pass. An llm name
is only overwritten by another llm pass when --force is set.

Public API:
    name_one(con, cluster_id, *, force=False, force_fallback=False,
             cfg=None) -> dict
    name_all(con, *, since=None, force=False, force_fallback=False,
             cfg=None) -> list[dict]
    set_user_name(con, cluster_id, name, one_liner=None) -> None

CLI:
    python -m workpulse.core.name_clusters                  # name everything pending
    python -m workpulse.core.name_clusters --since 2026-06-01
    python -m workpulse.core.name_clusters --force          # re-name llm entries
    python -m workpulse.core.name_clusters --no-llm         # fallback-only path
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import atoms, db, think
from workpulse.common import ROOT, load_config, PKG


_SKILL_PATH = PKG / "skills" / "name-cluster.md"
_DEFAULT_MODEL = "claude-haiku-4-5"

# Stopwords kept small + focused: English chrome + frequent app suffixes.
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "you", "are", "was", "this", "that",
    "from", "have", "has", "had", "but", "not", "any", "all", "can",
    "into", "out", "off", "via", "use", "using", "used",
    "your", "our", "their", "his", "her", "its",
    "untitled", "document", "new", "draft", "open", "save",
    # browser / app chrome that leaks into titles
    "word", "excel", "powerpoint", "outlook", "safari", "chrome", "firefox",
    "edge", "brave", "opera", "vivaldi", "claude", "code", "vscode",
    "microsoft", "google", "page", "tab", "tabs", "more", "pages",
})

_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")


# ── shared helpers ──────────────────────────────────────────────────────────

def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_skill() -> str:
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ("Reply with two lines:\n"
                "NAME: <short title-case name>\n"
                "ONELINER: <one sentence describing the cluster>\n")


def _cluster_meta(con: sqlite3.Connection, cluster_id: str) -> dict | None:
    return dict(con.execute(
        "SELECT * FROM job_view WHERE cluster_id = ?", (cluster_id,)
    ).fetchone() or {}) or None


def _cluster_titles(con: sqlite3.Connection, cluster_id: str,
                    *, limit: int = 40) -> list[tuple[str, int]]:
    """Return [(raw_title, count)] for sessions in this cluster, ordered by
    frequency. We read from session_local because the public table only has
    the hash."""
    rows = con.execute(
        """
        SELECT COALESCE(sl.raw_title, s.app) AS title, COUNT(*) AS n
          FROM session s
          LEFT JOIN session_local sl ON sl.session_id = s.id
         WHERE s.cluster_id = ?
         GROUP BY title
         ORDER BY n DESC
         LIMIT ?
        """,
        (cluster_id, limit),
    ).fetchall()
    return [(r["title"], r["n"]) for r in rows]


def _cluster_plan_items(con: sqlite3.Connection, started_at: str,
                        ended_at: str, stream: str | None,
                        *, limit: int = 6) -> list[dict]:
    """Plan items touching the cluster's date range. Loose join — plan items
    only carry a plan_date, not a precise time."""
    start_d = started_at[:10]
    end_d = ended_at[:10]
    if stream is None:
        rows = con.execute(
            "SELECT plan_date, name, stream FROM plan_item "
            "WHERE plan_date >= ? AND plan_date <= ? "
            "ORDER BY plan_date LIMIT ?",
            (start_d, end_d, limit),
        )
    else:
        rows = con.execute(
            "SELECT plan_date, name, stream FROM plan_item "
            "WHERE plan_date >= ? AND plan_date <= ? AND stream = ? "
            "ORDER BY plan_date LIMIT ?",
            (start_d, end_d, stream, limit),
        )
    return [dict(r) for r in rows]


# ── fallback (zero-key) ─────────────────────────────────────────────────────

def _tokens(text: str) -> list[str]:
    if not text:
        return []
    out: list[str] = []
    for m in _TOKEN_RE.finditer(text):
        tok = m.group(0).lower()
        if tok in _STOPWORDS:
            continue
        out.append(tok)
    return out


def _fallback_name(meta: dict, titles: list[tuple[str, int]]) -> dict:
    bag: Counter = Counter()
    for title, n in titles:
        for t in _tokens(title):
            bag[t] += n
    apps = json.loads(meta.get("apps_seen") or "[]")
    top = [t for t, _ in bag.most_common(3)]
    if top:
        # Title-case the tokens, drop short cruft like 'pm', 'us'
        name_words = [w.capitalize() for w in top if len(w) >= 3][:3]
        name = " ".join(name_words) if name_words else "Untagged Cluster"
    else:
        name = "Untagged Cluster"
        if apps:
            name = f"{apps[0]} Activity"
    # Oneliner: dominant app + dominant content fragment
    dominant_app = apps[0] if apps else "?"
    if titles:
        # Strip trailing app suffixes from the most common title
        sample = titles[0][0].split(" — ")[0][:60]
        one = f"Mostly {dominant_app}; top window: \"{sample}\""
    else:
        one = f"Cluster of {meta.get('session_count', 0)} sessions in {dominant_app}"
    return {"name": name, "one_liner": one, "confidence": 0.3}


# ── LLM path ────────────────────────────────────────────────────────────────

def _build_prompt(meta: dict, titles: list[tuple[str, int]],
                  plan_items: list[dict], skill: str) -> str:
    apps = json.loads(meta.get("apps_seen") or "[]")
    hours = (meta.get("total_seconds") or 0) / 3600.0
    title_block = "\n".join(f"  ({n}×) {t}" for t, n in titles[:30])
    plan_block = "\n".join(
        f"  [{p['plan_date']}] {p['name']} (stream={p.get('stream') or '—'})"
        for p in plan_items
    ) or "  (none)"
    return (
        f"{skill}\n\n"
        f"---\n\n"
        f"STREAM: {meta.get('stream') or '<untagged>'}\n"
        f"WHEN: {meta.get('started_at')} → {meta.get('ended_at')}\n"
        f"TOTAL_HOURS: {hours:.1f}\n"
        f"APPS: {', '.join(apps) if apps else '(none recorded)'}\n"
        f"WINDOW_TITLES:\n{title_block}\n\n"
        f"PLAN_ITEMS (near this date range, same stream if any):\n{plan_block}\n"
    )


_NAME_LINE_RE     = re.compile(r"^\s*NAME:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)
_ONELINER_LINE_RE = re.compile(r"^\s*ONELINER:\s*(.+?)\s*$", re.IGNORECASE | re.MULTILINE)


def _parse_response(text: str) -> tuple[str | None, str | None]:
    n = _NAME_LINE_RE.search(text or "")
    o = _ONELINER_LINE_RE.search(text or "")
    return (n.group(1).strip() if n else None,
            o.group(1).strip() if o else None)


# ── write ───────────────────────────────────────────────────────────────────

def _upsert_name(con: sqlite3.Connection, *, cluster_id: str, name: str,
                 one_liner: str | None, confidence: float, source: str,
                 model: str | None, skill_run_id: str | None) -> None:
    con.execute(
        """
        INSERT INTO cluster_name(cluster_id, name, one_liner, confidence,
                                  source, named_at, model, skill_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cluster_id) DO UPDATE SET
          name = excluded.name,
          one_liner = excluded.one_liner,
          confidence = excluded.confidence,
          source = excluded.source,
          named_at = excluded.named_at,
          model = excluded.model,
          skill_run_id = excluded.skill_run_id
        """,
        (cluster_id, name, one_liner, confidence, source, _now_iso(),
         model, skill_run_id),
    )


# ── public API ──────────────────────────────────────────────────────────────

def name_one(con: sqlite3.Connection, cluster_id: str, *,
             force: bool = False, force_fallback: bool = False,
             cfg: dict | None = None) -> dict:
    """Name one cluster. Returns a result dict with keys:
       cluster_id, name, one_liner, source, model, fallback, skipped, skill_run.
    """
    meta = _cluster_meta(con, cluster_id)
    if meta is None:
        return {"cluster_id": cluster_id, "skipped": True, "reason": "no such cluster"}

    existing = con.execute(
        "SELECT source FROM cluster_name WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    if existing:
        if existing["source"] == "user":
            return {"cluster_id": cluster_id, "skipped": True,
                    "reason": "user name present"}
        if existing["source"] == "llm" and not force:
            return {"cluster_id": cluster_id, "skipped": True,
                    "reason": "llm name present (use --force to rewrite)"}

    titles = _cluster_titles(con, cluster_id)
    plan_items = _cluster_plan_items(con,
                                     meta.get("started_at") or "",
                                     meta.get("ended_at") or "",
                                     meta.get("stream"))

    skill = _load_skill()
    used_model: str | None = None
    in_tok = out_tok = 0
    duration_s = 0.0
    status = "fallback"
    fallback = True
    name = one_liner = None
    confidence = 0.0

    if not force_fallback:
        model = ((cfg or {}).get("llm", {}) or {}).get("model") or _DEFAULT_MODEL
        prompt = _build_prompt(meta, titles, plan_items, skill)
        result = think._call_anthropic(prompt, model=model, cfg=cfg)
        if result is not None:
            raw, in_tok, out_tok, duration_s = result
            n, o = _parse_response(raw)
            if n:
                name, one_liner = n, o
                used_model = model
                status = "ok"
                fallback = False
                confidence = 0.7

    if fallback:
        fb = _fallback_name(meta, titles)
        name = fb["name"]
        one_liner = fb["one_liner"]
        confidence = fb["confidence"]

    # Record the skill run regardless of path
    sr_id = atoms.new_id()
    ts = _now_iso()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd, input, output, status)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sr_id, ts, "name-cluster", used_model, in_tok, out_tok,
         think._cost(in_tok, out_tok, cfg),
         cluster_id[:200],
         (name or "") + "\n" + (one_liner or ""),
         status),
    )
    if used_model:
        atoms.write_ai_call(con, provider="anthropic", model=used_model,
                            in_tokens=in_tok, out_tokens=out_tok,
                            cost_usd=think._cost(in_tok, out_tok, cfg),
                            prompt_slug="skill:name-cluster", ts=ts)

    _upsert_name(con, cluster_id=cluster_id, name=name or "Untagged Cluster",
                 one_liner=one_liner, confidence=confidence,
                 source=("llm" if not fallback else "fallback"),
                 model=used_model, skill_run_id=sr_id)

    return {
        "cluster_id": cluster_id,
        "name":       name,
        "one_liner":  one_liner,
        "source":     "llm" if not fallback else "fallback",
        "model":      used_model,
        "fallback":   fallback,
        "skipped":    False,
        "skill_run":  sr_id,
    }


def name_all(con: sqlite3.Connection, *, since: str | None = None,
             force: bool = False, force_fallback: bool = False,
             cfg: dict | None = None) -> list[dict]:
    sql = "SELECT cluster_id FROM job_view"
    params: tuple = ()
    if since:
        sql += " WHERE ended_at >= ?"
        params = (since,)
    sql += " ORDER BY total_seconds DESC"
    rows = con.execute(sql, params).fetchall()
    out: list[dict] = []
    for r in rows:
        out.append(name_one(con, r["cluster_id"], force=force,
                            force_fallback=force_fallback, cfg=cfg))
    return out


def set_user_name(con: sqlite3.Connection, cluster_id: str, name: str,
                  one_liner: str | None = None) -> None:
    """The user has the final word. This overrides any automated name."""
    _upsert_name(con, cluster_id=cluster_id, name=name, one_liner=one_liner,
                 confidence=1.0, source="user", model=None, skill_run_id=None)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    results = name_all(con, since=args.since, force=args.force,
                       force_fallback=args.no_llm, cfg=cfg)
    named = sum(1 for r in results if not r.get("skipped"))
    skipped = sum(1 for r in results if r.get("skipped"))
    llm = sum(1 for r in results if r.get("source") == "llm")
    fb = sum(1 for r in results if r.get("source") == "fallback")
    print(f"named {named}  (llm={llm}, fallback={fb}, skipped={skipped})")
    for r in results:
        if r.get("skipped"):
            continue
        tag = "🤖" if r["source"] == "llm" else "··"
        print(f"  {tag} {r['cluster_id']}  {r.get('name')}")
        if r.get("one_liner"):
            print(f"        {r['one_liner']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp name-clusters",
                                     description="Name clusters in job_view.")
    parser.add_argument("--since", metavar="YYYY-MM-DD",
                        help="only name clusters that ended on/after this date")
    parser.add_argument("--force", action="store_true",
                        help="re-name LLM entries (user names are still safe)")
    parser.add_argument("--no-llm", action="store_true",
                        help="force the zero-key fallback path")
    args = parser.parse_args(argv[1:])
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
