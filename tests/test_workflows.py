from datetime import datetime, timedelta, timezone

from workpulse.core import atoms, db, workflows


def _at(days_ago: int, hour: int = 9) -> str:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=days_ago)).replace(
        hour=hour, minute=0, second=0, microsecond=0
    ).isoformat()


def test_proposal_method_is_learned_from_observed_journeys(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})

    # Two completed journeys and one held-out working journey. The sensitive
    # names must never be returned by the learner.
    evidence = [
        (12, "/Private/Client Alpha/Terms of Reference.pdf"),
        (11, "/Private/Client Alpha/Financial Proposal.docx"),
        (10, "/Private/Client Alpha/Financial Proposal.xlsx"),
        (9, "/Private/Client Alpha/Financial Proposal v2.xlsx"),
        (8, "/Private/Client Alpha/Financial Proposal FINAL.pdf"),
        (7, "/Private/Student Name/Dissertation Proposal.docx"),
        (6, "/Private/Student Name/Dissertation Proposal REVISED.docx"),
        (5, "/Private/Student Name/Dissertation Proposal FINAL SIGNED.pdf"),
        (2, "/Private/Partner Delta/Programme Concept Note.docx"),
    ]
    for days_ago, path in evidence:
        atoms.write_file_event(
            con, raw_path=path, kind="modified", ts=_at(days_ago)
        )

    candidate = workflows.learn_proposal_method(con)

    assert candidate["enabled"] is True
    assert candidate["status"] == "candidate"
    assert candidate["is_demo"] is False
    assert len(candidate["examples"]) >= 3
    assert len(candidate["steps"]) >= 3
    rendered = str(candidate)
    assert "Client Alpha" not in rendered
    assert "Student Name" not in rendered
    assert "Partner Delta" not in rendered
    assert candidate["held_out"]["label"].startswith("Observed proposal journey")

    confirmed = workflows.confirm_proposal_method(con)
    assert confirmed["status"] == "confirmed"
    assert con.execute(
        "SELECT COUNT(*) FROM workflow_step WHERE method_id=?",
        (workflows.PROPOSAL_METHOD_ID,),
    ).fetchone()[0] == len(candidate["steps"])
    assert con.execute(
        "SELECT is_demo FROM workflow_method WHERE id=?",
        (workflows.PROPOSAL_METHOD_ID,),
    ).fetchone()[0] == 0
