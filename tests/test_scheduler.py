"""
Tests for scripts/scheduler.py (PLAN.md §7 step 8b).

We don't actually call launchctl or schtasks — those are the OS's job and
would make the test suite platform-dependent. We test the renderer logic,
the job spec, and the human-schedule formatter.

Run: python -m pytest tests/test_scheduler.py
"""

from __future__ import annotations

import re
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from types import SimpleNamespace


import pytest

from workpulse.ops import scheduler


# ── job spec ────────────────────────────────────────────────────────────────

def test_jobs_are_all_interval_based():
    """No StartCalendarInterval jobs — those silently skip when the Mac is
    asleep at the target minute. Everything must be interval-based now."""
    js = scheduler.jobs()
    slugs = [j["slug"] for j in js]
    assert "nightly" in slugs          # replaced consolidate/report/profile
    assert "dream-refresh" in slugs
    assert "calendar-sync" in slugs
    # Sensors are now their own daemon agents (window-server + FSEvents need
    # a real launchd Aqua session, not a tray Popen child).
    assert "activity" in slugs
    assert "watcher" in slugs
    # The old fragile calendar jobs are gone.
    assert "consolidate" not in slugs
    assert "report-daily" not in slugs
    assert "report-weekly" not in slugs
    assert "profile" not in slugs
    # browser-tracker is no longer a standalone agent — it's bound into
    # activity.py's sample loop.
    assert "browser-tracker" not in slugs
    # No calendar-time jobs. Each job is either an interval or a daemon.
    for j in js:
        assert j.get("interval_seconds") or j.get("daemon"), \
            f"{j['slug']} is neither interval nor daemon"
        assert j["hour"] is None and j["minute"] is None


def test_daemon_plist_has_aqua_and_keepalive():
    j = next(j for j in scheduler.jobs() if j["slug"] == "activity")
    xml = scheduler.render_launchd_plist(j)
    assert "<key>RunAtLoad</key>" in xml
    assert "<key>KeepAlive</key>" in xml
    assert "<key>SessionCreate</key>" in xml
    assert "<string>Aqua</string>" in xml
    # A daemon has no interval or calendar trigger.
    assert "StartInterval" not in xml
    assert "StartCalendarInterval" not in xml


def test_each_job_has_required_fields():
    for j in scheduler.jobs():
        for k in ("slug", "label", "module", "args",
                  "hour", "minute", "weekday", "description"):
            assert k in j, f"job {j['slug']} missing {k!r}"


def test_label_prefix_is_namespaced():
    for j in scheduler.jobs():
        assert j["label"].startswith("com.workpulse.")


def test_human_schedule_format():
    js = scheduler.jobs()
    # All interval-based now.
    assert scheduler._human_schedule(
        next(j for j in js if j["slug"] == "nightly")) == "every 1h"
    assert scheduler._human_schedule(
        next(j for j in js if j["slug"] == "dream-refresh")) == "every 30 min"
    # interval formatter still handles seconds (synthetic)
    assert scheduler._human_schedule({"interval_seconds": 30, "hour": None,
                                      "minute": None, "weekday": None}) == "every 30s"


def test_human_schedule_interval():
    job = {"interval_seconds": 1800, "hour": None, "minute": None,
           "weekday": None}
    assert scheduler._human_schedule(job) == "every 30 min"
    job["interval_seconds"] = 7200
    assert scheduler._human_schedule(job) == "every 2h"
    job["interval_seconds"] = 45
    assert scheduler._human_schedule(job) == "every 45s"


def test_dream_refresh_job_present():
    slugs = [j["slug"] for j in scheduler.jobs()]
    assert "dream-refresh" in slugs
    job = next(j for j in scheduler.jobs() if j["slug"] == "dream-refresh")
    assert job["interval_seconds"] == 1800


def test_launchd_plist_renders_start_interval():
    job = next(j for j in scheduler.jobs() if j["slug"] == "dream-refresh")
    xml = scheduler.render_launchd_plist(job)
    assert "<key>StartInterval</key>" in xml
    assert "<integer>1800</integer>" in xml
    # Should NOT use StartCalendarInterval for an interval job
    assert "StartCalendarInterval</key>" not in xml or \
           "<key>StartCalendarInterval</key>" not in xml.split("StartCalendarIntervalRespectsRunStatus")[0]


def test_windows_xml_renders_repetition_for_interval():
    job = next(j for j in scheduler.jobs() if j["slug"] == "dream-refresh")
    xml = scheduler.render_windows_task_xml(job)
    assert "<Repetition>" in xml
    assert "<Interval>PT30M</Interval>" in xml


# ── launchd plist renderer ──────────────────────────────────────────────────

def test_render_launchd_plist_is_valid_xml():
    for j in scheduler.jobs():
        xml = scheduler.render_launchd_plist(j)
        # Should parse without error
        ET.fromstring(xml.split("\n", 1)[1] if xml.startswith("<?xml")
                      else xml)


def test_launchd_plist_carries_label():
    j = scheduler.jobs()[0]
    xml = scheduler.render_launchd_plist(j)
    assert f"<string>{j['label']}</string>" in xml


def test_launchd_plist_uses_python_path():
    j = scheduler.jobs()[0]
    fake_py = Path("/opt/fake/python")
    xml = scheduler.render_launchd_plist(j, python=fake_py)
    assert f"<string>{fake_py}</string>" in xml


def test_launchd_plist_includes_module_and_args():
    j = next(j for j in scheduler.jobs() if j["slug"] == "dream-refresh")
    xml = scheduler.render_launchd_plist(j)
    assert "<string>-m</string>" in xml
    assert f"<string>{j['module']}</string>" in xml
    assert "<string>--quiet</string>" in xml


def test_frozen_launchd_uses_desktop_agent_protocol(monkeypatch):
    """A packaged WorkPulse binary is not Python and cannot accept ``-m``.
    This regression left every macOS agent running the menu bar while the
    dashboard port stayed closed on the first external install."""
    monkeypatch.setattr(scheduler.sys, "frozen", True, raising=False)
    job = next(j for j in scheduler.jobs() if j["slug"] == "dashboard")
    xml = scheduler.render_launchd_plist(job)
    assert "<string>--agent</string>" in xml
    assert "<string>workpulse.cli</string>" in xml
    assert "<string>-m</string>" not in xml


# A synthetic calendar job — no real job uses calendar scheduling anymore,
# but the renderer still supports it, so we test that path directly.
_CAL_DAILY = {"slug": "cal-daily", "label": "com.workpulse.cal-daily",
              "module": "scripts.x", "args": [], "hour": 23, "minute": 0,
              "weekday": None, "description": "d"}
_CAL_WEEKLY = {"slug": "cal-weekly", "label": "com.workpulse.cal-weekly",
               "module": "scripts.x", "args": ["weekly"], "hour": 20,
               "minute": 0, "weekday": 0, "description": "w"}


def test_launchd_daily_has_no_weekday():
    xml = scheduler.render_launchd_plist(_CAL_DAILY)
    assert "Weekday" not in xml


def test_launchd_weekly_has_weekday_zero_for_sunday():
    xml = scheduler.render_launchd_plist(_CAL_WEEKLY)
    assert re.search(r"<key>Weekday</key>\s*<integer>0</integer>", xml)


def test_launchd_writes_logs_outside_documents():
    """Agent stdout/stderr must live under ~/Library/Logs/WorkPulse, NEVER
    inside the (TCC-protected) project dir. launchd opens these paths before
    spawning; a Documents path aborts the job with EX_CONFIG (78)."""
    j = scheduler.jobs()[0]
    xml = scheduler.render_launchd_plist(j, root=Path("/Users/x/Documents/WorkPulse"))
    assert f"Library/Logs/WorkPulse/{j['slug']}.out.log" in xml
    assert f"Library/Logs/WorkPulse/{j['slug']}.err.log" in xml
    # The log path must NOT be under the project root.
    assert "/Documents/WorkPulse/logs/" not in xml


# ── Windows Task Scheduler XML ──────────────────────────────────────────────

def test_render_windows_xml_is_valid():
    for j in scheduler.jobs():
        xml = scheduler.render_windows_task_xml(j)
        # strip the XML declaration so ET doesn't complain about the
        # encoding="UTF-16" (we'd render bytes for real use)
        body = xml.split("\n", 1)[1]
        ET.fromstring(body)


def test_windows_xml_carries_description():
    j = scheduler.jobs()[0]
    xml = scheduler.render_windows_task_xml(j)
    assert j["description"] in xml


def test_windows_xml_interval_uses_repetition():
    j = next(j for j in scheduler.jobs() if j["slug"] == "nightly")
    xml = scheduler.render_windows_task_xml(j)
    assert "<Repetition>" in xml


def test_windows_xml_daemon_uses_logon_trigger():
    j = next(j for j in scheduler.jobs() if j["slug"] == "activity")
    xml = scheduler.render_windows_task_xml(j)
    assert "<LogonTrigger>" in xml
    assert "<RestartOnFailure>" in xml
    assert "<DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>" in xml
    assert "<StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>" in xml
    assert "<StartWhenAvailable>true</StartWhenAvailable>" in xml


def test_windows_xml_calendar_weekly_uses_sunday():
    xml = scheduler.render_windows_task_xml(_CAL_WEEKLY)
    assert "<ScheduleByWeek>" in xml
    assert "<Sunday/>" in xml


def test_windows_xml_uses_python_path():
    j = scheduler.jobs()[0]
    fake_py = Path("C:/fake/pythonw.exe")
    xml = scheduler.render_windows_task_xml(j, python=fake_py)
    assert str(fake_py) in xml


def test_frozen_windows_task_uses_desktop_agent_protocol(monkeypatch):
    monkeypatch.setattr(scheduler.sys, "frozen", True, raising=False)
    job = next(j for j in scheduler.jobs() if j["slug"] == "activity")
    xml = scheduler.render_windows_task_xml(job)
    assert '"--agent" "workpulse.signals.activity"' in xml
    assert '"-m"' not in xml


def test_windows_daemon_starts_immediately_after_registration(tmp_path, monkeypatch):
    """A fresh install happens after logon, so LogonTrigger alone would leave
    sensors dormant until the next sign-in."""
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(scheduler, "ROOT", tmp_path)
    monkeypatch.setattr(scheduler.subprocess, "run", fake_run)
    job = next(j for j in scheduler.jobs() if j["slug"] == "activity")

    assert scheduler._windows_install_one(job).startswith("OK")
    assert any(c[:2] == ["schtasks", "/Create"] for c in calls)
    assert ["schtasks", "/Run", "/TN", job["label"]] in calls


# ── platform dispatch ──────────────────────────────────────────────────────

def test_install_unsupported_platform_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = scheduler.install_all()
    assert rc == 2


def test_uninstall_unsupported_platform_returns_2(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = scheduler.uninstall_all()
    assert rc == 2


def test_gui_agents_present_and_darwin_only():
    """The dashboard + menu-bar GUI agents ship, but only on macOS — Windows
    uses ops/tray.py for that role, so they must never register there."""
    slugs = [j["slug"] for j in scheduler.jobs()]
    assert "dashboard" in slugs and "menubar" in slugs
    for slug in ("dashboard", "menubar"):
        j = next(x for x in scheduler.jobs() if x["slug"] == slug)
        assert j["platforms"] == ("darwin",)
        assert j["daemon"] is True                      # long-running GUI agents


def test_jobs_for_platform_filters_gui_agents(monkeypatch):
    monkeypatch.setattr(scheduler.sys, "platform", "darwin")
    darwin = {j["slug"] for j in scheduler._jobs_for_platform()}
    assert {"dashboard", "menubar"} <= darwin
    assert "activity" in darwin                          # unpinned jobs stay

    monkeypatch.setattr(scheduler.sys, "platform", "win32")
    win = {j["slug"] for j in scheduler._jobs_for_platform()}
    assert "dashboard" not in win and "menubar" not in win
    # activity + watcher are tray-owned on Windows (start_activity/start_watcher),
    # NOT scheduled tasks — registering both double-manages and breaks capture.
    assert "activity" not in win and "watcher" not in win
    assert {"nightly", "dream-refresh", "calendar-sync", "doctor"} <= win


def test_dashboard_agent_runs_the_web_server():
    j = next(x for x in scheduler.jobs() if x["slug"] == "dashboard")
    xml = scheduler.render_launchd_plist(j)
    # python -m workpulse.cli web --port 5700
    for tok in ("workpulse.cli", "web", "--port", "5700"):
        assert f"<string>{tok}</string>" in xml
    assert "<key>KeepAlive</key>" in xml and "<string>Aqua</string>" in xml
