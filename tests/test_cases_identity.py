"""Identity rules: which decisions the identifiers support, and how records group."""

import pytest

from osint_agent.cases.identity import (
    allowed_decisions,
    compare,
    group_into_candidates,
    parse_name,
    state_code,
    what_would_settle,
)
from osint_agent.cases.models import Decision, EvidenceRecord, PersonFacts

SUBJECT = PersonFacts(
    name="Jordan A. Whitlock",
    date_of_birth="1988-03-14",
    residence_city="Dayton",
    residence_state="Ohio",
)
SAME, DIFFERENT, UNSURE = (
    Decision.SAME_PERSON,
    Decision.DIFFERENT_PERSON,
    Decision.UNSURE,
)


@pytest.mark.parametrize(
    ("name", "expected"),
    [
        ("Jordan A. Whitlock", ("jordan", "a", "whitlock")),
        ("WHITLOCK, JORDAN A", ("jordan", "a", "whitlock")),
        ("Jordan Whitlock Jr.", ("jordan", "", "whitlock")),
        ("Jordan Alexander Lee Whitlock", ("jordan", "alexander lee", "whitlock")),
        ("Whitlock", ("", "", "whitlock")),
        ("", ("", "", "")),
    ],
)
def test_parse_name_handles_common_forms(name, expected):
    assert tuple(parse_name(name)) == expected


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("Ohio", "OH"),
        ("oh", "OH"),
        ("D.C.", "DC"),
        ("District of Columbia", "DC"),
        (None, None),
        ("  ", None),
    ],
)
def test_state_code_accepts_names_and_codes(value, expected):
    assert state_code(value) == expected


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        # A second identifier that matches allows "same person".
        (
            PersonFacts(name="Jordan Whitlock", date_of_birth="1988-03-14"),
            [SAME, UNSURE],
        ),
        (
            PersonFacts(
                name="Jordan A Whitlock", residence_city="dayton", residence_state="OH"
            ),
            [SAME, UNSURE],
        ),
        # An identifier that conflicts allows "different person".
        (
            PersonFacts(name="Jordan Whitlock", date_of_birth="1961-07-02"),
            [DIFFERENT, UNSURE],
        ),
        (PersonFacts(name="Jordan R. Whitlock"), [DIFFERENT, UNSURE]),
        # A different city or state is not a conflict: people move.
        (
            PersonFacts(
                name="Jordan Whitlock",
                residence_city="Columbus",
                residence_state="Ohio",
            ),
            [UNSURE],
        ),
        (
            PersonFacts(
                name="Jordan Whitlock",
                residence_city="Tampa",
                residence_state="Florida",
            ),
            [UNSURE],
        ),
        # A name alone, or one weak match, supports nothing.
        (PersonFacts(name="Jordan Whitlock"), [UNSURE]),
        (PersonFacts(name="Jordan A. Whitlock"), [UNSURE]),
        (
            PersonFacts(
                name="Jordan Whitlock", residence_city="Dayton", residence_state="Ohio"
            ),
            [UNSURE],
        ),
        # A matching birth date outweighs a middle initial that differs.
        (
            PersonFacts(name="Jordan R. Whitlock", date_of_birth="1988-03-14"),
            [SAME, UNSURE],
        ),
    ],
)
def test_allowed_decisions_follow_the_identifiers(record, expected):
    assert allowed_decisions(compare(SUBJECT, record)) == expected


def test_subject_with_only_a_name_can_only_produce_unsure():
    name_only = PersonFacts(name="Jordan Whitlock")
    record = PersonFacts(
        name="Jordan R. Whitlock",
        date_of_birth="1961-07-02",
        residence_city="Tampa",
        residence_state="Florida",
    )

    assert allowed_decisions(compare(name_only, record)) == [UNSURE]


def test_identifier_the_subject_listed_allows_same_person():
    comparison = compare(
        SUBJECT, PersonFacts(name=""), stated_identifier="jwhitlock_dayton"
    )

    assert allowed_decisions(comparison) == [SAME, UNSURE]


def test_what_would_settle_names_the_missing_identifier_on_each_side():
    record_without_birth_date = PersonFacts(
        name="Jordan Whitlock", residence_state="Ohio"
    )
    subject_without_birth_date = PersonFacts(name="Jordan A. Whitlock")
    record_with_birth_date = PersonFacts(
        name="Jordan A. Whitlock", date_of_birth="1988-03-14"
    )

    from_record = what_would_settle(compare(SUBJECT, record_without_birth_date))
    from_subject = what_would_settle(
        compare(subject_without_birth_date, record_with_birth_date)
    )

    assert "the date of birth shown on the record" in from_record
    assert "the subject's date of birth" in from_subject


def test_what_would_settle_is_empty_once_a_decision_is_supported():
    decided = compare(
        SUBJECT, PersonFacts(name="Jordan Whitlock", date_of_birth="1961-07-02")
    )

    assert what_would_settle(decided) == []


def _record(evidence_id: str, **person) -> EvidenceRecord:
    facts = PersonFacts(**person) if person else None
    return EvidenceRecord(
        evidence_id=evidence_id, source="test", text="text", person=facts
    )


def test_records_group_only_when_they_share_an_identifier_and_nothing_conflicts():
    records = [
        _record(
            "E1",
            name="Jordan R. Whitlock",
            date_of_birth="1961-07-02",
            residence_city="Tampa",
            residence_state="FL",
        ),
        _record(
            "E2",
            name="Jordan A. Whitlock",
            residence_city="Dayton",
            residence_state="OH",
        ),
        _record(
            "E3",
            name="Jordan Whitlock",
            date_of_birth="1988-03-14",
            residence_city="Dayton",
            residence_state="Ohio",
        ),
        _record(
            "E4",
            name="Jordan Whitlock",
            residence_city="Columbus",
            residence_state="Ohio",
        ),
        _record(
            "E5",
            name="Jordan R. Whitlock",
            residence_city="Tampa",
            residence_state="Florida",
        ),
    ]

    groups = [candidate.evidence_ids for candidate in group_into_candidates(records)]

    assert groups == [["E1", "E5"], ["E2", "E3"], ["E4"]]


def test_records_from_a_listed_handle_form_their_own_candidate():
    post = EvidenceRecord(
        evidence_id="E1", source="posts", text="a post", stated_identifier="jw"
    )
    other = EvidenceRecord(
        evidence_id="E2", source="posts", text="another", stated_identifier="JW"
    )

    candidates = group_into_candidates([post, other])

    assert [c.evidence_ids for c in candidates] == [["E1", "E2"]]
    assert candidates[0].stated_identifier == "jw"


def test_candidate_numbering_is_stable_when_evidence_is_added():
    first = [_record("E1", name="Jordan Whitlock", date_of_birth="1988-03-14")]
    later = first + [_record("E2", name="Jordan Whitlock", date_of_birth="1961-07-02")]

    before = group_into_candidates(first)[0]
    after = group_into_candidates(later)[0]

    assert (before.candidate_id, before.evidence_ids) == (
        after.candidate_id,
        after.evidence_ids,
    )
