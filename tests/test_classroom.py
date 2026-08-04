from pathlib import Path
from datetime import datetime, timedelta, timezone

from workpulse.core import classroom


def test_class_session_persists_and_restores_normal(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    started = classroom.start_session(
        mode="class", title="Research Methods", duration_minutes=60, path=vault
    )
    assert started["mode"] == "class"
    assert started["active_session"]["title"] == "Research Methods"
    ended = classroom.end_session(path=vault)
    assert ended["mode"] == "normal"
    assert ended["active_session"] is None


def test_class_policy_flags_unapproved_domain(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    classroom.start_session(mode="class", title="Geography", path=vault)
    result = classroom.evaluate_signal(
        {
            "source_ref": "browser:test1",
            "ts": "2026-07-29T10:00:00+00:00",
            "app": "Google Chrome",
            "title": "Social",
            "domain": "facebook.com",
        },
        path=vault,
    )
    assert result["decision"] == "policy_event"
    assert result["event"]["action"] == "flagged"
    assert result["event"]["device_id"] == "Device 01"
    assert len(classroom.session_status(path=vault)["events"]) == 1


def test_class_policy_allows_approved_domain(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    classroom.start_session(mode="class", title="Research Methods", path=vault)
    result = classroom.evaluate_signal(
        {
            "source_ref": "browser:test2",
            "ts": "2026-07-29T10:00:00+00:00",
            "app": "Google Chrome",
            "title": "Scholar",
            "domain": "scholar.google.com",
        },
        path=vault,
    )
    assert result["decision"] == "allowed"
    assert classroom.session_status(path=vault)["events"] == []


def test_exam_policy_flags_unapproved_application(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    classroom.start_session(mode="exam", title="Exam", path=vault)
    result = classroom.evaluate_signal(
        {
            "source_ref": "session:test3",
            "ts": "2026-07-29T10:00:00+00:00",
            "app": "Microsoft Word",
            "title": "Notes",
            "domain": "",
        },
        path=vault,
    )
    assert result["decision"] == "policy_event"
    assert result["event"]["severity"] == "urgent"


def test_duplicate_event_is_suppressed(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    classroom.start_session(mode="class", title="Class", path=vault)
    signal = {
        "source_ref": "browser:test4",
        "ts": "2026-07-29T10:00:00+00:00",
        "app": "Google Chrome",
        "title": "Social",
        "domain": "facebook.com",
    }
    classroom.evaluate_signal(signal, path=vault)
    classroom.evaluate_signal(signal, path=vault)
    events = classroom.session_status(path=vault)["events"]
    assert len(events) == 1
    assert events[0]["occurrence_count"] == 2


def test_future_session_is_scheduled_without_replacing_normal_mode(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    status = classroom.start_session(
        mode="class",
        title="Scheduled seminar",
        starts_at=future,
        duration_minutes=45,
        path=vault,
    )
    assert status["mode"] == "normal"
    assert status["active_session"] is None
    assert status["scheduled_session"]["title"] == "Scheduled seminar"


def test_saved_policy_is_used_by_next_session(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    classroom.save_policy("class", {
        "allowed_domains": ["example.edu"],
        "allowed_apps": ["Safari"],
        "blocked_domains": [],
        "blocked_apps": [],
    }, path=vault)
    status = classroom.start_session(mode="class", title="Seminar", path=vault)
    assert status["active_session"]["policy"]["allowed_domains"] == ["example.edu"]


def test_class_invitation_enrolls_multiple_devices_and_attributes_heartbeat(tmp_path: Path):
    vault = tmp_path / "classroom.db"
    pairing = classroom.create_pairing(path=vault)
    enrolled = classroom.enroll_device(
        pairing["code"], "Lab Laptop", "macOS", path=vault
    )
    classroom.start_session(mode="class", title="Seminar", path=vault)
    result = classroom.device_heartbeat(enrolled["token"], {
        "app": "Steam", "title": "Steam", "domain": "", "source_ref": "agent:1"
    }, path=vault)
    assert result["decision"]["event"]["device_id"] == enrolled["device_id"]
    assert classroom.devices(path=vault)[0]["name"] == "Lab Laptop"
    second = classroom.enroll_device(
        pairing["code"], "Another", "Windows", path=vault
    )
    assert second["device_id"] != enrolled["device_id"]
    assert {device["name"] for device in classroom.devices(path=vault)} == {
        "Lab Laptop", "Another",
    }


def test_console_status_excludes_synthetic_devices(monkeypatch):
    import workpulse.web.app as appmod

    monkeypatch.setattr(appmod.classroom_core, "session_status", lambda: {
        "mode": "class",
        "active_session": {"id": "session-1"},
        "scheduled_session": None,
        "events": [
            {"device_id": "Device 01", "rule": "old prototype event"},
            {"device_id": "Device LIVE", "rule": "live event"},
        ],
    })
    monkeypatch.setattr(appmod.classroom_core, "devices", lambda: [{
        "id": "Device LIVE",
        "name": "Lab Laptop",
        "platform": "Windows",
        "last_seen": "2026-07-30T16:00:00+00:00",
        "last_app": "Microsoft Word",
        "last_title": "Assignment.docx",
        "last_domain": "",
    }])

    status = appmod.api_classroom_status()

    assert [event["device_id"] for event in status["events"]] == ["Device LIVE"]
    assert status["latest_signal"]["device_name"] == "Lab Laptop"
    assert status["latest_signal"]["app"] == "Microsoft Word"


def test_classroom_invitation_accepts_private_gateway_only():
    import workpulse.web.app as appmod

    assert appmod._validate_classroom_server("http://10.10.1.80:5722") == \
        "http://10.10.1.80:5722"

    for unsafe in (
        "https://10.10.1.80:5722",
        "http://10.10.1.80:9999",
        "http://example.com:5722",
        "http://8.8.8.8:5722",
    ):
        try:
            appmod._validate_classroom_server(unsafe)
            assert False, unsafe
        except ValueError:
            pass


def test_local_join_enrolls_and_starts_agent(monkeypatch):
    from fastapi.testclient import TestClient
    import workpulse.classroom_agent as agent
    import workpulse.web.app as appmod

    calls = {}

    def fake_enroll(server, code, name):
        calls["enroll"] = (server, code, name)
        return {"device_id": "Device ABCD", "device_name": name, "token": "secret"}

    monkeypatch.setattr(agent, "enroll", fake_enroll)
    monkeypatch.setattr(agent, "start_background",
                        lambda: {"running": True, "started": True})

    response = TestClient(appmod.app).post(
        "/api/v2/classroom/local-agent/join",
        json={
            "server": "http://10.10.1.80:5722",
            "code": "vqj-324",
            "name": "Library Laptop",
        },
    )

    assert response.status_code == 200
    assert calls["enroll"] == (
        "http://10.10.1.80:5722", "VQJ-324", "Library Laptop"
    )
    assert response.json()["running"] is True


def test_declared_exercise_and_ai_rule_are_persisted(tmp_path: Path):
    vault = tmp_path / "learning.db"
    status = classroom.start_session(
        mode="class",
        title="Research Methods",
        exercise="Compare two sampling approaches",
        learning_goal="Defend a suitable sampling method",
        ai_use="brainstorm",
        path=vault,
    )
    session = status["active_session"]
    assert session["exercise"] == "Compare two sampling approaches"
    assert session["learning_goal"] == "Defend a suitable sampling method"
    assert session["ai_use"] == "brainstorm"


def test_prohibited_ai_use_creates_factual_policy_event(tmp_path: Path):
    vault = tmp_path / "learning.db"
    classroom.start_session(
        mode="class", title="Independent analysis", ai_use="prohibited", path=vault
    )
    result = classroom.evaluate_signal({
        "app": "Google Chrome", "domain": "chatgpt.com", "title": "ChatGPT",
        "device_id": "Device TEST", "source_ref": "browser:ai",
    }, path=vault)
    assert result["decision"] == "policy_event"
    assert "AI use is not permitted" in result["rule"]


def test_facilitator_support_reaches_device_and_report(tmp_path: Path):
    vault = tmp_path / "learning.db"
    pairing = classroom.create_pairing(path=vault)
    enrolled = classroom.enroll_device(pairing["code"], "Lab 12", "Windows", path=vault)
    status = classroom.start_session(
        mode="class", title="Data analysis", exercise="Clean the dataset", path=vault
    )
    heartbeat = classroom.device_heartbeat(enrolled["token"], {
        "app": "Microsoft Excel", "title": "survey-data.xlsx", "domain": "",
    }, path=vault)
    assert heartbeat["decision"]["decision"] == "policy_event"
    sent = classroom.send_intervention(
        enrolled["device_id"], "Check the missing-value column first", path=vault
    )
    second = classroom.device_heartbeat(enrolled["token"], {
        "app": "Microsoft Excel", "title": "survey-data.xlsx", "domain": "",
    }, path=vault)
    assert second["status"]["interventions"][0]["message"] == \
        "Check the missing-value column first"
    classroom.acknowledge_intervention(enrolled["token"], sent["id"], path=vault)
    report = classroom.session_report(status["active_session"]["id"], path=vault)
    assert report["summary"]["devices_observed"] == 1
    assert report["summary"]["facilitator_actions"] == 1
    assert report["summary"]["acknowledged_actions"] == 1
    assert report["interventions"][0]["device_name"] == "Lab 12"


def test_product_entry_routes_are_separate():
    from fastapi.testclient import TestClient
    import workpulse.web.app as appmod

    client = TestClient(appmod.app)
    assert client.get("/personal").status_code == 200
    assert client.get("/learning").status_code == 200
    source = client.get("/learning").text
    assert "learning-product-nav" in source


def test_learning_retention_removes_heavy_evidence_but_keeps_session_marker(tmp_path: Path):
    vault = tmp_path / "learning.db"
    pairing = classroom.create_pairing(path=vault)
    enrolled = classroom.enroll_device(pairing["code"], "Lab 2", "Windows", path=vault)
    status = classroom.start_session(
        mode="class", title="Methods", exercise="Draft a sampling plan", path=vault
    )
    classroom.device_heartbeat(enrolled["token"], {
        "app": "Steam", "title": "Private window title", "source_ref": "agent:raw"
    }, path=vault)
    future = datetime.now(timezone.utc) + timedelta(days=101)
    result = classroom.apply_retention(now=future, path=vault)
    assert result["raw_evidence_redacted"] == 1
    assert result["events_expired"] == 1
    report = classroom.session_report(status["active_session"]["id"], path=vault)
    assert report["session"]["exercise"] == "Draft a sampling plan"
    assert report["events"] == []
