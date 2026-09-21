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


def import_form_from_skill(con: sqlite3.Connection, skill_text: str,
                           cfg: dict, *, source_skill: str = "skill") -> str | None:
    """Propose a candidate form from a skill definition (plan U2, KTD2).

    The model reads the skill's text and returns a form spec
    {name, fill_mode, sections:[{name, expected_evidence}]}; it is stored as a
    candidate for the user to confirm. Needs a provider -- with none, returns
    None (manual authoring stays the keyless path). A malformed response writes
    nothing. The skill text is redacted before it leaves the machine.
    """
    from workpulse.core import llm, content_capture
    backend, _ = llm._resolve_route(cfg, "form_import")
    if backend == "none":
        return None
    prompt = (
        "Read this skill/methodology definition and describe the OUTPUT FORM it "
        "produces. Reply ONLY as JSON: "
        '{"name": str, "fill_mode": "strict"|"full-draft", '
        '"sections": [{"name": str, "expected_evidence": str}]}. '
        "expected_evidence = what data would be needed to fill that section.\n\n"
        + content_capture.redact(skill_text))
    obj, _meta = llm.ask_json(prompt, feature="form_import", cfg=cfg)
    if not isinstance(obj, dict):
        return None
    name = obj.get("name")
    sections = obj.get("sections")
    if not name or not isinstance(sections, list) or not sections:
        return None
    fill_mode = obj.get("fill_mode")
    if fill_mode not in ("strict", "full-draft"):
        fill_mode = "strict"
    fid = create_form(con, name, fill_mode=fill_mode, source_skill=source_skill)
    for i, sec in enumerate(sections, start=1):
        if isinstance(sec, dict) and sec.get("name"):
            add_section(con, fid, i, sec["name"], sec.get("expected_evidence"))
    return fid


def section_evidence(con: sqlite3.Connection, section: dict, *,
                     since: str | None = None, limit: int = 10,
                     with_vector: bool = False) -> list[dict]:
    """Corpus evidence relevant to a form section (plan U3, R4).

    Retrieves via the shared hybrid search using the section's expected
    evidence (falling back to its name) as the query, scoped by ``since``.
    Returned atoms carry their ids for citation and span every corpus kind,
    including parsed document content (content_capture)."""
    from workpulse.core import search
    query = (section.get("expected_evidence") or section.get("name") or "").strip()
    if not query:
        return []
    return search.search(con, query, limit=limit, since=since,
                        with_vector=with_vector)
