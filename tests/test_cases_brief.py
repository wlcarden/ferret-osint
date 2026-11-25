"""The brief: what it must show, and what it must never contain."""

import json

import pytest

from osint_agent.cases.brief import NO_VERDICT
from tests import cases_fixture as fx


@pytest.fixture
def engine(tmp_path):
    made = fx.make_engine(tmp_path)
    yield made
    made.store.close()


@pytest.fixture
def brief(engine):
    return engine.render_brief(fx.complete_review(engine))


def _section(markdown: str, heading: str) -> str:
    """The text of one section, up to the next heading."""
    start = markdown.index(f"## {heading}")
    end = markdown.find("\n## ", start + 1)
    return markdown[start : end if end != -1 else len(markdown)]


def test_brief_states_that_it_gives_no_verdict(brief):
    assert NO_VERDICT in brief["markdown"]


def test_brief_has_no_place_for_a_verdict_score_or_recommendation(brief):
    keys = json.dumps(brief["brief"]).lower()

    assert not any(
        word in keys
        for word in ('"verdict"', '"score"', '"rating"', '"recommendation"')
    )


def test_confirmed_findings_carry_the_exact_quote_and_its_source(brief):
    confirmed = _section(brief["markdown"], "Confirmed findings")

    assert f'Quote: "{fx.THREAT_QUOTE}"' in confirmed
    assert f"public_statements, {fx.E_THREAT}" in confirmed
    assert f'Quote: "{fx.DISMISSED_QUOTE}"' in confirmed


def test_finding_shows_the_quote_in_the_context_of_its_record(brief):
    """A chosen quote can drop what follows it; the record must stay visible."""
    confirmed = _section(brief["markdown"], "Confirmed findings")

    assert 'In context: "Post on 2024-11-08: If the HOA board' in confirmed
    assert "Kidding. Mostly." in confirmed


def test_namesakes_record_never_appears_as_a_finding(brief):
    findings = _section(brief["markdown"], "Confirmed findings") + _section(
        brief["markdown"], "Leads that could not be tied to the subject or ruled out"
    )

    assert "strangulation" not in findings
    assert "09-CF-004417" not in brief["markdown"]


def test_namesake_is_listed_as_a_different_person_with_the_reason(brief):
    identity = _section(brief["markdown"], "Who the records are about")

    assert (
        "Jordan R. Whitlock; born 1961-07-02; Tampa, Florida): different person."
        in identity
    )
    assert "Conflicts: date of birth, middle name." in identity


def test_unconfirmed_lead_says_what_would_settle_it(brief):
    leads = _section(
        brief["markdown"], "Leads that could not be tied to the subject or ruled out"
    )

    assert fx.ASSAULT_QUOTE in leads
    assert "Would settle it: the date of birth shown on the record" in leads


def test_planted_instructions_are_reported_whoever_ran_the_review(brief):
    planted = _section(brief["markdown"], "Instructions found inside evidence")

    assert fx.E_PLANTED in planted
    assert "It was not acted on." in planted


def test_evidence_set_aside_is_listed_with_its_reason(brief):
    set_aside = _section(brief["markdown"], "Reviewed and not reported")

    assert (
        f"{fx.E_RANGE} (public_statements): Ordinary post about a range visit."
        in set_aside
    )


def test_coverage_names_sources_that_failed_were_missing_or_await_a_person(brief):
    checked = _section(brief["markdown"], "What was checked")

    assert (
        "Federal courts: failed, not searched. TimeoutError: upstream timeout"
        in checked
    )
    assert "Registry: not available here, not searched." in checked
    assert "State court portal: waiting for a person to look." in checked
    assert "Court records: returned records." in checked


def test_brief_records_the_basis_and_the_reporting_criteria(brief):
    markdown = brief["markdown"]

    assert "Basis for this review: consent (agreed on the form)" in markdown
    assert "- Reports: Statements about violence." in markdown
    assert "- Leaves out: Political opinions as such." in markdown
    assert "A search that finds nothing is weak evidence." in _section(
        markdown, "Limits"
    )


def test_brief_for_a_subject_with_only_a_name_confirms_nothing_about_them(engine):
    case = fx.open_case(engine, {"name": "Jordan Whitlock"})
    fx.run_all(engine, case)
    candidates = engine.list_candidates(case)["candidates"]
    for candidate in candidates:
        engine.record_judgement(
            case, candidate["candidate_id"], "unsure", "Only the name matches."
        )
    for record in engine.store.evidence(case):
        engine.dismiss_evidence(
            case, record.evidence_id, "Cannot be tied to the applicant."
        )

    produced = engine.render_brief(case)

    assert all(candidate["allowed_decisions"] == ["unsure"] for candidate in candidates)
    assert produced["brief"]["confirmed"] == []
    assert (
        "Not given: middle name, date of birth, city of residence"
        in produced["markdown"]
    )
    assert "## Confirmed findings\n\nNone." in produced["markdown"]
