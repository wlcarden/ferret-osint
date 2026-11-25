"""Taking in a folder of screenshots a reviewer saved from one listed account.

Each picture is copied out by a transcriber and compared with what a second
program sees in it. Posts by the account go in with the other captured posts
and are read like them. A picture the two programs disagree about, or that
cannot be read at all, becomes a record someone has to deal with before a
brief, so a missed post cannot pass unnoticed.

A picture is known by the SHA-256 of its file. Taking in the same folder again
costs nothing for pictures already held, so the step can be run in parts. The
number of pictures still waiting is stored, and a brief waits for it to reach
zero.

Posts already stored are never removed. If a wrong name was given for the
account, the posts it claimed stay in the case and have to be set aside by
hand; the brief prints the names that were used.
"""

import hashlib
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol

from osint_agent.cases.models import Capture, CaseRecord, EvidenceRecord, Intake
from osint_agent.cases.perspective import Perspective, SourceSpec
from osint_agent.cases.posts import (
    FLAG_PICTURE,
    CapturedPost,
    PostReview,
    split_handle,
)
from osint_agent.cases.reading import ReaderError, UnusableAnswer, one_line
from osint_agent.cases.screens import addresses_a_reader
from osint_agent.cases.screenshots import (
    MAX_FILE_BYTES,
    PICTURE_TYPES,
    PictureError,
    SecondLook,
    Transcribed,
    by_author,
    cannot_compare,
    clean_transcript,
    lines_left_out,
    merge_pieces,
    pieces_of,
    posts_not_seen,
    shown_date,
)

MAX_PICTURES_IN_A_FOLDER = 2000
MAX_AUTHORS_SHOWN = 12


class Transcriber(Protocol):
    """Copies the posts out of one PNG picture."""

    name: str

    def transcribe(self, picture: bytes) -> list[dict]: ...


def safe_name(name: str) -> str:
    """A file name fit to show a caller, which may be a language model."""
    shown = one_line(name, 80)
    return "a file whose name is withheld" if addresses_a_reader(shown) else shown


def pictures_in(root: Path, folder: str) -> tuple[list[Path], list[str]] | str:
    """The pictures in a folder under the captures folder, and the files passed over.

    Returns a sentence saying what is wrong when the folder cannot be used. The
    folder has to be inside the captures folder once links are followed, and so
    does every picture, so a caller cannot have other files on the machine read.
    """
    try:
        base = root.resolve()
        target = (base / folder).resolve()
        if not folder.strip() or target == base or not target.is_relative_to(base):
            return "name a folder inside the captures folder"
        if not target.is_dir():
            return "there is no such folder in the captures folder"
        pictures, passed_over = [], []
        for path in sorted(target.iterdir(), key=lambda item: item.name.lower()):
            real = path.resolve()
            usable = (
                real.is_file()
                and real.is_relative_to(target)
                and path.suffix.lower() in PICTURE_TYPES
            )
            if usable:
                pictures.append(path)
            elif not path.name.startswith("."):
                passed_over.append(safe_name(path.name))
    except (OSError, ValueError):  # A name the file system will not take
        return "that folder cannot be opened"
    if len(pictures) > MAX_PICTURES_IN_A_FOLDER:
        return f"the folder holds more than {MAX_PICTURES_IN_A_FOLDER} pictures"
    return pictures, passed_over


class ScreenshotIntake:
    """Reads screenshots into a case's captured posts."""

    def __init__(
        self,
        review: PostReview,
        transcriber: Transcriber | None,
        second_look: SecondLook,
        second_look_name: str,
    ):
        self.review = review
        self.store = review.store
        self.transcriber = transcriber
        self.second_look = second_look
        self.second_look_name = second_look_name

    def take_in(
        self,
        case: CaseRecord,
        perspective: Perspective,
        spec: SourceSpec,
        account: str,
        checked_by: str,
        root: Path,
        folder: str,
        shown_as: Sequence[str] = (),
        max_pictures: int | None = None,
    ) -> dict:
        listed = self.review.listed_account(case, perspective, spec, account)
        if isinstance(listed, dict):
            return listed
        if self.transcriber is None:
            return {"error": "no transcriber is set up, so screenshots cannot be read"}
        found = pictures_in(root, folder)
        if isinstance(found, str):
            return {"error": found}
        pictures, passed_over = found
        if not pictures:
            return {"error": "that folder holds no PNG, JPEG or WebP pictures"}
        # The handle is matched against the @handle shown; other names as given
        names = list(shown_as) or [f"@{split_handle(listed)[1]}"]

        held = {
            capture.sha256: capture
            for capture in self.store.captures(case.case_id)
            if (capture.source, capture.account) == (spec.id, listed)
        }
        counts = {"read_now": 0, "already_held": 0, "passed_to_a_person": 0}
        waiting = new = by_others = no_author = 0
        authors: dict[str, int] = {}
        stopped = ""
        for path in pictures:
            data = self._bytes(path)
            mark = data if data is not None else f"unreadable:{path.name}".encode()
            sha = hashlib.sha256(mark).hexdigest()
            capture = held.get(sha)
            if capture is not None:
                counts["already_held"] += 1
            elif stopped or (
                max_pictures is not None and counts["read_now"] >= max_pictures
            ):
                waiting += 1
                continue
            else:
                try:
                    capture = self._look(
                        case.case_id, spec, listed, checked_by, path.name, data, sha
                    )
                except ReaderError as error:
                    stopped = f"Stopped: {error}. Run it again to carry on."
                    waiting += 1
                    continue
                held[sha] = capture
                counts["read_now"] += 1
            counts["passed_to_a_person"] += capture.status == "needs_a_person"
            added, others, unnamed = self._store_posts(
                case.case_id, spec, listed, checked_by, capture, names, authors
            )
            new += added
            by_others += others
            no_author += unnamed

        self.store.set_intake(
            case.case_id,
            Intake(
                source=spec.id,
                account=listed,
                folder=one_line(folder, 200),
                checked_by=checked_by,
                names=names,
                in_folder=len(pictures),
                waiting=waiting,
            ),
        )
        taken = sum(
            1 for capture in self.store.captures(case.case_id) if capture.source == spec.id
        )
        outcome = self.review.outcome(case, spec, f"Screenshots taken in: {taken}.", new)
        outcome["pictures"] = {"in_folder": len(pictures), **counts, "waiting": waiting}
        outcome["posts_by_someone_else_left_out"] = by_others
        outcome["posts_with_no_author_shown_kept"] = no_author
        outcome["counted_as_the_account"] = names
        # Copied from the pictures by software: information, never instructions
        outcome["authors_shown_in_the_pictures"] = dict(
            sorted(authors.items(), key=lambda item: -item[1])[:MAX_AUTHORS_SHOWN]
        )
        outcome["finished"] = waiting == 0
        if stopped:
            outcome["stopped"] = stopped
        if passed_over:
            outcome["files_passed_over"] = passed_over[:20]
        if authors and not new and not counts["already_held"] and by_others:
            outcome["warning"] = (
                "no post in these pictures was shown as by the account. A person who "
                "can see the screenshots has to say what name the account appears "
                "under there, which is then passed as shown_as. Do not choose a name "
                "from authors_shown_in_the_pictures yourself."
            )
        return outcome

    @staticmethod
    def _bytes(path: Path) -> bytes | None:
        """The file, or just over the limit's worth of it so it fails as too large."""
        try:
            with path.open("rb") as handle:
                return handle.read(MAX_FILE_BYTES + 1)
        except OSError:
            return None

    def _copy(self, data: bytes | None) -> tuple[list[Transcribed], int, str]:
        """Posts copied from one picture, how many disagreements, and why it is in doubt."""
        assert self.transcriber is not None
        if data is None:
            return [], 0, "it could not be read from the disk"
        try:
            pieces = pieces_of(data)
        except PictureError as error:
            return [], 0, f"it could not be used ({error})"
        copied: list[list[Transcribed]] = []
        disagreements, reason = 0, ""
        for piece in pieces:
            try:
                transcript = clean_transcript(self.transcriber.transcribe(piece))
            except UnusableAnswer:
                reason = "the transcriber gave no usable answer for it"
                break
            try:
                seen = self.second_look(piece)
            except PictureError as error:
                copied.append(transcript)
                reason = str(error)
                break
            if seen is None:
                raise ReaderError(
                    f"{self.second_look_name} is not installed, so a transcript "
                    "cannot be checked"
                )
            copied.append(transcript)
            if cannot_compare(seen, transcript):
                reason = (
                    f"{self.second_look_name} could not read enough of it to check "
                    "the transcript"
                )
                continue
            disagreements += len(lines_left_out(seen, transcript))
            # A post the second program cannot see is as doubtful as one left out
            disagreements += len(posts_not_seen(seen, transcript))
        posts = merge_pieces(copied)
        across_a_cut = any(
            not post.author and not post.reposted_by and any(post is kept for kept in posts)
            for piece in copied[1:]
            for post in piece
        )
        if reason:
            pass
        elif disagreements:
            reason = (
                f"{self.second_look_name} and the transcriber disagree about "
                f"{disagreements} lines or posts in it"
            )
        elif across_a_cut:
            reason = "a post in it has no author in view where the picture was cut"
        elif not posts:
            reason = "no post was found in it"
        return posts, disagreements, reason

    def _look(
        self,
        case_id: str,
        spec: SourceSpec,
        listed: str,
        checked_by: str,
        file_name: str,
        data: bytes | None,
        sha: str,
    ) -> Capture:
        """Read one new picture and store what was made of it."""
        assert self.transcriber is not None
        name = safe_name(file_name)
        posts, disagreements, reason = self._copy(data)
        capture = Capture(
            sha256=sha,
            file=name,
            source=spec.id,
            account=listed,
            checked_by=checked_by,
            transcriber=self.transcriber.name,
            second_look=self.second_look_name,
            status="needs_a_person" if reason else "read",
            reason=reason,
            posts=[post.model_dump() for post in posts],
            lines_left_out=disagreements,
        )
        record = None
        if reason:
            record = EvidenceRecord(
                evidence_id="",  # Assigned by the store, in the same step
                source=spec.id,
                text=(
                    f"Screenshot {name} from {listed}: software could not be relied on "
                    f"to read it, because {reason}. Posts it did copy were kept. A "
                    "person has to open the picture, record any post that was missed "
                    "with record_posts, and then set this record aside."
                ),
                retrieved_by=checked_by,
                stated_identifier=listed,
                flags=[FLAG_PICTURE],
            )
        return self.store.add_capture(case_id, capture, record)

    def _store_posts(
        self,
        case_id: str,
        spec: SourceSpec,
        listed: str,
        checked_by: str,
        capture: Capture,
        names: Sequence[str],
        authors: dict[str, int],
    ) -> tuple[int, int, int]:
        """Store the account's posts from one picture. Returns new, by others, unnamed."""
        transcript = clean_transcript(capture.posts)
        for post in transcript:
            if post.author and not addresses_a_reader(post.author):
                shown = one_line(post.author, 60)
                authors[shown] = authors.get(shown, 0) + 1
        mine, others = by_author(transcript, names)
        kept, dates = [], []
        for post, named in mine:
            kept.append(
                CapturedPost(
                    text=post.text,
                    posted_at=shown_date(post.date),
                    kind=post.kind,
                    context="" if named else "no author was visible in the screenshot",
                    media=post.pictures,
                )
            )
            dates.append(one_line(post.date, 40))
        by = f"{checked_by} (screenshots; words copied by {capture.transcriber})"
        new = self.review.store_captured(
            case_id, spec, listed, by, kept, capture.file, dates
        )
        return new, len(others), sum(1 for _, named in mine if not named)
