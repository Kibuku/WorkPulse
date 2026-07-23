"""
Tests for workpulse/core/profile.py — the living profile.

Verifies the deterministic finders produce honest packets, the fallback
profile is real (not a placeholder), and the LLM path is wired correctly.

Run: python -m pytest tests/test_profile.py
"""

from __future__ import annotations

import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, profile as pf, cluster, db, llm, name_clusters as nc, think


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    # Redirect profile path to tmp_path
    monkeypatch.setattr(pf, "_PROFILE_PATH", tmp_path / "brain" / "profile.md")
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt):
    return dt.isoformat()


def _seed_week(con, week_start: date):
    """A week of mixed work: 6h dev, 4h work, 1h misc + some captures."""
    base = datetime.combine(week_start, datetime.min.time()).replace(
        hour=10, tzinfo=timezone.utc)
    # Day 1-2: 3h each on dev
    for day in range(2):
        for i in range(180):
            sid = atoms.write_session(
                con, app="Code", title="atoms.py", stream="dev",
                started_at=_iso(base + timedelta(days=day, minutes=i)))
            atoms.close_session(
                con, sid,
                ended_at=_iso(base + timedelta(days=day, minutes=i + 1)))
    # Day 3: 4h on work (narrative)
    for i in range(240):
        sid = atoms.write_session(
            con, app="Word", title="Uganda MEMD narrative", stream="work",
            started_at=_iso(base + timedelta(days=2, minutes=i)))
        atoms.close_session(
            con, sid,
            ended_at=_iso(base + timedelta(days=2, minutes=i + 1)))
    # Day 4: 1h on misc (Outlook)
    for i in range(60):
        sid = atoms.write_session(
            con, app="Microsoft Outlook", title="Inbox", stream="misc",
            started_at=_iso(base + timedelta(days=3, minutes=i)))
        atoms.close_session(
            con, sid,
            ended_at=_iso(base + timedelta(days=3, minutes=i + 1)))
    cluster.refresh(con)
    nc.name_all(con, force_fallback=True)
    # Some captures
    atoms.write_capture(
        con, body="Mwangi prefers the Mt. Elgon framing for Uganda MEMD",
        ts=_iso(base + timedelta(days=2, hours=4)))
    atoms.write_capture(
        con, body="I want to ship WorkPulse v2 by end of month",
        ts=_iso(base + timedelta(days=1, hours=3)))
    atoms.write_capture(
        con, body="Consolidation done", author="system",
        ts=_iso(base + timedelta(days=3, hours=2)))


# ── params ──────────────────────────────────────────────────────────────────

def test_load_params_reads_frontmatter():
    p = pf.load_params()
    assert p["window_days"] == 7
    assert p["min_stream_hours_floor"] == 0.5
    assert p["include_unpinned"] is True


# ── deterministic finders ───────────────────────────────────────────────────

def test_findings_totals(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    assert f["totals"]["tracked_hours"] >= 7.0
    assert f["totals"]["session_count"] >= 600
    assert f["totals"]["capture_count"] == 3
    assert f["totals"]["human_capture_count"] == 2


def test_findings_streams_ranked_by_hours(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    streams = [s["key"] for s in f["streams"]]
    # dev (6h) > work (4h) > misc (1h)
    assert streams[:3] == ["dev", "work", "misc"]


def test_findings_stream_has_dominant_apps(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    dev = next(s for s in f["streams"] if s["key"] == "dev")
    assert "Code" in dev["dominant_apps"]
    work = next(s for s in f["streams"] if s["key"] == "work")
    assert "Word" in work["dominant_apps"]


def test_findings_stream_has_named_clusters(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    work = next(s for s in f["streams"] if s["key"] == "work")
    # The narrative cluster should have a fallback name from name_clusters
    assert any(c.get("name") for c in work["top_clusters"])


def test_findings_captures_in_window(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    bodies = [c["body"] for c in f["captures"]]
    assert any("Mwangi" in b for b in bodies)
    assert any("WorkPulse v2" in b for b in bodies)


def test_findings_stated_goals_detected(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=14, cfg={"paths": {}})
    # "I want to ship WorkPulse v2 by end of month" — should match "want to"
    assert len(f["stated_goals"]) >= 1
    assert any("ship WorkPulse" in g["body"] for g in f["stated_goals"])


def test_findings_trajectory_includes_prior_window(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    f = pf.findings(con, as_of=today, days=7, cfg={"paths": {}})
    assert "deltas" in f["trajectory"]
    assert "prior_window" in f["trajectory"]


def test_open_questions_surfaces_unpinned_captures(env):
    con = _con()
    today = date(2026, 6, 17)
    # Seed 4 unpinned captures
    for i in range(4):
        atoms.write_capture(
            con, body=f"unpinned note {i} — something meaningful and long",
            ts=_iso(datetime(2026, 6, 15, 10, i, tzinfo=timezone.utc)))
    f = pf.findings(con, as_of=today, days=7, cfg={"paths": {}})
    qs = " | ".join(f["open_questions"])
    assert "unpinned" in qs or "aren't pinned" in qs


# ── fallback profile ────────────────────────────────────────────────────────

def test_fallback_profile_writes_a_real_document(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    r = pf.update_profile(con, as_of=today, days=14, force_fallback=True,
                          cfg={"paths": {}})
    assert r["fallback"] is True
    text = Path(r["path"]).read_text(encoding="utf-8")
    # Frontmatter present
    assert text.startswith("---")
    assert "last_updated:" in text
    assert "window:" in text
    # Required sections present
    for header in ("# Profile", "## Identity", "## Streams",
                   "## How you work", "## On your mind",
                   "## Open questions"):
        assert header in text, f"missing {header!r}"
    # The captures should appear by their body in "On your mind"
    assert "Mwangi" in text
    assert "WorkPulse v2" in text


def test_fallback_profile_handles_empty_window(env):
    con = _con()
    today = date(2026, 6, 17)
    # No seed — empty DB
    r = pf.update_profile(con, as_of=today, days=14, force_fallback=True,
                          cfg={"paths": {}})
    text = r["raw"]
    # Should be the "What would help" mode, not a fake-full profile
    assert "What would help" in text
    assert "Streams" not in text  # no streams section when empty
    # Should still have valid frontmatter
    assert text.startswith("---")


def test_fallback_profile_includes_stated_goals_section(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    r = pf.update_profile(con, as_of=today, days=14, force_fallback=True,
                          cfg={"paths": {}})
    text = r["raw"]
    assert "What you've said you want to do" in text
    assert "ship WorkPulse" in text


def test_dry_run_does_not_write(env):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    r = pf.update_profile(con, as_of=today, days=14, force_fallback=True,
                          cfg={"paths": {}}, dry_run=True)
    assert not Path(r["path"]).exists()
    # But skill_run is recorded
    sr = con.execute("SELECT * FROM skill_run WHERE id = ?",
                     (r["skill_run"],)).fetchone()
    assert sr["skill_slug"] == "profile"


# ── LLM path ────────────────────────────────────────────────────────────────

def test_llm_path_writes_file_and_records_ai_call(env, monkeypatch):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    canned = (
        "---\n"
        "last_updated: 2026-06-17T00:00:00+00:00\n"
        "window: 2026-06-04 to 2026-06-17\n"
        "total_tracked_hours: 11\n"
        "---\n\n"
        "# Profile\n\n"
        "## Identity\n\nYou're shipping WorkPulse v2.\n\n"
        "## Streams\n\n### dev (6h)\n\n…\n\n"
        "## How you work\n\n- pattern.\n\n"
        "## On your mind\n\n- thought.\n\n"
        "## Open questions\n\n- q.\n"
    )
    monkeypatch.setattr(
        llm, "ask_text",
        lambda prompt, *, max_tokens, model, cfg: (
            canned, {"backend": "anthropic", "model": model,
                     "input_tokens": 500, "output_tokens": 200,
                     "duration_s": 1.2}),
    )
    r = pf.update_profile(con, as_of=today, days=14, cfg={"paths": {}})
    assert r["fallback"] is False
    text = Path(r["path"]).read_text(encoding="utf-8")
    assert "shipping WorkPulse v2" in text
    ai = con.execute(
        "SELECT prompt_slug, in_tokens FROM ai_call ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert ai["prompt_slug"] == "skill:profile"
    assert ai["in_tokens"] == 500


def test_llm_failure_falls_back(env, monkeypatch):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    monkeypatch.setattr(
        llm, "ask_text",
        lambda prompt, *, max_tokens, model, cfg: (
            None, {"backend": "none", "model": None,
                   "input_tokens": 0, "output_tokens": 0}),
    )
    r = pf.update_profile(con, as_of=today, days=14, cfg={"paths": {}})
    assert r["fallback"] is True


def test_skill_file_appears_in_prompt(env, monkeypatch):
    con = _con()
    today = date(2026, 6, 17)
    _seed_week(con, today - timedelta(days=6))
    captured = {}
    def _fake(prompt, *, max_tokens, model, cfg):
        captured["prompt"] = prompt
        return (("---\nlast_updated: x\nwindow: x\ntotal_tracked_hours: 1\n"
                "---\n\n# Profile\n\n## Identity\n\nx\n\n## Streams\n\n\n\n"
                "## How you work\n\n\n\n## On your mind\n\n\n\n"
                "## Open questions\n\n"),
                {"backend": "ollama", "model": "test-local",
                 "input_tokens": 1, "output_tokens": 1, "duration_s": 0.1})
    monkeypatch.setattr(llm, "ask_text", _fake)
    pf.update_profile(con, as_of=today, days=14, cfg={"paths": {}})
    p = captured["prompt"]
    assert "WINDOW:" in p and "TOTALS:" in p and "STREAMS:" in p
    # Skill body markers
    assert "Output contract" in p or "Profile" in p
