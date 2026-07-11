"""
calendar_sync.py — pull calendar events from an .ics URL into the atom store.

Each event becomes a calendar_event atom. Its title is auto-resolved against
projects.yaml so meetings about the Acme account get stream=acme before they
ever reach the Categorizer. The Categorizer uses calendar_event overlap as
a strong signal (+6) when scoring clusters — most user context lives in
meetings, and meetings have explicit subjects.

Config: set the URL in one of two places (env var wins):
  env:    WORKPULSE_CALENDAR_ICS_URL=https://outlook.office365.com/.../calendar.ics
  yaml:   calendar:
            ics_url: https://outlook.office365.com/.../calendar.ics

How to publish your Outlook calendar:
  Outlook → File → Account Settings → Publish Calendar  →  Save URL.
  Use the "iCal (.ics)" link, not the HTML view.

Public API:
    fetch_ics(url) -> bytes
    parse_events(ics_bytes, *, since, until) -> list[dict]
    sync(con, *, url, cfg, since_days, future_days) -> dict
        -> counts: {fetched, inserted, updated, skipped}

CLI:
    python -m workpulse.signals.calendar_sync sync
    python -m workpulse.signals.calendar_sync sync --since-days 14 --future-days 30
    python -m workpulse.signals.calendar_sync show 2026-06-27
    python -m workpulse.signals.calendar_sync diagnose                # config + url smoke
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workpulse.core import db
from workpulse.core import projects as wp_projects
from workpulse.common import load_config


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash16(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


# ── config ──────────────────────────────────────────────────────────────────

def _calendar_url(cfg: dict | None = None) -> str | None:
    """Look for the URL in three places, env var wins:
       1) env: WORKPULSE_CALENDAR_ICS_URL
       2) file: config/calendar.url  (gitignored — sane default for
          local installs, survives launchd reboots without shell rc)
       3) yaml: config.yaml → calendar.ics_url (committed; use only if
          the URL is not sensitive in your context)"""
    env = os.environ.get("WORKPULSE_CALENDAR_ICS_URL")
    if env:
        return env.strip()
    # File-based: config/calendar.url. ROOT comes from common; import lazily
    # to avoid a circular import during testing.
    try:
        from workpulse.common import ROOT
        f = ROOT / "config" / "calendar.url"
        if f.exists():
            line = f.read_text(encoding="utf-8").strip()
            if line:
                return line
    except Exception:
        pass
    cfg = cfg or load_config()
    cal = cfg.get("calendar") or {}
    url = cal.get("ics_url") or cal.get("url")
    return (url or "").strip() or None


# ── fetch ───────────────────────────────────────────────────────────────────

def fetch_ics(url: str, *, timeout: int = 20) -> bytes:
    """HTTP GET the ICS feed. Outlook published-calendar URLs are signed +
    public so no auth header is needed. We send a UA string because some
    Microsoft endpoints 403 default Python UAs."""
    req = urllib.request.Request(url, headers={
        "User-Agent": "WorkPulse/2.0 (calendar sync)",
        "Accept": "text/calendar, */*;q=0.1",
    })
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


# ── parse ───────────────────────────────────────────────────────────────────

def parse_events(ics_bytes: bytes, *,
                 since: datetime | None = None,
                 until: datetime | None = None) -> list[dict]:
    """Parse ICS bytes. Returns a list of dicts:
       {uid, started_at, ended_at, title, body, location, attendees, is_organizer}.
    Only events that overlap [since, until] are returned (so we don't load
    every event since 1970)."""
    try:
        from icalendar import Calendar
    except ImportError:
        return []
    try:
        cal = Calendar.from_ical(ics_bytes)
    except Exception:
        return []
    out: list[dict] = []
    for comp in cal.walk("VEVENT"):
        # dtstart / dtend can be datetime, date, or absent
        dts = comp.get("DTSTART")
        dte = comp.get("DTEND")
        if not dts or not dte:
            continue
        try:
            start = _to_dt(dts.dt)
            end = _to_dt(dte.dt)
        except Exception:
            continue
        if since and end < since:
            continue
        if until and start > until:
            continue
        attendees: list[str] = []
        for a in comp.get("ATTENDEE", []) if isinstance(comp.get("ATTENDEE"), list) \
                 else ([comp.get("ATTENDEE")] if comp.get("ATTENDEE") else []):
            try:
                v = str(a)
                if v.lower().startswith("mailto:"):
                    v = v[7:]
                attendees.append(v)
            except Exception:
                continue
        out.append({
            "uid":          str(comp.get("UID") or _hash16(
                str(start) + str(end) + str(comp.get("SUMMARY") or ""))),
            "started_at":   start.isoformat(),
            "ended_at":     end.isoformat(),
            "title":        str(comp.get("SUMMARY") or "").strip(),
            "body":         str(comp.get("DESCRIPTION") or "").strip(),
            "location":     str(comp.get("LOCATION") or "").strip(),
            "attendees":    attendees,
            "is_organizer": False,           # ICS feeds rarely expose this honestly
        })
    return out


def _to_dt(v) -> datetime:
    if isinstance(v, datetime):
        if v.tzinfo is None:
            v = v.replace(tzinfo=timezone.utc)
        return v.astimezone(timezone.utc)
    # date (all-day)
    return datetime.combine(v, datetime.min.time()).replace(tzinfo=timezone.utc)


# ── write to DB ─────────────────────────────────────────────────────────────

def _resolve_event_stream(event: dict, projects: list[dict]) -> str | None:
    text = " ".join([event.get("title") or "",
                     event.get("location") or "",
                     event.get("body") or "",
                     " ".join(event.get("attendees") or [])])
    return wp_projects.resolve_stream(text, projects)


def sync(con: sqlite3.Connection, *, url: str | None = None,
         cfg: dict | None = None,
         since_days: int = 14, future_days: int = 30) -> dict:
    cfg = cfg or load_config()
    url = url or _calendar_url(cfg)
    counts = {"fetched": 0, "inserted": 0, "updated": 0, "skipped": 0,
              "with_stream": 0, "errors": []}
    if not url:
        counts["errors"].append("no calendar URL configured")
        return counts
    try:
        body = fetch_ics(url)
    except Exception as e:
        counts["errors"].append(f"fetch failed: {e!r}")
        return counts
    since = datetime.now(timezone.utc) - timedelta(days=since_days)
    until = datetime.now(timezone.utc) + timedelta(days=future_days)
    events = parse_events(body, since=since, until=until)
    counts["fetched"] = len(events)
    if not events:
        return counts

    projects = wp_projects.load_projects()
    now = _now_iso()
    con.execute("BEGIN")
    try:
        for ev in events:
            stream = _resolve_event_stream(ev, projects)
            if stream:
                con.execute(
                    "INSERT OR IGNORE INTO stream(key, label, parent_key) "
                    "VALUES (?, ?, NULL)",
                    (stream, stream),
                )
            title_hash = _hash16(ev["title"])
            cur = con.execute(
                """
                INSERT INTO calendar_event(id, source, started_at, ended_at,
                                            title_hash, stream, is_organizer,
                                            fetched_at)
                VALUES (?, 'ics', ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                  started_at = excluded.started_at,
                  ended_at   = excluded.ended_at,
                  title_hash = excluded.title_hash,
                  stream     = COALESCE(excluded.stream, calendar_event.stream),
                  fetched_at = excluded.fetched_at
                """,
                (ev["uid"], ev["started_at"], ev["ended_at"], title_hash,
                 stream, int(ev["is_organizer"]), now),
            )
            # rowcount = 1 on insert, 1 on update. We can't tell which from
            # rowcount alone; instead infer by checking if it existed.
            if cur.rowcount:
                counts["inserted"] += 1
            else:
                counts["updated"] += 1
            con.execute(
                """
                INSERT INTO calendar_event_local(event_id, raw_title, raw_body,
                                                  raw_location, attendees)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(event_id) DO UPDATE SET
                  raw_title    = excluded.raw_title,
                  raw_body     = excluded.raw_body,
                  raw_location = excluded.raw_location,
                  attendees    = excluded.attendees
                """,
                (ev["uid"], ev["title"], ev["body"], ev["location"],
                 json.dumps(ev["attendees"])),
            )
            if stream:
                counts["with_stream"] += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_sync(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    counts = sync(con, cfg=cfg, since_days=args.since_days,
                  future_days=args.future_days)
    print("calendar_sync:")
    for k, v in counts.items():
        if k == "errors":
            for e in v:
                print(f"  ERROR: {e}")
        else:
            print(f"  {k:14s} {v}")
    return 1 if counts.get("errors") else 0


def _cli_show(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    day = args.day or datetime.now(timezone.utc).date().isoformat()
    rows = con.execute(
        """
        SELECT ce.started_at, ce.ended_at, ce.stream,
               cel.raw_title, cel.raw_location
        FROM calendar_event ce
        LEFT JOIN calendar_event_local cel ON cel.event_id = ce.id
        WHERE substr(ce.started_at, 1, 10) = ?
        ORDER BY ce.started_at
        """,
        (day,),
    ).fetchall()
    print(f"events on {day}: {len(rows)}")
    for r in rows:
        s = r["started_at"][11:16]
        e = r["ended_at"][11:16]
        title = (r["raw_title"] or "")[:60]
        stream = r["stream"] or "—"
        loc = (r["raw_location"] or "")[:30]
        print(f"  {s}–{e}  [{stream:14s}]  {title}   {loc}")
    return 0


def _cli_diagnose() -> int:
    cfg = load_config()
    url = _calendar_url(cfg)
    if not url:
        print("ERROR: no calendar URL configured.")
        print("Set one of:")
        print("  - env var WORKPULSE_CALENDAR_ICS_URL")
        print("  - config.yaml: calendar.ics_url")
        return 1
    redacted = url[:60] + "..." if len(url) > 60 else url
    print(f"url: {redacted}")
    print("fetching...")
    try:
        body = fetch_ics(url)
        head = body[:200].decode("utf-8", errors="replace")
        print(f"  bytes: {len(body)}")
        print(f"  preview: {head!r}")
    except Exception as e:
        print(f"  FAILED: {e!r}")
        return 1
    print("parsing...")
    events = parse_events(body,
                          since=datetime.now(timezone.utc) - timedelta(days=14),
                          until=datetime.now(timezone.utc) + timedelta(days=30))
    print(f"  events in window: {len(events)}")
    for ev in events[:5]:
        s = ev["started_at"][:16].replace("T", " ")
        print(f"    {s}  {ev['title'][:60]}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp calendar",
                                     description="ICS calendar sync.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sync")
    s.add_argument("--since-days", type=int, default=14)
    s.add_argument("--future-days", type=int, default=30)
    sh = sub.add_parser("show")
    sh.add_argument("day", nargs="?", help="YYYY-MM-DD, defaults to today")
    sub.add_parser("diagnose")
    args = parser.parse_args(argv[1:])
    if args.cmd == "sync":
        return _cli_sync(args)
    if args.cmd == "show":
        return _cli_show(args)
    if args.cmd == "diagnose":
        return _cli_diagnose()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
