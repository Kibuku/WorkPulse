from pathlib import Path

from datetime import date
from workpulse.core import coach, content_capture, db


def test_redaction_removes_common_sensitive_values():
    text = content_capture.redact(
        "Email me@example.com phone +256 700 123456 password: hunter2 card 4111 1111 1111 1111")
    assert "example.com" not in text
    assert "hunter2" not in text
    assert "4111" not in text


def test_capture_is_opt_in_and_deny_listed(monkeypatch):
    monkeypatch.setattr(content_capture, "_foreground",
                        lambda: ("Online Banking", 12, "Google Chrome"))
    assert content_capture.capture_once({"content_capture": {"enabled": False}})["blocked"]
    result = content_capture.capture_once({"content_capture": {"enabled": True}})
    assert result["blocked"]


def test_ephemeral_capture_is_deleted_and_persists_only_redacted_text(tmp_path, monkeypatch):
    monkeypatch.setattr(content_capture, "_foreground",
                        lambda: ("Proposal draft", 12, "Microsoft Word"))
    seen = {}
    def image(path: Path, pid: int):
        path.write_bytes(b"temporary image")
        seen["path"] = path
    def ocr(path: Path):
        assert path.exists()
        return "Draft proposal for Client A. Email owner@example.com for review.", "test-local"
    cfg = {"content_capture": {"enabled": True, "allow_apps": ["Word"]}}
    result = content_capture.capture_once(cfg, capture_image=image, ocr=ocr)
    assert result["ok"]
    assert "owner@example.com" not in result["redacted_text"]
    assert not seen["path"].exists()

    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    saved = content_capture.persist(con, result)
    row = con.execute("SELECT redacted_text FROM content_capture WHERE id=?", (saved["id"],)).fetchone()
    assert "[EMAIL]" in row[0]

    con.execute("UPDATE content_capture SET ts=?,stage='drafting',redacted_text='TBD owner for final review' WHERE id=?",
                (date.today().isoformat() + "T09:00:00+00:00", saved["id"]))
    message = coach._content_message(con, date.today())
    assert message["kind"] == "content-placeholder"
    assert "TBD" not in message["text"]
