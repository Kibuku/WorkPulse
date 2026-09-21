"""
U5 — output-forms surface (dashboard endpoints).

Run: .venv/bin/python -m pytest tests/test_forms_api.py
"""

from __future__ import annotations

import pytest

from workpulse.core import db, output_forms as of


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


def _client(monkeypatch):
    from workpulse.core import llm
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("none", None))
    from fastapi.testclient import TestClient
    from workpulse.web import app as webapp
    monkeypatch.setattr(webapp, "load_config", lambda: {"paths": {}}, raising=False)
    return TestClient(webapp.app)


def test_forms_list_endpoint(env, monkeypatch):
    con = _con()
    fid = of.create_form(con, "Status Update")
    of.confirm_form(con, fid)
    resp = _client(monkeypatch).get("/api/forms")
    assert resp.status_code == 200
    assert any(f["id"] == fid for f in resp.json()["forms"])


def test_fill_endpoint_returns_markdown(env, monkeypatch):
    con = _con()
    fid = of.create_form(con, "Report")
    of.add_section(con, fid, 1, "Info", "agenda")
    of.confirm_form(con, fid)
    resp = _client(monkeypatch).post(f"/api/forms/{fid}/fill", json={})
    assert resp.status_code == 200
    assert resp.json()["markdown"].startswith("# Report")


def test_fill_unconfirmed_form_rejected_cleanly(env, monkeypatch):
    con = _con()
    fid = of.create_form(con, "Draft")  # candidate, not confirmed
    resp = _client(monkeypatch).post(f"/api/forms/{fid}/fill", json={})
    assert resp.status_code == 400  # a clean rejection, not a 500


def test_fill_unknown_form_rejected_cleanly(env, monkeypatch):
    _con()
    resp = _client(monkeypatch).post("/api/forms/nope/fill", json={})
    assert resp.status_code == 400


# ── CLI surface (U5) ────────────────────────────────────────────────────────────

def test_cli_form_fill_prints_markdown(env, monkeypatch, capsys):
    con = _con()
    from workpulse.common import load_config as real_lc
    monkeypatch.setattr("workpulse.common.load_config", lambda: {"paths": {}})
    from workpulse.core import llm
    monkeypatch.setattr(llm, "_resolve_route", lambda cfg, feat: ("none", None))
    fid = of.create_form(con, "CLI Report")
    of.add_section(con, fid, 1, "Info", "agenda")
    of.confirm_form(con, fid)

    from workpulse import cli
    import argparse
    rc = cli.cmd_form(argparse.Namespace(form_args=["fill", fid]))
    out = capsys.readouterr().out
    assert rc == 0
    assert out.startswith("# CLI Report")

