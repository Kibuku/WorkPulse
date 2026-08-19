from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from workpulse.core import atoms, db, semantics
from workpulse.signals import browser_tracker
from workpulse.web import app as appmod


def _at(minutes: int) -> str:
    return (datetime.now(timezone.utc) - timedelta(minutes=minutes)).isoformat()


def _session(con, *, app: str, title: str, minutes: int, stream="client"):
    sid = atoms.write_session(
        con, app=app, title=title, stream=stream, started_at=_at(minutes))
    atoms.close_session(con, sid, ended_at=_at(minutes - 4))
    return sid


def test_semantic_refresh_derives_generic_stages_without_raw_text(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    _session(con, app="Word", title="Secret Client Proposal Draft.docx", minutes=30)
    _session(con, app="Word", title="Secret Client Proposal v2.docx", minutes=20)
    atoms.write_file_event(
        con, raw_path="/Private/Secret Client/Proposal Draft.docx",
        kind="modified", ts=_at(10))

    counts = semantics.refresh(con)
    result = semantics.observations(con)

    assert counts["stages"] >= 1
    assert any(item["semantic_key"] == "drafting" for item in result["items"])
    rendered = str(result)
    assert "Secret Client" not in rendered
    assert "Proposal Draft.docx" not in rendered
    assert con.execute("SELECT COUNT(*) FROM semantic_evidence_local").fetchone()[0] >= 2


def test_semantics_detects_context_switching_and_accepts_correction(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    apps = ["Chrome", "Word", "Outlook", "Excel", "Chrome", "Word", "Teams"]
    for index, app in enumerate(apps):
        _session(con, app=app, title=f"Work item {index}", minutes=28 - index * 3)

    semantics.refresh(con)
    item = next(x for x in semantics.observations(con)["items"]
                if x["semantic_key"] == "context-switching")
    semantics.set_status(con, item["id"], "confirmed")
    assert con.execute(
        "SELECT status FROM semantic_observation WHERE id=?", (item["id"],)
    ).fetchone()[0] == "confirmed"


def test_semantics_api_returns_and_dismisses_observation(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(appmod, "load_config", lambda: {"paths": {}})
    monkeypatch.setattr(appmod.product_identity, "has", lambda capability: True)
    con = db.connect(cfg={"paths": {}})
    _session(con, app="Word", title="Quarterly report draft.docx", minutes=20)
    _session(con, app="Word", title="Quarterly report working copy.docx", minutes=10)
    semantics.refresh(con)

    client = TestClient(appmod.app)
    body = client.get("/api/v2/semantics").json()
    assert body["items"]
    oid = body["items"][0]["id"]
    response = client.patch(f"/api/v2/semantics/{oid}", json={"status": "dismissed"})
    assert response.status_code == 200
    assert all(item["id"] != oid for item in client.get("/api/v2/semantics").json()["items"])


def test_private_semantics_stay_hidden_until_unlocked(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    _session(con, app="Chrome", title="Personal report draft", minutes=20, stream="personal")
    _session(con, app="Word", title="Personal report working copy", minutes=10, stream="personal")
    semantics.refresh(con)
    assert semantics.observations(con, include_private=False)["items"] == []
    assert semantics.observations(con, include_private=True)["items"]


def test_chrome_visit_is_semantic_evidence_without_exposing_url(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(browser_tracker.wp_projects, "classify_url",
                        lambda url: {"tier": "project", "stream": "client"})
    con = db.connect(cfg={"paths": {}})
    for minutes in (12, 6):
        browser_tracker.record_visit(con, {
            "app": "Google Chrome",
            "url": "https://private.example/research/client-alpha",
            "title": "Client Alpha research references",
        }, ts=_at(minutes))
    semantics.refresh(con)
    result = semantics.observations(con)
    research = next(x for x in result["items"] if x["semantic_key"] == "research")
    assert "browser_visit" in research["source_types"]
    assert "private.example" not in str(result)
    assert "Client Alpha" not in str(result)
