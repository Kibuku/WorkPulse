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


def test_generic_method_is_induced_from_repeated_real_sequences(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    con.execute("INSERT INTO stream(key,label) VALUES ('client-delivery','Client Delivery')")
    now = datetime.now(timezone.utc)
    for journey, days_ago in enumerate((4, 2, 0), 1):
        day = (now - timedelta(days=days_ago)).date().isoformat()
        for position, stage in enumerate(("research", "drafting", "review"), 1):
            ts = f"{day}T{8 + position:02d}:00:00+00:00"
            con.execute("""INSERT INTO semantic_observation
            (id,observed_date,kind,semantic_key,stream,summary,confidence,evidence_count,
             source_types,first_seen,last_seen,is_private,status,created_at,updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (f"j{journey}-{stage}", day, "stage", stage, "client-delivery",
             "generic summary", .8, 3, '["session"]', ts, ts, 0, "proposed", ts, ts))
    con.commit()

    learned = workflows.learn_repeated_methods(con)
    assert learned["enabled"] is True
    method = learned["methods"][0]
    assert method["name"] == "Your observed Client Delivery method"
    assert [s["action_type"] for s in method["steps"]] == ["research", "drafting", "review"]
    assert len(method["examples"]) == 3
    assert "proposal" not in str(method).lower()

    confirmed = workflows.confirm_repeated_method(con, method["method_id"])
    assert confirmed["status"] == "confirmed"


def test_generic_learner_refuses_single_journey(tmp_path, monkeypatch):
    monkeypatch.setattr(db, "db_path", lambda cfg=None: tmp_path / "wp.db")
    con = db.connect(cfg={"paths": {}})
    day = datetime.now(timezone.utc).date().isoformat()
    for position, stage in enumerate(("planning", "analysis"), 1):
        ts = f"{day}T{position + 8:02d}:00:00+00:00"
        con.execute("""INSERT INTO semantic_observation
        (id,observed_date,kind,semantic_key,summary,confidence,evidence_count,
         source_types,first_seen,last_seen,is_private,status,created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (stage, day, "stage", stage, "generic", .8, 2, '[]', ts, ts, 0,
         "proposed", ts, ts))
    con.commit()
    assert workflows.learn_repeated_methods(con)["enabled"] is False
