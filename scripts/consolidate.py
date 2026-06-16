"""
consolidate.py — the v2 dream cycle. PLAN.md §7 step 8.

Runs once a night (or on demand). Computes findings deterministically over
the last week of atoms, hands the findings to skills/consolidate.md, writes
the result to consolidation/YYYY-MM-DD.md.

Latent/deterministic split (principle #3):
  - Deterministic (this module): dedup detection by Jaccard, plan-vs-actual
    arithmetic, learned-tag staleness, untagged-bucket grouping. Pure
    SQL + Python.
  - Latent (skills/consolidate.md): the prose narrative — Headline, Worth
    your attention, Gap. The LLM sees a compact findings packet, never the
    raw DB.

Zero-key fallback: writes a templated version from the findings, respecting
the same section headings. Less prose, same triage.

Public API:
    load_params() -> dict
    findings(con, *, as_of=None, cfg=None) -> dict
    consolidate(con, *, as_of=None, force_fallback=False, cfg=None) -> dict
        -> {date, findings, raw, path, fallback, model, skill_run}

CLI:
    python -m scripts.consolidate                  # run for today
    python -m scripts.consolidate --date 2026-06-09
    python -m scripts.consolidate --no-llm         # force fallback
    python -m scripts.consolidate --dry-run        # findings only, no file
    python -m scripts.consolidate --json           # findings + result as JSON
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import atoms, capture, db, think
from scripts.common import ROOT, ensure_dir, load_config, resolve


_SKILL_PATH = ROOT / "skills" / "consolidate.md"

_DEFAULTS = {
    "dedup_days_back":        7,
    "plan_vs_actual_days":    7,
    "untagged_days_back":     7,
    "stale_tag_min_age_days": 60,
    "stale_tag_max_hits":     0,
    "untagged_top_buckets":   5,
    "dedup_jaccard_floor":    0.40,
    "plan_overrun_ratio":     2.0,
    "plan_underrun_ratio":    0.25,
}

_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)
_TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
_STOPWORDS = frozenset({
    "the", "and", "for", "with", "you", "are", "this", "that", "from",
    "your", "our", "untitled", "document", "new", "draft", "open", "save",
    "word", "excel", "powerpoint", "outlook", "safari", "chrome", "firefox",
    "edge", "brave", "claude", "code", "vscode", "microsoft", "google",
    "page", "tab", "tabs", "more", "pages",
})


# ── skill / params ──────────────────────────────────────────────────────────

def load_params(skill_path: Path | None = None) -> dict:
    path = skill_path or _SKILL_PATH
    out = dict(_DEFAULTS)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return out
    try:
        loaded = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return out
    if isinstance(loaded, dict):
        for k, v in loaded.items():
            if k in _DEFAULTS:
                out[k] = v
    return out


def _load_skill() -> str:
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        return ""


# ── tokenization ────────────────────────────────────────────────────────────

def _tokens(text: str) -> set[str]:
    if not text:
        return set()
    return {m.group(0).lower() for m in _TOKEN_RE.finditer(text)
            if m.group(0).lower() not in _STOPWORDS and len(m.group(0)) >= 3}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


# ── deterministic finders ───────────────────────────────────────────────────

def _cluster_title_bag(con: sqlite3.Connection, cluster_id: str) -> set[str]:
    rows = con.execute(
        """
        SELECT sl.raw_title FROM session s
        LEFT JOIN session_local sl ON sl.session_id = s.id
        WHERE s.cluster_id = ?
        """,
        (cluster_id,),
    ).fetchall()
    bag: set[str] = set()
    for r in rows:
        bag |= _tokens(r["raw_title"] or "")
    return bag


def find_dedup_candidates(con: sqlite3.Connection, p: dict,
                          as_of: date) -> list[dict]:
    """Pairs of clusters whose title bags overlap above the Jaccard floor and
    whose time ranges are within `dedup_days_back` of each other."""
    since = (as_of - timedelta(days=int(p["dedup_days_back"]))).isoformat()
    rows = con.execute(
        """
        SELECT jv.cluster_id, jv.stream, jv.started_at, jv.ended_at,
               jv.total_seconds, COALESCE(cn.name, '(unnamed)') AS name
        FROM job_view jv
        LEFT JOIN cluster_name cn ON cn.cluster_id = jv.cluster_id
        WHERE jv.ended_at >= ?
        ORDER BY jv.started_at
        """,
        (since,),
    ).fetchall()
    # Pre-compute title bags once.
    enriched = []
    for r in rows:
        enriched.append({
            "cluster_id":   r["cluster_id"],
            "stream":       r["stream"],
            "started_at":   r["started_at"],
            "ended_at":     r["ended_at"],
            "total_seconds": r["total_seconds"],
            "name":         r["name"],
            "bag":          _cluster_title_bag(con, r["cluster_id"]),
        })
    floor = float(p["dedup_jaccard_floor"])
    pairs: list[dict] = []
    for i, a in enumerate(enriched):
        for b in enriched[i + 1:]:
            score = _jaccard(a["bag"], b["bag"])
            if score >= floor:
                pairs.append({
                    "a":           {k: v for k, v in a.items() if k != "bag"},
                    "b":           {k: v for k, v in b.items() if k != "bag"},
                    "jaccard":     round(score, 2),
                    "shared":      sorted(a["bag"] & b["bag"])[:8],
                    "same_stream": a["stream"] == b["stream"],
                })
    pairs.sort(key=lambda d: -d["jaccard"])
    return pairs[:8]  # cap so the LLM packet stays small


def find_plan_vs_actual(con: sqlite3.Connection, p: dict,
                        as_of: date) -> list[dict]:
    """Per plan_item over the last `plan_vs_actual_days`, compute actual
    session-minutes that day in the same stream (loose match)."""
    since = (as_of - timedelta(days=int(p["plan_vs_actual_days"]))).isoformat()
    items = con.execute(
        """
        SELECT id, plan_date, name, planned_minutes, done, stream
        FROM plan_item
        WHERE plan_date >= ?
        ORDER BY plan_date
        """,
        (since,),
    ).fetchall()
    overrun = float(p["plan_overrun_ratio"])
    underrun = float(p["plan_underrun_ratio"])
    findings: list[dict] = []
    for it in items:
        # Sum session active seconds for the same stream on the same day.
        if it["stream"]:
            actual_s = con.execute(
                """
                SELECT COALESCE(SUM(
                    (julianday(ended_at) - julianday(started_at)) * 86400.0
                ), 0) AS s
                FROM session
                WHERE stream = ?
                  AND started_at LIKE ? || '%'
                  AND ended_at IS NOT NULL
                """,
                (it["stream"], it["plan_date"]),
            ).fetchone()["s"]
        else:
            actual_s = 0.0
        actual_min = round(actual_s / 60.0, 1)
        planned = int(it["planned_minutes"] or 0)
        if planned == 0 and actual_min == 0:
            continue
        ratio = (actual_min / planned) if planned else float("inf")
        flag = None
        if planned and actual_min >= planned * overrun:
            flag = "overrun"
        elif planned and actual_min <= planned * underrun:
            flag = "underrun"
        elif planned == 0 and actual_min > 0:
            flag = "unplanned-time"
        if flag is None:
            continue
        findings.append({
            "plan_date":   it["plan_date"],
            "name":        it["name"],
            "stream":      it["stream"],
            "planned_min": planned,
            "actual_min":  actual_min,
            "ratio":       round(ratio, 2) if ratio != float("inf") else None,
            "flag":        flag,
            "done":        bool(it["done"]),
        })
    return findings[:10]


def find_untagged_buckets(con: sqlite3.Connection, p: dict,
                          as_of: date) -> list[dict]:
    """Group untagged sessions in the window by the most-common significant
    token in their raw_title, surface the heaviest buckets."""
    since = (as_of - timedelta(days=int(p["untagged_days_back"]))).isoformat()
    rows = con.execute(
        """
        SELECT s.id, s.app, s.started_at, s.ended_at, sl.raw_title
        FROM session s
        LEFT JOIN session_local sl ON sl.session_id = s.id
        WHERE s.stream IS NULL
          AND s.started_at >= ?
          AND s.ended_at IS NOT NULL
        """,
        (since,),
    ).fetchall()
    buckets: dict[str, dict] = defaultdict(
        lambda: {"seconds": 0.0, "session_count": 0, "apps": Counter(),
                 "sample_titles": []}
    )
    for r in rows:
        toks = _tokens(r["raw_title"] or "")
        if not toks:
            key = (r["app"] or "?").lower()
        else:
            key = sorted(toks)[0]
        try:
            s = datetime.fromisoformat(r["started_at"].replace("Z", "+00:00"))
            e = datetime.fromisoformat(r["ended_at"].replace("Z", "+00:00"))
            dur = max(0.0, (e - s).total_seconds())
        except (ValueError, AttributeError):
            dur = 0.0
        b = buckets[key]
        b["seconds"] += dur
        b["session_count"] += 1
        b["apps"][r["app"] or "?"] += 1
        if len(b["sample_titles"]) < 3 and r["raw_title"]:
            b["sample_titles"].append(r["raw_title"][:80])
    out: list[dict] = []
    for key, b in buckets.items():
        if b["seconds"] < 60:  # under a minute total — noise
            continue
        out.append({
            "token":         key,
            "minutes":       round(b["seconds"] / 60.0, 1),
            "session_count": b["session_count"],
            "top_apps":      [a for a, _ in b["apps"].most_common(3)],
            "sample_titles": b["sample_titles"],
        })
    out.sort(key=lambda d: -d["minutes"])
    return out[: int(p["untagged_top_buckets"])]


def find_stale_learned_tags(p: dict, as_of: date,
                            cfg: dict | None = None) -> list[dict]:
    """learned_tags.json carries hit_count + created_at; retire rules that
    haven't earned hits and are older than the floor."""
    cfg = cfg or {}
    logs_dir = resolve(cfg.get("paths", {}).get("logs", "logs"))
    p_path = logs_dir / "learned_tags.json"
    try:
        data = json.loads(p_path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return []
    min_age = int(p["stale_tag_min_age_days"])
    max_hits = int(p["stale_tag_max_hits"])
    cutoff = as_of - timedelta(days=min_age)
    stale: list[dict] = []
    for r in data if isinstance(data, list) else []:
        try:
            created = datetime.fromisoformat(r.get("created_at", "")).date()
        except (ValueError, TypeError):
            continue
        if created > cutoff:
            continue
        if int(r.get("hit_count") or 0) > max_hits:
            continue
        stale.append({
            "pattern":   r.get("pattern"),
            "stream":    r.get("stream"),
            "source":    r.get("source"),
            "age_days":  (as_of - created).days,
            "hit_count": int(r.get("hit_count") or 0),
        })
    stale.sort(key=lambda d: -d["age_days"])
    return stale[:8]


# ── top-level finders ───────────────────────────────────────────────────────

def findings(con: sqlite3.Connection, *, as_of: date | None = None,
             cfg: dict | None = None) -> dict:
    p = load_params()
    today = as_of or datetime.now(timezone.utc).date()
    # quick totals for the headline
    since_iso = (today - timedelta(days=7)).isoformat()
    weekly_total = con.execute(
        """
        SELECT COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400), 0) AS s
        FROM session
        WHERE started_at >= ? AND ended_at IS NOT NULL
        """,
        (since_iso,),
    ).fetchone()["s"]
    cluster_n = con.execute("SELECT COUNT(*) AS n FROM job_view").fetchone()["n"]
    return {
        "as_of":        today.isoformat(),
        "params":       p,
        "totals": {
            "weekly_tracked_hours": round(weekly_total / 3600.0, 1),
            "cluster_count":        cluster_n,
        },
        "dedup_candidates":   find_dedup_candidates(con, p, today),
        "plan_vs_actual":     find_plan_vs_actual(con, p, today),
        "untagged_buckets":   find_untagged_buckets(con, p, today),
        "stale_learned_tags": find_stale_learned_tags(p, today, cfg),
    }


# ── LLM evidence packet + fallback rendering ────────────────────────────────

def _packet(f: dict) -> str:
    """Compact text packet for the LLM. Keep it under a few KB."""
    lines = [f"AS_OF: {f['as_of']}",
             f"WEEKLY_TRACKED_HOURS: {f['totals']['weekly_tracked_hours']}",
             f"CLUSTERS_TOTAL: {f['totals']['cluster_count']}",
             ""]
    lines.append("DEDUP_CANDIDATES:")
    for d in f["dedup_candidates"]:
        lines.append(
            f"  jaccard={d['jaccard']} "
            f"a='{d['a']['name']}'/{d['a']['stream'] or '<untagged>'} "
            f"b='{d['b']['name']}'/{d['b']['stream'] or '<untagged>'} "
            f"shared={','.join(d['shared'][:6])}"
        )
    if not f["dedup_candidates"]:
        lines.append("  (none)")
    lines += ["", "PLAN_VS_ACTUAL:"]
    for it in f["plan_vs_actual"]:
        lines.append(
            f"  {it['plan_date']} [{it['flag']}] '{it['name']}' "
            f"stream={it['stream'] or '—'} "
            f"planned={it['planned_min']}m actual={it['actual_min']}m "
            f"ratio={it['ratio']}"
        )
    if not f["plan_vs_actual"]:
        lines.append("  (all plans matched actuals)")
    lines += ["", "UNTAGGED_BUCKETS:"]
    for b in f["untagged_buckets"]:
        sample = "; ".join(b["sample_titles"][:2])
        lines.append(
            f"  token='{b['token']}' minutes={b['minutes']} "
            f"sessions={b['session_count']} apps={','.join(b['top_apps'])} "
            f"samples={sample}"
        )
    if not f["untagged_buckets"]:
        lines.append("  (nothing untagged worth triaging)")
    lines += ["", "STALE_LEARNED_TAGS:"]
    for t in f["stale_learned_tags"]:
        lines.append(
            f"  pattern='{t['pattern']}' stream={t['stream']} "
            f"source={t['source']} age_days={t['age_days']} hits={t['hit_count']}"
        )
    if not f["stale_learned_tags"]:
        lines.append("  (clean)")
    return "\n".join(lines)


def _fallback_markdown(f: dict) -> str:
    """Templated version. Less prose, same triage. Respects section headings."""
    weekly = f["totals"]["weekly_tracked_hours"]
    lines = [
        "## Headline",
        "",
        f"{weekly:.1f} h tracked this week across {f['totals']['cluster_count']} clusters. "
        f"No LLM available — this is raw triage, not synthesis.",
        "",
        "## Worth your attention",
        "",
    ]
    bullets = []
    if f["dedup_candidates"]:
        bullets.append(f"- {len(f['dedup_candidates'])} dedup candidate(s) — see below.")
    if f["plan_vs_actual"]:
        bullets.append(f"- {len(f['plan_vs_actual'])} plan item(s) where actual diverged from planned.")
    if f["untagged_buckets"]:
        bullets.append(f"- {len(f['untagged_buckets'])} untagged time bucket(s) worth tagging.")
    if f["stale_learned_tags"]:
        bullets.append(f"- {len(f['stale_learned_tags'])} learned-tag rule(s) look stale.")
    if not bullets:
        bullets.append("- Quiet week; nothing flagged by the deterministic pre-pass.")
    lines += bullets
    lines += ["", "## Dedup candidates", ""]
    if f["dedup_candidates"]:
        for d in f["dedup_candidates"]:
            lines.append(
                f"- **{d['a']['name']}** ↔ **{d['b']['name']}**  "
                f"(jaccard={d['jaccard']}, shared: {', '.join(d['shared'][:5])})"
            )
    else:
        lines.append("No dedup candidates this pass.")
    lines += ["", "## Plan vs actual", ""]
    if f["plan_vs_actual"]:
        for it in f["plan_vs_actual"]:
            lines.append(
                f"- [{it['plan_date']}] **{it['flag']}** — _{it['name']}_ "
                f"(planned {it['planned_min']}m, actual {it['actual_min']}m)"
            )
    else:
        lines.append("Plans matched actuals.")
    lines += ["", "## Untagged time", ""]
    if f["untagged_buckets"]:
        for b in f["untagged_buckets"]:
            sample = b["sample_titles"][0] if b["sample_titles"] else "(no titles)"
            lines.append(
                f"- **{b['minutes']} min** — token `{b['token']}`, "
                f"{b['session_count']} sessions, top app(s) {', '.join(b['top_apps'])}. "
                f"Sample: \"{sample}\""
            )
    else:
        lines.append("Nothing untagged worth triaging.")
    lines += ["", "## Tag hygiene", ""]
    if f["stale_learned_tags"]:
        for t in f["stale_learned_tags"]:
            lines.append(
                f"- `{t['pattern']}` → {t['stream']} "
                f"({t['age_days']} days old, {t['hit_count']} hits) — candidate to retire."
            )
    else:
        lines.append("Clean.")
    lines += [
        "",
        "## Gap",
        "",
        "No LLM available — set `ANTHROPIC_API_KEY` for synthesis instead of "
        "raw triage. The dedup logic only sees title-token overlap; clusters "
        "with the same project intent but different vocabulary will be missed.",
        "",
    ]
    return "\n".join(lines)


# ── orchestrator ────────────────────────────────────────────────────────────

def _consolidations_dir() -> Path:
    p = ROOT / "consolidation"
    ensure_dir(p)
    return p


def _file_for(d: date) -> Path:
    return _consolidations_dir() / f"{d.isoformat()}.md"


def consolidate(con: sqlite3.Connection, *, as_of: date | None = None,
                force_fallback: bool = False, cfg: dict | None = None,
                dry_run: bool = False) -> dict:
    cfg = cfg or {}
    today = as_of or datetime.now(timezone.utc).date()
    f = findings(con, as_of=today, cfg=cfg)

    skill = _load_skill()
    packet = _packet(f)
    prompt = (
        f"{skill}\n\n"
        f"---\n\nFINDINGS:\n{packet}\n"
    )

    used_model: str | None = None
    in_tok = out_tok = 0
    fallback = True
    status = "fallback"
    raw = ""

    if not force_fallback:
        model = ((cfg.get("llm") or {}).get("model")) or "claude-sonnet-4-5-20250929"
        result = think._call_anthropic(prompt, model=model, cfg=cfg)
        if result is not None:
            raw, in_tok, out_tok, _ = result
            used_model = model
            fallback = False
            status = "ok"

    if fallback:
        raw = _fallback_markdown(f)

    # Record audit
    sr_id = atoms.new_id()
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd, input, output, status)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sr_id, ts, "consolidate", used_model, in_tok, out_tok,
         think._cost(in_tok, out_tok, cfg),
         packet[:4000], raw[:16000], status),
    )
    if used_model:
        atoms.write_ai_call(con, provider="anthropic", model=used_model,
                            in_tokens=in_tok, out_tokens=out_tok,
                            cost_usd=think._cost(in_tok, out_tok, cfg),
                            prompt_slug="skill:consolidate", ts=ts)

    out_path = _file_for(today)
    if not dry_run:
        out_path.write_text(raw, encoding="utf-8")
        # The consolidate run leaves a system-author capture so the brain
        # knows about itself.
        try:
            capture.capture(
                body=f"Consolidation written for {today.isoformat()} "
                     f"({'fallback' if fallback else used_model})",
                author="system", cfg=cfg,
            )
        except Exception:
            pass

    return {
        "date":     today.isoformat(),
        "findings": f,
        "raw":      raw,
        "path":     str(out_path),
        "fallback": fallback,
        "model":    used_model,
        "skill_run": sr_id,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    as_of = date.fromisoformat(args.date) if args.date else None
    result = consolidate(con, as_of=as_of, force_fallback=args.no_llm,
                         cfg=cfg, dry_run=args.dry_run)
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(f"consolidation written for {result['date']}")
    print(f"  path:      {result['path']}")
    tag = "fallback" if result['fallback'] else f"model={result['model']}"
    print(f"  source:    {tag}")
    print(f"  skill_run: {result['skill_run']}")
    f = result["findings"]
    print(f"  dedup={len(f['dedup_candidates'])}  "
          f"plan-vs-actual={len(f['plan_vs_actual'])}  "
          f"untagged-buckets={len(f['untagged_buckets'])}  "
          f"stale-tags={len(f['stale_learned_tags'])}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp consolidate",
                                     description="Dream-cycle consolidation pass.")
    parser.add_argument("--date", metavar="YYYY-MM-DD")
    parser.add_argument("--no-llm", action="store_true")
    parser.add_argument("--dry-run", action="store_true",
                        help="compute findings + render markdown but do not write a file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
