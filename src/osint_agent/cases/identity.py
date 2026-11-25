"""Identity rules: deciding whether a record is about the subject.

The failure to design against is a record about someone who shares the
subject's name being attached to the subject, or a possible match being waved
away. These rules make both impossible to record without evidence:

* "same person" needs a second identifier that matches;
* "different person" needs an identifier that conflicts;
* anything else can only be "unsure".

A different city or state is recorded as a difference and never as a conflict,
because people move. Blank fields on either side simply remove an identifier
from the comparison, so a subject with nothing but a name can only ever produce
"unsure" candidates.

This is a small rule set for single cases. nomenklatura, the resolver behind
OpenSanctions, is the intended replacement once cases share a store with the
entity graph.
"""

from collections.abc import Sequence
from typing import NamedTuple

from pydantic import BaseModel, Field

from osint_agent.cases.models import Candidate, Decision, EvidenceRecord, PersonFacts

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv"}

_STATE_CODES = {
    "alabama": "AL", "alaska": "AK", "arizona": "AZ", "arkansas": "AR",
    "california": "CA", "colorado": "CO", "connecticut": "CT", "delaware": "DE",
    "district of columbia": "DC", "florida": "FL", "georgia": "GA", "hawaii": "HI",
    "idaho": "ID", "illinois": "IL", "indiana": "IN", "iowa": "IA", "kansas": "KS",
    "kentucky": "KY", "louisiana": "LA", "maine": "ME", "maryland": "MD",
    "massachusetts": "MA", "michigan": "MI", "minnesota": "MN", "mississippi": "MS",
    "missouri": "MO", "montana": "MT", "nebraska": "NE", "nevada": "NV",
    "new hampshire": "NH", "new jersey": "NJ", "new mexico": "NM", "new york": "NY",
    "north carolina": "NC", "north dakota": "ND", "ohio": "OH", "oklahoma": "OK",
    "oregon": "OR", "pennsylvania": "PA", "rhode island": "RI",
    "south carolina": "SC", "south dakota": "SD", "tennessee": "TN", "texas": "TX",
    "utah": "UT", "vermont": "VT", "virginia": "VA", "washington": "WA",
    "west virginia": "WV", "wisconsin": "WI", "wyoming": "WY",
}  # fmt: skip
_KNOWN_CODES = set(_STATE_CODES.values())

FIELD_LABELS = {
    "stated_identifier": "an identifier the subject listed",
    "date_of_birth": "date of birth",
    "middle_name": "middle name",
    "residence_city": "city of residence",
    "residence_state": "state of residence",
}


class ParsedName(NamedTuple):
    given: str
    middle: str
    surname: str


def parse_name(name: str) -> ParsedName:
    """Split a name into given, middle and surname, lower-cased.

    Accepts "Given Middle Surname" and the court-record form "Surname, Given
    Middle". Generational suffixes are dropped.
    """
    if "," in name:
        surname_part, _, rest = name.partition(",")
        ordered = f"{rest} {surname_part}"
    else:
        ordered = name
    tokens = [token.strip(".,").lower() for token in ordered.split()]
    tokens = [token for token in tokens if token and token not in _SUFFIXES]
    if not tokens:
        return ParsedName("", "", "")
    if len(tokens) == 1:
        return ParsedName("", "", tokens[0])
    return ParsedName(tokens[0], " ".join(tokens[1:-1]), tokens[-1])


def state_code(value: str | None) -> str | None:
    """Two-letter code for a state given by name or code; None when blank."""
    if not value or not value.strip():
        return None
    cleaned = value.strip().strip(".").lower()
    if cleaned.upper() in _KNOWN_CODES:
        return cleaned.upper()
    if cleaned in ("d.c", "washington dc", "washington, dc", "washington d.c"):
        return "DC"
    return _STATE_CODES.get(cleaned, cleaned)


def _clean(value: str | None) -> str | None:
    if not value or not value.strip():
        return None
    return " ".join(value.strip().lower().split())


class Comparison(BaseModel):
    """How one person's stated facts line up against another's."""

    matches: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    differences: list[str] = Field(default_factory=list)
    missing_on_subject: list[str] = Field(default_factory=list)
    missing_on_record: list[str] = Field(default_factory=list)


def compare(
    subject: PersonFacts, other: PersonFacts, stated_identifier: str | None = None
) -> Comparison:
    """Compare the subject's facts with what a candidate's records say."""
    result = Comparison()
    if stated_identifier:
        result.matches.append("stated_identifier")

    def weigh(
        field: str, ours: str | None, theirs: str | None, on_clash: list[str]
    ) -> None:
        if ours and theirs:
            (result.matches if ours == theirs else on_clash).append(field)
            return
        if not ours:
            result.missing_on_subject.append(field)
        if not theirs:
            result.missing_on_record.append(field)

    weigh(
        "date_of_birth",
        _clean(subject.date_of_birth),
        _clean(other.date_of_birth),
        result.conflicts,
    )
    weigh(
        "middle_name",
        parse_name(subject.name).middle[:1] or None,
        parse_name(other.name).middle[:1] or None,
        result.conflicts,
    )
    our_state, their_state = state_code(subject.residence_state), state_code(
        other.residence_state
    )
    weigh("residence_state", our_state, their_state, result.differences)
    if "residence_state" in result.differences:
        result.differences.append("residence_city")
    else:
        weigh(
            "residence_city",
            _clean(subject.residence_city),
            _clean(other.residence_city),
            result.differences,
        )
    return result


def allowed_decisions(comparison: Comparison) -> list[Decision]:
    """Decisions the evidence supports, most specific first. "unsure" is always allowed."""
    matches, conflicts = set(comparison.matches), set(comparison.conflicts)
    if "stated_identifier" in matches or "date_of_birth" in matches:
        return [Decision.SAME_PERSON, Decision.UNSURE]
    if conflicts:
        return [Decision.DIFFERENT_PERSON, Decision.UNSURE]
    if {"middle_name", "residence_city"} <= matches:
        return [Decision.SAME_PERSON, Decision.UNSURE]
    return [Decision.UNSURE]


def _labels(fields: Sequence[str]) -> str:
    return ", ".join(FIELD_LABELS.get(field, field) for field in fields) or "none"


def explain(comparison: Comparison) -> str:
    """One line a reviewer can read: what matched, conflicted, differed."""
    parts = [
        f"Matches: {_labels(comparison.matches)}.",
        f"Conflicts: {_labels(comparison.conflicts)}.",
    ]
    if comparison.differences:
        parts.append(f"Differs but can change: {_labels(comparison.differences)}.")
    return " ".join(parts)


def what_would_settle(comparison: Comparison) -> list[str]:
    """Facts that, if obtained, would let an unsure candidate be decided.

    Empty when the identifiers already support a decision either way.
    """
    if allowed_decisions(comparison) != [Decision.UNSURE]:
        return []
    needed = []
    for field in ("date_of_birth", "middle_name"):
        label = FIELD_LABELS[field]
        if field in comparison.missing_on_subject:
            needed.append(f"the subject's {label}")
        if field in comparison.missing_on_record:
            needed.append(f"the {label} shown on the record")
    return needed


def _shares_identifier(comparison: Comparison) -> bool:
    matches = set(comparison.matches)
    return bool(
        "date_of_birth" in matches
        or "residence_city" in matches
        or {"middle_name", "residence_state"} <= matches
    )


def _find_home(candidates: list[Candidate], record: EvidenceRecord) -> Candidate | None:
    if record.stated_identifier:
        wanted = record.stated_identifier.lower()
        return next(
            (c for c in candidates if (c.stated_identifier or "").lower() == wanted),
            None,
        )
    if record.person is None:
        return next(
            (c for c in candidates if not c.names and not c.stated_identifier), None
        )
    for candidate in candidates:
        if not candidate.names:
            continue
        comparison = compare(candidate.as_person(), record.person)
        compatible = (
            not comparison.conflicts and "residence_state" not in comparison.differences
        )
        if compatible and _shares_identifier(comparison):
            return candidate
    return None


def _absorb(candidate: Candidate, record: EvidenceRecord) -> None:
    candidate.evidence_ids.append(record.evidence_id)
    candidate.stated_identifier = candidate.stated_identifier or record.stated_identifier
    person = record.person
    if person is None:
        return
    if person.name and person.name not in candidate.names:
        candidate.names.append(person.name)
    candidate.date_of_birth = candidate.date_of_birth or person.date_of_birth
    candidate.residence_city = candidate.residence_city or person.residence_city
    candidate.residence_state = candidate.residence_state or person.residence_state


def group_into_candidates(records: Sequence[EvidenceRecord]) -> list[Candidate]:
    """Group records into the distinct people they could refer to.

    Records join an existing candidate only when nothing conflicts and they
    share an identifier with it. Records are taken in the order given, so
    candidate numbering is stable as evidence is added.
    """
    candidates: list[Candidate] = []
    for record in records:
        home = _find_home(candidates, record)
        if home is None:
            home = Candidate(candidate_id=f"C{len(candidates) + 1}")
            candidates.append(home)
        _absorb(home, record)
    return candidates
