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

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

from scripts import scheduler


# ── job spec ────────────────────────────────────────────────────────────────

def test_jobs_has_three_canonical_entries():
    js = scheduler.jobs()
    slugs = [j["slug"] for j in js]
    assert slugs == ["consolidate", "report-daily", "report-weekly"]


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
    assert scheduler._human_schedule(js[0]) == "daily 23:00"
    assert scheduler._human_schedule(js[1]) == "daily 23:15"
    assert scheduler._human_schedule(js[2]) == "Sun 20:00"


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
    j = scheduler.jobs()[2]  # weekly: has 'weekly' arg
    xml = scheduler.render_launchd_plist(j)
    assert "<string>-m</string>" in xml
    assert f"<string>{j['module']}</string>" in xml
    assert "<string>weekly</string>" in xml


def test_launchd_daily_has_no_weekday():
    j = scheduler.jobs()[0]
    xml = scheduler.render_launchd_plist(j)
    assert "Weekday" not in xml


def test_launchd_weekly_has_weekday_zero_for_sunday():
    j = scheduler.jobs()[2]
    xml = scheduler.render_launchd_plist(j)
    assert re.search(r"<key>Weekday</key>\s*<integer>0</integer>", xml)


def test_launchd_writes_logs_under_root():
    j = scheduler.jobs()[0]
    fake_root = Path("/some/root")
    xml = scheduler.render_launchd_plist(j, root=fake_root)
    assert f"{fake_root}/logs/consolidate.out.log" in xml
    assert f"{fake_root}/logs/consolidate.err.log" in xml


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
    assert "Dream cycle — nightly consolidation." in xml


def test_windows_xml_daily_uses_schedule_by_day():
    j = scheduler.jobs()[0]
    xml = scheduler.render_windows_task_xml(j)
    assert "<ScheduleByDay>" in xml
    assert "<ScheduleByWeek>" not in xml


def test_windows_xml_weekly_uses_sunday():
    j = scheduler.jobs()[2]
    xml = scheduler.render_windows_task_xml(j)
    assert "<ScheduleByWeek>" in xml
    assert "<Sunday/>" in xml


def test_windows_xml_uses_python_path():
    j = scheduler.jobs()[0]
    fake_py = Path("C:/fake/pythonw.exe")
    xml = scheduler.render_windows_task_xml(j, python=fake_py)
    assert str(fake_py) in xml


# ── platform dispatch ──────────────────────────────────────────────────────

def test_install_unsupported_platform_returns_2(monkeypatch, capsys):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = scheduler.install_all()
    assert rc == 2


def test_uninstall_unsupported_platform_returns_2(monkeypatch):
    monkeypatch.setattr(sys, "platform", "linux")
    rc = scheduler.uninstall_all()
    assert rc == 2
