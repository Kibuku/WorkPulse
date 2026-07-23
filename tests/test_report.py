"""
Tests for scripts/report.py (PLAN.md §7 step 9).

LLM and SMTP calls are monkeypatched. The fallback path is exercised
against seeded data; the LLM path verifies wiring + audit recording.

Run: python -m pytest tests/test_report.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, capture as capmod, categorize, cluster, consolidate, db, name_clusters as nc, report as rmod, think


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    monkeypatch.setattr(rmod, "ROOT", tmp_path)
    monkeypatch.setattr(consolidate, "ROOT", tmp_path)
    monkeypatch.setattr(consolidate, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else tmp_path / rel))
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(capmod, "ROOT", tmp_path)
    def _cap_dir():
        p = tmp_path / "captures"; p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(capmod, "_captures_dir", _cap_dir)
    # Point the skill files at the real repo location so they load.
    real_root = Path(__file__).resolve().parent.parent
    monkeypatch.setattr(rmod, "_SKILLS", {
        "daily":  real_root / "skills" / "report-daily.md",
        "weekly": real_root / "skills" / "report-weekly.md",
    })
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


def _seed_today(con, day: date):
    """A focused day: 30 min on dev, 60 min on work, 5 min untagged + a capture."""
    t0 = datetime.combine(day, datetime.min.time()).replace(
        hour=9, tzinfo=timezone.utc)
    for i in range(30):
        sid = atoms.write_session(con, app="Code", title="atoms.py",
                                  stream="dev",
                                  started_at=_iso(t0 + timedelta(minutes=i)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    for i in range(60):
        sid = atoms.write_session(con, app="Word", title="Uganda narrative",
                                  stream="work",
                                  started_at=_iso(t0 + timedelta(hours=1, minutes=i)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(hours=1, minutes=i + 1)))
    for i in range(5):
        sid = atoms.write_session(con, app="Safari", title="random tab",
                                  stream=None,
                                  started_at=_iso(t0 + timedelta(hours=2, minutes=i)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(hours=2, minutes=i + 1)))
    cluster.refresh(con)
    nc.name_all(con, force_fallback=True)
    atoms.write_capture(con, body="Mwangi prefers Mt. Elgon framing",
                        ts=_iso(t0 + timedelta(hours=3)))


# ── deterministic helpers ───────────────────────────────────────────────────

def test_time_breakdown_by_stream(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    tb = rmod._time_breakdown(con, start=d, end=d)
    streams = {s["stream"]: s["hours"] for s in tb["by_stream"]}
    assert streams.get("dev",  0) >= 0.4
    assert streams.get("work", 0) >= 0.9
    assert "<untagged>" in streams
    assert tb["total_hours"] >= 1.4


def test_time_breakdown_prefers_cluster_assignment_over_session_stream(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    row = con.execute(
        "SELECT cluster_id FROM job_view WHERE stream = 'dev' LIMIT 1"
    ).fetchone()
    categorize.correct_assignment(con, row["cluster_id"], "work")
    tb = rmod._time_breakdown(con, start=d, end=d)
    streams = {s["stream"]: s["hours"] for s in tb["by_stream"]}
    assert streams.get("dev", 0) == 0
    assert streams.get("work", 0) >= 1.4


def test_time_breakdown_treats_fallback_as_untagged(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    row = con.execute(
        "SELECT cluster_id FROM job_view WHERE stream IS NULL LIMIT 1"
    ).fetchone()
    categorize.assign_cluster(con, row["cluster_id"])
    tb = rmod._time_breakdown(con, start=d, end=d)
    streams = {s["stream"]: s["hours"] for s in tb["by_stream"]}
    assert streams.get("<untagged>", 0) > 0
    assert "misc" not in streams


def test_top_clusters_overlap(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    rows = rmod._top_clusters(con, start=d, end=d)
    assert len(rows) >= 1
    assert "hours" in rows[0]


def test_captures_in_window(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    caps = rmod._captures_in(con, start=d, end=d)
    assert any("Mwangi" in c["body"] for c in caps)


# ── fallback paths ──────────────────────────────────────────────────────────

def test_daily_fallback_writes_file(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    r = rmod.daily(con, on=d, force_fallback=True, cfg={"paths": {}})
    assert r["fallback"] is True
    text = Path(r["path"]).read_text(encoding="utf-8")
    for header in ("## Summary", "## Time breakdown",
                   "## What you actually worked on", "## Notes", "## Gap"):
        assert header in text
    assert "Mwangi" in text   # capture surfaced
    assert "dev"    in text
    assert "work"   in text


def test_daily_fallback_no_evidence_short(env):
    con = _con()
    d = date(2026, 6, 9)
    # nothing seeded
    r = rmod.daily(con, on=d, force_fallback=True, cfg={"paths": {}})
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert "0.0 h tracked" in text or "0.0 h" in text
    assert "## Gap" in text


def test_daily_dry_run_no_file(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    r = rmod.daily(con, on=d, force_fallback=True, cfg={"paths": {}},
                   dry_run=True)
    assert not Path(r["path"]).exists()
    # but skill_run is recorded
    sr = con.execute("SELECT * FROM skill_run WHERE id = ?",
                     (r["skill_run"],)).fetchone()
    assert sr is not None


def test_weekly_fallback_writes_file(env):
    con = _con()
    end = date(2026, 6, 12)
    _seed_today(con, end - timedelta(days=1))
    _seed_today(con, end)
    r = rmod.weekly(con, ending=end, force_fallback=True, cfg={"paths": {}})
    assert r["fallback"] is True
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert "Week of" in text
    for header in ("## Summary", "## Time breakdown",
                   "## Highlights", "## Notes", "## Gap"):
        assert header in text


# ── LLM path ────────────────────────────────────────────────────────────────

def test_daily_llm_path(env, monkeypatch):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    canned = (
        f"# Tuesday, {d.isoformat()}\n\n"
        "## Summary\n\nFocused day on dev + Uganda.\n\n"
        "## Time breakdown\n\n- dev: 0.5 h\n- work: 1.0 h\n\n"
        "## What you actually worked on\n\n- **WorkPulse Substrate** (0.5 h, dev) — atoms.py.\n\n"
        "## Notes\n\n- Mwangi prefers Mt. Elgon framing\n\n"
        "## Gap\n\nNo captures pinned to clusters.\n"
    )
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: (canned, 250, 90, 1.1),
    )
    r = rmod.daily(con, on=d, cfg={"paths": {}})
    assert r["fallback"] is False
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert "Focused day on dev" in text
    ai = con.execute(
        "SELECT prompt_slug, in_tokens FROM ai_call ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert ai["prompt_slug"] == "skill:report-daily"
    assert ai["in_tokens"] == 250


def test_report_records_system_capture(env):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    rmod.daily(con, on=d, force_fallback=True, cfg={"paths": {}})
    row = con.execute(
        "SELECT body FROM capture WHERE author='system' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "Daily report written" in row["body"]


# ── email path ──────────────────────────────────────────────────────────────

def test_email_disabled_returns_false(env):
    ok = rmod.send_report_email("subj", "body", cfg={"email": {"enabled": False}})
    assert ok is False


def test_email_sends_with_monkeypatched_smtp(env, monkeypatch):
    cfg = {"email": {
        "enabled": True,
        "smtp_host": "smtp.example.com",
        "smtp_port": 587,
        "smtp_user": "me@example.com",
        "from_addr": "me@example.com",
        "to_addr":   "me@example.com",
    }}

    sent = {}
    class _FakeSMTP:
        def __init__(self, host, port, timeout):
            sent["host"] = host
            sent["port"] = port
        def __enter__(self): return self
        def __exit__(self, *a): pass
        def starttls(self): sent["tls"] = True
        def login(self, u, p):
            sent["user"] = u
            sent["pass"] = p
        def sendmail(self, frm, to, msg):
            sent["from"] = frm
            sent["to"] = to
            sent["msg"] = msg
    monkeypatch.setattr(rmod.smtplib, "SMTP", _FakeSMTP)

    # Fake the secret loader
    import types
    fake_secrets = types.SimpleNamespace(get=lambda name: "fake-app-password"
                                          if name == "smtp_password" else None)
    monkeypatch.setitem(sys.modules, "workpulse.wp_secrets", fake_secrets)

    ok = rmod.send_report_email("subj", "# hello\n\nbody", cfg=cfg)
    assert ok is True
    assert sent["host"] == "smtp.example.com"
    assert sent["user"] == "me@example.com"
    assert sent["pass"] == "fake-app-password"
    # MIMEText base64-encodes the body; verify headers landed cleanly
    # and the body bytes are present (encoded).
    import base64
    assert "Subject: subj" in sent["msg"]
    assert "From: me@example.com" in sent["msg"]
    assert base64.b64encode(b"# hello\n\nbody").decode() in sent["msg"]


def test_daily_with_email_flag_calls_send(env, monkeypatch):
    con = _con()
    d = date(2026, 6, 9)
    _seed_today(con, d)
    called = {"n": 0}
    def _fake_send(subject, body, cfg=None):
        called["n"] += 1
        called["subject"] = subject
        called["body"] = body
        return True
    monkeypatch.setattr(rmod, "send_report_email", _fake_send)
    r = rmod.daily(con, on=d, force_fallback=True, cfg={"paths": {}},
                   send_email=True)
    assert called["n"] == 1
    assert r["emailed"] is True
    assert d.isoformat() in called["subject"]
