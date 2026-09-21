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
