"""
Tests for scripts/consolidate.py.

Verifies:
  - Each deterministic finder produces the right shape on seeded data.
  - Fallback markdown contains required section headers and named findings.
  - LLM path is wired (monkeypatched) and records skill_run + ai_call.
  - File is written under consolidation/YYYY-MM-DD.md.
  - --dry-run does not write the file but still records the skill_run.

Run: python -m pytest tests/test_consolidate.py
"""

from __future__ import annotations

import json
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, cluster
from workpulse.core import capture as capmod, consolidate as cmod
from workpulse.core import db, think
from workpulse.core import name_clusters as nc


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    # Redirect the consolidation directory + capture sidecar dir
    monkeypatch.setattr(cmod, "ROOT", tmp_path)
    def _con_dir():
        p = tmp_path / "consolidation"; p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(cmod, "_consolidations_dir", _con_dir)
    monkeypatch.setattr(capmod, "ROOT", tmp_path)
    def _cap_dir():
        p = tmp_path / "captures"; p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(capmod, "_captures_dir", _cap_dir)
    # Resolve the logs dir to tmp_path too so stale_learned_tags has a place
    monkeypatch.setattr(cmod, "resolve",
                        lambda rel: (Path(rel) if Path(rel).is_absolute() else tmp_path / rel))
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _iso(dt: datetime) -> str:
    return dt.isoformat()


# ── tokenization / jaccard ───────────────────────────────────────────────────

def test_tokens_drops_stopwords():
    out = cmod._tokens("the Uganda MEMD draft narrative")
    assert "uganda" in out and "memd" in out and "narrative" in out
    assert "the" not in out
    assert "word" not in out  # app chrome stopword


def test_jaccard_basic():
    a = {"uganda", "memd", "narrative"}
    b = {"uganda", "memd", "draft"}
    assert 0.4 < cmod._jaccard(a, b) < 0.6


# ── load_params ──────────────────────────────────────────────────────────────

def test_load_params_reads_skill_frontmatter():
    p = cmod.load_params()
    assert p["dedup_days_back"] == 7
    assert p["dedup_jaccard_floor"] == 0.40
    assert p["plan_overrun_ratio"] == 2.0


# ── dedup finder ────────────────────────────────────────────────────────────

def test_dedup_finds_overlapping_clusters(env):
    con = _con()
    t0 = datetime(2026, 6, 8, 9, 0, tzinfo=timezone.utc)
    # Cluster A: Uganda narrative work
    for i, title in enumerate(["Uganda MEMD narrative draft"] * 3):
        sid = atoms.write_session(con, app="Word", title=title, stream="work",
                                  started_at=_iso(t0 + timedelta(minutes=i)))
        atoms.close_session(con, sid, ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    # Cluster B: separated by a 4-hour gap → different cluster, same titles
    t1 = t0 + timedelta(hours=4)
    for i, title in enumerate(["Uganda MEMD narrative review"] * 3):
        sid = atoms.write_session(con, app="Word", title=title, stream="work",
                                  started_at=_iso(t1 + timedelta(minutes=i)))
        atoms.close_session(con, sid, ended_at=_iso(t1 + timedelta(minutes=i + 1)))
    cluster.refresh(con)
    nc.name_all(con, force_fallback=True)
    f = cmod.findings(con, as_of=date(2026, 6, 9), cfg={"paths": {}})
    assert len(f["dedup_candidates"]) >= 1
    pair = f["dedup_candidates"][0]
    assert pair["jaccard"] >= 0.4
    assert pair["same_stream"] is True
    assert "uganda" in pair["shared"]


# ── plan vs actual finder ───────────────────────────────────────────────────

def test_plan_vs_actual_flags_overrun(env):
    con = _con()
    plan_d = "2026-06-09"
    con.execute("INSERT OR IGNORE INTO stream(key, label) VALUES ('work', 'Work')")
    con.execute(
        "INSERT INTO plan_item(id, plan_date, name, planned_minutes, done, "
        "stream, section, raw) VALUES ('p1', ?, 'Uganda doc', 30, 0, "
        "'work', 'new', NULL)",
        (plan_d,),
    )
    # Add 3 hours of real activity on that day in the same stream
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    for i in range(180):
        sid = atoms.write_session(con, app="Word", title="Uganda",
                                  stream="work",
                                  started_at=_iso(t0 + timedelta(minutes=i)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    f = cmod.findings(con, as_of=date(2026, 6, 9), cfg={"paths": {}})
    flagged = [it for it in f["plan_vs_actual"] if it["name"] == "Uganda doc"]
    assert flagged and flagged[0]["flag"] == "overrun"
    assert flagged[0]["planned_min"] == 30
    assert flagged[0]["actual_min"] > 30


def test_plan_vs_actual_flags_underrun(env):
    con = _con()
    plan_d = "2026-06-09"
    con.execute("INSERT OR IGNORE INTO stream(key, label) VALUES ('work', 'Work')")
    con.execute(
        "INSERT INTO plan_item(id, plan_date, name, planned_minutes, done, "
        "stream, section, raw) VALUES ('p2', ?, 'Tagged but no time', 60, 0, "
        "'work', 'new', NULL)",
        (plan_d,),
    )
    # 5 minutes of activity
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    sid = atoms.write_session(con, app="Word", title="x", stream="work",
                              started_at=_iso(t0))
    atoms.close_session(con, sid, ended_at=_iso(t0 + timedelta(minutes=5)))
    f = cmod.findings(con, as_of=date(2026, 6, 9), cfg={"paths": {}})
    flagged = [it for it in f["plan_vs_actual"] if it["name"] == "Tagged but no time"]
    assert flagged and flagged[0]["flag"] == "underrun"


# ── untagged buckets ────────────────────────────────────────────────────────

def test_untagged_buckets_groups_by_token(env):
    con = _con()
    t0 = datetime(2026, 6, 9, 9, 0, tzinfo=timezone.utc)
    # 10 sessions of "Mwangi call notes" — 1 minute each
    for i in range(10):
        sid = atoms.write_session(con, app="Notes",
                                  title="Mwangi call notes",
                                  stream=None,
                                  started_at=_iso(t0 + timedelta(minutes=i)))
        atoms.close_session(con, sid,
                            ended_at=_iso(t0 + timedelta(minutes=i + 1)))
    f = cmod.findings(con, as_of=date(2026, 6, 9), cfg={"paths": {}})
    assert any("mwangi" in b["token"] or "mwangi" in [t.lower() for t in b["sample_titles"]][0]
               for b in f["untagged_buckets"])


# ── stale learned tags ──────────────────────────────────────────────────────

def test_stale_learned_tags_picks_old_zero_hit_rules(env):
    (env / "logs" / "learned_tags.json").write_text(
        json.dumps([
            {"pattern": "stale-one",  "stream": "misc", "source": "ai",
             "raw_title": "Stale One",  "created_at": "2026-01-01T00:00:00+03:00",
             "hit_count": 0},
            {"pattern": "active-one", "stream": "dev",  "source": "user",
             "raw_title": "Active One", "created_at": "2026-06-01T00:00:00+03:00",
             "hit_count": 12},
            {"pattern": "old-but-firing", "stream": "work", "source": "ai",
             "raw_title": "Old But Firing", "created_at": "2026-01-01T00:00:00+03:00",
             "hit_count": 50},
        ]),
        encoding="utf-8",
    )
    con = _con()
    f = cmod.findings(con, as_of=date(2026, 6, 9), cfg={"paths": {}})
    patterns = [t["pattern"] for t in f["stale_learned_tags"]]
    assert "stale-one" in patterns
    assert "active-one" not in patterns
    assert "old-but-firing" not in patterns


# ── orchestrator / file write ───────────────────────────────────────────────

def test_consolidate_writes_file_and_records_skill_run(env):
    con = _con()
    result = cmod.consolidate(con, as_of=date(2026, 6, 9),
                              force_fallback=True, cfg={"paths": {}})
    assert result["fallback"] is True
    out_path = Path(result["path"])
    assert out_path.exists()
    text = out_path.read_text(encoding="utf-8")
    for header in ("## Headline", "## Worth your attention",
                   "## Dedup candidates", "## Plan vs actual",
                   "## Untagged time", "## Tag hygiene", "## Gap"):
        assert header in text
    sr = con.execute("SELECT * FROM skill_run WHERE id = ?",
                     (result["skill_run"],)).fetchone()
    assert sr["skill_slug"] == "consolidate"
    assert sr["status"] == "fallback"


def test_consolidate_dry_run_no_file_write(env):
    con = _con()
    result = cmod.consolidate(con, as_of=date(2026, 6, 9),
                              force_fallback=True, cfg={"paths": {}},
                              dry_run=True)
    out_path = Path(result["path"])
    assert not out_path.exists()
    # But the skill_run is still recorded for audit
    sr = con.execute("SELECT * FROM skill_run WHERE id = ?",
                     (result["skill_run"],)).fetchone()
    assert sr is not None


def test_consolidate_llm_path(env, monkeypatch):
    con = _con()
    canned = (
        "## Headline\n\ny\n\n## Worth your attention\n\n- thing\n\n"
        "## Dedup candidates\n\nNo dedup candidates this pass.\n\n"
        "## Plan vs actual\n\nClean.\n\n## Untagged time\n\nQuiet.\n\n"
        "## Tag hygiene\n\nClean.\n\n## Gap\n\nNothing.\n"
    )
    monkeypatch.setattr(
        think, "_call_anthropic",
        lambda prompt, *, model, cfg: (canned, 100, 200, 0.7),
    )
    result = cmod.consolidate(con, as_of=date(2026, 6, 9),
                              cfg={"paths": {}})
    assert result["fallback"] is False
    out_path = Path(result["path"])
    assert out_path.exists()
    assert "## Headline" in out_path.read_text(encoding="utf-8")
    ai = con.execute(
        "SELECT prompt_slug, in_tokens, out_tokens FROM ai_call "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert ai["prompt_slug"] == "skill:consolidate"
    assert ai["in_tokens"] == 100 and ai["out_tokens"] == 200


def test_consolidate_writes_system_capture(env):
    con = _con()
    cmod.consolidate(con, as_of=date(2026, 6, 9),
                     force_fallback=True, cfg={"paths": {}})
    # A system-author capture should have been written by the orchestrator.
    row = con.execute(
        "SELECT author, body FROM capture WHERE author = 'system' "
        "ORDER BY ts DESC LIMIT 1"
    ).fetchone()
    assert row is not None
    assert "Consolidation written" in row["body"]
