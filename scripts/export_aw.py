"""
export_aw.py — Phase 4: ActivityWatch REST client.

Pulls window-focus and AFK events from the local ActivityWatch server
and returns them as structured dicts for use by report.py.

Usage:
  python scripts\\export_aw.py --date today
  python scripts\\export_aw.py --date 2026-04-18
  python scripts\\export_aw.py --date 2026-04-14 --days 7   # week starting that date
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config

# ── helpers ───────────────────────────────────────────────────────────────────

def _iso(d: date, hour: int = 0) -> str:
    """Return ISO-8601 UTC string for midnight (or given hour) on a date."""
    dt = datetime(d.year, d.month, d.day, hour, tzinfo=timezone.utc)
    return dt.isoformat()


def _parse_date(s: str) -> date:
    if s == "today":
        return date.today()
    return date.fromisoformat(s)


# ── AW client ─────────────────────────────────────────────────────────────────

class AWClient:
    def __init__(self, cfg: dict):
        aw_cfg = cfg["activitywatch"]
        self._base = aw_cfg["base_url"].rstrip("/")
        self._bucket_window = aw_cfg["bucket_window"]
        self._bucket_afk = aw_cfg["bucket_afk"]
        self._http = httpx.Client(timeout=10)

    def _get(self, path: str, **params) -> Any:
        url = f"{self._base}{path}"
        r = self._http.get(url, params=params, follow_redirects=True)
        r.raise_for_status()
        return r.json()

    def buckets(self) -> list[str]:
        """Return list of bucket IDs available on the server."""
        data = self._get("/api/0/buckets")
        return list(data.keys())

    def events(self, bucket_id: str, start: date, end: date) -> list[dict]:
        """Return all events in [start, end) for the given bucket."""
        return self._get(
            f"/api/0/buckets/{bucket_id}/events",
            start=_iso(start),
            end=_iso(end),
            limit=-1,
        )

    def window_events(self, start: date, end: date) -> list[dict]:
        return self.events(self._bucket_window, start, end)

    def afk_events(self, start: date, end: date) -> list[dict]:
        return self.events(self._bucket_afk, start, end)

    def close(self):
        self._http.close()


# ── aggregation ───────────────────────────────────────────────────────────────

def aggregate_by_app(events: list[dict]) -> dict[str, float]:
    """Return {app_name: total_seconds} from window events."""
    totals: dict[str, float] = {}
    for ev in events:
        app = ev.get("data", {}).get("app", "unknown")
        duration = ev.get("duration", 0)
        totals[app] = totals.get(app, 0) + duration
    return dict(sorted(totals.items(), key=lambda x: x[1], reverse=True))


def active_seconds(afk_events: list[dict]) -> float:
    """Return total non-AFK seconds."""
    return sum(
        ev.get("duration", 0)
        for ev in afk_events
        if ev.get("data", {}).get("status") == "not-afk"
    )


def fetch_day(client: AWClient, d: date) -> dict:
    """Fetch and aggregate a single day of AW data."""
    next_day = d + timedelta(days=1)
    window_evs = client.window_events(d, next_day)
    afk_evs = client.afk_events(d, next_day)

    return {
        "date": d.isoformat(),
        "active_seconds": active_seconds(afk_evs),
        "by_app": aggregate_by_app(window_evs),
        "raw_window_events": window_evs,
    }


def fetch_range(client: AWClient, start: date, days: int) -> list[dict]:
    """Fetch `days` consecutive days starting from `start`."""
    return [fetch_day(client, start + timedelta(days=i)) for i in range(days)]


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Export ActivityWatch data")
    parser.add_argument("--date", default="today", help="Date to export (YYYY-MM-DD or 'today')")
    parser.add_argument("--days", type=int, default=1, help="Number of days to export")
    parser.add_argument("--json", action="store_true", help="Output raw JSON instead of summary")
    args = parser.parse_args()

    cfg = load_config()
    client = AWClient(cfg)

    try:
        start = _parse_date(args.date)
    except ValueError:
        print(f"Invalid date: {args.date!r}", file=sys.stderr)
        sys.exit(1)

    # Quick connectivity check
    try:
        buckets = client.buckets()
    except Exception as e:
        print(f"Cannot reach ActivityWatch at {cfg['activitywatch']['base_url']}: {e}", file=sys.stderr)
        print("Make sure ActivityWatch is running.", file=sys.stderr)
        sys.exit(1)

    aw_cfg = cfg["activitywatch"]
    for bucket_key in ("bucket_window", "bucket_afk"):
        name = aw_cfg[bucket_key]
        if name not in buckets:
            print(f"WARNING: bucket {name!r} not found. Available: {buckets}", file=sys.stderr)

    results = fetch_range(client, start, args.days)
    client.close()

    if args.json:
        print(json.dumps(results, indent=2))
        return

    for day in results:
        active_min = round(day["active_seconds"] / 60, 1)
        print(f"\n── {day['date']}  ({active_min} min active) ──────────────")
        for app, secs in list(day["by_app"].items())[:10]:
            print(f"  {app:<35} {round(secs/60, 1):>6.1f} min")


if __name__ == "__main__":
    main()
