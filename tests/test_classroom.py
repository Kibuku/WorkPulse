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


def test_device_pairing_is_one_time_and_heartbeat_attributes_device(tmp_path: Path):
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
    try:
        classroom.enroll_device(pairing["code"], "Another", "Windows", path=vault)
        assert False, "pairing code should be single use"
    except ValueError:
        pass
