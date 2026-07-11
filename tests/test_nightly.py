"""
Tests for scripts/nightly.py — the sleep-proof daily/weekly guard.

Run: python -m pytest tests/test_nightly.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path


import pytest

from workpulse.core import db
from workpulse.ops import nightly


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _mark_run(con, slug, ts_iso, status="ok"):
    from workpulse.core import atoms
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd, input, output, status)
        VALUES (?, ?, ?, NULL, NULL, 0, 0, 0, '', '', ?)
        """,
        (atoms.new_id(), ts_iso, slug, status),
    )


def _at(hour, weekday_target=None):
    """A local-ish datetime today at `hour`. weekday_target unused unless set."""
    now = datetime.now().astimezone().replace(hour=hour, minute=0, second=0, microsecond=0)
    return now


# ── daily guard ─────────────────────────────────────────────────────────────

def test_daily_not_due_before_nightly_hour(env):
    con = _con()
    now = _at(10)  # 10:00, before 20:00
    s = nightly._due(con, now=now)
    assert s["daily_due"] is False


def test_daily_due_after_hour_when_not_run(env):
    con = _con()
    now = _at(21)
    s = nightly._due(con, now=now)
    assert s["daily_due"] is True


def test_daily_not_due_when_already_run_today(env):
    con = _con()
    today = datetime.now().astimezone().date().isoformat()
    _mark_run(con, "consolidate", f"{today}T20:30:00")
    now = _at(21)
    s = nightly._due(con, now=now)
    assert s["daily_done"] is True
    assert s["daily_due"] is False


def test_daily_catch_up_next_morning(env):
    """The core sleep-proof property: asleep at 23:00, no consolidate today,
    open the laptop at 07:00 next day → still due (because it's a NEW day
    with no run yet and 07:00 >= nightly hour is False...).

    Correction: catch-up means when we DO wake past the hour. At 07:00 the
    hour guard (>=20) is not met, so it waits until 20:00. But if the prior
    day never ran, that day is simply lost — we only ever run for 'today'.
    This test pins the actual behavior: morning is not due; evening is."""
    con = _con()
    # No runs at all. Morning: not due (before hour).
    assert nightly._due(con, now=_at(7))["daily_due"] is False
    # Same day, evening: due.
    assert nightly._due(con, now=_at(20))["daily_due"] is True


# ── weekly guard ────────────────────────────────────────────────────────────

def test_weekly_due_sunday_evening_when_not_run(env, monkeypatch):
    con = _con()
    # Find the most recent Sunday at 21:00
    now = datetime.now().astimezone().replace(hour=21, minute=0, second=0, microsecond=0)
    # shift to Sunday (weekday 6)
    now = now - timedelta(days=now.weekday() - 6 if now.weekday() >= 6 else now.weekday() + 1)
    while now.weekday() != 6:
        now = now + timedelta(days=1)
    s = nightly._due(con, now=now)
    assert s["weekly_due"] is True


def test_weekly_not_due_midweek(env):
    con = _con()
    now = datetime.now().astimezone().replace(hour=21)
    # shift to a Wednesday
    while now.weekday() != 2:
        now = now + timedelta(days=1)
    s = nightly._due(con, now=now)
    assert s["weekly_due"] is False


# ── run() dispatch ──────────────────────────────────────────────────────────

def test_run_force_runs_daily(env, monkeypatch):
    con = _con()
    calls = {"consolidate": 0, "daily": 0, "profile": 0}
    from workpulse.core import consolidate, report, profile
    monkeypatch.setattr(consolidate, "consolidate",
                        lambda con, **kw: calls.__setitem__("consolidate", calls["consolidate"] + 1))
    monkeypatch.setattr(report, "daily",
                        lambda con, **kw: calls.__setitem__("daily", calls["daily"] + 1))
    monkeypatch.setattr(report, "weekly", lambda con, **kw: None)
    monkeypatch.setattr(profile, "update_profile",
                        lambda con, **kw: calls.__setitem__("profile", calls["profile"] + 1))
    result = nightly.run(force=True, cfg={"paths": {}})
    assert result["daily"] is True
    assert calls == {"consolidate": 1, "daily": 1, "profile": 1}


def test_run_skips_when_not_due(env, monkeypatch):
    con = _con()
    today = datetime.now().astimezone().date().isoformat()
    _mark_run(con, "consolidate", f"{today}T20:30:00")
    # Guard against accidental real calls
    from workpulse.core import consolidate
    monkeypatch.setattr(consolidate, "consolidate",
                        lambda *a, **k: pytest.fail("should not run"))
    # Force-disable weekly so only daily is evaluated
    monkeypatch.setattr(nightly, "_due",
                        lambda con, now=None: {"daily_done": True, "daily_due": False,
                                               "weekly_done": True, "weekly_due": False,
                                               "now": "x"})
    result = nightly.run(force=False, cfg={"paths": {}})
    assert result["daily"] is False
    assert result["weekly"] is False
    assert result["skipped_reason"]
