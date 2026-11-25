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
* a brief is refused until every source was tried, every candidate judged and
  every record either cited, set aside with a reason, or about someone else.
"""

import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field, ValidationError

from osint_agent.cases import identity
from osint_agent.cases.brief import INSTRUCTIONS_FLAG, build_brief, to_markdown
from osint_agent.cases.identity import Comparison
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
from osint_agent.cases.store import CaseStore

MAX_RETURNED_TEXT = 2000
MIN_QUOTE_LENGTH = 15

# Text that addresses an automated reader. A match is flagged, never obeyed.
_INSTRUCTION_PATTERN = re.compile(
    r"ignore (all |any )?(previous|prior|above|earlier) instructions"
    r"|system (notice|prompt|message|override)"
    r"|\b(ai|llm) (agents?|assistants?|models?)\b"
    r"|do not (mention|reveal|report) this"
    r"|\b(record_judgement|verify_claims|render_brief|dismiss_evidence|run_source)\b",
    re.IGNORECASE,
)


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


def squash(text: str) -> str:
    """Lower-case with runs of whitespace collapsed, for comparing quotes."""
    return " ".join(text.lower().split())


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
    ):
        self.store = store
        self.sources = dict(sources or {})  # A copy: callers may reuse theirs
        self.perspectives_dir = perspectives_dir
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
            handle.lstrip("@").strip() for handle in facts.handles if handle.strip()
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
            flags=[INSTRUCTIONS_FLAG] if _INSTRUCTION_PATTERN.search(raw.text) else [],
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
        needed = min(MIN_QUOTE_LENGTH, len(squash(record.text)))
        if len(squash(quote)) < needed or squash(quote) not in squash(record.text):
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
            reported = record.evidence_id in cited or INSTRUCTIONS_FLAG in record.flags
            if not (someone_else or reported or record.evidence_id in set_aside):
                unaccounted.append(record.evidence_id)
        found = {
            "sources_not_run": [
                s.id for s in state.perspective.sources if s.id not in runs
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
                    "run_source for each source; record_judgement for each candidate; "
                    "then verify_claims or dismiss_evidence for each record"
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
        )
        self.store.log(case_id, "render_brief", {"confirmed": len(brief["confirmed"])})
        return {"brief": brief, "markdown": to_markdown(brief)}
