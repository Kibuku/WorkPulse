"""
Tests for scripts/coach.py (PLAN.md §7 step 10).

The whole step 10 principle is "popup last." Every test below verifies one
piece of that principle is load-bearing: the gate stays closed until earned,
the off switch works, content thresholds are real, the budget caps at one
message per day, the priority order is honored.

Run: python -m pytest tests/test_coach.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, coach, consolidate, db


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    monkeypatch.setattr(consolidate, "ROOT", tmp_path)
    monkeypatch.setattr(consolidate, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else tmp_path / rel))
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


def _record_think(con, *, day: date, n: int = 1):
    """Drop `n` skill_run rows for wp think on `day`."""
    for i in range(n):
        con.execute(
            """
            INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                                  in_tokens, out_tokens, cost_usd,
                                  input, output, status)
            VALUES (?, ?, 'think', NULL, 'claude-x', 100, 50, 0.001,
                    'q', 'a', 'ok')
            """,
            (atoms.new_id(), datetime.combine(day, datetime.min.time())
             .replace(hour=10 + i, tzinfo=timezone.utc).isoformat()),
        )


# ── params ──────────────────────────────────────────────────────────────────

def test_load_params_reads_frontmatter():
    p = coach.load_params()
    assert p["gate_window_days"] == 14
    assert p["gate_distinct_days"] == 5
    assert p["max_messages_per_day"] == 1
    assert p["enabled"] is True


# ── gate ────────────────────────────────────────────────────────────────────

def test_gate_closed_when_no_think_runs(env):
    con = _con()
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    assert open_ is False
    assert info["distinct_days"] == 0
    assert info["threshold"] == 5


def test_gate_closed_under_threshold(env):
    con = _con()
    # 4 distinct days — one short
    base = date(2026, 6, 10)
    for i in range(4):
        _record_think(con, day=base + timedelta(days=i))
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    assert open_ is False
    assert info["distinct_days"] == 4


def test_gate_opens_at_threshold(env):
    con = _con()
    base = date(2026, 6, 10)
    for i in range(5):
        _record_think(con, day=base + timedelta(days=i))
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    assert open_ is True
    assert info["distinct_days"] == 5


def test_gate_counts_distinct_days_not_invocations(env):
    """Five invocations on the same day count as 1, not 5."""
    con = _con()
    _record_think(con, day=date(2026, 6, 16), n=5)
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    assert open_ is False
    assert info["distinct_days"] == 1


def test_gate_window_excludes_old_runs(env):
    """Old `wp think` runs outside the window don't count."""
    con = _con()
    base = date(2026, 5, 1)  # 46 days before as_of
    for i in range(10):
        _record_think(con, day=base + timedelta(days=i))
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    assert info["distinct_days"] == 0
    assert open_ is False


def test_gate_excludes_child_skill_runs(env):
    """Only top-level wp-think calls count. A think invocation made by
    consolidate or report (parent_run_id NOT NULL) does NOT count toward
    the gate — those are system-initiated."""
    con = _con()
    parent_id = atoms.new_id()
    # First, the legitimate top-level think run
    _record_think(con, day=date(2026, 6, 10))
    # Then, a parent skill_run for the consolidate pass
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd,
                              input, output, status)
        VALUES (?, ?, 'consolidate', NULL, 'claude-x', 100, 50, 0.001,
                'q', 'a', 'ok')
        """,
        (parent_id, _iso(datetime(2026, 6, 11, 10, tzinfo=timezone.utc))),
    )
    # And a child-think run that consolidate spawned (parent != NULL)
    for i in range(4):
        con.execute(
            """
            INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                                  in_tokens, out_tokens, cost_usd,
                                  input, output, status)
            VALUES (?, ?, 'think', ?, 'claude-x', 50, 25, 0.0005,
                    'q', 'a', 'ok')
            """,
            (atoms.new_id(),
             _iso(datetime(2026, 6, 11 + i, 11, tzinfo=timezone.utc)),
             parent_id),
        )
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16))
    # Only the one top-level think counts — the four child ones are
    # consolidate-spawned and don't earn the user the right.
    assert info["distinct_days"] == 1
    assert open_ is False


def test_gate_off_switch_in_skill_file(env, tmp_path):
    """Setting enabled: false closes the gate even when thresholds clear."""
    base = date(2026, 6, 10)
    con = _con()
    for i in range(10):
        _record_think(con, day=base + timedelta(days=i))
    p = coach.load_params()
    p["enabled"] = False
    open_, info = coach.is_gated_on(con, as_of=date(2026, 6, 16), params=p)
    assert open_ is False
    assert "disabled" in info["reason"]


# ── content selection (priority cascade) ────────────────────────────────────

def _open_gate(con, *, as_of: date):
    """Helper: drop 5 distinct-day think runs so the gate is open."""
    base = as_of - timedelta(days=6)
    for i in range(5):
        _record_think(con, day=base + timedelta(days=i))


def test_silence_when_no_findings_clear_floors(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [], "plan_vs_actual": [],
                            "untagged_buckets": [], "stale_learned_tags": [],
                        })
    msg = coach.next_message(con, as_of=date(2026, 6, 16))
    assert msg is None


def test_priority_overrun_beats_untagged(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [],
                            "plan_vs_actual": [{
                                "plan_date": "2026-06-16",
                                "name": "Uganda doc",
                                "stream": "work",
                                "planned_min": 30, "actual_min": 240,
                                "ratio": 8.0, "flag": "overrun", "done": False,
                            }],
                            "untagged_buckets": [{
                                "token": "safari", "minutes": 120,
                                "session_count": 80,
                                "top_apps": ["Safari"],
                                "sample_titles": ["Safari"],
                            }],
                            "stale_learned_tags": [],
                        })
    msg = coach.next_message(con, as_of=date(2026, 6, 16))
    assert msg["kind"] == "plan_overrun"
    assert "Uganda doc" in msg["text"]
    assert "240" in msg["text"]


def test_overrun_below_floor_does_not_fire(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [],
                            "plan_vs_actual": [{
                                "plan_date": "2026-06-16",
                                "name": "Small Task", "stream": "work",
                                "planned_min": 30, "actual_min": 50,  # only +20m
                                "ratio": 1.7, "flag": "overrun", "done": False,
                            }],
                            "untagged_buckets": [],
                            "stale_learned_tags": [],
                        })
    msg = coach.next_message(con, as_of=date(2026, 6, 16))
    # +20m is below the 30m floor → silence on overrun → no other findings → silence
    assert msg is None


def test_untagged_message_when_no_plan_issues(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [], "plan_vs_actual": [],
                            "untagged_buckets": [{
                                "token": "verst", "minutes": 180,
                                "session_count": 60,
                                "top_apps": ["Word"],
                                "sample_titles": ["Verst Carbon narrative"],
                            }],
                            "stale_learned_tags": [],
                        })
    msg = coach.next_message(con, as_of=date(2026, 6, 16))
    assert msg["kind"] == "untagged_bucket"
    assert "Verst Carbon" in msg["text"]


def test_dedup_only_when_high_jaccard_and_same_stream(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    # Jaccard 0.5 (below 0.6 floor) → no dedup fire → silence
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [{
                                "a": {"name": "A", "stream": "work"},
                                "b": {"name": "B", "stream": "work"},
                                "jaccard": 0.5, "shared": ["x"],
                                "same_stream": True,
                            }],
                            "plan_vs_actual": [], "untagged_buckets": [],
                            "stale_learned_tags": [],
                        })
    assert coach.next_message(con, as_of=date(2026, 6, 16)) is None


def test_dedup_skips_cross_stream(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: {
                            "as_of": "2026-06-16", "params": {}, "totals": {},
                            "dedup_candidates": [{
                                "a": {"name": "A", "stream": "dev"},
                                "b": {"name": "B", "stream": "work"},
                                "jaccard": 0.9, "shared": ["x"],
                                "same_stream": False,
                            }],
                            "plan_vs_actual": [], "untagged_buckets": [],
                            "stale_learned_tags": [],
                        })
    assert coach.next_message(con, as_of=date(2026, 6, 16)) is None


# ── budget ──────────────────────────────────────────────────────────────────

def test_max_one_message_per_day(env, monkeypatch):
    con = _con()
    _open_gate(con, as_of=date(2026, 6, 16))
    findings_with_overrun = {
        "as_of": "2026-06-16", "params": {}, "totals": {},
        "dedup_candidates": [],
        "plan_vs_actual": [{
            "plan_date": "2026-06-16",
            "name": "Big overrun", "stream": "work",
            "planned_min": 30, "actual_min": 240,
            "ratio": 8.0, "flag": "overrun", "done": False,
        }],
        "untagged_buckets": [], "stale_learned_tags": [],
    }
    monkeypatch.setattr(consolidate, "findings",
                        lambda con, *, as_of=None, cfg=None: findings_with_overrun)

    first = coach.next_message(con, as_of=date(2026, 6, 16))
    second = coach.next_message(con, as_of=date(2026, 6, 16))
    assert first is not None
    assert second is None  # budget exhausted


def test_status_reports_silence_reason(env, monkeypatch):
    con = _con()
    s = coach.status(con, as_of=date(2026, 6, 16))
    assert s["would_say"] is None
    assert "gate closed" in s["reason"]


def test_status_reports_disabled(env, monkeypatch):
    con = _con()
    monkeypatch.setattr(coach, "load_params",
                        lambda: dict(coach._DEFAULTS, enabled=False))
    s = coach.status(con, as_of=date(2026, 6, 16))
    assert s["would_say"] is None
    assert s["reason"] == "disabled in skills/coach.md"
