"""
Corpus-grounded output forms.

U1 — form registry (schema + CRUD). Later units add import, retrieval, fill.

Run: .venv/bin/python -m pytest tests/test_output_forms.py
"""

from __future__ import annotations

import pytest

from workpulse.core import db, output_forms as of


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


# ── schema ──────────────────────────────────────────────────────────────────────

def test_migration_is_idempotent(env):
    con = _con()
    assert db.migrate(con) == 0  # applied on connect; nothing new


def test_tables_exist(env):
    con = _con()
    tables = {r["name"] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}
    assert "output_form" in tables
    assert "output_form_section" in tables


# ── registry CRUD ────────────────────────────────────────────────────────────────

def test_create_and_read_form_with_ordered_sections(env):
    con = _con()
    fid = of.create_form(con, "LSC Engagement Report", fill_mode="strict",
                         source_skill="lsc-report")
    of.add_section(con, fid, 2, "Invitations", "invitation dates and methods")
    of.add_section(con, fid, 1, "Information Made Available", "agenda, NTS")
    form = of.get_form(con, fid)
    assert form["name"] == "LSC Engagement Report"
    assert form["fill_mode"] == "strict"
    assert form["status"] == "candidate"
    # sections returned in position order
    assert [s["name"] for s in form["sections"]] == [
        "Information Made Available", "Invitations"]


def test_confirm_form_flips_status(env):
    con = _con()
    fid = of.create_form(con, "Status Update")
    assert of.get_form(con, fid)["status"] == "candidate"
    of.confirm_form(con, fid)
    form = of.get_form(con, fid)
    assert form["status"] == "confirmed"
    assert form["confirmed_at"] is not None


def test_list_forms_filters_by_status(env):
    con = _con()
    a = of.create_form(con, "A")
    b = of.create_form(con, "B")
    of.confirm_form(con, b)
    confirmed = {f["id"] for f in of.list_forms(con, status="confirmed")}
    assert b in confirmed and a not in confirmed


def test_default_fill_mode_is_strict(env):
    con = _con()
    fid = of.create_form(con, "Default")
    assert of.get_form(con, fid)["fill_mode"] == "strict"


# ── import from skill (U2, R2/KTD2) ─────────────────────────────────────────────

from workpulse.core import llm  # noqa: E402


def _provider(monkeypatch, response):
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    monkeypatch.setattr(llm, "ask_json", lambda *a, **k: (response, {"backend": "glm"}))


def test_import_creates_candidate_form(env, monkeypatch):
    con = _con()
    _provider(monkeypatch, {
        "name": "LSC Engagement Report", "fill_mode": "strict",
        "sections": [
            {"name": "Information Made Available", "expected_evidence": "agenda, NTS"},
            {"name": "Invitations", "expected_evidence": "invitation dates"},
        ]})
    fid = of.import_form_from_skill(con, "# skill: lsc-report\n...", {"llm": {}})
    assert fid is not None
    form = of.get_form(con, fid)
    assert form["status"] == "candidate"
    assert form["source_skill"] is not None
    assert [s["name"] for s in form["sections"]] == [
        "Information Made Available", "Invitations"]


def test_import_no_provider_returns_none(env, monkeypatch):
    con = _con()
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("none", None))
    assert of.import_form_from_skill(con, "skill text", {"llm": {}}) is None
    assert of.list_forms(con) == []  # nothing written


def test_import_malformed_response_writes_nothing(env, monkeypatch):
    con = _con()
    _provider(monkeypatch, None)  # model returned nothing usable
    assert of.import_form_from_skill(con, "skill text", {"llm": {}}) is None
    assert of.list_forms(con) == []


def test_import_redacts_skill_text(env, monkeypatch):
    con = _con()
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("glm", None))
    captured = {}

    def spy(prompt, **k):
        captured["prompt"] = prompt
        return {"name": "F", "sections": [{"name": "S", "expected_evidence": "x"}]}, {}

    monkeypatch.setattr(llm, "ask_json", spy)
    of.import_form_from_skill(con, "contact h.abigaba@energy.go.ug for details",
                              {"llm": {}})
    assert "h.abigaba@energy.go.ug" not in captured["prompt"]


# ── section retrieval (U3, R4) ──────────────────────────────────────────────────

from datetime import datetime, timedelta, timezone  # noqa: E402
from workpulse.core import atoms, search  # noqa: E402


def test_section_evidence_retrieves_matching_atom(env):
    con = _con()
    atoms.write_capture(con, body="The Gulu consultation agenda covered eCooking",
                        ts=datetime.now(timezone.utc).isoformat())
    search.reindex(con)
    section = {"name": "Info", "expected_evidence": "consultation agenda"}
    ev = of.section_evidence(con, section)
    assert any("Gulu consultation" in (a.get("content") or "") for a in ev)


def test_section_no_match_returns_empty(env):
    con = _con()
    atoms.write_capture(con, body="unrelated note about lunch",
                        ts=datetime.now(timezone.utc).isoformat())
    search.reindex(con)
    section = {"name": "X", "expected_evidence": "quantum cryptography"}
    assert of.section_evidence(con, section) == []


def test_section_evidence_respects_since(env):
    con = _con()
    now = datetime.now(timezone.utc)
    atoms.write_capture(con, body="stakeholder engagement register recent",
                        ts=now.isoformat())
    atoms.write_capture(con, body="stakeholder engagement register ancient",
                        ts=(now - timedelta(days=90)).isoformat())
    search.reindex(con)
    section = {"expected_evidence": "stakeholder engagement register"}
    ev = of.section_evidence(con, section, since=(now - timedelta(days=7)).isoformat())
    bodies = " ".join(a.get("content") or "" for a in ev)
    assert "recent" in bodies and "ancient" not in bodies
