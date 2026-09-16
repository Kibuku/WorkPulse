"""
U6 — idempotent backfill + orchestration (plan R8/R9, AE3/AE5).

run_attribution_pass ties discovery + session attribution + observation
attribution into one pass that is safe to run over the whole backlog and again:
identical result on re-run, and resumable after a mid-pass failure with the
database left consistent.

Run: .venv/bin/python -m pytest tests/test_attribution_pass.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, attribution, db, discovery


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seed_attributable(con, n):
    # n sessions that clearly belong to one discoverable project.
    for i in range(n):
        atoms.write_session(con, app="Word",
                            title=f"Verst Carbon_MADDs Kenya note {i}",
                            started_at=_now())


def _attributed_count(con):
    return con.execute(
        "SELECT COUNT(*) c FROM session WHERE project_id IS NOT NULL").fetchone()["c"]


# ── the pass attributes the backlog, and is idempotent (AE3) ─────────────────────

def test_pass_attributes_and_rerun_is_stable(env):
    con = _con()
    _seed_attributable(con, 6)
    s1 = attribution.run_attribution_pass(con)
    n1 = _attributed_count(con)
    s2 = attribution.run_attribution_pass(con)
    n2 = _attributed_count(con)
    assert n1 > 0
    assert n1 == n2                       # identical attribution set
    assert s2["sessions"]["attributed"] == 0  # nothing new to do second time


# ── a mid-pass failure leaves committed progress and is resumable (AE5) ──────────

def test_pass_resumable_after_midway_failure(env, monkeypatch):
    con = _con()
    _seed_attributable(con, 6)
    discovery.discover_projects(con)  # taxonomy ready

    calls = {"n": 0}
    real_best = attribution._best

    def flaky_best(*a, **k):
        calls["n"] += 1
        if calls["n"] == 4:              # fail during the second batch
            raise RuntimeError("simulated backend outage")
        return real_best(*a, **k)

    monkeypatch.setattr(attribution, "_best", flaky_best)
    with pytest.raises(RuntimeError):
        attribution.attribute_all(con, batch_size=2)

    partial = _attributed_count(con)
    assert partial >= 2                  # first committed batch survived

    monkeypatch.setattr(attribution, "_best", real_best)
    attribution.attribute_all(con, batch_size=2)  # resume
    assert _attributed_count(con) == 6


# ── the pass also attributes observations ───────────────────────────────────────

def test_pass_attributes_observations(env):
    con = _con()
    sid = atoms.write_session(con, app="Word", title="Verst Carbon_MADDs Kenya",
                              started_at=_now())
    _ = _seed_attributable(con, 3)
    con.execute(
        "INSERT INTO semantic_observation(id, observed_date, kind, semantic_key, "
        "summary, confidence, evidence_count, source_types, first_seen, last_seen, "
        "is_private, status, created_at, updated_at) "
        "VALUES ('obs1','2026-09-16','stage','analysis','x',0.9,1,'[\"session\"]',?,?,0,"
        "'proposed',?,?)", (_now(), _now(), _now(), _now()))
    con.execute(
        "INSERT INTO semantic_evidence_local(observation_id, source_kind, source_id) "
        "VALUES ('obs1','session',?)", (sid,))
    attribution.run_attribution_pass(con)
    op = con.execute("SELECT project_id FROM semantic_observation WHERE id='obs1'").fetchone()["project_id"]
    assert op is not None
