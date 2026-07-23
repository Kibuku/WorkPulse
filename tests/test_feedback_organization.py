from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

import workpulse.web.app as appmod
from workpulse.core import atoms, db, feedback


def _client(tmp_path, monkeypatch):
    cfg = {"paths": {}, "streams": {"school": {"label": "School programme"}},
           "feedback": {"endpoint_url": ""}}
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    monkeypatch.setattr(appmod, "load_config", lambda: cfg)
    monkeypatch.setattr(feedback, "ROOT", tmp_path)
    monkeypatch.setattr(appmod, "_manager_context_path",
                        lambda: tmp_path / "manager_context.json")
    return TestClient(appmod.app), cfg


def test_feedback_due_after_five_hours_and_saved_without_work_data(tmp_path, monkeypatch):
    client, cfg = _client(tmp_path, monkeypatch)
    con = db.connect(cfg)
    start = datetime.now(timezone.utc) - timedelta(hours=6)
    sid = atoms.write_session(con, app="Word", title="Sensitive pupil file",
                              stream="school", started_at=start.isoformat())
    atoms.close_session(con, sid, ended_at=(start + timedelta(hours=5, minutes=1)).isoformat())
    assert client.get("/api/v2/feedback/status").json()["due"] is True

    result = client.post("/api/v2/feedback", json={"answers": {
        "rating": 4, "missing": "Clearer weekly reports",
    }}).json()
    assert result["saved"] is True and result["delivered"] is False
    raw = (tmp_path / "logs" / "feedback.jsonl").read_text(encoding="utf-8")
    assert "Clearer weekly reports" in raw
    assert "Sensitive pupil file" not in raw
    assert client.get("/api/v2/feedback/status").json()["cooldown"] is True


def test_organization_preview_excludes_untagged_and_raw_activity(tmp_path, monkeypatch):
    client, cfg = _client(tmp_path, monkeypatch)
    con = db.connect(cfg)
    # The dashboard's "today" is the user's local date. Around local midnight,
    # UTC may still be yesterday, so construct 09:00 on the local day and then
    # store it as UTC like the sensors do.
    now = datetime.now().astimezone().replace(
        hour=9, minute=0, second=0, microsecond=0).astimezone(timezone.utc)
    for stream, title in (("school", "Confidential assessment"), (None, "Personal browsing")):
        sid = atoms.write_session(con, app="Word", title=title, stream=stream,
                                  started_at=now.isoformat())
        atoms.close_session(con, sid, ended_at=(now + timedelta(hours=1)).isoformat())
    body = client.get("/api/v2/organization/preview").json()
    assert body["total_hours"] == 1.0
    assert [p["key"] for p in body["projects"]] == ["school"]
    rendered = str(body)
    assert "Confidential assessment" not in rendered
    assert "Personal browsing" not in rendered
    assert "URLs" in body["excluded"]


def test_manager_context_is_explicitly_local_only(tmp_path, monkeypatch):
    client, _ = _client(tmp_path, monkeypatch)
    payload = {"priorities": "Complete safeguarding review",
               "expected_outcomes": "Draft ready", "feedback": "Clarify evidence"}
    saved = client.post("/api/v2/organization/context", json=payload).json()
    assert saved["ok"] is True and saved["local_only"] is True
    read = client.get("/api/v2/organization/context").json()
    assert read["priorities"] == payload["priorities"]
    assert read["local_only"] is True
