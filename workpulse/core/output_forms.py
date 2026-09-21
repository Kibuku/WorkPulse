"""
output_forms.py — corpus-grounded output forms (plan).

A form is an adopted output definition (sections + per-section expected evidence
+ a fill mode) that Pulse fills accurately from its corpus: every claim cited,
unmet expectations flagged as gaps, never fabricated. Form/manner authority
stays with the adopted skill; Pulse holds data/accuracy authority.

U1 (this file, first pass): the form registry (schema CRUD + confirm lifecycle).
Later units add import-from-skill, per-section retrieval, and the fill engine.
"""

from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from workpulse.core.atoms import new_id


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def create_form(con: sqlite3.Connection, name: str, *,
                fill_mode: str = "strict", source_skill: str | None = None) -> str:
    """Create a candidate form. Returns its id."""
    fid = new_id()
    con.execute(
        "INSERT INTO output_form(id, name, source_skill, fill_mode, status, "
        "version, created_at) VALUES (?,?,?,?,?,?,?)",
        (fid, name, source_skill, fill_mode, "candidate", 1, _now()))
    con.commit()
    return fid


def add_section(con: sqlite3.Connection, form_id: str, position: int, name: str,
                expected_evidence: str | None = None, *, required: bool = True) -> str:
    sid = new_id()
    con.execute(
        "INSERT INTO output_form_section(id, form_id, position, name, "
        "expected_evidence, required) VALUES (?,?,?,?,?,?)",
        (sid, form_id, position, name, expected_evidence, 1 if required else 0))
    con.commit()
    return sid


def get_form(con: sqlite3.Connection, form_id: str) -> dict | None:
    row = con.execute(
        "SELECT id, name, source_skill, fill_mode, status, version, "
        "created_at, confirmed_at FROM output_form WHERE id = ?",
        (form_id,)).fetchone()
    if row is None:
        return None
    form = dict(row)
    form["sections"] = [dict(s) for s in con.execute(
        "SELECT position, name, expected_evidence, required "
        "FROM output_form_section WHERE form_id = ? ORDER BY position",
        (form_id,)).fetchall()]
    return form


def list_forms(con: sqlite3.Connection, *, status: str | None = None) -> list[dict]:
    if status:
        rows = con.execute(
            "SELECT id, name, status, fill_mode FROM output_form "
            "WHERE status = ? ORDER BY created_at", (status,)).fetchall()
    else:
        rows = con.execute(
            "SELECT id, name, status, fill_mode FROM output_form "
            "ORDER BY created_at").fetchall()
    return [dict(r) for r in rows]


def confirm_form(con: sqlite3.Connection, form_id: str) -> None:
    con.execute(
        "UPDATE output_form SET status = 'confirmed', "
        "confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ?",
        (_now(), form_id))
    con.commit()
