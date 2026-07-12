"""
Tests for scripts/doctor.py — the autonomous health check.

Process/launchctl checks are integration-only (skipped here); we test the
DB-backed checks and the verdict aggregation, which is where the logic lives.

Run: python -m pytest tests/test_doctor.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, db
from workpulse.ops import doctor


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


# ── sensor_liveness ─────────────────────────────────────────────────────────

def test_liveness_fail_when_no_sessions(env):
    con = _con()
    r = doctor.check_sensor_liveness(con)
    assert r["status"] == doctor.FAIL


def test_liveness_ok_when_recent(env):
    con = _con()
    atoms.write_session(con, app="Code", title="x",
                        started_at=doctor._local_now().isoformat())
    r = doctor.check_sensor_liveness(con)
    assert r["status"] == doctor.OK


# ── sensor_not_stuck ────────────────────────────────────────────────────────

def test_not_stuck_ok_with_variety(env):
    con = _con()
    now = doctor._local_now()
    for i in range(30):
        app = "Code" if i % 2 else "Safari"
        atoms.write_session(con, app=app, title="x",
                            started_at=_iso(now - timedelta(minutes=i)))
    r = doctor.check_sensor_not_stuck(con)
    assert r["status"] == doctor.OK
    assert "distinct" in r["message"]


def test_not_stuck_warns_when_single_app(env):
    con = _con()
    now = doctor._local_now()
    for i in range(30):
        atoms.write_session(con, app="Claude", title="x",
                            started_at=_iso(now - timedelta(minutes=i * 2)))
    r = doctor.check_sensor_not_stuck(con)
    assert r["status"] == doctor.WARN
    assert "Claude" in r["message"]


def test_not_stuck_ok_when_too_few(env):
    con = _con()
    now = doctor._local_now()
    for i in range(3):
        atoms.write_session(con, app="Claude", title="x",
                            started_at=_iso(now - timedelta(minutes=i)))
    r = doctor.check_sensor_not_stuck(con)
    assert r["status"] == doctor.OK
    assert "too few" in r["message"]


# ── nightly_ran ─────────────────────────────────────────────────────────────

def _mark(con, slug, ts):
    con.execute(
        "INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model, "
        "in_tokens, out_tokens, cost_usd, input, output, status) "
        "VALUES (?, ?, ?, NULL, NULL, 0, 0, 0, '', '', 'ok')",
        (atoms.new_id(), ts, slug),
    )


def test_nightly_ok_before_evening(env, monkeypatch):
    con = _con()
    monkeypatch.setattr(doctor, "_local_now",
                        lambda: datetime.now().astimezone().replace(hour=10))
    r = doctor.check_nightly_ran(con)
    assert r["status"] == doctor.OK
    assert "not expected" in r["message"]


def test_nightly_warns_after_evening_if_not_run(env, monkeypatch):
    con = _con()
    monkeypatch.setattr(doctor, "_local_now",
                        lambda: datetime.now().astimezone().replace(hour=22))
    r = doctor.check_nightly_ran(con)
    assert r["status"] == doctor.WARN


def test_nightly_ok_after_evening_when_run(env, monkeypatch):
    con = _con()
    fixed = datetime.now().astimezone().replace(hour=22)
    monkeypatch.setattr(doctor, "_local_now", lambda: fixed)
    _mark(con, "consolidate", f"{fixed.date().isoformat()}T21:00:00")
    r = doctor.check_nightly_ran(con)
    assert r["status"] == doctor.OK


# ── verdict aggregation ─────────────────────────────────────────────────────

def test_verdict_is_worst_of_checks(env, monkeypatch):
    con = _con()
    # Seed a healthy-ish DB then force one FAIL check
    atoms.write_session(con, app="Code", title="x",
                        started_at=doctor._local_now().isoformat())
    monkeypatch.setattr(doctor, "check_agents_healthy",
                        lambda: {"check": "agents_healthy", "status": doctor.FAIL,
                                 "message": "activity: NOT loaded"})
    monkeypatch.setattr(doctor, "check_sensors_running",
                        lambda: {"check": "sensors_running", "status": doctor.OK,
                                 "message": "ok"})
    result = doctor.run_checks(cfg={"paths": {}})
    assert result["verdict"] == doctor.FAIL
    assert "wrong" in result["summary"].lower()


def test_summary_ok_when_all_ok(env, monkeypatch):
    checks = [{"check": "x", "status": doctor.OK, "message": "fine"}]
    assert "healthy" in doctor._summary_line(doctor.OK, checks).lower()


def test_agents_healthy_ignores_windows_status_codes(monkeypatch):
    """267011 (SCHED_S_TASK_HAS_NOT_RUN) is a status, not a failure; calendar-sync
    exiting non-zero (no ICS configured) is a WARN, not a FAIL."""
    from workpulse import platform_util
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    monkeypatch.setattr(platform_util, "is_mac", lambda: False)
    monkeypatch.setattr(platform_util, "agent_last_exit",
                        lambda slug: (True, 1 if slug == "calendar-sync" else 267011))
    r = doctor.check_agents_healthy()
    assert r["status"] == "warn"
    assert "calendar-sync" in r["message"]


def test_agents_healthy_real_failure_still_fails(monkeypatch):
    from workpulse import platform_util
    monkeypatch.setattr(platform_util, "is_windows", lambda: True)
    monkeypatch.setattr(platform_util, "is_mac", lambda: False)
    monkeypatch.setattr(platform_util, "agent_last_exit",
                        lambda slug: (True, 78 if slug == "activity" else 0))
    r = doctor.check_agents_healthy()
    assert r["status"] == "fail" and "activity" in r["message"]
