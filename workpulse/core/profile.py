"""
profile.py — produce brain/profile.md from the last N days of atoms.

A brain that learns *who the user is and how they work*, not just a
screen-time tracker — the knowledge layer over the raw activity signal.

Two paths:
- LLM (when ANTHROPIC_API_KEY is set): reads skills/profile.md, hands the
  deterministic FINDINGS packet to the model, gets back a full profile in
  the contract's shape.
- Fallback (zero-key): templated profile from the same FINDINGS, with
  honest sections that surface real signal even without synthesis.
  The fallback profile is genuinely readable, not a placeholder.

Public API:
    findings(con, *, as_of=None, days=None, cfg=None) -> dict
    update_profile(con, *, as_of=None, days=None, force_fallback=False,
                   cfg=None, dry_run=False) -> dict
        -> {path, raw, fallback, model, skill_run, findings}

CLI:
    python -m workpulse.core.profile update              # nightly entrypoint
    python -m workpulse.core.profile update --days 30
    python -m workpulse.core.profile update --no-llm     # force fallback
    python -m workpulse.core.profile update --dry-run    # render to stdout, no write
    python -m workpulse.core.profile show                # cat the current profile
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

from workpulse.core import atoms, db, think
from workpulse.common import ROOT, ensure_dir, load_config, PKG


_SKILL_PATH    = PKG / "skills" / "profile.md"
_PROFILE_PATH  = ROOT / "brain"  / "profile.md"
_DEFAULT_MODEL = "claude-haiku-4-5"

_DEFAULTS = {
    "window_days":             30,
    "include_unpinned":        True,
    "min_stream_hours_floor":  0.5,
    "goal_phrase_min_length":  20,
}

_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)

# Captures whose text contains any of these phrases get flagged as candidate goals
_GOAL_HINTS = ("want to", "should ", "plan to", "going to", "need to",
               "trying to", "hoping to", "intend to", "will ", "my goal")


# ── skill params ────────────────────────────────────────────────────────────

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


# ── deterministic findings ──────────────────────────────────────────────────

def _totals(con: sqlite3.Connection, start: str, end: str) -> dict:
    row = con.execute(
        """
        SELECT
          COUNT(*) AS sessions,
          COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400.0), 0) AS secs
        FROM session
        WHERE substr(started_at,1,10) BETWEEN ? AND ?
          AND ended_at IS NOT NULL
        """,
        (start, end),
    ).fetchone()
    caps = con.execute(
        "SELECT COUNT(*) AS n FROM capture WHERE substr(ts,1,10) BETWEEN ? AND ?",
        (start, end),
    ).fetchone()["n"]
    human_caps = con.execute(
        """
        SELECT COUNT(*) AS n FROM capture
        WHERE substr(ts,1,10) BETWEEN ? AND ? AND author = 'human'
        """,
        (start, end),
    ).fetchone()["n"]
    cluster_n = con.execute(
        """
        SELECT COUNT(*) AS n FROM job_view
        WHERE substr(ended_at,1,10) >= ? AND substr(started_at,1,10) <= ?
        """,
        (start, end),
    ).fetchone()["n"]
    return {
        "tracked_hours":      round(row["secs"] / 3600.0, 1),
        "session_count":      int(row["sessions"] or 0),
        "capture_count":      int(caps),
        "human_capture_count": int(human_caps),
        "cluster_count":      int(cluster_n),
    }


def _streams(con: sqlite3.Connection, start: str, end: str,
             min_hours: float) -> list[dict]:
    rows = con.execute(
        """
        SELECT COALESCE(stream, '<untagged>') AS s,
               COUNT(*) AS sessions,
               COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400), 0) AS secs,
               COUNT(DISTINCT substr(started_at,1,10)) AS active_days
        FROM session
        WHERE substr(started_at,1,10) BETWEEN ? AND ?
          AND ended_at IS NOT NULL
        GROUP BY stream
        """,
        (start, end),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        hours = r["secs"] / 3600.0
        if hours < min_hours:
            continue
        # Dominant apps in this stream during window
        apps = con.execute(
            """
            SELECT app, COUNT(*) AS n FROM session
            WHERE substr(started_at,1,10) BETWEEN ? AND ?
              AND COALESCE(stream, '<untagged>') = ?
              AND ended_at IS NOT NULL
            GROUP BY app ORDER BY n DESC LIMIT 4
            """,
            (start, end, r["s"]),
        ).fetchall()
        # Top named clusters in this stream
        clusters = con.execute(
            """
            SELECT jv.cluster_id, jv.total_seconds,
                   COALESCE(cn.name, NULL) AS name
            FROM job_view jv
            LEFT JOIN cluster_name cn ON cn.cluster_id = jv.cluster_id
            WHERE COALESCE(jv.stream, '<untagged>') = ?
              AND substr(jv.started_at,1,10) <= ?
              AND substr(jv.ended_at,1,10)   >= ?
            ORDER BY jv.total_seconds DESC LIMIT 5
            """,
            (r["s"], end, start),
        ).fetchall()
        # Time-of-day pattern: average start hour, weighted by duration
        hour_rows = con.execute(
            """
            SELECT CAST(substr(started_at, 12, 2) AS INTEGER) AS hr,
                   COUNT(*) AS n
            FROM session
            WHERE substr(started_at,1,10) BETWEEN ? AND ?
              AND COALESCE(stream, '<untagged>') = ?
              AND ended_at IS NOT NULL
            GROUP BY hr
            """,
            (start, end, r["s"]),
        ).fetchall()
        hour_dist = {int(h["hr"]): int(h["n"]) for h in hour_rows}
        out.append({
            "key":          r["s"],
            "hours":        round(hours, 1),
            "session_count": int(r["sessions"]),
            "active_days":  int(r["active_days"]),
            "hours_per_active_day": round(hours / max(int(r["active_days"]), 1), 1),
            "dominant_apps": [a["app"] for a in apps],
            "top_clusters": [
                {"name": c["name"], "hours": round(c["total_seconds"] / 3600.0, 1)}
                for c in clusters
            ],
            "hour_distribution": hour_dist,
            "peak_hour": (max(hour_dist.items(), key=lambda x: x[1])[0]
                          if hour_dist else None),
        })
    out.sort(key=lambda d: -d["hours"])
    return out


def _captures(con: sqlite3.Connection, start: str, end: str,
              *, include_unpinned: bool) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, ts, author, body, pinned_kind, pinned_id
        FROM capture
        WHERE substr(ts,1,10) BETWEEN ? AND ?
        ORDER BY ts
        """,
        (start, end),
    ).fetchall()
    out: list[dict] = []
    for r in rows:
        if not include_unpinned and r["pinned_kind"] is None and r["author"] != "human":
            continue
        out.append({
            "id":          r["id"],
            "ts":          r["ts"],
            "author":      r["author"],
            "body":        r["body"],
            "pinned_kind": r["pinned_kind"],
            "pinned_id":   r["pinned_id"],
        })
    return out


def _trajectory(con: sqlite3.Connection, start: str, end: str,
                window_days: int) -> dict:
    """This-period vs prior-period per-stream delta."""
    prior_end_dt = date.fromisoformat(start) - timedelta(days=1)
    prior_start_dt = prior_end_dt - timedelta(days=window_days - 1)
    prior_start = prior_start_dt.isoformat()
    prior_end = prior_end_dt.isoformat()
    now = con.execute(
        """
        SELECT COALESCE(stream,'<untagged>') AS s,
               COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400), 0) AS secs
        FROM session
        WHERE substr(started_at,1,10) BETWEEN ? AND ? AND ended_at IS NOT NULL
        GROUP BY stream
        """,
        (start, end),
    ).fetchall()
    prior = con.execute(
        """
        SELECT COALESCE(stream,'<untagged>') AS s,
               COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400), 0) AS secs
        FROM session
        WHERE substr(started_at,1,10) BETWEEN ? AND ? AND ended_at IS NOT NULL
        GROUP BY stream
        """,
        (prior_start, prior_end),
    ).fetchall()
    now_map = {r["s"]: r["secs"] / 3600.0 for r in now}
    prior_map = {r["s"]: r["secs"] / 3600.0 for r in prior}
    streams = set(now_map) | set(prior_map)
    deltas = []
    for s in streams:
        n = now_map.get(s, 0.0)
        p = prior_map.get(s, 0.0)
        deltas.append({"stream": s,
                       "hours_now": round(n, 1),
                       "hours_prior": round(p, 1),
                       "delta": round(n - p, 1)})
    deltas.sort(key=lambda d: -abs(d["delta"]))
    return {
        "prior_window": {"start": prior_start, "end": prior_end},
        "deltas":       deltas,
        "new_streams":     [d["stream"] for d in deltas
                            if d["hours_prior"] == 0 and d["hours_now"] > 0],
        "fading_streams":  [d["stream"] for d in deltas
                            if d["hours_prior"] > 0 and d["hours_now"] == 0],
    }


def _stated_goals(captures: list[dict], min_length: int) -> list[dict]:
    """Captures whose body looks like an intention (uses a goal-hint phrase
    and is long enough to be substantive)."""
    out = []
    for c in captures:
        if c["author"] != "human":
            continue
        body = (c["body"] or "").strip()
        if len(body) < min_length:
            continue
        low = body.lower()
        if any(h in low for h in _GOAL_HINTS):
            out.append(c)
    return out


def _open_questions(streams: list[dict], captures: list[dict],
                    trajectory: dict, as_of: date) -> list[str]:
    """Deterministic 'what the brain doesn't know but would benefit from
    knowing' — surfaces in the profile's Open questions section."""
    out: list[str] = []
    # Unpinned captures (no atom anchor)
    unpinned = [c for c in captures if c["author"] == "human"
                and c["pinned_kind"] is None]
    if len(unpinned) >= 3:
        out.append(
            f"{len(unpinned)} captures aren't pinned to any session, job, or "
            f"plan item. The brain can't tell what work they refer to."
        )
    # Fading streams
    for s in trajectory.get("fading_streams", []):
        out.append(
            f"You've spent zero time on `{s}` in this window. Last period "
            f"you spent some. Intentional pause, or did it drop off?"
        )
    # Untagged time
    untagged = next((s for s in streams if s["key"] == "<untagged>"), None)
    if untagged and untagged["hours"] >= 2:
        out.append(
            f"{untagged['hours']:.1f}h of tracked time is untagged. The "
            f"brain has the time but no stream label."
        )
    # Streams with high time but no human captures pinned
    for s in streams:
        if s["hours"] < 4 or s["key"] == "<untagged>":
            continue
        pinned_to_stream = sum(1 for c in captures
                               if c["author"] == "human" and
                                  c["pinned_kind"] is not None)
        if pinned_to_stream == 0:
            out.append(
                f"You spent {s['hours']:.1f}h on `{s['key']}` with zero "
                f"human captures pinned. The brain knows when, not what."
            )
            break
    return out


# ── top-level finder ────────────────────────────────────────────────────────

def findings(con: sqlite3.Connection, *,
             as_of: date | None = None,
             days: int | None = None,
             cfg: dict | None = None) -> dict:
    p = load_params()
    today = as_of or datetime.now(timezone.utc).date()
    window_days = int(days or p["window_days"])
    start_d = today - timedelta(days=window_days - 1)
    start = start_d.isoformat()
    end = today.isoformat()

    totals = _totals(con, start, end)
    streams = _streams(con, start, end, float(p["min_stream_hours_floor"]))
    captures = _captures(con, start, end, include_unpinned=bool(p["include_unpinned"]))
    trajectory = _trajectory(con, start, end, window_days)
    stated_goals = _stated_goals(captures, int(p["goal_phrase_min_length"]))
    open_qs = _open_questions(streams, captures, trajectory, today)

    return {
        "window":      {"start": start, "end": end, "days": window_days},
        "totals":      totals,
        "streams":     streams,
        "captures":    captures,
        "trajectory":  trajectory,
        "stated_goals": stated_goals,
        "open_questions": open_qs,
    }


# ── LLM packet ──────────────────────────────────────────────────────────────

def _packet(f: dict) -> str:
    lines = [
        f"WINDOW: {f['window']['start']} → {f['window']['end']} "
        f"({f['window']['days']} days)",
        "",
        f"TOTALS:",
        f"  tracked_hours:       {f['totals']['tracked_hours']}",
        f"  sessions:            {f['totals']['session_count']}",
        f"  captures (total):    {f['totals']['capture_count']}",
        f"  captures (human):    {f['totals']['human_capture_count']}",
        f"  clusters in window:  {f['totals']['cluster_count']}",
        "",
        "STREAMS:",
    ]
    for s in f["streams"]:
        names = [c["name"] for c in s["top_clusters"] if c.get("name")]
        names_str = ("named clusters: " + "; ".join(names)
                     if names else "(no named clusters)")
        peak = f"peak hour: {s['peak_hour']:02d}:00" if s["peak_hour"] is not None else ""
        lines.append(
            f"  {s['key']:18s} {s['hours']:>5.1f}h  "
            f"{s['active_days']} days  apps={','.join(s['dominant_apps'])}  "
            f"{peak}"
        )
        lines.append(f"    {names_str}")
    if not f["streams"]:
        lines.append("  (no streams above the hours floor)")
    lines += ["", "CAPTURES (chronological):"]
    for c in f["captures"]:
        tag = f"({c['author']})"
        pin = (f" → {c['pinned_kind']}:{c['pinned_id'][:8] if c['pinned_id'] else '?'}"
               if c["pinned_kind"] else "")
        body = (c["body"] or "").replace("\n", " ")[:240]
        lines.append(f"  {c['ts'][:16].replace('T', ' ')} {tag}{pin} {body}")
    if not f["captures"]:
        lines.append("  (no captures in window)")
    lines += ["", "TRAJECTORY (vs prior period):",
              f"  prior_window: {f['trajectory']['prior_window']['start']} → "
              f"{f['trajectory']['prior_window']['end']}"]
    for d in f["trajectory"]["deltas"][:10]:
        arrow = "↑" if d["delta"] > 0 else "↓" if d["delta"] < 0 else "→"
        lines.append(
            f"  {arrow} {d['stream']:18s} {d['hours_now']:>5.1f}h "
            f"(was {d['hours_prior']:.1f}h, Δ {d['delta']:+.1f}h)"
        )
    if f["trajectory"]["new_streams"]:
        lines.append(f"  NEW: {', '.join(f['trajectory']['new_streams'])}")
    if f["trajectory"]["fading_streams"]:
        lines.append(f"  FADING: {', '.join(f['trajectory']['fading_streams'])}")
    lines += ["", "STATED_GOALS (from captures):"]
    for g in f["stated_goals"]:
        body = (g["body"] or "").replace("\n", " ")[:200]
        lines.append(f"  [{g['ts'][:10]}] {body}")
    if not f["stated_goals"]:
        lines.append("  (no goal-shaped captures detected)")
    lines += ["", "OPEN_QUESTIONS (from the deterministic pass):"]
    for q in f["open_questions"]:
        lines.append(f"  - {q}")
    if not f["open_questions"]:
        lines.append("  (none)")
    return "\n".join(lines)


# ── fallback profile ────────────────────────────────────────────────────────

def _fmt_h(h: float) -> str:
    return f"{h:.1f}h"


def _fallback_profile(f: dict) -> str:
    w = f["window"]
    t = f["totals"]
    last_updated = datetime.now(timezone.utc).isoformat()

    lines = [
        "---",
        f"last_updated: {last_updated}",
        f"window: {w['start']} to {w['end']}",
        f"total_tracked_hours: {t['tracked_hours']}",
        "---",
        "",
        "# Profile",
        "",
    ]

    # Identity — derived from top stream + recent captures
    if t["tracked_hours"] < 2 or t["session_count"] == 0:
        lines += [
            "## Identity",
            "",
            f"The brain has only {t['tracked_hours']}h tracked over "
            f"{w['days']} days. Not enough yet to describe who you are.",
            "",
            "## What would help",
            "",
            "- Fix the sensor blindness so daytime work gets captured (Fix A).",
            "- Use `wp capture \"thought\" --auto-pin` to add narrative context "
            "alongside passive tracking.",
            "- Tag any large untagged buckets so streams have real weight.",
            "",
        ]
        return "\n".join(lines)

    top_stream = f["streams"][0] if f["streams"] else None
    human_captures = [c for c in f["captures"] if c["author"] == "human"]
    if top_stream:
        identity_bits = [
            f"Over the last {w['days']} days you tracked {t['tracked_hours']}h "
            f"across {t['cluster_count']} clusters."
        ]
        identity_bits.append(
            f"Your dominant stream is **{top_stream['key']}** at "
            f"{_fmt_h(top_stream['hours'])} ({top_stream['active_days']} "
            f"active days), focused on "
            f"{', '.join(top_stream['dominant_apps'][:2]) or 'unspecified apps'}."
        )
        if len(f["streams"]) >= 2:
            second = f["streams"][1]
            identity_bits.append(
                f"Second is **{second['key']}** at {_fmt_h(second['hours'])}."
            )
        if human_captures:
            identity_bits.append(
                f"You've written {len(human_captures)} captures in this window, "
                f"which the brain reads as your own voice annotating the work."
            )
        else:
            identity_bits.append(
                "No human captures landed in this window — the brain has time "
                "data but no narrative on what you actually accomplished."
            )
        lines += ["## Identity", "", " ".join(identity_bits), ""]

    # Streams section
    if f["streams"]:
        lines += ["## Streams", ""]
        total_h = sum(s["hours"] for s in f["streams"]) or 0.001
        for s in f["streams"]:
            pct = round(100 * s["hours"] / total_h)
            lines.append(f"### {s['key']}  ({_fmt_h(s['hours'])} / {pct}%)")
            lines.append("")
            named = [c["name"] for c in s["top_clusters"] if c.get("name")]
            bits = []
            if named:
                bits.append(f"Top named work: {', '.join(named[:3])}.")
            if s["dominant_apps"]:
                bits.append(f"Dominant apps: {', '.join(s['dominant_apps'])}.")
            if s["peak_hour"] is not None:
                bits.append(f"Peak hour: {s['peak_hour']:02d}:00.")
            bits.append(
                f"Active {s['active_days']} days, "
                f"averaging {_fmt_h(s['hours_per_active_day'])} per active day."
            )
            # Trajectory note for this stream
            delta = next((d for d in f["trajectory"]["deltas"]
                          if d["stream"] == s["key"]), None)
            if delta and abs(delta["delta"]) >= 1:
                arrow = "Up" if delta["delta"] > 0 else "Down"
                bits.append(
                    f"{arrow} {abs(delta['delta']):.1f}h vs the previous "
                    f"{w['days']}-day window."
                )
            lines.append(" ".join(bits))
            lines.append("")

    # How you work — patterns (deterministic, light)
    lines += ["## How you work", ""]
    patterns = []
    # Peak hour across streams
    all_hours: Counter = Counter()
    for s in f["streams"]:
        for hr, n in s.get("hour_distribution", {}).items():
            all_hours[hr] += n
    if all_hours:
        peak = max(all_hours.items(), key=lambda x: x[1])[0]
        patterns.append(
            f"Your most active hour overall is **{peak:02d}:00**."
        )
    # Capture cadence
    if human_captures:
        # Days between captures
        days_with_human_cap = {c["ts"][:10] for c in human_captures}
        if len(days_with_human_cap) >= 2:
            patterns.append(
                f"You captured on {len(days_with_human_cap)} of {w['days']} "
                f"days — roughly {len(days_with_human_cap)*100//w['days']}% "
                f"of the window."
            )
    if not patterns:
        patterns.append(
            "Window too small or too uniform to surface a distinctive "
            "rhythm yet. More tracking + captures will sharpen this."
        )
    lines += ["- " + p for p in patterns]
    lines.append("")

    # On your mind — captures grouped by author + chronology
    lines += ["## On your mind", ""]
    if human_captures:
        for c in human_captures[-12:]:
            body = (c["body"] or "").replace("\n", " ").strip()
            ts = c["ts"][:10]
            lines.append(f"- [{ts}] {body}")
    else:
        sys_caps = [c for c in f["captures"] if c["author"] == "system"]
        if sys_caps:
            lines.append(
                f"No human captures in this window. The brain has "
                f"{len(sys_caps)} system traces from dream cycle / report "
                f"runs, but nothing in your own words."
            )
        else:
            lines.append("Nothing captured in this window.")
    lines.append("")

    # Stated goals
    if f["stated_goals"]:
        lines += ["## What you've said you want to do", ""]
        for g in f["stated_goals"][-6:]:
            body = (g["body"] or "").replace("\n", " ").strip()
            ts = g["ts"][:10]
            lines.append(f"- [{ts}] {body}")
        lines.append("")

    # Open questions
    lines += ["## Open questions", ""]
    if f["open_questions"]:
        for q in f["open_questions"]:
            lines.append(f"- {q}")
    else:
        lines.append("None the brain can name right now.")
    lines.append("")

    return "\n".join(lines)


# ── orchestrator ────────────────────────────────────────────────────────────

def update_profile(con: sqlite3.Connection, *,
                   as_of: date | None = None,
                   days: int | None = None,
                   force_fallback: bool = False,
                   cfg: dict | None = None,
                   dry_run: bool = False) -> dict:
    cfg = cfg or {}
    today = as_of or datetime.now(timezone.utc).date()
    f = findings(con, as_of=today, days=days, cfg=cfg)

    skill = _load_skill()
    packet = _packet(f)
    prompt = f"{skill}\n\n---\n\nFINDINGS:\n{packet}\n"

    used_model: str | None = None
    in_tok = out_tok = 0
    fallback = True
    status = "fallback"
    raw = ""

    if not force_fallback:
        model = ((cfg.get("llm") or {}).get("model")) or _DEFAULT_MODEL
        result = think._call_anthropic(prompt, model=model, cfg=cfg)
        if result is not None:
            raw, in_tok, out_tok, _ = result
            used_model = model
            fallback = False
            status = "ok"

    if fallback:
        raw = _fallback_profile(f)

    sr_id = atoms.new_id()
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd,
                              input, output, status)
        VALUES (?, ?, 'profile', NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sr_id, ts, used_model, in_tok, out_tok,
         think._cost(in_tok, out_tok, cfg),
         packet[:4000], raw[:16000], status),
    )
    if used_model:
        atoms.write_ai_call(con, provider="anthropic", model=used_model,
                            in_tokens=in_tok, out_tokens=out_tok,
                            cost_usd=think._cost(in_tok, out_tok, cfg),
                            prompt_slug="skill:profile", ts=ts)

    if not dry_run:
        ensure_dir(_PROFILE_PATH.parent)
        _PROFILE_PATH.write_text(raw, encoding="utf-8")

    return {
        "path":     str(_PROFILE_PATH),
        "raw":      raw,
        "fallback": fallback,
        "model":    used_model,
        "skill_run": sr_id,
        "findings": f,
    }


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_update(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    days = args.days if args.days else None
    r = update_profile(con, days=days, force_fallback=args.no_llm,
                       cfg=cfg, dry_run=args.dry_run)
    if args.dry_run:
        print(r["raw"])
        return 0
    print(f"profile updated at {r['path']}")
    source = "fallback" if r["fallback"] else f"model={r['model']}"
    print(f"  source:    {source}")
    print(f"  skill_run: {r['skill_run']}")
    print(f"  window:    {r['findings']['window']['start']} -> "
          f"{r['findings']['window']['end']} "
          f"({r['findings']['window']['days']} days)")
    print(f"  signal:    "
          f"{r['findings']['totals']['tracked_hours']}h, "
          f"{len(r['findings']['streams'])} streams, "
          f"{r['findings']['totals']['human_capture_count']} human captures")
    return 0


def _cli_show() -> int:
    if not _PROFILE_PATH.exists():
        print("(no profile yet — run `python -m workpulse.core.profile update`)")
        return 1
    print(_PROFILE_PATH.read_text(encoding="utf-8"))
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp profile",
                                     description="Build the living profile.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    u = sub.add_parser("update")
    u.add_argument("--days", type=int)
    u.add_argument("--no-llm", action="store_true")
    u.add_argument("--dry-run", action="store_true",
                   help="render to stdout, do not write the file")
    sub.add_parser("show")
    args = parser.parse_args(argv[1:])
    if args.cmd == "update":
        return _cli_update(args)
    if args.cmd == "show":
        return _cli_show()
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
