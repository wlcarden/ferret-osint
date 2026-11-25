"""The case engine's rules, exercised on a made-up applicant."""

import pytest

from osint_agent.cases import PersonFacts, RawRecord, SourceResult
from osint_agent.cases.engine import MAX_RETURNED_TEXT, name_variants
from tests import cases_fixture as fx


@pytest.fixture
def engine(tmp_path):
    made = fx.make_engine(tmp_path)
    yield made
    made.store.close()


@pytest.fixture
def case_id(engine):
    return fx.open_case(engine)


@pytest.fixture
def gathered(engine, case_id):
    """A case with every source run and nothing judged yet."""
    fx.run_all(engine, case_id)
    return case_id


@pytest.fixture
def judged(engine, gathered):
    fx.judge_all(engine, gathered)
    return gathered


def _claim(evidence_id: str, quote: str, text: str = "A finding.") -> dict:
    return {"text": text, "evidence_id": evidence_id, "quote": quote}


def _result(engine, case, claim: dict) -> dict:
    return engine.verify_claims(case, [claim])["results"][0]


# Opening a case


def test_open_case_refuses_a_basis_the_perspective_does_not_allow(engine):
    refused = engine.open_case("trial", fx.SUBJECT, "public_role")

    assert "cannot be opened on the basis" in refused["error"]
    assert refused["allowed_bases"] == ["consent", "notice"]


def test_open_case_refuses_a_subject_without_a_name(engine):
    assert (
        "needs a name" in engine.open_case("trial", {"name": "  "}, "consent")["error"]
    )


def test_open_case_refuses_an_unknown_perspective(engine):
    assert (
        "no perspective named"
        in engine.open_case("absent", fx.SUBJECT, "consent")["error"]
    )


def test_open_case_accepts_a_subject_with_only_a_name(engine):
    opened = engine.open_case("trial", {"name": "Jordan Whitlock"}, "notice")

    assert opened["case_id"].startswith("case-")


def test_open_case_returns_what_the_review_reports(engine):
    opened = engine.open_case("trial", fx.SUBJECT, "consent")

    assert opened["report"]["include"] == [
        "Court records tied to the subject.",
        "Statements about violence.",
    ]


def test_operations_on_an_unknown_case_say_so(engine):
    assert "no case" in engine.list_sources("case-missing")["error"]
    assert "no case" in engine.render_brief("case-missing")["error"]


# Sources


def test_run_source_stores_records_and_returns_them_as_untrusted_text(engine, case_id):
    result = engine.run_source(case_id, "court_records")

    assert result["status"] == "answered"
    assert [item["evidence_id"] for item in result["evidence"]] == [
        "E1",
        "E2",
        "E3",
        "E4",
    ]
    assert result["evidence"][0]["untrusted_text"].startswith("State of Florida v.")


def test_source_that_raises_is_recorded_as_failed_and_the_review_continues(
    engine, case_id
):
    failed = engine.run_source(case_id, "federal_courts")
    after = engine.run_source(case_id, "news")

    assert failed["status"] == "failed"
    assert "upstream timeout" in failed["note"]
    assert after["status"] == "answered"


def test_source_with_no_adapter_is_reported_as_not_available(engine, case_id):
    assert engine.run_source(case_id, "registry")["status"] == "not_configured"


def test_source_can_only_be_asked_about_the_cases_own_subject(engine, case_id):
    refused = engine.run_source(case_id, "court_records", query="Alex Moreno")

    assert "only be used to search for its own subject" in refused["error"]


def test_name_source_accepts_another_spelling_of_the_subject(engine, case_id):
    assert (
        engine.run_source(case_id, "court_records", query="Whitlock, Jordan")["status"]
        == "answered"
    )


def test_handle_source_refuses_a_handle_the_subject_did_not_list(engine, case_id):
    refused = engine.run_source(case_id, "public_statements", query="someone_else")

    assert "only handles the subject listed" in refused["error"]
    assert refused["listed"] == ["jwhitlock_dayton"]


def test_handle_source_is_empty_when_the_subject_listed_no_handle(engine):
    case = fx.open_case(engine, {"name": "Jordan A. Whitlock"})

    result = engine.run_source(case, "public_statements")

    assert result["status"] == "empty"
    assert "listed no handle" in result["note"]


def test_text_addressed_to_an_automated_reader_is_flagged(engine, case_id):
    result = engine.run_source(case_id, "public_statements")

    flagged = [item["evidence_id"] for item in result["evidence"] if item["flags"]]

    assert flagged == ["E3"]


def test_long_text_is_cut_in_the_reply_but_kept_whole_in_the_store(engine):
    tail = "the final sentence of a very long record"
    long_text = "word " * 1000 + tail
    engine.sources["news"] = lambda query, subject: SourceResult(
        records=[
            RawRecord(
                text=long_text,
                person=PersonFacts(name="Jordan Whitlock", date_of_birth="1988-03-14"),
            )
        ]
    )
    case = fx.open_case(engine)

    shown = engine.run_source(case, "news")["evidence"][0]
    engine.record_judgement(case, "C1", "same_person", "Birth date matches.")

    assert shown["truncated"] is True
    assert len(shown["untrusted_text"]) == MAX_RETURNED_TEXT
    assert _result(engine, case, _claim("E1", tail))["result"] == "verified"


def test_manual_source_returns_what_a_person_should_search_for(engine, case_id):
    asked = engine.run_source(case_id, "state_portal")

    assert asked["status"] == "manual_pending"
    assert asked["needs_a_person"] is True
    assert asked["url"] == "https://courts.example/search"
    assert "Whitlock, Jordan" in asked["search_for"]


def test_manual_result_is_stored_with_who_looked(engine, case_id):
    engine.run_source(case_id, "state_portal")

    stored = engine.record_manual_result(
        case_id,
        "state_portal",
        "R. Ortega",
        [{"text": "No cases found under either spelling."}],
    )

    assert stored["status"] == "answered"
    assert "Checked by R. Ortega" in stored["note"]


def test_manual_result_with_no_records_counts_as_a_search_that_found_nothing(
    engine, case_id
):
    assert (
        engine.record_manual_result(case_id, "state_portal", "R. Ortega")["status"]
        == "empty"
    )


def test_manual_result_needs_a_name_and_a_manual_source(engine, case_id):
    assert (
        "say who" in engine.record_manual_result(case_id, "state_portal", " ")["error"]
    )
    assert (
        "not a manual source"
        in engine.record_manual_result(case_id, "news", "R. Ortega")["error"]
    )


def test_name_variants_cover_the_forms_court_sites_use():
    assert name_variants("jordan a. whitlock") == [
        "Jordan A Whitlock",
        "Jordan Whitlock",
        "Whitlock, Jordan",
        "Jordan A. Whitlock",
    ]


# Identity


def test_candidates_come_with_the_decisions_the_identifiers_allow(engine, gathered):
    candidates = {
        c["candidate_id"]: c for c in engine.list_candidates(gathered)["candidates"]
    }

    assert candidates[fx.C_NAMESAKE]["allowed_decisions"] == [
        "different_person",
        "unsure",
    ]
    assert candidates[fx.C_SUBJECT]["allowed_decisions"] == ["same_person", "unsure"]
    assert candidates[fx.C_UNKNOWN]["allowed_decisions"] == ["unsure"]
    assert candidates[fx.C_HANDLE]["allowed_decisions"] == ["same_person", "unsure"]


def test_record_with_no_second_identifier_cannot_be_ruled_out(engine, gathered):
    """The local-model trial's failure: a different city read as a different person."""
    refused = engine.record_judgement(
        gathered, fx.C_UNKNOWN, "different_person", "Other city."
    )

    assert "not supported by the identifiers" in refused["error"]
    assert refused["allowed_decisions"] == ["unsure"]
    assert "the date of birth shown on the record" in refused["would_settle"]


def test_namesake_cannot_be_judged_the_subject(engine, gathered):
    refused = engine.record_judgement(
        gathered, fx.C_NAMESAKE, "same_person", "Same name."
    )

    assert refused["allowed_decisions"] == ["different_person", "unsure"]


def test_judgement_needs_a_reason_and_a_known_decision(engine, gathered):
    assert (
        "give a reason"
        in engine.record_judgement(gathered, fx.C_SUBJECT, "same_person", " ")["error"]
    )
    assert (
        "not supported"
        in engine.record_judgement(gathered, fx.C_SUBJECT, "probably", "x")["error"]
    )
    assert (
        "no candidate"
        in engine.record_judgement(gathered, "C9", "unsure", "x")["error"]
    )


def test_judgement_lapses_when_new_evidence_changes_the_candidate(engine, judged):
    """A clerk's lookup shows a middle initial; the earlier "unsure" no longer stands."""
    engine.record_manual_result(
        judged,
        "state_portal",
        "R. Ortega",
        [
            {
                "text": "Franklin County case 21-CRB-11902, defendant Jordan T. Whitlock.",
                "person": {
                    "name": "Jordan T. Whitlock",
                    "residence_city": "Columbus",
                    "residence_state": "OH",
                },
            }
        ],
    )

    candidates = {c["candidate_id"]: c for c in engine.list_candidates(judged)["candidates"]}
    unknown = candidates[fx.C_UNKNOWN]

    assert unknown["evidence_ids"] == [fx.E_NO_BIRTH_DATE, "E9"]
    assert unknown["judged_before_new_evidence"] is True
    assert unknown["judgement"] is None
    assert unknown["allowed_decisions"] == ["different_person", "unsure"]


def test_brief_is_refused_again_after_a_judgement_lapses(engine):
    case = fx.complete_review(engine)
    engine.record_manual_result(
        case,
        "state_portal",
        "R. Ortega",
        [
            {
                "text": "Franklin County case 21-CRB-11902, defendant Jordan T. Whitlock.",
                "person": {
                    "name": "Jordan T. Whitlock",
                    "residence_city": "Columbus",
                    "residence_state": "OH",
                },
            }
        ],
    )

    outstanding = engine.render_brief(case)["outstanding"]

    assert outstanding["candidates_without_a_judgement"] == [fx.C_UNKNOWN]


# Claims


def test_claim_with_a_quote_from_the_subjects_record_is_verified(engine, judged):
    assert (
        _result(engine, judged, _claim(fx.E_DISMISSED, fx.DISMISSED_QUOTE))["result"]
        == "verified"
    )


def test_quote_check_ignores_case_and_spacing(engine, judged):
    spaced = "disposition:   DISMISSED\n2016-09-27"

    assert (
        _result(engine, judged, _claim(fx.E_DISMISSED, spaced))["result"] == "verified"
    )


def test_claim_on_an_unsure_candidate_is_kept_as_a_lead(engine, judged):
    assert (
        _result(engine, judged, _claim(fx.E_NO_BIRTH_DATE, fx.ASSAULT_QUOTE))["result"]
        == "unconfirmed"
    )


def test_claim_resting_on_the_namesakes_record_is_rejected(engine, judged):
    result = _result(engine, judged, _claim(fx.E_NAMESAKE, fx.NAMESAKE_QUOTE))

    assert result["result"] == "rejected"
    assert "different person" in result["why"]


@pytest.mark.parametrize(
    ("claim", "why"),
    [
        (_claim("E3", "Disposition: convicted 2016-09-27"), "does not appear"),
        (_claim("E3", "dismissed"), "does not appear"),
        (
            _claim("E99", "Disposition: dismissed 2016-09-27"),
            "no evidence with that id",
        ),
        (_claim("E3", "Disposition: dismissed 2016-09-27", text=" "), "no text"),
        ("not a claim", "needs text"),
    ],
)
def test_claim_that_cannot_be_checked_fails_with_the_reason(engine, judged, claim, why):
    result = _result(engine, judged, claim)

    assert result["result"] == "failed"
    assert why in result["why"]


def test_claim_fails_until_its_candidate_has_a_judgement(engine, gathered):
    result = _result(engine, gathered, _claim(fx.E_DISMISSED, fx.DISMISSED_QUOTE))

    assert result["result"] == "failed"
    assert "no current judgement" in result["why"]


def test_setting_evidence_aside_needs_a_reason(engine, judged):
    assert "give a reason" in engine.dismiss_evidence(judged, fx.E_RANGE, "")["error"]
    assert "no evidence" in engine.dismiss_evidence(judged, "E99", "x")["error"]
    assert engine.dismiss_evidence(judged, fx.E_RANGE, "Ordinary post.") == {
        "set_aside": fx.E_RANGE
    }


# The brief is refused until everything is accounted for


def test_brief_is_refused_while_a_source_has_not_been_tried(engine, case_id):
    engine.run_source(case_id, "court_records")

    refused = engine.render_brief(case_id)

    assert refused["error"] == "the brief is not ready"
    assert "state_portal" in refused["outstanding"]["sources_not_run"]


def test_brief_is_refused_while_a_candidate_has_no_judgement(engine, gathered):
    outstanding = engine.render_brief(gathered)["outstanding"]

    assert outstanding["candidates_without_a_judgement"] == ["C1", "C2", "C3", "C4"]


def test_brief_is_refused_while_a_record_is_neither_cited_nor_set_aside(engine, judged):
    """Quote checking cannot see an omission; this rule can."""
    engine.verify_claims(judged, [_claim(fx.E_DISMISSED, fx.DISMISSED_QUOTE)])

    outstanding = engine.render_brief(judged)["outstanding"]

    assert outstanding == {
        "evidence_neither_cited_nor_set_aside": [
            fx.E_CIVIL,
            fx.E_NO_BIRTH_DATE,
            fx.E_RANGE,
            fx.E_THREAT,
        ]
    }


def test_brief_is_produced_once_everything_is_accounted_for(engine):
    case = fx.complete_review(engine)

    produced = engine.render_brief(case)

    assert "error" not in produced
    assert produced["markdown"].startswith("# Trial review: Jordan A. Whitlock")


def test_every_operation_is_written_to_the_case_log(engine):
    case = fx.complete_review(engine)
    engine.render_brief(case)

    operations = [event["operation"] for event in engine.store.events(case)]

    assert operations[0] == "open_case"
    assert operations[-1] == "render_brief"
    assert {
        "run_source",
        "record_judgement",
        "verify_claims",
        "dismiss_evidence",
    } <= set(operations)
