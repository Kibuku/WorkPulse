"""
Tests for scripts/projects.py + the capture.py routing hookup.

Verifies:
  - load_projects() reads config/projects.yaml + falls back gracefully
  - resolve_match() finds keywords, aliases, folder patterns
  - Specificity ordering: uganda-memd matches before generic verst-carbon
  - capture() auto-creates `about_stream` edge when body mentions a project
  - capture() propagates stream to a pinned, untagged session
  - capture() never overwrites an existing session stream
  - backfill_captures() is idempotent

Run: python -m pytest tests/test_projects.py
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path


import pytest

from workpulse.core import atoms, db
from workpulse.core import capture as capmod, projects as wp_projects


@pytest.fixture()
def env(tmp_path, monkeypatch):
    db_file = tmp_path / "wp.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)
    from workpulse import common as wp_common
    monkeypatch.setattr(wp_common, "load_config", lambda: {"paths": {}})
    monkeypatch.setattr(capmod, "ROOT", tmp_path)
    def _cap_dir():
        p = tmp_path / "captures"; p.mkdir(parents=True, exist_ok=True)
        return p
    monkeypatch.setattr(capmod, "_captures_dir", _cap_dir)
    # Reset the project cache between tests so any monkeypatched path takes.
    wp_projects._CACHE["mtime"] = 0
    wp_projects._CACHE["projects"] = []
    return tmp_path


def _con():
    return db.connect(cfg={"paths": {}})


# ── resolver: load + match ──────────────────────────────────────────────────

def test_load_projects_returns_real_taxonomy():
    """Real config/projects.yaml committed in the repo loads cleanly."""
    ps = wp_projects.load_projects()
    streams = {p["stream"] for p in ps}
    assert "uganda" in streams
    assert "uganda2" in streams
    assert "dev" in streams


def test_resolve_uganda_memd():
    m = wp_projects.resolve_match(
        "today I want to do the ER schedule for UGANDA MEMD project")
    assert m is not None
    assert m["stream"] == "uganda"
    assert m["label"] == "Uganda MEMD"


def test_resolve_mercy_corps():
    m = wp_projects.resolve_match("met with the mercy corps team")
    assert m["stream"] == "uganda2"


def test_resolve_workpulse_dev():
    m = wp_projects.resolve_match("Built dashboard capture box — WorkPulse")
    assert m["stream"] == "dev"


def test_resolve_alias_mwangi_routes_to_uganda():
    """Mwangi is a Uganda MEMD person; the keyword should route there."""
    m = wp_projects.resolve_match("Mwangi prefers the Mt. Elgon framing")
    assert m["stream"] == "uganda"


def test_resolve_returns_none_on_no_match():
    assert wp_projects.resolve_match("random ungrounded thought") is None
    assert wp_projects.resolve_match("") is None


def test_resolve_specificity_uganda_beats_verst_carbon():
    """uganda-memd is a child of verst-carbon. Resolver should pick uganda
    when uganda keywords are present, not the parent verst-carbon."""
    m = wp_projects.resolve_match("Uganda MEMD methodology under verst carbon")
    assert m["stream"] == "uganda"


def test_resolve_path_mode_uses_folder_patterns():
    m = wp_projects.resolve_match(
        "/Users/g/Documents/uganda-memd/narrative.docx", kind="path")
    assert m["stream"] == "uganda"


def test_load_projects_missing_file(tmp_path, monkeypatch):
    fake = tmp_path / "nope.yaml"
    monkeypatch.setattr(wp_projects, "_DEFAULT_PATH", fake)
    wp_projects._CACHE["mtime"] = 0
    wp_projects._CACHE["projects"] = []
    assert wp_projects.load_projects() == []


# ── capture hookup ──────────────────────────────────────────────────────────

def test_capture_creates_about_stream_edge(env):
    cid = capmod.capture(body="Uganda MEMD ER schedule for biomass cookstove",
                         author="human", cfg={"paths": {}})
    con = _con()
    edge = con.execute(
        "SELECT rel, dst_id FROM edge WHERE src_kind='capture' AND src_id=?",
        (cid,),
    ).fetchone()
    assert edge["rel"] == "about_stream"
    assert edge["dst_id"] == "uganda"


def test_capture_creates_stream_row_lazily(env):
    capmod.capture(body="Mercy Corps stakeholder mapping",
                   author="human", cfg={"paths": {}})
    con = _con()
    row = con.execute(
        "SELECT label FROM stream WHERE key='uganda2'"
    ).fetchone()
    assert row is not None
    assert row["label"] == "Mercy Corps"


def test_capture_propagates_stream_to_untagged_session(env):
    con = _con()
    sid = atoms.write_session(
        con, app="Word", title="narrative_v3.docx", stream=None,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    capmod.capture(
        body="Working on Uganda MEMD narrative methodology section",
        author="human", pinned_kind="session", pinned_id=sid,
        cfg={"paths": {}},
    )
    row = con.execute("SELECT stream FROM session WHERE id=?", (sid,)).fetchone()
    assert row["stream"] == "uganda"


def test_capture_does_not_overwrite_existing_stream(env):
    con = _con()
    sid = atoms.write_session(
        con, app="Word", title="x", stream="dev",  # already tagged
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    capmod.capture(
        body="Uganda MEMD work happening here",
        author="human", pinned_kind="session", pinned_id=sid,
        cfg={"paths": {}},
    )
    row = con.execute("SELECT stream FROM session WHERE id=?", (sid,)).fetchone()
    assert row["stream"] == "dev"  # not overwritten


def test_capture_with_no_match_does_not_create_edge(env):
    cid = capmod.capture(body="completely unrelated thought",
                         author="human", cfg={"paths": {}})
    con = _con()
    n = con.execute(
        "SELECT COUNT(*) AS n FROM edge WHERE src_kind='capture' "
        "AND src_id=? AND rel='about_stream'",
        (cid,),
    ).fetchone()["n"]
    assert n == 0


def test_system_captures_are_not_routed(env):
    """System-author captures (dream cycle traces) should not be routed —
    they're audit, not declared intent."""
    cid = capmod.capture(body="Consolidation written for Uganda MEMD",
                         author="system", cfg={"paths": {}})
    con = _con()
    n = con.execute(
        "SELECT COUNT(*) AS n FROM edge WHERE src_kind='capture' "
        "AND src_id=? AND rel='about_stream'",
        (cid,),
    ).fetchone()["n"]
    assert n == 0


# ── backfill ────────────────────────────────────────────────────────────────

def test_backfill_routes_existing_captures(env):
    """Captures written BEFORE the routing was added should be picked up by
    the backfill pass (and the propagation should land on their sessions)."""
    con = _con()
    sid = atoms.write_session(con, app="Word", title="x", stream=None,
                              started_at=datetime.now(timezone.utc).isoformat())
    # Write a capture directly via atoms.write_capture so the capture()
    # routing hook is bypassed — simulating the pre-fix state.
    cap_id = atoms.write_capture(
        con, body="Uganda MEMD household ERs and improved biomass",
        author="human", pinned_kind="session", pinned_id=sid,
    )
    # Sanity check: no routing happened yet.
    n_before = con.execute(
        "SELECT COUNT(*) FROM edge WHERE rel='about_stream' AND src_id=?",
        (cap_id,),
    ).fetchone()[0]
    assert n_before == 0
    sess_before = con.execute(
        "SELECT stream FROM session WHERE id=?", (sid,)
    ).fetchone()["stream"]
    assert sess_before is None

    counts = wp_projects.backfill_captures(con)
    assert counts["captures_scanned"] >= 1
    assert counts["captures_matched"] >= 1
    assert counts["edges_created"] >= 1
    assert counts["sessions_tagged"] >= 1

    # Post-state
    edge = con.execute(
        "SELECT dst_id FROM edge WHERE rel='about_stream' AND src_id=?",
        (cap_id,),
    ).fetchone()
    assert edge["dst_id"] == "uganda"
    sess_after = con.execute(
        "SELECT stream FROM session WHERE id=?", (sid,)
    ).fetchone()["stream"]
    assert sess_after == "uganda"


def test_backfill_is_idempotent(env):
    con = _con()
    atoms.write_capture(
        con, body="Mercy Corps planning call",
        author="human", pinned_kind=None, pinned_id=None,
    )
    first = wp_projects.backfill_captures(con)
    second = wp_projects.backfill_captures(con)
    assert first["edges_created"] >= 1
    assert second["edges_created"] == 0


def test_resolve_all_extracts_every_project_from_list_capture():
    """The Model B pattern: morning list capture with multiple projects."""
    text = ("Today: Uganda MEMD ER schedule, Mercy Corps stakeholder mapping, "
            "WorkPulse v2 brain build, and Verst Carbon methodology review")
    matches = wp_projects.resolve_all(text)
    streams = [m["stream"] for m in matches]
    assert "uganda" in streams
    assert "uganda2" in streams
    assert "dev" in streams
    assert "verst-carbon" in streams


def test_resolve_all_dedupes_by_stream():
    """Same project mentioned multiple times = one match."""
    text = "Uganda MEMD work — household ERs and improved biomass for Uganda"
    matches = wp_projects.resolve_all(text)
    streams = [m["stream"] for m in matches]
    # Even though uganda, MEMD, household ERs, improved biomass all match,
    # there should be only one entry for stream=uganda
    assert streams.count("uganda") == 1


def test_resolve_all_empty_for_no_match():
    assert wp_projects.resolve_all("random unrelated thought") == []
    assert wp_projects.resolve_all("") == []


def test_resolve_all_returns_in_taxonomy_order():
    """Match order follows the YAML order, not the order projects appear
    in the input text. This keeps results stable across runs."""
    # YAML order: uganda, uganda2, verst-carbon, dev, masters
    text = "WorkPulse build today plus some Mercy Corps emails"
    matches = wp_projects.resolve_all(text)
    streams = [m["stream"] for m in matches]
    # uganda2 should come before dev because uganda2 appears earlier in the YAML
    assert streams.index("uganda2") < streams.index("dev")


def test_capture_list_creates_one_edge_per_project(env):
    con = _con()
    cid = capmod.capture(
        body=("Today: Uganda MEMD ER schedule, Mercy Corps check-in, "
              "WorkPulse v2 brain build"),
        author="human", cfg={"paths": {}},
    )
    edges = list(con.execute(
        "SELECT dst_id FROM edge WHERE src_kind='capture' AND src_id=? "
        "AND rel='about_stream' ORDER BY dst_id",
        (cid,),
    ))
    streams = {e["dst_id"] for e in edges}
    assert streams == {"uganda", "uganda2", "dev"}


def test_capture_list_does_not_propagate_to_pinned_session(env):
    """Multi-project capture cannot pick a single session stream —
    propagation is skipped so a downstream cluster assignment step
    can pick the right one per cluster."""
    con = _con()
    sid = atoms.write_session(
        con, app="Word", title="x", stream=None,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    capmod.capture(
        body="Uganda MEMD ER schedule and Mercy Corps stakeholder review",
        author="human", pinned_kind="session", pinned_id=sid,
        cfg={"paths": {}},
    )
    row = con.execute("SELECT stream FROM session WHERE id=?", (sid,)).fetchone()
    assert row["stream"] is None  # NOT propagated


def test_capture_single_match_still_propagates(env):
    """Single-project capture pinned to an untagged session SHOULD still
    propagate (preserves earlier behavior)."""
    con = _con()
    sid = atoms.write_session(
        con, app="Word", title="x", stream=None,
        started_at=datetime.now(timezone.utc).isoformat(),
    )
    capmod.capture(
        body="Uganda MEMD ER schedule",
        author="human", pinned_kind="session", pinned_id=sid,
        cfg={"paths": {}},
    )
    row = con.execute("SELECT stream FROM session WHERE id=?", (sid,)).fetchone()
    assert row["stream"] == "uganda"


def test_backfill_dry_run_writes_nothing(env):
    con = _con()
    atoms.write_capture(
        con, body="Uganda MEMD ER schedule",
        author="human", pinned_kind=None, pinned_id=None,
    )
    counts = wp_projects.backfill_captures(con, dry_run=True)
    assert counts["captures_matched"] >= 1
    n_edges = con.execute(
        "SELECT COUNT(*) FROM edge WHERE rel='about_stream'"
    ).fetchone()[0]
    assert n_edges == 0
