"""
nightly.py — sleep-proof orchestrator for the once-a-day rollups.

Replaces four StartCalendarInterval agents (consolidate, report-daily,
profile, report-weekly). Those only fired if the Mac happened to be
awake at the exact scheduled minute; asleep at 23:00 meant the whole day's
rollup was silently skipped, with no catch-up. That's how the brain went
two weeks without a consolidation.

This runs HOURLY (StartInterval) and guards in code:

  - Daily rollup (consolidate + report-daily + profile): runs once
    per calendar day, the first time we wake up at/after `nightly_hour`
    local. If the Mac was asleep all evening and you open it at 07:00, the
    rollup runs at 07:00 for the prior period — late, but not lost.

  - Weekly rollup (report-weekly): runs once per ISO week, first wake at/
    after `weekly_hour` on/after the configured weekday.

"Have I already run today?" is answered from the skill_run audit table —
no new state, no migration. A successful `consolidate` row dated today
means the daily rollup is done.

CLI:
    python -m workpulse.ops.nightly run            # hourly entrypoint (guarded)
    python -m workpulse.ops.nightly run --force    # ignore the guard, run now
    python -m workpulse.ops.nightly status         # what would run + why
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timezone
from pathlib import Path

from workpulse.core import db
from workpulse.common import load_config

# Local-time hour after which the daily rollup is allowed to run.
_NIGHTLY_HOUR = 20
# Weekly rollup: allowed on/after this weekday (0=Mon .. 6=Sun) at _WEEKLY_HOUR.
_WEEKLY_WEEKDAY = 6   # Sunday
_WEEKLY_HOUR = 20


def _local_now() -> datetime:
    return datetime.now().astimezone()


def _ran_today(con, slug: str, on: date) -> bool:
    """True if `slug` has a successful skill_run dated `on` (local date)."""
    row = con.execute(
        """
        SELECT 1 FROM skill_run
        WHERE skill_slug = ? AND status IN ('ok', 'fallback')
          AND substr(ts, 1, 10) = ?
        LIMIT 1
        """,
        (slug, on.isoformat()),
    ).fetchone()
    return row is not None


def _ran_this_week(con, slug: str, iso_year: int, iso_week: int) -> bool:
    """True if `slug` ran successfully anytime in the given ISO week."""
    rows = con.execute(
        """
        SELECT substr(ts, 1, 10) AS d FROM skill_run
        WHERE skill_slug = ? AND status IN ('ok', 'fallback')
          AND ts >= date('now', '-8 days')
        """,
        (slug,),
    ).fetchall()
    for r in rows:
        try:
            d = date.fromisoformat(r["d"])
        except (ValueError, TypeError):
            continue
        iso = d.isocalendar()
        if iso.year == iso_year and iso.week == iso_week:
            return True
    return False


def _due(con, *, now: datetime | None = None) -> dict:
    """Compute what's due right now. Pure read; no side effects."""
    now = now or _local_now()
    today = now.date()
    iso = today.isocalendar()

    daily_done = _ran_today(con, "consolidate", today)
    daily_due = (now.hour >= _NIGHTLY_HOUR) and not daily_done

    weekly_done = _ran_this_week(con, "report-weekly", iso.year, iso.week)
    weekly_ready = (now.weekday() >= _WEEKLY_WEEKDAY and now.hour >= _WEEKLY_HOUR)
    weekly_due = weekly_ready and not weekly_done

    return {
        "now":          now.isoformat(),
        "daily_done":   daily_done,
        "daily_due":    daily_due,
        "weekly_done":  weekly_done,
        "weekly_due":   weekly_due,
    }


def run(*, force: bool = False, cfg: dict | None = None) -> dict:
    """The hourly entrypoint. Runs the daily and/or weekly rollup if due
    (or always, when force=True)."""
    cfg = cfg or load_config()
    con = db.connect(cfg)
    now = _local_now()
    state = _due(con, now=now)
    did = {"daily": False, "weekly": False, "skipped_reason": None}

    run_daily = force or state["daily_due"]
    run_weekly = force or state["weekly_due"]

    if run_daily:
        from workpulse.core import cleanup, consolidate, report, profile
        # Order matters: categorize is already handled by dream-refresh
        # every 30 min, so clusters are current. Consolidate first (it's
        # what _ran_today keys on), then the report, then the profile.
        consolidate.consolidate(con, cfg=cfg)
        report.daily(con, cfg=cfg, send_email=bool(
            (cfg.get("email") or {}).get("enabled")))
        profile.update_profile(con, cfg=cfg)
        # Synthesize first, then discard old reconstructable evidence. Durable
        # captures/reports/profile markers are intentionally never pruned.
        did["retention"] = cleanup.apply_retention(con, cfg=cfg)
        did["daily"] = True

    if run_weekly:
        from workpulse.core import report
        report.weekly(con, cfg=cfg, send_email=bool(
            (cfg.get("email") or {}).get("enabled")))
        did["weekly"] = True

    if not run_daily and not run_weekly:
        if state["daily_done"]:
            did["skipped_reason"] = "daily rollup already done today"
        elif now.hour < _NIGHTLY_HOUR:
            did["skipped_reason"] = f"before nightly hour ({_NIGHTLY_HOUR}:00 local)"
        else:
            did["skipped_reason"] = "nothing due"

    did["state"] = state
    return did


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp nightly",
                                     description="Sleep-proof daily/weekly rollups.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("--force", action="store_true",
                   help="ignore the once-a-day guard and run now")
    sub.add_parser("status")
    args = parser.parse_args(argv[1:])

    if args.cmd == "status":
        con = db.connect(load_config())
        s = _due(con)
        print(f"now:          {s['now']}")
        print(f"daily done:   {s['daily_done']}")
        print(f"daily due:    {s['daily_due']}")
        print(f"weekly done:  {s['weekly_done']}")
        print(f"weekly due:   {s['weekly_due']}")
        return 0

    if args.cmd == "run":
        result = run(force=args.force)
        ran = [k for k in ("daily", "weekly") if result[k]]
        if ran:
            print(f"nightly: ran {', '.join(ran)}")
        else:
            print(f"nightly: nothing ran — {result['skipped_reason']}")
        return 0

    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
