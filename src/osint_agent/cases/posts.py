"""Posts from accounts the subject listed: collecting, reading, promoting.

An account can hold thousands of posts, far too many to treat each as a record
someone must cite or set aside. So posts are stored apart from evidence and
read in bulk by a reader. A post becomes evidence, and so needs a decision,
only when something asks for it to be looked at:

* a reader reported it, as a statement or as an instruction addressed to it;
* code found wording in it that addresses an automated reader, so the reader
  was never shown it;
* the reader could not be shown to have read it;
* it is too long to hand to the reader.

Every other post is recorded as read, with the readings that covered it.

Three checks stand between a reader's mistake and a post being passed over.
Each post is read more than once, beside different posts each time. Every batch
carries a control post the reader must report, and a batch whose answer leaves
it out is split and read again. A post with violent wording that no reading
reported is read once more in a small batch.
"""

import hashlib
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date

from pydantic import BaseModel, Field, ValidationError, model_validator

from osint_agent.cases.brief import INSTRUCTIONS_FLAG
from osint_agent.cases.models import (
    CaseRecord,
    Collection,
    EvidenceRecord,
    Post,
    SourceStatus,
    clean_timestamp,
)
from osint_agent.cases.perspective import Perspective, SourceSpec, StatementsSpec
from osint_agent.cases.reading import (
    Reader,
    ReaderError,
    batches,
    build_instructions,
    check_answer,
    reading_order,
    render_listing,
    too_long,
)
from osint_agent.cases.screens import addresses_a_reader, has_watch_words
from osint_agent.cases.store import CaseStore

PostCollector = Callable[[str, str | None], Collection]

READ_CLEAR, READ_FLAGGED = "clear", "flagged"
WORDS_READING = "words"  # The extra reading for posts with violent wording
WORDS_BATCH = 10
MAX_SPLITS = 2  # Times a distrusted batch is split before a person must read it

FLAG_READER = "flagged_by_reader"
FLAG_UNREAD = "reader_unreliable"
FLAG_TOO_LONG = "too_long_for_reader"
FLAG_NOT_SHOWN = "not_shown_to_reader"
# A promoted post carrying any of these must be cited or set aside by someone.
NEEDS_DECISION = frozenset({FLAG_READER, FLAG_UNREAD, FLAG_TOO_LONG, FLAG_NOT_SHOWN})

_PLATFORM = re.compile(r"[A-Za-z][A-Za-z0-9_-]{1,30}")


def split_handle(listed: str) -> tuple[str | None, str]:
    """A listed handle as (platform, name).

    "bluesky:@me.bsky.social" gives ("bluesky", "me.bsky.social"). A handle with
    no platform in front gives (None, name).
    """
    platform, colon, name = listed.partition(":")
    if colon and _PLATFORM.fullmatch(platform.strip()) and not name.startswith("//"):
        return platform.strip().lower(), name.strip().lstrip("@").strip()
    return None, listed.strip().lstrip("@").strip()


def normalise_handle(listed: str) -> str:
    """A listed handle in the one spelling the case keeps."""
    platform, name = split_handle(listed)
    return f"{platform}:{name}" if platform else name


def evidence_text(post: Post) -> str:
    """A post as it reads once promoted to evidence."""
    when = (post.posted_at or "an unknown date")[:10]
    text = f"{post.kind.capitalize()} by {post.account} on {when}: {post.text}"
    if post.context:
        text += f" [{post.context}]"
    if post.media:
        text += f" ({post.media} image or video attached, not read)"
    return text


class CapturedPost(BaseModel):
    """One post as a person recorded it."""

    text: str = ""
    posted_at: str | None = None
    kind: str = "post"
    context: str = ""
    url: str | None = None
    media: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _is_usable(self) -> "CapturedPost":
        if not self.text.strip() and not self.media:
            raise ValueError("a post needs its text, or a count of its images")
        if self.posted_at is not None and clean_timestamp(self.posted_at) is None:
            raise ValueError("posted_at must be a date such as 2025-06-02")
        if self.url is not None and not self.url.startswith(("https://", "http://")):
            raise ValueError("url must be a web address")
        if self.kind not in ("post", "reply", "repost"):
            raise ValueError("kind must be post, reply or repost")
        return self


@dataclass
class _Run:
    """Counters for one call to read."""

    case_id: str
    budget: int | None
    calls: int = 0
    flagged: int = 0
    by_code: int = 0
    too_long: int = 0
    unreliable: int = 0
    unknown: int = 0
    stopped: bool = False
    note: str = ""


class PostReview:
    """Collects posts from listed accounts, reads them, and promotes what is flagged."""

    def __init__(
        self,
        store: CaseStore,
        collectors: dict[str, PostCollector] | None = None,
        reader: Reader | None = None,
        today: Callable[[], date] = date.today,
    ):
        self.store = store
        self.collectors = dict(collectors or {})
        self.reader = reader
        self._today = today

    # Which listed accounts a source covers

    @staticmethod
    def accounts_for(
        case: CaseRecord, perspective: Perspective, spec: SourceSpec
    ) -> list[str]:
        """The listed handles this source covers.

        A "posts" source covers the handles on its platform. A "capture" source
        covers every handle that no "posts" source in the perspective covers.
        """
        handles = case.subject.handles
        if spec.kind == "posts":
            return [h for h in handles if split_handle(h)[0] == spec.platform]
        by_program = {s.platform for s in perspective.sources if s.kind == "posts"}
        return [h for h in handles if split_handle(h)[0] not in by_program]

    # Collecting

    def collect(
        self,
        case: CaseRecord,
        perspective: Perspective,
        spec: SourceSpec,
        query: str | None,
    ) -> dict:
        """Fetch the posts of the listed accounts on this source's platform."""
        accounts = self.accounts_for(case, perspective, spec)
        if query is not None:
            wanted = normalise_handle(query).lower()
            accounts = [
                a for a in accounts if wanted in (a.lower(), split_handle(a)[1].lower())
            ]
            if not accounts:
                return {
                    "error": "only accounts the subject listed can be read",
                    "listed": case.subject.handles,
                }
        if not accounts:
            return self._outcome(
                case,
                spec,
                SourceStatus.EMPTY,
                f"The subject listed no {spec.platform} account.",
            )
        collector = self.collectors.get(spec.platform or "")
        if collector is None:
            note = f"No collector is installed for {spec.platform}."
            return self._outcome(case, spec, SourceStatus.NOT_CONFIGURED, note)
        since = self._cutoff(perspective.statements)
        notes, failures, new = [], [], 0
        for account in accounts:
            try:
                found = collector(split_handle(account)[1], since)
            except (
                Exception
            ) as error:  # noqa: BLE001 - a failing collector must not end the review
                failures.append(
                    f"{account}: {type(error).__name__}: {str(error)[:160]}"
                )
                continue
            if found.status is SourceStatus.FAILED:
                failures.append(
                    f"{account}: {found.note or 'the collector reported a failure'}"
                )
                continue
            drafts = [
                Post(post_id="", source=spec.id, account=account, **item.model_dump())
                for item in found.posts
            ]
            new += self.store.add_posts(case.case_id, drafts)
            line = f"{account}: {len(found.posts)} posts"
            if found.note or not found.complete:
                line += f". {found.note or 'Collection was incomplete.'}"
            notes.append(line)
        outcome = self._outcome(
            case, spec, SourceStatus.EMPTY, "; ".join(notes + failures), new
        )
        if outcome["collected"]:
            outcome["status"] = SourceStatus.ANSWERED
        elif failures:
            outcome["status"] = SourceStatus.FAILED
        return outcome

    def record(
        self,
        case: CaseRecord,
        perspective: Perspective,
        spec: SourceSpec,
        account: str,
        checked_by: str,
        posts: Sequence[object],
    ) -> dict:
        """Store posts a person captured from one listed account."""
        accounts = self.accounts_for(case, perspective, spec)
        wanted = normalise_handle(account).lower()
        listed = next((a for a in accounts if a.lower() == wanted), None)
        if listed is None:
            return {
                "error": "posts can only be recorded for an account the subject listed",
                "accounts": accounts,
            }
        try:
            captured = [CapturedPost(**item) for item in posts]  # type: ignore[arg-type]
        except (ValidationError, TypeError) as error:
            return {"error": f"a post is not usable: {error}"}
        drafts = []
        for item in captured:
            mark = f"{listed}|{item.posted_at}|{item.text}|{item.url}".encode()
            drafts.append(
                Post(
                    post_id="",
                    source=spec.id,
                    account=listed,
                    native_id=f"capture:{hashlib.sha256(mark).hexdigest()[:20]}",
                    retrieved_by=checked_by,
                    **item.model_dump(),
                )
            )
        new = self.store.add_posts(case.case_id, drafts)
        note = f"Captured by {checked_by}: {new} posts from {listed}."
        outcome = self._outcome(case, spec, SourceStatus.EMPTY, note, new)
        if outcome["collected"]:
            outcome["status"] = SourceStatus.ANSWERED
        return outcome

    def _outcome(
        self,
        case: CaseRecord,
        spec: SourceSpec,
        status: SourceStatus,
        note: str,
        new: int = 0,
    ) -> dict:
        held = sum(
            1 for post in self.store.posts(case.case_id) if post.source == spec.id
        )
        return {"status": status, "note": note, "collected": held, "new": new}

    # Where each post stands

    def _cutoff(self, spec: StatementsSpec | None) -> str | None:
        """The earliest date inside the review period, or None when there is no limit."""
        if spec is None or spec.lookback_years is None:
            return None
        today = self._today()
        year = today.year - spec.lookback_years
        try:
            return today.replace(year=year).isoformat()
        except ValueError:  # 29 February in a year without one
            return today.replace(year=year, day=28).isoformat()

    @staticmethod
    def _standing(post: Post, cutoff: str | None) -> str:
        """outside, image_only, empty or readable."""
        if cutoff and post.posted_at and post.posted_at[:10] < cutoff:
            return "outside"
        if not post.text.strip():
            return "image_only" if post.media else "empty"
        return "readable"

    @staticmethod
    def _needed(post: Post, reads: dict[str, dict], spec: StatementsSpec) -> list[str]:
        """Readings a readable post still lacks. Empty once it is evidence or fully read."""
        if post.evidence_id:
            return []
        done = reads.get(post.post_id, {})
        missing = [str(n) for n in range(1, spec.readings + 1) if str(n) not in done]
        if not missing and has_watch_words(post.text) and WORDS_READING not in done:
            missing.append(WORDS_READING)
        return missing

    def unread(self, case: CaseRecord, perspective: Perspective) -> dict[str, int]:
        """Posts inside the review period that have not been fully read, by source."""
        spec = perspective.statements
        if spec is None:
            return {}
        cutoff = self._cutoff(spec)
        reads = self.store.post_reads(case.case_id)
        waiting: dict[str, int] = {}
        for post in self.store.posts(case.case_id):
            if self._standing(post, cutoff) == "readable" and self._needed(
                post, reads, spec
            ):
                waiting[post.source] = waiting.get(post.source, 0) + 1
        return waiting

    # Reading

    def read(
        self, case: CaseRecord, perspective: Perspective, max_batches: int | None
    ) -> dict:
        """Read every post that still needs it, and promote what is flagged."""
        spec = perspective.statements
        if spec is None:
            return {"error": "this review does not read posts"}
        cutoff = self._cutoff(spec)
        readable = [
            post
            for post in self.store.posts(case.case_id)
            if self._standing(post, cutoff) == "readable"
        ]
        run = _Run(case_id=case.case_id, budget=max_batches)
        for post in readable:
            if post.evidence_id:
                continue
            if addresses_a_reader(f"{post.text} {post.context}"):
                self._promote(run.case_id, post, [INSTRUCTIONS_FLAG, FLAG_NOT_SHOWN])
                run.by_code += 1
            elif too_long(post):
                self._promote(run.case_id, post, [FLAG_TOO_LONG])
                run.too_long += 1
        reads = self.store.post_reads(case.case_id)
        if self.reader is None and any(self._needed(p, reads, spec) for p in readable):
            return {
                "error": "no reader is configured, so the posts cannot be read",
                "how": "set LLM_BASE_URL and LLM_MODEL, or give the engine a reader",
            }
        instructions = build_instructions(perspective.title, spec)
        readings = [*(str(n) for n in range(1, spec.readings + 1)), WORDS_READING]
        try:
            for reading in readings:
                reads = self.store.post_reads(case.case_id)
                pending = [
                    p for p in readable if reading in self._needed(p, reads, spec)
                ]
                size = WORDS_BATCH if reading == WORDS_READING else spec.batch_size
                ordered = reading_order(pending, case.case_id, reading)
                for chunk in batches(ordered, size):
                    self._read_chunk(run, spec, instructions, reading, chunk, 0)
                if run.stopped:
                    break
        except ReaderError as error:
            run.stopped, run.note = True, f"Reading stopped: {error}."
        return {
            "reader": self.reader.name if self.reader else None,
            "reader_calls": run.calls,
            "flagged_now": run.flagged,
            "addressed_to_a_reader": run.by_code,
            "too_long_for_reader": run.too_long,
            "passed_to_a_person": run.unreliable,
            "answers_naming_no_post": run.unknown,
            "finished": not self.unread(case, perspective),
            "note": run.note,
        }

    def _read_chunk(
        self,
        run: _Run,
        spec: StatementsSpec,
        instructions: str,
        reading: str,
        chunk: Sequence[Post],
        depth: int,
    ) -> None:
        chunk = [post for post in chunk if not post.evidence_id]
        if not chunk or run.stopped:
            return
        if run.budget is not None and run.calls >= run.budget:
            run.stopped, run.note = True, "Stopped at the batch limit given."
            return
        assert self.reader is not None  # read() checked before any batch
        seed = f"{run.case_id}:{reading}:{chunk[0].post_id}:{depth}"
        raw = self.reader.read(
            instructions, render_listing(chunk, spec.control_post, seed)
        )
        run.calls += 1
        answer = check_answer(chunk, raw)
        run.unknown += answer.unknown
        by_id = {post.post_id: post for post in chunk}
        reported = [
            (post_id, "", [INSTRUCTIONS_FLAG, FLAG_READER])
            for post_id in answer.instructions
        ]
        reported += [
            (post_id, quote, [FLAG_READER]) for post_id, quote in answer.flagged.items()
        ]
        for post_id, quote, flags in sorted(
            reported, key=lambda item: int(item[0][1:])
        ):
            # Evidence first. If the read were recorded and the promotion then failed,
            # the post would count as read with nobody left to look at it.
            self._promote(run.case_id, by_id[post_id], flags)
            self.store.add_post_reads(
                run.case_id, reading, self.reader.name, {post_id: (READ_FLAGGED, quote)}
            )
            run.flagged += 1
        rest = [post for post in chunk if not post.evidence_id]
        if answer.instructions:
            # A post that addressed the reader was in this batch, so the reading of
            # its neighbours is not trusted. They are read again without it.
            self._again(run, spec, instructions, reading, [rest], depth)
        elif not answer.control_seen:
            half = (len(rest) + 1) // 2
            self._again(
                run, spec, instructions, reading, [rest[:half], rest[half:]], depth
            )
        else:
            clear = {post.post_id: (READ_CLEAR, "") for post in rest}
            self.store.add_post_reads(run.case_id, reading, self.reader.name, clear)

    def _again(
        self,
        run: _Run,
        spec: StatementsSpec,
        instructions: str,
        reading: str,
        parts: Sequence[Sequence[Post]],
        depth: int,
    ) -> None:
        """Read parts of a distrusted batch again, or hand them to a person."""
        for part in parts:
            if depth < MAX_SPLITS:
                self._read_chunk(run, spec, instructions, reading, part, depth + 1)
                continue
            for post in part:
                self._promote(run.case_id, post, [FLAG_UNREAD])
                run.unreliable += 1

    def _promote(self, case_id: str, post: Post, flags: list[str]) -> None:
        """Turn a post into evidence, which then needs citing or setting aside."""
        if post.evidence_id:
            return
        record = EvidenceRecord(
            evidence_id="",  # Assigned by the store, in the same step as the link
            source=post.source,
            text=evidence_text(post),
            url=post.url,
            retrieved_at=post.retrieved_at,
            retrieved_by=post.retrieved_by,
            stated_identifier=post.account,
            flags=flags,
        )
        post.evidence_id = self.store.add_post_evidence(case_id, post.post_id, record)

    # What was collected and read, for the caller and for the brief

    def promoted(self, case_id: str) -> dict[str, dict]:
        """Posts that became evidence, by evidence id, with the reader's checked quote."""
        reads = self.store.post_reads(case_id)
        found = {}
        for post in self.store.posts(case_id):
            if not post.evidence_id:
                continue
            quotes = [
                r["quote"] for r in reads.get(post.post_id, {}).values() if r["quote"]
            ]
            found[post.evidence_id] = {
                "post_id": post.post_id,
                "posted_at": post.posted_at,
                "kind": post.kind,
                "quote": quotes[0] if quotes else "",
            }
        return found

    def summary(self, case: CaseRecord, perspective: Perspective) -> dict[str, dict]:
        """Per source: how many posts, from when, and what became of each.

        Every post lands in exactly one of the counted groups, so the groups
        add up to the number collected.
        """
        spec = perspective.statements
        if spec is None:
            return {}
        cutoff = self._cutoff(spec)
        reads = self.store.post_reads(case.case_id)
        evidence = {
            record.evidence_id: record for record in self.store.evidence(case.case_id)
        }
        groups = (
            "cleared",
            "reported",
            "addressed_to_a_reader",
            "passed_to_a_person",
            "not_read",
            "image_only_not_read",
            "empty",
            "outside_review_period",
        )
        found: dict[str, dict] = {}
        for post in self.store.posts(case.case_id):
            entry = found.setdefault(
                post.source,
                {
                    "accounts": [],
                    "collected_by": [],
                    "readers": [],
                    "collected": 0,
                    "first": None,
                    "last": None,
                    **{group: 0 for group in groups},
                },
            )
            entry["collected"] += 1
            for key, value in (
                ("accounts", post.account),
                ("collected_by", post.retrieved_by),
            ):
                if value not in entry[key]:
                    entry[key].append(value)
            when = (post.posted_at or "")[:10]
            if when:
                entry["first"] = min(entry["first"] or when, when)
                entry["last"] = max(entry["last"] or when, when)
            for read in reads.get(post.post_id, {}).values():
                if read["reader"] not in entry["readers"]:
                    entry["readers"].append(read["reader"])
            standing = self._standing(post, cutoff)
            flags = set(evidence[post.evidence_id].flags) if post.evidence_id else set()
            if standing == "outside":
                group = "outside_review_period"
            elif standing == "image_only":
                group = "image_only_not_read"
            elif standing == "empty":
                group = "empty"
            elif flags & {FLAG_UNREAD, FLAG_TOO_LONG}:
                group = "passed_to_a_person"
            elif INSTRUCTIONS_FLAG in flags:
                group = "addressed_to_a_reader"
            elif post.evidence_id:
                group = "reported"
            elif self._needed(post, reads, spec):
                group = "not_read"
            else:
                group = "cleared"
            entry[group] += 1
        for entry in found.values():
            entry["readings"] = spec.readings
            entry["review_period_starts"] = cutoff
        return found
