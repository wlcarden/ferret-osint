"""Case files: reviews of one subject that end in a checked, sourced brief.

A case records why the review is allowed, what was stated about the subject,
every record the sources returned, which person each record is about, and the
claims whose quotes were found in those records. The engine enforces the rules
that keep a brief honest whoever is driving it, a person or a language model.
"""

from osint_agent.cases.engine import CaseEngine, RawRecord, SourceResult
from osint_agent.cases.models import Basis, Decision, PersonFacts, SourceStatus
from osint_agent.cases.perspective import (
    Perspective,
    PerspectiveError,
    load_perspective,
)
from osint_agent.cases.store import CaseStore

__all__ = [
    "Basis",
    "CaseEngine",
    "CaseStore",
    "Decision",
    "PersonFacts",
    "Perspective",
    "PerspectiveError",
    "RawRecord",
    "SourceResult",
    "SourceStatus",
    "load_perspective",
]
