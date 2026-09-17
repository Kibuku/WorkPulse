"""
U2 — taxonomy auto-discovery.

discover_projects reads raw session signal and proposes candidate projects with
no hand-authored keywords (plan R2/R3, KTD2/KTD4). It must work with no LLM
(local-first degradation) and must not duplicate candidates on re-run.

Run: .venv/bin/python -m pytest tests/test_discovery.py
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from workpulse.core import atoms, db, discovery


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _now():
    return datetime.now(timezone.utc).isoformat()


def _seed(con, titles, app="Microsoft Word"):
    for t in titles:
        atoms.write_session(con, app=app, title=t, started_at=_now())


MADDS_TITLES = [
    "Verst Carbon_Development of MADDs in Kenya and Zambia_Engagement_Letter",
    "Verst Carbon_MADDs Kenya_Methodology Draft",
    "MADDs Zambia baseline notes",
]
DISS_TITLES = [
    "Dissertation Proposal - GK 083565 (REVISED)",
    "Dissertation chapter 3 - dispatch analysis",
]


def _projects(con):
    return con.execute(
        "SELECT client, name, status, confidence, id FROM project").fetchall()


# ── discovery proposes projects from real signal (AE1) ──────────────────────────

def test_discovers_madds_project_without_keywords(env):
    con = _con()
    _seed(con, MADDS_TITLES)
    discovery.discover_projects(con)
    names = " ".join(r["name"].lower() for r in _projects(con))
    assert "madds" in names


def test_discovered_projects_are_candidates(env):
    con = _con()
    _seed(con, MADDS_TITLES)
    discovery.discover_projects(con)
    rows = _projects(con)
    assert rows and all(r["status"] == "candidate" for r in rows)


# ── local-first degradation: no LLM still names candidates (AE5 partial) ─────────

def test_names_candidates_without_llm(env):
    con = _con()
    _seed(con, MADDS_TITLES + DISS_TITLES)
    cands = discovery.discover_projects(con)
    # conftest disables ollama and no key is set -> deterministic naming path.
    assert cands
    assert all(c["name"].strip() for c in cands)


# ── idempotent re-run: no duplicate candidates ──────────────────────────────────

def test_rerun_does_not_duplicate(env):
    con = _con()
    _seed(con, MADDS_TITLES)
    discovery.discover_projects(con)
    n1 = len(_projects(con))
    discovery.discover_projects(con)
    n2 = len(_projects(con))
    assert n1 == n2


# ── two-level extraction: client from "Client_Project" titles (KTD3) ────────────

def test_extracts_client_from_underscore_titles(env):
    con = _con()
    _seed(con, MADDS_TITLES)
    discovery.discover_projects(con)
    clients = {r["client"] for r in _projects(con) if r["client"]}
    assert any(c and "verst carbon" in c.lower() for c in clients)


# ── app/system titles are not proposed as projects ──────────────────────────────

def test_ignores_app_name_only_titles(env):
    con = _con()
    # Sessions whose window title is just the app name carry no project signal.
    for _ in range(5):
        atoms.write_session(con, app="Terminal", title="Terminal", started_at=_now())
        atoms.write_session(con, app="Finder", title="Finder", started_at=_now())
    discovery.discover_projects(con)
    names = " ".join(r["name"].lower() for r in _projects(con))
    assert "terminal" not in names
    assert "finder" not in names


# ── client extraction rejects hyphenated filename slugs ──────────────────────────

def test_client_rejects_hyphenated_slug(env):
    con = _con()
    _seed(con, [
        "TERMS-OF-REFERENCE-TORS-FOR-COMESA_Carbon Market Framework",
        "TERMS-OF-REFERENCE-TORS-FOR-COMESA_Carbon Market Draft",
    ])
    discovery.discover_projects(con)
    clients = {r["client"] for r in _projects(con) if r["client"]}
    assert not any("terms-of-reference" in (c or "").lower() for c in clients)


# ── singletons below evidence floor are not proposed ────────────────────────────

def test_ignores_low_evidence_singletons(env):
    con = _con()
    _seed(con, ["One-off random note nobody repeats"])
    discovery.discover_projects(con, min_evidence=2)
    assert _projects(con) == []
