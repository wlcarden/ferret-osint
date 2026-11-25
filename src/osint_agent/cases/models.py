"""Data models for case files.

A case is one review of one subject under one perspective. It holds what the
reviewer stated about the subject, every record a source returned, which person
each record is about, and the claims that survived checking against those
records. It is deliberately narrower than an investigation in the entity graph:
a case exists to produce a brief a person can act on and defend.
"""

import re
from datetime import UTC, date, datetime
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator

_TIMESTAMP = re.compile(
    r"\d{4}-\d{2}-\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,9})?)?(?:Z|[+-]\d{2}:?\d{2})?)?"
)
_WEB_ADDRESS = re.compile(r"https?://[^\s<>\"']{1,2000}")


def utc_now() -> str:
    """Current time as an ISO 8601 string in UTC, to the second."""
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def clean_timestamp(value: object) -> str | None:
    """The value if it is an ISO date or date-time naming a real day, else None."""
    if not isinstance(value, str) or not _TIMESTAMP.fullmatch(value):
        return None
    try:
        date.fromisoformat(value[:10])
    except ValueError:
        return None
    return value


class Basis(str, Enum):
    """Why a review of this subject is allowed. Recorded with every case."""

    SELF = "self"  # The subject is the reviewer
    CONSENT = "consent"  # The subject agreed to the review
    NOTICE = "notice"  # The subject was told the review would happen
    BUSINESS_ROLE = "business_role"  # Records about a person's business dealings
    PUBLIC_ROLE = "public_role"  # Reporting on an official's public role
    ORGANISATION = "organisation"  # The subject is a company or other body


class Decision(str, Enum):
    """Whether a candidate found in the evidence is the subject."""

    SAME_PERSON = "same_person"
    DIFFERENT_PERSON = "different_person"
    UNSURE = "unsure"


class SourceStatus(str, Enum):
    """What happened when a source was asked."""

    ANSWERED = "answered"  # Returned at least one record
    EMPTY = "empty"  # Answered, found nothing
    FAILED = "failed"  # Did not answer
    NOT_CONFIGURED = "not_configured"  # No adapter is installed for it here
    MANUAL_PENDING = "manual_pending"  # A person has to do this lookup


class ClaimStatus(str, Enum):
    """Outcome of checking one claim against stored evidence."""

    VERIFIED = "verified"  # Quote found; evidence is about the subject
    UNCONFIRMED = "unconfirmed"  # Quote found; identity of the person is unsure
    REJECTED = "rejected"  # Evidence is about someone judged a different person
    FAILED = "failed"  # No such evidence, or the quote is not in it


class PersonFacts(BaseModel):
    """Identifying facts about a person. Every field except the name may be blank.

    Used both for what the reviewer stated about the subject and for what a
    single record says about the person it concerns.
    """

    name: str
    date_of_birth: str | None = None  # ISO date, YYYY-MM-DD
    residence_city: str | None = None
    residence_state: str | None = None  # Full name or postal abbreviation
    handles: list[str] = Field(default_factory=list)


class EvidenceRecord(BaseModel):
    """One record returned by a source, stored as it was received."""

    evidence_id: str
    source: str
    text: str  # Untrusted: whatever the source returned
    url: str | None = None
    retrieved_at: str = Field(default_factory=utc_now)
    retrieved_by: str = "adapter"  # "adapter", or the person who looked it up
    person: PersonFacts | None = None  # Who the record says it concerns
    stated_identifier: str | None = None  # Handle the subject listed that led here
    flags: list[str] = Field(default_factory=list)


class Candidate(BaseModel):
    """A distinct person the evidence could refer to."""

    candidate_id: str
    names: list[str] = Field(default_factory=list)
    date_of_birth: str | None = None
    residence_city: str | None = None
    residence_state: str | None = None
    stated_identifier: str | None = None
    evidence_ids: list[str] = Field(default_factory=list)

    def signature(self) -> str:
        """Identifying attributes as one string. A judgement is tied to this."""
        parts = [
            "|".join(sorted(self.names)),
            self.date_of_birth or "",
            self.residence_city or "",
            self.residence_state or "",
            self.stated_identifier or "",
        ]
        return "~".join(parts)

    def as_person(self) -> PersonFacts:
        return PersonFacts(
            name=max(self.names, key=len) if self.names else "",
            date_of_birth=self.date_of_birth,
            residence_city=self.residence_city,
            residence_state=self.residence_state,
        )


class Judgement(BaseModel):
    """A recorded decision about one candidate."""

    candidate_id: str
    decision: Decision
    reason: str
    signature: str  # Candidate attributes when the decision was made
    recorded_at: str = Field(default_factory=utc_now)


class SourceRun(BaseModel):
    """The latest result of asking one source."""

    source: str
    status: SourceStatus
    note: str = ""
    ran_at: str = Field(default_factory=utc_now)


class CollectedPost(BaseModel):
    """One post as a collector hands it over."""

    native_id: str  # The platform's own identifier, used to avoid duplicates
    posted_at: str | None = None  # ISO date or date-time
    kind: Literal["post", "reply", "repost"] = "post"
    text: str = ""  # Untrusted: the account holder's words, or the reposted words
    context: str = ""  # Untrusted: who it replies to, or what it quotes
    url: str | None = None
    media: int = Field(default=0, ge=0)  # Images or videos attached; they are not read

    @field_validator("posted_at", mode="before")
    @classmethod
    def _only_a_real_date(cls, value: object) -> str | None:
        """Drop a date that is not one. Platforms pass on what an author typed."""
        return clean_timestamp(value)

    @field_validator("url", mode="before")
    @classmethod
    def _only_a_web_address(cls, value: object) -> str | None:
        if isinstance(value, str) and _WEB_ADDRESS.fullmatch(value):
            return value
        return None


class Collection(BaseModel):
    """What a collector returns for one account."""

    status: SourceStatus = SourceStatus.ANSWERED
    posts: list[CollectedPost] = Field(default_factory=list)
    note: str = ""
    complete: bool = True  # False when collection stopped before the end


class Post(CollectedPost):
    """One post held for a case.

    Posts are kept apart from evidence because an account can hold thousands.
    A post becomes evidence only when a reading asks for it to be looked at.
    """

    post_id: str  # P1, P2, ... within the case; assigned by the store
    source: str
    account: str  # The listed handle, exactly as the subject gave it
    retrieved_at: str = Field(default_factory=utc_now)
    retrieved_by: str = "adapter"  # "adapter", or the person who captured it
    evidence_id: str | None = None  # Set when the post is promoted to evidence


class CaseRecord(BaseModel):
    """The fixed facts of a case, set when it is opened."""

    case_id: str
    perspective: str
    subject: PersonFacts
    basis: Basis
    basis_note: str = ""
    opened_by: str = ""
    opened_at: str = Field(default_factory=utc_now)
