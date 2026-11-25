"""The case engine: the operations a person or an agent uses to run a review.

Every operation takes and returns plain data, so the same engine can sit behind
a command line, a tool server or a test. Mistakes by the caller come back as
``{"error": ...}`` with what to do next; they are not raised, because the
caller is often a language model that needs to read the reason and try again.

Rules enforced here, so that they hold whoever is driving:

* a case cannot be opened without a basis the perspective allows;
* a source can only be asked about the subject, or a handle the subject listed;
* a candidate can only be judged the way the identifiers support;
* a claim counts only if its quote appears in the evidence it cites;
* posts are collected only from accounts the subject listed, and each one is
  read before a brief, more than once and with the reader's answers checked;
* screenshots are read only from the captures folder, and a picture that two
  programs read differently goes to a person;
* a brief is refused until every source was tried, every post read, every
  candidate judged and every record either cited, set aside with a reason, or
  about someone else.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from osint_agent.cases import identity
from osint_agent.cases.brief import INSTRUCTIONS_FLAG, build_brief, to_markdown
from osint_agent.cases.identity import Comparison
from osint_agent.cases.intake import ScreenshotIntake, Transcriber
from osint_agent.cases.models import (
    Basis,
    Candidate,
    CaseRecord,
    ClaimStatus,
    Decision,
    EvidenceRecord,
    Judgement,
    PersonFacts,
    SourceRun,
    SourceStatus,
)
from osint_agent.cases.perspective import (
    DEFAULT_PERSPECTIVES_DIR,
    Perspective,
    PerspectiveError,
    SourceSpec,
    load_perspective,
)
from osint_agent.cases.posts import (
    FLAG_PICTURE,
    NEEDS_DECISION,
    PostCollector,
    PostReview,
    normalise_handle,
)
from osint_agent.cases.quotes import quote_is_in
from osint_agent.cases.reading import Reader
from osint_agent.cases.screens import addresses_a_reader
from osint_agent.cases.screenshots import (
    MIN_NAME_CHARACTERS,
    SecondLook,
    tesseract,
    usable_names,
)
from osint_agent.cases.store import CaseStore

MAX_RETURNED_TEXT = 2000
_SCREENSHOT_NOTE = re.compile(r"Screenshots taken in: \d+\.")


class RawRecord(BaseModel):
    """One record as a source adapter hands it over."""

    text: str
    url: str | None = None
    person: PersonFacts | None = None


class SourceResult(BaseModel):
    """What a source adapter returns for one query."""

    status: SourceStatus = SourceStatus.ANSWERED
    records: list[RawRecord] = Field(default_factory=list)
    note: str = ""


SourceFunction = Callable[[str, PersonFacts], SourceResult]


def name_variants(name: str) -> list[str]:
    """Spellings of a name worth searching by hand."""
    given, middle, surname = (part.title() for part in identity.parse_name(name))
    variants = [f"{given} {surname}".strip(), f"{surname}, {given}".strip(", ")]
    if middle:
        variants.insert(0, f"{given} {middle} {surname}")
        variants.append(f"{given} {middle[0]}. {surname}")
    return list(dict.fromkeys(variant for variant in variants if variant))


def _error(message: str, **extra: object) -> dict:
    return {"error": message, **extra}


@dataclass
class _State:
    """Everything known about a case at one moment."""

    case: CaseRecord
    perspective: Perspective
    evidence: list[EvidenceRecord]
    candidates: list[Candidate]
    judgements: dict[str, Judgement]  # Only judgements still matching their candidate
    stale: set[str]  # Candidates whose evidence changed after they were judged

    def owner(self, evidence_id: str) -> Candidate:
        return next(c for c in self.candidates if evidence_id in c.evidence_ids)

    def comparison(self, candidate: Candidate) -> Comparison:
        return identity.compare(
            self.case.subject, candidate.as_person(), candidate.stated_identifier
        )


class CaseEngine:
    """Runs reviews. One instance serves any number of cases."""

    def __init__(
        self,
        store: CaseStore,
        sources: dict[str, SourceFunction] | None = None,
        perspectives_dir: Path | None = None,
        collectors: dict[str, PostCollector] | None = None,
        reader: Reader | None = None,
        today: Callable[[], date] = date.today,
        captures_dir: Path | None = None,
        transcriber: Transcriber | None = None,
        second_look: SecondLook = tesseract,
        second_look_name: str = "tesseract",
    ):
        self.store = store
        self.sources = dict(sources or {})  # A copy: callers may reuse theirs
        self.perspectives_dir = perspectives_dir
        self.posts = PostReview(store, collectors, reader, today)
        # The one folder screenshots may be read from. Without it none are read.
        self.captures_dir = captures_dir
        if transcriber is None and hasattr(reader, "transcribe"):
            transcriber = reader  # type: ignore[assignment]
        self.intake = ScreenshotIntake(
            self.posts, transcriber, second_look, second_look_name
        )
        self._loaded: dict[str, tuple[int | None, Perspective]] = {}

    def _perspective(self, name: str) -> Perspective:
        """Load a perspective, re-reading its file only when the file has changed."""
        path = (self.perspectives_dir or DEFAULT_PERSPECTIVES_DIR) / f"{name}.yaml"
        stamp = path.stat().st_mtime_ns if path.is_file() else None
        cached = self._loaded.get(name)
        if cached is None or cached[0] != stamp:
            cached = (stamp, load_perspective(name, self.perspectives_dir))
            self._loaded[name] = cached
        return cached[1]

    def _state(self, case_id: str) -> _State | None:
        case = self.store.get_case(case_id)
        if case is None:
            return None
        evidence = self.store.evidence(case_id)
        candidates = identity.group_into_candidates(evidence)
        current, stale = {}, set()
        for candidate_id, judgement in self.store.judgements(case_id).items():
            candidate = next(
                (c for c in candidates if c.candidate_id == candidate_id), None
            )
            if candidate is not None and candidate.signature() == judgement.signature:
                current[candidate_id] = judgement
            else:
                stale.add(candidate_id)
        perspective = self._perspective(case.perspective)
        return _State(case, perspective, evidence, candidates, current, stale)

    # Opening a case

    def open_case(
        self,
        perspective: str,
        subject: dict,
        basis: str,
        basis_note: str = "",
        opened_by: str = "",
    ) -> dict:
        """Start a review. The basis must be one the perspective allows."""
        try:
            loaded = self._perspective(perspective)
        except PerspectiveError as error:
            return _error(str(error))
        allowed = [item.value for item in loaded.allowed_bases]
        if basis not in allowed:
            return _error(
                f"a '{loaded.name}' review cannot be opened on the basis '{basis}'",
                allowed_bases=allowed,
            )
        try:
            facts = PersonFacts(**subject)
        except (ValidationError, TypeError) as error:
            return _error(f"the subject's facts are not usable: {error}")
        if not facts.name.strip():
            return _error("the subject needs a name")
        facts.handles = [
            normalise_handle(handle) for handle in facts.handles if handle.strip()
        ]
        case = CaseRecord(
            case_id=f"case-{uuid4().hex[:8]}",
            perspective=loaded.name,
            subject=facts,
            basis=Basis(basis),
            basis_note=basis_note,
            opened_by=opened_by,
        )
        self.store.add_case(case)
        self.store.log(
            case.case_id, "open_case", {"perspective": loaded.name, "basis": basis}
        )
        return {
            "case_id": case.case_id,
            "perspective": loaded.name,
            "purpose": " ".join(loaded.purpose.split()),
            "report": loaded.report.model_dump(),
            "next": "list_sources, then run_source for each one",
        }

    # Sources

    def list_sources(self, case_id: str) -> dict:
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        runs = self.store.source_runs(case_id)
        return {
            "sources": [
                {
                    "source": spec.id,
                    "title": spec.title,
                    "kind": spec.kind,
                    "status": (
                        runs[spec.id].status.value if spec.id in runs else "not run"
                    ),
                }
                for spec in state.perspective.sources
            ]
        }

    def describe_source(self, case_id: str, source: str) -> dict:
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        spec = state.perspective.source(source)
        if spec is None:
            return _error(
                f"no source '{source}'", known=[s.id for s in state.perspective.sources]
            )
        description = spec.model_dump()
        description["covers"] = " ".join(spec.covers.split())
        description["instructions"] = " ".join(spec.instructions.split())
        return description

    def _queries(
        self, state: _State, spec: SourceSpec, query: str | None
    ) -> list[str] | dict:
        subject = state.case.subject
        if spec.query == "handle":
            if not subject.handles:
                return []
            if query is None:
                return subject.handles
            wanted = query.lstrip("@").strip().lower()
            match = [handle for handle in subject.handles if handle.lower() == wanted]
            return match or _error(
                "only handles the subject listed can be searched",
                listed=subject.handles,
            )
        if query is None:
            return [subject.name]
        if (
            identity.parse_name(query).surname
            != identity.parse_name(subject.name).surname
        ):
            return _error("a case can only be used to search for its own subject")
        return [query]

    def _keep(
        self,
        case_id: str,
        spec: SourceSpec,
        raw: RawRecord,
        by: str,
        handle: str | None,
    ) -> EvidenceRecord:
        record = EvidenceRecord(
            evidence_id=self.store.next_evidence_id(case_id),
            source=spec.id,
            text=raw.text,
            url=raw.url,
            retrieved_by=by,
            person=raw.person,
            stated_identifier=handle,
            flags=[INSTRUCTIONS_FLAG] if addresses_a_reader(raw.text) else [],
        )
        self.store.add_evidence(case_id, record)
        return record

    @staticmethod
    def _shown(record: EvidenceRecord) -> dict:
        return {
            "evidence_id": record.evidence_id,
            "untrusted_text": record.text[:MAX_RETURNED_TEXT],
            "truncated": len(record.text) > MAX_RETURNED_TEXT,
            "flags": record.flags,
        }

    def run_source(self, case_id: str, source: str, query: str | None = None) -> dict:
        """Ask one source about the subject and store whatever it returns."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        spec = state.perspective.source(source)
        if spec is None:
            return _error(
                f"no source '{source}'", known=[s.id for s in state.perspective.sources]
            )
        if spec.kind == "manual":
            return self._ask_a_person(state, spec)
        if spec.kind == "capture":
            return self._ask_for_capture(state, spec)
        if spec.kind == "posts":
            return self._collect_posts(state, spec, query)
        adapter = self.sources.get(spec.id)
        if adapter is None:
            return self._finish(
                case_id, spec, SourceStatus.NOT_CONFIGURED, [], "No adapter installed."
            )
        queries = self._queries(state, spec, query)
        if isinstance(queries, dict):
            return queries
        if not queries:
            note = "The subject listed no handle, so there was nothing to search."
            return self._finish(case_id, spec, SourceStatus.EMPTY, [], note)
        kept: list[EvidenceRecord] = []
        failures: list[str] = []
        for term in queries:
            try:
                result = adapter(term, state.case.subject)
            except (
                Exception
            ) as error:  # noqa: BLE001 - a failing source must not end the review
                failures.append(f"{type(error).__name__}: {str(error)[:160]}")
                continue
            if result.status is SourceStatus.FAILED:
                failures.append(result.note or "the source reported a failure")
                continue
            handle = term if spec.query == "handle" else None
            kept += [
                self._keep(case_id, spec, raw, "adapter", handle)
                for raw in result.records
            ]
        if kept:
            status = SourceStatus.ANSWERED
        else:
            status = SourceStatus.FAILED if failures else SourceStatus.EMPTY
        return self._finish(case_id, spec, status, kept, "; ".join(failures))

    def _finish(
        self,
        case_id: str,
        spec: SourceSpec,
        status: SourceStatus,
        kept: list[EvidenceRecord],
        note: str,
    ) -> dict:
        self.store.set_source_run(
            case_id, SourceRun(source=spec.id, status=status, note=note)
        )
        self.store.log(
            case_id,
            "run_source",
            {"source": spec.id, "status": status.value, "records": len(kept)},
        )
        return {
            "source": spec.id,
            "status": status.value,
            "note": note,
            "evidence": [self._shown(record) for record in kept],
        }

    def _ask_a_person(self, state: _State, spec: SourceSpec) -> dict:
        case_id = state.case.case_id
        runs = self.store.source_runs(case_id)
        if spec.id not in runs:
            pending = SourceRun(source=spec.id, status=SourceStatus.MANUAL_PENDING)
            self.store.set_source_run(case_id, pending)
            self.store.log(
                case_id, "run_source", {"source": spec.id, "status": "manual_pending"}
            )
        status = runs[spec.id].status.value if spec.id in runs else "manual_pending"
        return {
            "source": spec.id,
            "status": status,
            "needs_a_person": True,
            "url": spec.url,
            "search_for": name_variants(state.case.subject.name),
            "instructions": " ".join(spec.instructions.split()),
            "then": "record_manual_result with who checked and what they found",
        }

    def record_manual_result(
        self,
        case_id: str,
        source: str,
        checked_by: str,
        records: list[dict] | None = None,
        note: str = "",
    ) -> dict:
        """Store what a person found, or that they found nothing."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        spec = state.perspective.source(source)
        if spec is None or spec.kind != "manual":
            return _error(f"'{source}' is not a manual source in this review")
        if not checked_by.strip():
            return _error("say who did the lookup")
        try:
            raw = [RawRecord(**item) for item in records or []]
        except (ValidationError, TypeError) as error:
            return _error(f"a record is not usable: {error}")
        kept = [
            self._keep(case_id, spec, item, checked_by.strip(), None) for item in raw
        ]
        status = SourceStatus.ANSWERED if kept else SourceStatus.EMPTY
        detail = f"Checked by {checked_by.strip()}." + (f" {note}" if note else "")
        return self._finish(case_id, spec, status, kept, detail)

    # Posts from accounts the subject listed

    def _finish_posts(self, state: _State, spec: SourceSpec, outcome: dict) -> dict:
        """Record a collection or capture, and say what to do with the posts."""
        if "error" in outcome:
            return outcome
        result = self._finish(
            state.case.case_id, spec, outcome["status"], [], outcome["note"]
        )
        del result["evidence"]  # Posts are not evidence until a reading flags them
        result["posts"] = {"held": outcome["collected"], "new": outcome["new"]}
        result["next"] = (
            "read_posts, which reads them and returns those needing a decision"
        )
        return result

    def _collect_posts(
        self, state: _State, spec: SourceSpec, query: str | None
    ) -> dict:
        outcome = self.posts.collect(state.case, state.perspective, spec, query)
        return self._finish_posts(state, spec, outcome)

    def _ask_for_capture(self, state: _State, spec: SourceSpec) -> dict:
        case_id = state.case.case_id
        accounts = self.posts.accounts_for(state.case, state.perspective, spec)
        runs = self.store.source_runs(case_id)
        if not accounts:
            note = "The subject listed no account that a person has to capture."
            result = self._finish(case_id, spec, SourceStatus.EMPTY, [], note)
            del result["evidence"]
            return result
        if spec.id not in runs:
            pending = SourceRun(source=spec.id, status=SourceStatus.MANUAL_PENDING)
            self.store.set_source_run(case_id, pending)
            self.store.log(
                case_id, "run_source", {"source": spec.id, "status": "manual_pending"}
            )
        status = runs[spec.id].status.value if spec.id in runs else "manual_pending"
        return {
            "source": spec.id,
            "status": status,
            "needs_a_person": True,
            "accounts": accounts,
            "instructions": " ".join(spec.instructions.split()),
            "then": (
                "for each account, record_screenshots with a folder of screenshots, "
                "or record_posts with the posts typed out (an empty list if there "
                "were none); then read_posts"
            ),
        }

    def record_posts(
        self,
        case_id: str,
        source: str,
        account: str,
        checked_by: str,
        posts: list[dict] | None = None,
        note: str = "",
    ) -> dict:
        """Store the posts a person captured from one account the subject listed."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        spec = state.perspective.source(source)
        if spec is None or spec.kind != "capture":
            return _error(f"'{source}' is not a capture source in this review")
        if not checked_by.strip():
            return _error("say who captured the posts")
        outcome = self.posts.record(
            state.case,
            state.perspective,
            spec,
            account,
            checked_by.strip(),
            posts or [],
        )
        if "error" not in outcome:
            earlier = self.store.source_runs(case_id).get(spec.id)
            parts = [earlier.note if earlier else "", outcome["note"], note]
            outcome["note"] = " ".join(part for part in parts if part)
        return self._finish_posts(state, spec, outcome)

    def record_screenshots(
        self,
        case_id: str,
        source: str,
        account: str,
        checked_by: str,
        folder: str,
        shown_as: list[str] | None = None,
        max_pictures: int | None = None,
    ) -> dict:
        """Take in a folder of screenshots a person saved from one listed account.

        The folder is named relative to the captures folder. Pictures already
        taken in are not read again, so this can be called until finished is
        true, and again with shown_as when the account appears under a name
        other than its handle.
        """
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        spec = state.perspective.source(source)
        if spec is None or spec.kind != "capture":
            return _error(f"'{source}' is not a capture source in this review")
        if not checked_by.strip():
            return _error("say who saved the screenshots")
        if self.captures_dir is None:
            return _error("no captures folder is set up, so screenshots cannot be read")
        if max_pictures is not None and max_pictures < 1:
            return _error("max_pictures must be at least 1")
        names = usable_names(shown_as if shown_as is not None else [])
        if names is None:
            return _error(
                "shown_as must be a list of names, each at least "
                f"{MIN_NAME_CHARACTERS} letters or digits long"
            )
        outcome = self.intake.take_in(
            state.case,
            state.perspective,
            spec,
            account,
            checked_by.strip(),
            self.captures_dir,
            folder if isinstance(folder, str) else "",
            names,
            max_pictures,
        )
        if "error" in outcome:
            return outcome
        earlier = self.store.source_runs(case_id).get(spec.id)
        # The count of screenshots replaces the one from the call before
        before = _SCREENSHOT_NOTE.sub("", earlier.note if earlier else "").strip()
        outcome["note"] = " ".join(part for part in (before, outcome["note"]) if part)
        result = self._finish_posts(state, spec, outcome)
        for key, value in outcome.items():
            if key not in ("status", "note", "collected", "new"):
                result[key] = value
        if not outcome["finished"]:
            result["next"] = (
                "record_screenshots again until finished is true; a brief waits for it"
            )
        elif outcome["pictures"]["passed_to_a_person"]:
            result["next"] = (
                "read_posts; it also lists each screenshot a person has to open, "
                "which is then set aside with dismiss_evidence once checked"
            )
        return result

    def _undecided_posts(self, state: _State) -> list[dict]:
        """Posts a reading flagged that are neither cited nor set aside yet."""
        case_id = state.case.case_id
        promoted = self.posts.promoted(case_id)
        cited = {claim["evidence_id"] for claim in self.store.claims(case_id)}
        set_aside = set(self.store.dismissals(case_id))
        waiting = []
        for record in state.evidence:
            post = promoted.get(record.evidence_id)
            picture = FLAG_PICTURE in record.flags
            if not picture and (post is None or not NEEDS_DECISION & set(record.flags)):
                continue
            if record.evidence_id in cited or record.evidence_id in set_aside:
                continue
            shown = self._shown(record)
            shown["url"] = record.url
            if post is not None and post["quote"]:
                when = (post["posted_at"] or "an unknown date")[:10]
                shown["claim_if_reported"] = {
                    "evidence_id": record.evidence_id,
                    "text": f"{post['kind'].capitalize()} dated {when}.",
                    "quote": post["quote"],
                }
            waiting.append(shown)
        return waiting

    def read_posts(self, case_id: str, max_batches: int | None = None) -> dict:
        """Read the collected posts, and return those that need a decision.

        Reading picks up where it stopped, so it is safe to call again after a
        failure or a batch limit.
        """
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        outcome = self.posts.read(state.case, state.perspective, max_batches)
        if "error" in outcome:
            return outcome
        self.store.log(
            case_id,
            "read_posts",
            {
                key: outcome[key]
                for key in ("reader", "reader_calls", "flagged_now", "finished")
            },
        )
        state = self._state(case_id)  # Flagged posts are evidence now
        assert state is not None
        outcome["needs_a_decision"] = self._undecided_posts(state)
        outcome["next"] = (
            "record_judgement for the account's candidate, then verify_claims or "
            "dismiss_evidence for each post listed"
            if outcome["finished"]
            else "read_posts again until finished is true"
        )
        return outcome

    # Identity

    def list_candidates(self, case_id: str) -> dict:
        """The distinct people the evidence could refer to, and what each allows."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        listed = []
        for candidate in state.candidates:
            comparison = state.comparison(candidate)
            judgement = state.judgements.get(candidate.candidate_id)
            listed.append(
                {
                    "candidate_id": candidate.candidate_id,
                    "names": candidate.names,
                    "date_of_birth": candidate.date_of_birth,
                    "residence_city": candidate.residence_city,
                    "residence_state": candidate.residence_state,
                    "stated_identifier": candidate.stated_identifier,
                    "evidence_ids": candidate.evidence_ids,
                    "comparison": identity.explain(comparison),
                    "allowed_decisions": [
                        d.value for d in identity.allowed_decisions(comparison)
                    ],
                    "would_settle": identity.what_would_settle(comparison),
                    "judgement": judgement.decision.value if judgement else None,
                    "judged_before_new_evidence": candidate.candidate_id in state.stale,
                }
            )
        return {"candidates": listed}

    def record_judgement(
        self, case_id: str, candidate_id: str, decision: str, reason: str
    ) -> dict:
        """Record whether a candidate is the subject, if the identifiers support it."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        candidate = next(
            (c for c in state.candidates if c.candidate_id == candidate_id), None
        )
        if candidate is None:
            return _error(
                f"no candidate '{candidate_id}'",
                known=[c.candidate_id for c in state.candidates],
            )
        comparison = state.comparison(candidate)
        allowed = identity.allowed_decisions(comparison)
        chosen = next((d for d in Decision if d.value == decision), None)
        if chosen not in allowed:
            return _error(
                f"'{decision}' is not supported by the identifiers for {candidate_id}",
                allowed_decisions=[d.value for d in allowed],
                because=identity.explain(comparison),
                would_settle=identity.what_would_settle(comparison),
            )
        if not reason.strip():
            return _error("give a reason for the decision")
        self.store.set_judgement(
            case_id,
            Judgement(
                candidate_id=candidate_id,
                decision=chosen,
                reason=reason.strip(),
                signature=candidate.signature(),
            ),
        )
        self.store.log(
            case_id,
            "record_judgement",
            {"candidate": candidate_id, "decision": chosen.value},
        )
        return {"recorded": {"candidate_id": candidate_id, "decision": chosen.value}}

    # Claims and evidence set aside

    def _check_claim(self, state: _State, claim: object) -> tuple[ClaimStatus, str]:
        if not isinstance(claim, dict):
            return ClaimStatus.FAILED, "a claim needs text, evidence_id and quote"
        text, quote = str(claim.get("text") or ""), str(claim.get("quote") or "")
        record = next(
            (r for r in state.evidence if r.evidence_id == claim.get("evidence_id")),
            None,
        )
        if record is None:
            return ClaimStatus.FAILED, "no evidence with that id in this case"
        if not text.strip():
            return ClaimStatus.FAILED, "the claim has no text"
        if FLAG_PICTURE in record.flags:
            return (
                ClaimStatus.FAILED,
                "a screenshot's record is a task, not a finding: open the picture, "
                "record what was missed, then set the record aside",
            )
        if not quote_is_in(quote, record.text):
            return ClaimStatus.FAILED, "the quote does not appear in that evidence"
        candidate = state.owner(record.evidence_id)
        judgement = state.judgements.get(candidate.candidate_id)
        if judgement is None:
            why = f"{candidate.candidate_id} has no current judgement; record one first"
            return ClaimStatus.FAILED, why
        if judgement.decision is Decision.DIFFERENT_PERSON:
            return (
                ClaimStatus.REJECTED,
                "that evidence is about someone judged a different person",
            )
        self.store.add_claim(
            state.case.case_id, record.evidence_id, text.strip(), quote.strip()
        )
        if judgement.decision is Decision.UNSURE:
            return (
                ClaimStatus.UNCONFIRMED,
                "kept as a lead; the person's identity is unsure",
            )
        return ClaimStatus.VERIFIED, ""

    def verify_claims(self, case_id: str, claims: list) -> dict:
        """Check each claim's quote against the evidence it cites."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        if not isinstance(claims, list):
            return _error("claims must be a list")
        results = []
        for claim in claims:
            status, why = self._check_claim(state, claim)
            evidence_id = claim.get("evidence_id") if isinstance(claim, dict) else None
            results.append(
                {"evidence_id": evidence_id, "result": status.value, "why": why}
            )
        tally = {status.value: 0 for status in ClaimStatus}
        for result in results:
            tally[result["result"]] += 1
        self.store.log(case_id, "verify_claims", tally)
        return {"results": results}

    def dismiss_evidence(self, case_id: str, evidence_id: str, reason: str) -> dict:
        """Set a record aside. The reason is printed in the brief."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        if not any(record.evidence_id == evidence_id for record in state.evidence):
            return _error(f"no evidence '{evidence_id}' in this case")
        if not reason.strip():
            return _error("give a reason; it is printed in the brief")
        self.store.set_dismissal(case_id, evidence_id, reason.strip())
        self.store.log(case_id, "dismiss_evidence", {"evidence": evidence_id})
        return {"set_aside": evidence_id}

    # The brief

    def _outstanding(self, state: _State) -> dict[str, list[str]]:
        case_id = state.case.case_id
        runs = self.store.source_runs(case_id)
        cited = {claim["evidence_id"] for claim in self.store.claims(case_id)}
        set_aside = set(self.store.dismissals(case_id))
        unjudged = [
            c.candidate_id
            for c in state.candidates
            if c.candidate_id not in state.judgements
        ]
        unaccounted = []
        for record in state.evidence:
            judgement = state.judgements.get(
                state.owner(record.evidence_id).candidate_id
            )
            someone_else = (
                judgement is not None
                and judgement.decision is Decision.DIFFERENT_PERSON
            )
            # A record that addresses an automated reader is printed in the brief
            # without being cited. A post is different: it may say more than that,
            # so it still needs a decision.
            printed_anyway = (
                INSTRUCTIONS_FLAG in record.flags
                and not NEEDS_DECISION & set(record.flags)
            )
            reported = record.evidence_id in cited or printed_anyway
            if not (someone_else or reported or record.evidence_id in set_aside):
                unaccounted.append(record.evidence_id)
        unread = self.posts.unread(state.case, state.perspective)
        found = {
            "sources_not_run": [
                s.id for s in state.perspective.sources if s.id not in runs
            ],
            "posts_not_read": [
                f"{source}: {count}" for source, count in sorted(unread.items())
            ],
            "screenshots_not_taken_in": [
                f"{intake.source}, {intake.account}, folder {intake.folder}: "
                f"{intake.waiting}"
                for intake in self.store.intakes(case_id)
                if intake.waiting
            ],
            "candidates_without_a_judgement": unjudged,
            "evidence_neither_cited_nor_set_aside": unaccounted,
        }
        return {key: value for key, value in found.items() if value}

    def render_brief(self, case_id: str) -> dict:
        """Produce the brief, or say exactly what still has to be done."""
        state = self._state(case_id)
        if state is None:
            return _error(f"no case '{case_id}'")
        outstanding = self._outstanding(state)
        if outstanding:
            return _error(
                "the brief is not ready",
                outstanding=outstanding,
                how=(
                    "run_source for each source; record_screenshots until none wait; "
                    "read_posts until every post is read; "
                    "record_judgement for each candidate; then verify_claims or "
                    "dismiss_evidence for each record"
                ),
            )
        brief = build_brief(
            case=state.case,
            perspective=state.perspective,
            evidence=state.evidence,
            candidates=state.candidates,
            judgements=state.judgements,
            comparisons={c.candidate_id: state.comparison(c) for c in state.candidates},
            claims=self.store.claims(case_id),
            dismissals=self.store.dismissals(case_id),
            runs=self.store.source_runs(case_id),
            posts=self.posts.summary(state.case, state.perspective),
            screenshots=self.posts.screenshots(case_id),
        )
        self.store.log(case_id, "render_brief", {"confirmed": len(brief["confirmed"])})
        return {"brief": brief, "markdown": to_markdown(brief)}
