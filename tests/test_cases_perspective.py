"""Perspective files: loading, and the mistakes the loader must explain."""

import pytest

from osint_agent.cases.models import Basis
from osint_agent.cases.perspective import (
    PerspectiveError,
    available_perspectives,
    load_perspective,
)

VALID = """
name: sample
title: Sample review
purpose: Used by the tests.
subject_kind: person
allowed_bases: [consent]
sources:
  - {id: court_records, title: Court records, kind: adapter}
report:
  include: [Court records tied to the subject.]
"""


def _write(tmp_path, text: str, name: str = "sample"):
    (tmp_path / f"{name}.yaml").write_text(text, encoding="utf-8")
    return tmp_path


def test_shipped_membership_review_loads_and_requires_the_applicant_to_know():
    perspective = load_perspective("membership_safety")

    assert set(perspective.allowed_bases) == {Basis.CONSENT, Basis.NOTICE}
    assert perspective.subject_kind == "person"


def test_shipped_membership_review_sends_state_courts_to_a_person():
    perspective = load_perspective("membership_safety")

    manual = {source.id for source in perspective.sources if source.kind == "manual"}

    assert manual == {"va_statewide_case_search", "md_case_search", "dc_superior_court"}


def test_valid_file_loads_with_defaults(tmp_path):
    perspective = load_perspective("sample", _write(tmp_path, VALID))

    assert perspective.sources[0].query == "name"
    assert perspective.report.exclude == []
    assert perspective.caveats == []


def test_available_perspectives_lists_file_names(tmp_path):
    _write(tmp_path, VALID)
    _write(tmp_path, VALID.replace("name: sample", "name: other"), name="other")

    assert available_perspectives(tmp_path) == ["other", "sample"]


def test_missing_file_names_what_is_available(tmp_path):
    _write(tmp_path, VALID)

    with pytest.raises(PerspectiveError, match="available: sample"):
        load_perspective("absent", tmp_path)


@pytest.mark.parametrize(
    ("change", "expected"),
    [
        (("purpose: Used by the tests.\n", ""), "purpose"),
        (("allowed_bases: [consent]", "allowed_bases: []"), "allowed_bases"),
        (("allowed_bases: [consent]", "allowed_bases: [curiosity]"), "allowed_bases"),
        (("kind: adapter", "kind: scraper"), "kind"),
        (("subject_kind: person", "subject_kind: vehicle"), "subject_kind"),
        (
            ("  include: [Court records tied to the subject.]", "  include: []"),
            "include",
        ),
    ],
)
def test_incomplete_or_wrong_file_names_the_setting(tmp_path, change, expected):
    old, new = change
    assert old in VALID

    with pytest.raises(PerspectiveError, match=expected):
        load_perspective("sample", _write(tmp_path, VALID.replace(old, new)))


def test_duplicate_source_ids_are_refused(tmp_path):
    doubled = VALID.replace(
        "  - {id: court_records, title: Court records, kind: adapter}",
        "  - {id: court_records, title: Court records, kind: adapter}\n"
        "  - {id: court_records, title: Again, kind: manual}",
    )

    with pytest.raises(PerspectiveError, match="more than once"):
        load_perspective("sample", _write(tmp_path, doubled))


def test_declared_name_must_match_the_file_name(tmp_path):
    with pytest.raises(PerspectiveError, match="must match the file name"):
        load_perspective("renamed", _write(tmp_path, VALID, name="renamed"))


def test_invalid_yaml_is_reported_as_such(tmp_path):
    with pytest.raises(PerspectiveError, match="not valid YAML"):
        load_perspective("sample", _write(tmp_path, "name: [unclosed"))


def test_file_that_is_not_a_mapping_is_refused(tmp_path):
    with pytest.raises(PerspectiveError, match="mapping"):
        load_perspective("sample", _write(tmp_path, "- just\n- a list\n"))
