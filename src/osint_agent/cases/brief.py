"""Build the brief for a case from what is stored.

The brief is assembled by code from stored evidence, recorded judgements and
claims whose quotes were found in the evidence. Whoever ran the review writes
none of it directly, so nothing reaches the reader that was not checked.

It also shows its own limits: what was not stated about the subject, which
sources failed or still need a person to look, which records were set aside and
why, and which records concern someone else. It never gives a verdict.
"""

from osint_agent.cases import identity
from osint_agent.cases.identity import Comparison
from osint_agent.cases.models import (
    Candidate,
    CaseRecord,
    Decision,
    EvidenceRecord,
    Judgement,
    SourceRun,
    SourceStatus,
)
from osint_agent.cases.perspective import Perspective

INSTRUCTIONS_FLAG = "contains_instructions"

NO_VERDICT = (
    "This brief reports evidence and how firmly each record is tied to the subject. "
    "It gives no verdict; the decision belongs to the reviewers."
)

_STATUS_WORDS = {
    SourceStatus.ANSWERED: "returned records",
    SourceStatus.EMPTY: "searched, nothing found",
    SourceStatus.FAILED: "failed, not searched",
    SourceStatus.NOT_CONFIGURED: "not available here, not searched",
    SourceStatus.MANUAL_PENDING: "waiting for a person to look",
}

# A source that collects posts has nothing to search: it either has posts or not.
_POST_STATUS_WORDS = {
    SourceStatus.ANSWERED: "posts collected",
    SourceStatus.EMPTY: "nothing collected",
}


def _meaning(kind: str, status: SourceStatus) -> str:
    """What a source's status means, in the words the brief uses."""
    if kind in ("posts", "capture") and status in _POST_STATUS_WORDS:
        return _POST_STATUS_WORDS[status]
    return _STATUS_WORDS[status]


def _excerpt(text: str, limit: int = 240) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: limit - 1].rstrip() + "…"


def _not_given(case: CaseRecord) -> list[str]:
    subject = case.subject
    missing = []
    if not identity.parse_name(subject.name).middle:
        missing.append("middle name")
    if not subject.date_of_birth:
        missing.append("date of birth")
    if not subject.residence_city:
        missing.append("city of residence")
    if not subject.residence_state:
        missing.append("state of residence")
    if not subject.handles:
        missing.append("online handles")
    return missing


def _describe(candidate: Candidate) -> str:
    if candidate.stated_identifier:
        return f"account the subject listed ({candidate.stated_identifier})"
    if not candidate.names:
        return "records with no identifying details"
    residence = ", ".join(
        p for p in (candidate.residence_city, candidate.residence_state) if p
    )
    parts = [
        " / ".join(candidate.names),
        (
            f"born {candidate.date_of_birth}"
            if candidate.date_of_birth
            else "date of birth not shown"
        ),
        residence or "residence not shown",
    ]
    return "; ".join(parts)


def build_brief(
    case: CaseRecord,
    perspective: Perspective,
    evidence: list[EvidenceRecord],
    candidates: list[Candidate],
    judgements: dict[str, Judgement],
    comparisons: dict[str, Comparison],
    claims: list[dict[str, str]],
    dismissals: dict[str, str],
    runs: dict[str, SourceRun],
    posts: dict[str, dict] | None = None,
) -> dict:
    """Assemble the brief as plain data. Every list may be empty.

    `posts` says, for each source that collected posts, how many there were and
    how they were read.
    """
    by_id = {record.evidence_id: record for record in evidence}
    owner = {
        eid: candidate for candidate in candidates for eid in candidate.evidence_ids
    }

    def decision_for(evidence_id: str) -> Decision:
        return judgements[owner[evidence_id].candidate_id].decision

    def finding(claim: dict[str, str]) -> dict:
        record = by_id[claim["evidence_id"]]
        return {
            "text": claim["text"],
            "quote": claim["quote"],
            "record": _excerpt(record.text, 400),
            "evidence_id": record.evidence_id,
            "source": record.source,
            "url": record.url,
            "retrieved_at": record.retrieved_at,
            "retrieved_by": record.retrieved_by,
        }

    confirmed, unconfirmed = [], []
    for claim in claims:
        decision = decision_for(claim["evidence_id"])
        if decision is Decision.SAME_PERSON:
            confirmed.append(finding(claim))
        elif decision is Decision.UNSURE:
            candidate = owner[claim["evidence_id"]]
            lead = finding(claim)
            lead["candidate"] = _describe(candidate)
            lead["would_settle"] = identity.what_would_settle(
                comparisons[candidate.candidate_id]
            )
            unconfirmed.append(lead)

    identities, not_the_subject = [], []
    for candidate in candidates:
        judgement = judgements[candidate.candidate_id]
        entry = {
            "candidate_id": candidate.candidate_id,
            "description": _describe(candidate),
            "decision": judgement.decision.value,
            "reason": judgement.reason,
            "comparison": identity.explain(comparisons[candidate.candidate_id]),
            "evidence_ids": candidate.evidence_ids,
        }
        identities.append(entry)
        if judgement.decision is Decision.DIFFERENT_PERSON:
            not_the_subject.append(entry)

    coverage = []
    for spec in perspective.sources:
        run = runs.get(spec.id)
        coverage.append(
            {
                "source": spec.id,
                "title": spec.title,
                "status": run.status.value if run else "not run",
                "meaning": _meaning(spec.kind, run.status) if run else "not run",
                "note": run.note if run else "",
                "covers": " ".join(spec.covers.split()),
                "url": spec.url,
                "posts": (posts or {}).get(spec.id),
            }
        )
    statements = perspective.statements
    reading_rules = None
    if statements is not None:
        reading_rules = {
            "report": [" ".join(item.split()) for item in statements.report],
            "leave_out": [" ".join(item.split()) for item in statements.leave_out],
            "lookback_years": statements.lookback_years,
        }

    return {
        "title": perspective.title,
        "case_id": case.case_id,
        "perspective": perspective.name,
        "purpose": " ".join(perspective.purpose.split()),
        "no_verdict": NO_VERDICT,
        "subject": {"stated": case.subject.model_dump(), "not_given": _not_given(case)},
        "basis": {
            "basis": case.basis.value,
            "note": case.basis_note,
            "opened_by": case.opened_by,
            "opened_at": case.opened_at,
        },
        "identity": identities,
        "confirmed": confirmed,
        "unconfirmed": unconfirmed,
        "not_the_subject": not_the_subject,
        "instructions_in_evidence": [
            {
                "evidence_id": r.evidence_id,
                "source": r.source,
                "excerpt": _excerpt(r.text),
            }
            for r in evidence
            if INSTRUCTIONS_FLAG in r.flags
        ],
        "reviewed_not_reported": [
            {"evidence_id": eid, "source": by_id[eid].source, "reason": reason}
            for eid, reason in sorted(
                dismissals.items(), key=lambda item: int(item[0][1:])
            )
        ],
        "coverage": coverage,
        "criteria": {
            "include": [" ".join(item.split()) for item in perspective.report.include],
            "exclude": [" ".join(item.split()) for item in perspective.report.exclude],
            "statements": reading_rules,
        },
        "caveats": [" ".join(item.split()) for item in perspective.caveats],
    }


def _post_lines(found: dict) -> list[str]:
    """How one source's posts were collected and what became of each.

    Every post is in exactly one of the groups named, so they add up to the
    number collected.
    """
    span = f", dated {found['first']} to {found['last']}" if found["first"] else ""
    people = [name for name in found["collected_by"] if name != "adapter"]
    how = f"captured by {' and '.join(people)}" if people else "collected by program"
    accounts = ", ".join(found["accounts"])
    lines = [f"  - Posts: {found['collected']} from {accounts}{span}, {how}."]
    times = {1: "once", 2: "twice", 3: "three times"}[found["readings"]]
    readers = f" ({', '.join(found['readers'])})" if found["readers"] else ""
    parts = []
    for key, wording in (
        ("cleared", f"read {times} by software{readers} and not reported"),
        ("reported", "reported by software for a decision"),
        ("addressed_to_a_reader", "addressed to an automated reader"),
        ("passed_to_a_person", "passed to a person because software could not be relied on"),
        ("not_read", "not read"),
        ("image_only_not_read", "with images only, not read"),
        ("empty", "with nothing in them"),
    ):
        if found[key]:
            parts.append(f"{found[key]} {wording}")
    if found["outside_review_period"]:
        parts.append(
            f"{found['outside_review_period']} from before "
            f"{found['review_period_starts']}, not read"
        )
    lines.append(f"  - Of those: {'; '.join(parts)}.")
    if found["cleared"]:
        lines.append("  - No person read the posts that software did not report.")
    return lines


def _finding_lines(item: dict) -> list[str]:
    where = f"{item['source']}, {item['evidence_id']}, retrieved {item['retrieved_at']}"
    if item["retrieved_by"] != "adapter":
        where += f" by {item['retrieved_by']}"
    lines = [
        f"- {item['text']}",
        f'  - Quote: "{item["quote"]}"',
        f"  - Source: {where}",
    ]
    if len(item["record"]) > len(item["quote"]):
        # A quote is chosen by whoever ran the review; the record gives its context.
        lines.insert(2, f'  - In context: "{item["record"]}"')
    if item.get("url"):
        lines.append(f"  - Link: {item['url']}")
    return lines


def to_markdown(brief: dict) -> str:
    """Render a brief for a person to read."""
    stated = brief["subject"]["stated"]
    lines = [f"# {brief['title']}: {stated['name']}", "", brief["no_verdict"], ""]

    lines += ["## Subject and basis", ""]
    for label, key in (
        ("Date of birth", "date_of_birth"),
        ("City", "residence_city"),
        ("State", "residence_state"),
    ):
        if stated.get(key):
            lines.append(f"- {label}: {stated[key]}")
    if stated.get("handles"):
        lines.append(f"- Handles listed: {', '.join(stated['handles'])}")
    if brief["subject"]["not_given"]:
        lines.append(f"- Not given: {', '.join(brief['subject']['not_given'])}")
    basis = brief["basis"]
    basis_line = f"- Basis for this review: {basis['basis']}"
    if basis["note"]:
        basis_line += f" ({basis['note']})"
    lines += [
        basis_line,
        f"- Opened {basis['opened_at']}"
        + (f" by {basis['opened_by']}" if basis["opened_by"] else ""),
        "",
    ]

    lines += ["## Confirmed findings", ""]
    if brief["confirmed"]:
        for item in brief["confirmed"]:
            lines += _finding_lines(item)
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Leads that could not be tied to the subject or ruled out", ""]
    if brief["unconfirmed"]:
        for item in brief["unconfirmed"]:
            lines += _finding_lines(item)
            lines.append(f"  - Record is about: {item['candidate']}")
            if item["would_settle"]:
                lines.append(f"  - Would settle it: {'; '.join(item['would_settle'])}")
    else:
        lines.append("None.")
    lines.append("")

    lines += ["## Who the records are about", ""]
    for entry in brief["identity"]:
        decision = entry["decision"].replace("_", " ")
        records = ", ".join(entry["evidence_ids"])
        lines.append(
            f"- {entry['candidate_id']} ({entry['description']}): {decision}. "
            f"{entry['reason']} {entry['comparison']} Records: {records}."
        )
    if not brief["identity"]:
        lines.append("No records were returned.")
    lines.append("")

    if brief["instructions_in_evidence"]:
        lines += ["## Instructions found inside evidence", ""]
        lines.append(
            "These records contain text addressed to an automated reviewer. It was not acted on."
        )
        for item in brief["instructions_in_evidence"]:
            lines.append(
                f'- {item["evidence_id"]} ({item["source"]}): "{item["excerpt"]}"'
            )
        lines.append("")

    if brief["reviewed_not_reported"]:
        lines += ["## Reviewed and not reported", ""]
        for item in brief["reviewed_not_reported"]:
            lines.append(
                f"- {item['evidence_id']} ({item['source']}): {item['reason']}"
            )
        lines.append("")

    lines += ["## What was checked", ""]
    for item in brief["coverage"]:
        line = f"- {item['title']}: {item['meaning']}."
        if item["note"]:
            line += f" {item['note']}"
        lines.append(line)
        if item["covers"]:
            lines.append(f"  - Covers: {item['covers']}")
        if item.get("posts"):
            lines += _post_lines(item["posts"])
    lines.append("")

    lines += ["## Limits", ""]
    lines += [f"- {caveat}" for caveat in brief["caveats"]] or ["- None stated."]
    lines.append("")

    lines += ["## What this review reports", ""]
    lines += [f"- Reports: {item}" for item in brief["criteria"]["include"]]
    lines += [f"- Leaves out: {item}" for item in brief["criteria"]["exclude"]]
    rules = brief["criteria"].get("statements")
    if rules:
        lines += [f"- In posts, reports: {item}" for item in rules["report"]]
        lines += [f"- In posts, leaves out: {item}" for item in rules["leave_out"]]
        if rules["lookback_years"]:
            years = rules["lookback_years"]
            lines.append(
                f"- Reads posts from the last {years} year{'s' if years != 1 else ''}."
            )
    return "\n".join(lines).rstrip() + "\n"
