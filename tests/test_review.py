from workpulse.core.review import _kind


def test_contradiction_has_highest_meaning():
    kind = _kind(source="agent", confidence=.9, assigned="workpulse",
                 suggested="uganda-memd")
    assert kind and kind[0] == "contradiction"


def test_fallback_needs_assignment():
    kind = _kind(source="fallback", confidence=0, assigned=None,
                 suggested=None)
    assert kind and kind[0] == "unassigned"


def test_low_confidence_agent_decision_is_reviewable():
    kind = _kind(source="agent", confidence=.42, assigned="workpulse",
                 suggested=None)
    assert kind and kind[0] == "low_confidence"


def test_user_confirmed_assignment_stays_out_of_inbox():
    assert _kind(source="user", confidence=1, assigned="workpulse",
                 suggested=None) is None
