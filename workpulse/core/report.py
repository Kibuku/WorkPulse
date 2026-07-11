"""
report.py — `wp report`, daily + weekly reports as `wp think` with fixed prompts.

PLAN.md §7 step 9. The v1 plan called for a bespoke report generator; the v2
shape is much smaller: a small evidence-packet builder + a skill file per
report type + the existing LLM machinery in scripts/think.py + a thin SMTP
sender. Roughly 200 lines instead of 800.

Public API:
    daily(con, *, on=None, force_fallback=False, cfg=None,
          dry_run=False, send_email=False) -> dict
    weekly(con, *, ending=None, force_fallback=False, cfg=None,
           dry_run=False, send_email=False) -> dict
    send_report_email(subject, body_md, cfg) -> bool

Both daily() and weekly() return:
    {
      "kind":     "daily" | "weekly",
      "date":     ISO date,
      "raw":      markdown body,
      "path":     output file path,
      "fallback": bool,
      "model":    str | None,
      "skill_run": str,
      "emailed":  bool,
    }

CLI:
    python -m workpulse.core.report daily                  # today
    python -m workpulse.core.report daily --date 2026-06-10
    python -m workpulse.core.report weekly                 # week ending today
    python -m workpulse.core.report weekly --ending 2026-06-15
    python -m workpulse.core.report daily --no-llm
    python -m workpulse.core.report daily --email          # send via SMTP
    python -m workpulse.core.report daily --dry-run        # compute + render, no write
"""

from __future__ import annotations

import argparse
import json
import smtplib
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from email.mime.text import MIMEText
from pathlib import Path

from workpulse.core import atoms, consolidate, db, think
from workpulse.core import capture as capmod
from workpulse.common import ROOT, ensure_dir, load_config, PKG


_SKILLS = {
    "daily":  PKG / "skills" / "report-daily.md",
    "weekly": PKG / "skills" / "report-weekly.md",
}
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"


# ── output paths ────────────────────────────────────────────────────────────

def _reports_dir(kind: str) -> Path:
    p = ROOT / "reports" / kind
    ensure_dir(p)
    return p


def _path_for(kind: str, d: date) -> Path:
    if kind == "daily":
        return _reports_dir("daily") / f"{d.isoformat()}.md"
    # weekly: ISO week, e.g. 2026-W24.md, keyed by the Monday of the week
    monday = d - timedelta(days=d.weekday())
    iso = monday.isocalendar()
    return _reports_dir("weekly") / f"{iso.year}-W{iso.week:02d}.md"


# ── deterministic evidence (sits on top of consolidate.findings) ────────────

def _time_breakdown(con: sqlite3.Connection, *, start: date, end: date) -> dict:
    """Per-stream hours within [start, end] inclusive. Used by both daily
    (start==end) and weekly."""
    rows = con.execute(
        """
        SELECT COALESCE(stream, '<untagged>') AS s,
               COALESCE(SUM(
                 (julianday(ended_at) - julianday(started_at)) * 86400.0
               ), 0) AS secs
        FROM session
        WHERE substr(started_at, 1, 10) BETWEEN ? AND ?
          AND ended_at IS NOT NULL
        GROUP BY stream
        ORDER BY secs DESC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    by_stream = [{"stream": r["s"], "hours": round(r["secs"] / 3600.0, 1)}
                 for r in rows]
    return {
        "by_stream":   by_stream,
        "total_hours": round(sum(s["hours"] for s in by_stream), 1),
    }


def _top_clusters(con: sqlite3.Connection, *, start: date, end: date,
                  limit: int = 6) -> list[dict]:
    """Top clusters whose time-range overlaps the window, by total_seconds."""
    rows = con.execute(
        """
        SELECT jv.cluster_id, jv.stream, jv.total_seconds,
               jv.started_at, jv.ended_at,
               COALESCE(cn.name, NULL) AS name,
               COALESCE(cn.one_liner, NULL) AS one_liner,
               COALESCE(cn.source, NULL) AS name_source
        FROM job_view jv
        LEFT JOIN cluster_name cn ON cn.cluster_id = jv.cluster_id
        WHERE substr(jv.started_at, 1, 10) <= ?
          AND substr(jv.ended_at,   1, 10) >= ?
        ORDER BY jv.total_seconds DESC
        LIMIT ?
        """,
        (end.isoformat(), start.isoformat(), limit),
    ).fetchall()
    return [{**dict(r), "hours": round(r["total_seconds"] / 3600.0, 1)}
            for r in rows]


def _captures_in(con: sqlite3.Connection, *, start: date, end: date) -> list[dict]:
    rows = con.execute(
        """
        SELECT id, ts, author, body, pinned_kind, pinned_id
        FROM capture
        WHERE substr(ts, 1, 10) BETWEEN ? AND ?
        ORDER BY ts
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()
    return [dict(r) for r in rows]


def _evidence(con: sqlite3.Connection, kind: str, *,
              start: date, end: date, cfg: dict | None) -> dict:
    """Build the evidence packet that the skill will see."""
    findings = consolidate.findings(con, as_of=end, cfg=cfg)
    # Narrow findings to this window for sections that care about it.
    findings["plan_vs_actual"] = [
        it for it in findings["plan_vs_actual"]
        if start.isoformat() <= it["plan_date"] <= end.isoformat()
    ]
    if kind == "weekly":
        prev_start = start - timedelta(days=7)
        prev_end = end - timedelta(days=7)
        prev_tb = _time_breakdown(con, start=prev_start, end=prev_end)
        prev_by_stream = {s["stream"]: s["hours"] for s in prev_tb["by_stream"]}
    else:
        prev_by_stream = {}
    tb = _time_breakdown(con, start=start, end=end)
    for s in tb["by_stream"]:
        prev = prev_by_stream.get(s["stream"], 0.0)
        s["wow_delta_hours"] = round(s["hours"] - prev, 1)
    return {
        "kind":            kind,
        "window":          {"start": start.isoformat(),
                            "end":   end.isoformat()},
        "time_breakdown":  tb,
        "top_clusters":    _top_clusters(con, start=start, end=end),
        "captures":        _captures_in(con, start=start, end=end),
        "findings":        findings,
    }


# ── LLM packet rendering ────────────────────────────────────────────────────

def _packet(ev: dict) -> str:
    tb = ev["time_breakdown"]
    lines = [
        f"KIND: {ev['kind']}",
        f"WINDOW: {ev['window']['start']} .. {ev['window']['end']}",
        f"TOTAL_HOURS: {tb['total_hours']}",
        "TIME_BREAKDOWN:",
    ]
    for s in tb["by_stream"]:
        wow = f"  (Δ {s.get('wow_delta_hours', 0):+.1f} h vs last week)" \
              if ev["kind"] == "weekly" else ""
        lines.append(f"  {s['stream']:18s} {s['hours']:>5.1f} h{wow}")
    lines.append("")
    lines.append("TOP_CLUSTERS:")
    for c in ev["top_clusters"]:
        name = c.get("name") or "(unnamed)"
        oneliner = c.get("one_liner") or ""
        lines.append(
            f"  [{c['hours']:>4.1f} h] {name}  stream={c['stream'] or '—'}"
        )
        if oneliner:
            lines.append(f"           {oneliner}")
    if not ev["top_clusters"]:
        lines.append("  (none)")
    lines.append("")
    lines.append("CAPTURES_IN_WINDOW:")
    if ev["captures"]:
        for c in ev["captures"]:
            t = c["ts"][:16].replace("T", " ")
            body = (c["body"] or "").replace("\n", " ")[:200]
            lines.append(f"  [{t}] ({c['author']}) {body}")
    else:
        lines.append("  (no captures)")
    lines.append("")
    lines.append("FINDINGS_PLAN_VS_ACTUAL:")
    for it in ev["findings"]["plan_vs_actual"]:
        lines.append(
            f"  {it['plan_date']} [{it['flag']}] '{it['name']}' "
            f"planned={it['planned_min']}m actual={it['actual_min']}m"
        )
    if not ev["findings"]["plan_vs_actual"]:
        lines.append("  (no flagged plan items in window)")
    lines.append("")
    lines.append("FINDINGS_DEDUP_CANDIDATES:")
    for d in ev["findings"]["dedup_candidates"]:
        lines.append(
            f"  jaccard={d['jaccard']} a='{d['a']['name']}' b='{d['b']['name']}' "
            f"shared={','.join(d['shared'][:5])}"
        )
    if not ev["findings"]["dedup_candidates"]:
        lines.append("  (none)")
    lines.append("")
    lines.append("FINDINGS_UNTAGGED_BUCKETS:")
    for b in ev["findings"]["untagged_buckets"]:
        lines.append(
            f"  token='{b['token']}' minutes={b['minutes']} "
            f"sessions={b['session_count']} apps={','.join(b['top_apps'])}"
        )
    if not ev["findings"]["untagged_buckets"]:
        lines.append("  (none)")
    return "\n".join(lines)


# ── fallback rendering (no LLM) ─────────────────────────────────────────────

def _fallback_daily(ev: dict) -> str:
    d = date.fromisoformat(ev["window"]["start"])
    tb = ev["time_breakdown"]
    lines = [
        f"# {d.strftime('%A')}, {d.isoformat()}",
        "",
        "## Summary",
        "",
        f"{tb['total_hours']:.1f} h tracked. "
        f"No LLM available — raw breakdown only.",
        "",
        "## Time breakdown",
        "",
    ]
    if tb["by_stream"]:
        for s in tb["by_stream"]:
            lines.append(f"- {s['stream']}: **{s['hours']:.1f} h**")
    else:
        lines.append("- (no time tracked)")
    lines += ["", "## What you actually worked on", ""]
    if ev["top_clusters"]:
        for c in ev["top_clusters"][:5]:
            name = c.get("name") or "(unnamed cluster)"
            oneliner = c.get("one_liner") or ""
            lines.append(f"- **{name}** ({c['hours']:.1f} h, "
                         f"{c['stream'] or '<untagged>'}) — {oneliner}")
    else:
        lines.append("- (no clusters touching today)")
    if ev["findings"]["plan_vs_actual"]:
        lines += ["", "## Plan vs actual", ""]
        for it in ev["findings"]["plan_vs_actual"]:
            lines.append(
                f"- [{it['flag']}] _{it['name']}_ "
                f"(planned {it['planned_min']}m, actual {it['actual_min']}m)"
            )
    lines += ["", "## Notes", ""]
    if ev["captures"]:
        for c in ev["captures"]:
            t = c["ts"][11:16]
            lines.append(f"- {t} — {c['body']}")
    else:
        lines.append("No captures today.")
    lines += [
        "",
        "## Gap",
        "",
        "No LLM available — set ANTHROPIC_API_KEY for prose synthesis.",
        "",
    ]
    return "\n".join(lines)


def _fallback_weekly(ev: dict) -> str:
    monday = date.fromisoformat(ev["window"]["start"])
    tb = ev["time_breakdown"]
    lines = [
        f"# Week of {monday.isoformat()}",
        "",
        "## Summary",
        "",
        f"{tb['total_hours']:.1f} h tracked across {len(ev['top_clusters'])} clusters. "
        f"No LLM available — raw breakdown only.",
        "",
        "## Time breakdown",
        "",
    ]
    for s in tb["by_stream"]:
        wow = (f" (Δ {s['wow_delta_hours']:+.1f} h vs last week)"
               if abs(s.get("wow_delta_hours", 0)) >= 1 else "")
        lines.append(f"- {s['stream']}: **{s['hours']:.1f} h**{wow}")
    lines += ["", "## Highlights", ""]
    if ev["top_clusters"]:
        for c in ev["top_clusters"]:
            name = c.get("name") or "(unnamed cluster)"
            oneliner = c.get("one_liner") or ""
            lines.append(f"- **{name}** ({c['hours']:.1f} h, "
                         f"{c['stream'] or '<untagged>'}) — {oneliner}")
    else:
        lines.append("- (no clusters this week)")
    if ev["findings"]["plan_vs_actual"]:
        lines += ["", "## Plan reliability", ""]
        n = len(ev["findings"]["plan_vs_actual"])
        lines.append(f"- {n} plan item(s) had actual diverge from planned.")
    if (ev["findings"]["dedup_candidates"] or ev["findings"]["untagged_buckets"]
            or ev["findings"]["stale_learned_tags"]):
        lines += ["", "## Worth noticing", ""]
        for d in ev["findings"]["dedup_candidates"][:3]:
            lines.append(f"- Dedup: **{d['a']['name']}** ↔ **{d['b']['name']}** "
                         f"(jaccard {d['jaccard']})")
        for b in ev["findings"]["untagged_buckets"][:3]:
            lines.append(f"- Untagged: **{b['minutes']:.0f} min** of `{b['token']}`")
        for t in ev["findings"]["stale_learned_tags"][:3]:
            lines.append(f"- Stale tag: `{t['pattern']}` → {t['stream']} "
                         f"({t['age_days']}d, {t['hit_count']} hits)")
    lines += ["", "## Notes", ""]
    if ev["captures"]:
        by_day: dict[str, list[str]] = {}
        for c in ev["captures"]:
            day = c["ts"][:10]
            by_day.setdefault(day, []).append(c["body"])
        for day, bodies in sorted(by_day.items()):
            lines.append(f"- **{day}**")
            for b in bodies:
                lines.append(f"  - {b}")
    else:
        lines.append("No captures this week.")
    lines += [
        "",
        "## Gap",
        "",
        "No LLM available — set ANTHROPIC_API_KEY for prose synthesis. "
        "The brain only sees this machine.",
        "",
    ]
    return "\n".join(lines)


# ── one orchestrator for both ───────────────────────────────────────────────

def _run(con: sqlite3.Connection, kind: str, *, start: date, end: date,
         force_fallback: bool, cfg: dict | None,
         dry_run: bool, send_email: bool) -> dict:
    cfg = cfg or {}
    ev = _evidence(con, kind, start=start, end=end, cfg=cfg)

    try:
        skill = _SKILLS[kind].read_text(encoding="utf-8")
    except FileNotFoundError:
        skill = ""

    packet = _packet(ev)
    prompt = f"{skill}\n\n---\n\nEVIDENCE:\n{packet}\n"

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
        raw = (_fallback_daily(ev) if kind == "daily" else _fallback_weekly(ev))

    # Audit
    sr_id = atoms.new_id()
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd, input, output, status)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sr_id, ts, f"report-{kind}", used_model, in_tok, out_tok,
         think._cost(in_tok, out_tok, cfg),
         packet[:4000], raw[:16000], status),
    )
    if used_model:
        atoms.write_ai_call(con, provider="anthropic", model=used_model,
                            in_tokens=in_tok, out_tokens=out_tok,
                            cost_usd=think._cost(in_tok, out_tok, cfg),
                            prompt_slug=f"skill:report-{kind}", ts=ts)

    out_path = _path_for(kind, end)
    if not dry_run:
        out_path.write_text(raw, encoding="utf-8")
        try:
            capmod.capture(
                body=f"{kind.capitalize()} report written for "
                     f"{ev['window']['start']}..{ev['window']['end']} "
                     f"({'fallback' if fallback else used_model})",
                author="system", cfg=cfg,
            )
        except Exception:
            pass

    emailed = False
    if send_email and not dry_run:
        subject = (f"WorkPulse {kind} — {end.isoformat()}"
                   if kind == "daily"
                   else f"WorkPulse weekly — week of {start.isoformat()}")
        emailed = bool(send_report_email(subject, raw, cfg))

    return {
        "kind":     kind,
        "date":     end.isoformat(),
        "raw":      raw,
        "path":     str(out_path),
        "fallback": fallback,
        "model":    used_model,
        "skill_run": sr_id,
        "emailed":  emailed,
        "evidence": ev,
    }


# ── public API ──────────────────────────────────────────────────────────────

def daily(con: sqlite3.Connection, *, on: date | None = None,
          force_fallback: bool = False, cfg: dict | None = None,
          dry_run: bool = False, send_email: bool = False) -> dict:
    d = on or datetime.now(timezone.utc).date()
    return _run(con, "daily", start=d, end=d, force_fallback=force_fallback,
                cfg=cfg, dry_run=dry_run, send_email=send_email)


def weekly(con: sqlite3.Connection, *, ending: date | None = None,
           force_fallback: bool = False, cfg: dict | None = None,
           dry_run: bool = False, send_email: bool = False) -> dict:
    end = ending or datetime.now(timezone.utc).date()
    start = end - timedelta(days=6)
    return _run(con, "weekly", start=start, end=end,
                force_fallback=force_fallback, cfg=cfg,
                dry_run=dry_run, send_email=send_email)


# ── thin SMTP sender ────────────────────────────────────────────────────────

def send_report_email(subject: str, body_md: str, cfg: dict | None = None) -> bool:
    """Send a markdown body as plain-text email. Uses the existing v1 email
    config + secrets. Returns True on success."""
    cfg = cfg or load_config()
    email_cfg = (cfg.get("email") or {})
    if not email_cfg.get("enabled"):
        return False
    smtp_user = email_cfg.get("smtp_user") or ""
    from_addr = email_cfg.get("from_addr") or smtp_user
    to_addr   = email_cfg.get("to_addr")   or smtp_user
    host = email_cfg.get("smtp_host") or "smtp.gmail.com"
    port = int(email_cfg.get("smtp_port") or 587)
    if not (smtp_user and from_addr and to_addr):
        return False
    try:
        from workpulse.wp_secrets import get as get_secret  # type: ignore
        password = get_secret("smtp_password")
    except Exception:
        password = None
    if not password:
        return False
    msg = MIMEText(body_md, "plain", "utf-8")
    msg["Subject"] = subject
    msg["From"] = from_addr
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(host, port, timeout=30) as s:
            s.starttls()
            s.login(smtp_user, password)
            s.sendmail(from_addr, [to_addr], msg.as_string())
        return True
    except Exception:
        return False


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    if args.cmd == "daily":
        on = date.fromisoformat(args.date) if args.date else None
        r = daily(con, on=on, force_fallback=args.no_llm, cfg=cfg,
                  dry_run=args.dry_run, send_email=args.email)
    else:
        ending = date.fromisoformat(args.ending) if args.ending else None
        r = weekly(con, ending=ending, force_fallback=args.no_llm, cfg=cfg,
                   dry_run=args.dry_run, send_email=args.email)
    if args.json:
        print(json.dumps({k: v for k, v in r.items() if k != "evidence"},
                         indent=2, default=str))
        return 0
    print(f"{r['kind']} report for {r['date']}")
    print(f"  path:      {r['path']}")
    tag = "fallback" if r["fallback"] else f"model={r['model']}"
    print(f"  source:    {tag}")
    if args.email:
        print(f"  emailed:   {r['emailed']}")
    print(f"  skill_run: {r['skill_run']}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp report",
                                     description="Daily and weekly reports.")
    sub = parser.add_subparsers(dest="cmd", required=True)

    d = sub.add_parser("daily")
    d.add_argument("--date", metavar="YYYY-MM-DD")

    w = sub.add_parser("weekly")
    w.add_argument("--ending", metavar="YYYY-MM-DD",
                   help="last day of the week to report on (defaults to today)")

    for p in (d, w):
        p.add_argument("--no-llm", action="store_true")
        p.add_argument("--email", action="store_true")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
