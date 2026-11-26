"""Case files: reviews of one subject that end in a checked, sourced brief.

A case records why the review is allowed, what was stated about the subject,
every record the sources returned, which person each record is about, and the
claims whose quotes were found in those records. The engine enforces the rules
that keep a brief honest whoever is driving it, a person or a language model.

Posts from accounts the subject listed are collected and read in bulk, and only
the ones a reading flags become records that need a decision.

For platforms a program may not read, a reviewer saves screenshots into a
folder. Their words are copied out by a model that can see pictures, checked
against a second text-recognition program, and then read like any other post.
"""

from osint_agent.cases.chat_reader import ChatReader
from osint_agent.cases.collectors import default_collectors
from osint_agent.cases.engine import CaseEngine, RawRecord, SourceResult
from osint_agent.cases.models import (
    Basis,
    CollectedPost,
    Collection,
    Decision,
    PersonFacts,
    SourceStatus,
)
from osint_agent.cases.perspective import (
    Perspective,
    PerspectiveError,
    load_perspective,
)
from osint_agent.cases.reading import Reader, ReaderError
from osint_agent.cases.store import CaseStore

__all__ = [
    "Basis",
    "CaseEngine",
    "CaseStore",
    "ChatReader",
    "CollectedPost",
    "Collection",
    "Decision",
    "PersonFacts",
    "Perspective",
    "PerspectiveError",
    "RawRecord",
    "Reader",
    "ReaderError",
    "SourceResult",
    "SourceStatus",
    "default_collectors",
    "load_perspective",
]
