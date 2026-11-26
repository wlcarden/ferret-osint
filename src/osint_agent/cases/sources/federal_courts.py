"""Federal court dockets, searched by party name through CourtListener.

CourtListener's RECAP archive holds federal dockets that someone has already
retrieved from the courts' own system. It is large but not complete, so
finding nothing here does not show that a person has no federal case.

A name search finds namesakes. Every docket returned is therefore a lead
about a person of that name, never a finding about the subject. A docket gives
no date of birth and no address, so a namesake can rarely be ruled in or out
from this source alone.

The record's person is written with the subject's own spelling of the given
name and surname, plus the middle name the docket shows, if it shows one. The
party's name as the docket prints it stays in the record's text. That keeps a
title, a capacity ("individually and as trustee") or an odd spelling from
being mistaken for a different middle name, which would let a docket about the
subject be set aside as someone else's.

Bankruptcy dockets are left out. They are about money, not safety, and a
review should not gather what it has no use for.
"""

import re
import time
import unicodedata
from collections.abc import Callable

import httpx

from osint_agent.cases.engine import RawRecord, SourceResult
from osint_agent.cases.identity import SURNAME_PARTICLES
from osint_agent.cases.models import PersonFacts, SourceStatus

SEARCH_URL = "https://www.courtlistener.com/api/rest/v4/search/"
SITE = "https://www.courtlistener.com"
PAGE_SIZE = 20
MAX_RECORDS = 40
MAX_PAGES = 5
MAX_OTHER_PARTIES = 8
NAME_DISTANCE = 4  # Words allowed between given name and surname in the search
BUSY_RETRIES = 2  # Times to wait and ask again when told to slow down
BUSY_WAIT = 30.0
ARCHIVE_NOTE = (
    "Searched CourtListener's archive of federal dockets, which is not the complete court record."
)

_TITLES = frozenset("dr mr mrs ms miss mx prof hon rev sir".split())
_SUFFIXES = frozenset("jr sr ii iii iv v md phd esq dds jd cpa".split())
# A party with one of these words in it is a body, not a person
_BODY_WORDS = frozenset(
    """association bank church city company corp corporation county department estate
    family foundation fund group inc incorporated llc llp lp ltd partners partnership
    properties services trust united university""".split()
)
_ALSO_KNOWN = re.compile(r"\b(?:d/b/a|dba|a/k/a|aka|f/k/a|fka|n/k/a|nka)\b.*", re.IGNORECASE)
_JOINED = re.compile(r"\s+(?:and|&)\s+", re.IGNORECASE)


def _one_line(value: object, limit: int = 200) -> str:
    """A field from the service as one short line. All of it is untrusted.

    Characters that do not print, or that change how text is laid out, are
    removed, and so are angle brackets, so nothing from the service can hide
    text or become markup in a brief.
    """
    if not isinstance(value, str):
        return ""
    kept = "".join(
        " " if unicodedata.category(c).startswith(("C", "Z")) else c for c in value if c not in "<>"
    )
    text = " ".join(kept.split())
    return text if len(text) <= limit else text[:limit].rstrip() + " [cut]"


def _fold(word: str) -> list[str]:
    """A word as plain lower-case letters: accents off, apostrophes out, hyphens split."""
    plain = unicodedata.normalize("NFKD", word)
    plain = "".join(c for c in plain if not unicodedata.combining(c)).casefold()
    plain = re.sub(r"['’‘`´]", "", plain)
    return [part for part in re.split(r"[^a-z]+", plain) if part]


def _words(name: str) -> list[str]:
    """A name's words without titles and suffixes. Hyphens are still in place."""
    words = [word for word in name.split() if _fold(word)]
    while words and "".join(_fold(words[0])) in _TITLES:
        words = words[1:]
    while words and "".join(_fold(words[-1])) in _SUFFIXES:
        words = words[:-1]
    return words


class SubjectName:
    """The subject's name, as needed to search for it and to recognise it."""

    def __init__(self, name: str):
        words = _words(_reordered(name) if "," in name else name)
        self.given_shown = words[0] if len(words) > 1 else ""
        start = len(words) - 1
        while start > 1 and "".join(_fold(words[start - 1])) in SURNAME_PARTICLES:
            start -= 1
        self.surname_shown = " ".join(words[start:]) if len(words) > 1 else ""
        self.given = _fold(self.given_shown)[:1]
        self.surname = [token for word in words[start:] for token in _fold(word)]
        self.middle = [t for word in words[1:start] for t in _fold(word)] if words else []
        self.usable = bool(self.given and self.surname)

    def query(self) -> str:
        """The given name and the last word of the surname, close together."""
        return f'"{self.given[0]} {self.surname[-1]}"~{NAME_DISTANCE}'


def _reordered(name: str) -> str:
    """ "Surname, Given Middle" as "Given Middle Surname"."""
    before, _, after = name.partition(",")
    return f"{after.replace(',', ' ')} {before}"


def _candidates(party: str) -> list[list[str]]:
    """The ways a party line can be read as one person's name, as folded tokens.

    A docket may print "Surname, Given", add a capacity after a comma ("Jordan
    Whitlock, individually"), add another name ("d/b/a ..."), or join two
    people with "and". Each reading is tried; a body such as a trust is none.
    """
    readings = []
    for piece in _JOINED.split(_ALSO_KNOWN.sub("", party)):
        piece = re.sub(r"^\s*estate of\s+", "", piece, flags=re.IGNORECASE)
        before, comma, after = piece.partition(",")
        forms = [before]
        if comma and 1 <= len(_words(before)) <= 3 and _words(after):
            forms.append(f"{after.partition(',')[0]} {before}")
        for form in forms:
            tokens = [token for word in _words(form) for token in _fold(word)]
            if tokens and not _BODY_WORDS & set(tokens):
                readings.append(tokens)
    return readings


def matching_party(parties: object, subject: SubjectName) -> tuple[str, str] | None:
    """The party that can be read as the subject's name, and the middle name it shows.

    Returns the party as printed and one middle name or initial, or "" when it
    shows none. When several parties fit, one whose middle initial agrees with
    the subject's is preferred, then one that shows no middle name, so that a
    namesake on the same docket does not hide the party who fits best.
    """
    found: list[tuple[int, str, str]] = []
    for party in parties if isinstance(parties, list) else []:
        if not isinstance(party, str):
            continue
        for tokens in _candidates(party):
            size = len(subject.surname)
            if len(tokens) <= size or tokens[0] != subject.given[0]:
                continue
            if tokens[-size:] != subject.surname:
                continue
            between = tokens[1:-size]
            middle = between[0] if between else ""
            mine = subject.middle[0][:1] if subject.middle else ""
            if middle and mine and middle[:1] == mine:
                rank = 0
            elif not middle or not mine:
                rank = 1
            else:
                rank = 2
            found.append((rank, _one_line(party, 120), middle))
    if not found:
        return None
    _, shown, middle = min(found, key=lambda item: item[0])
    return shown, middle


def is_bankruptcy(docket: dict) -> bool:
    court = docket.get("court")
    court_id = docket.get("court_id")
    return (
        bool(docket.get("chapter"))
        or (isinstance(court, str) and "bankruptcy" in court.lower())
        or (isinstance(court_id, str) and len(court_id) > 2 and court_id.endswith("b"))
    )


def docket_record(docket: dict, party: str, person: str) -> RawRecord:
    """One docket as a record: what the docket says, and nothing inferred from it."""
    number = _one_line(docket.get("docketNumber"), 60) or "number not given"
    court = _one_line(docket.get("court"), 120) or "court not given"
    parts = [
        f"Federal docket {number} in {court}: "
        f"{_one_line(docket.get('caseName')) or 'case name not given'}."
    ]
    filed = _one_line(docket.get("dateFiled"), 20)
    closed = _one_line(docket.get("dateTerminated"), 20)
    if filed:
        parts.append(f"Filed {filed}{f', closed {closed}' if closed else ''}.")
    for label, key in (("Cause", "cause"), ("Nature of suit", "suitNature")):
        value = _one_line(docket.get(key), 160).rstrip(".")
        if value:
            parts.append(f"{label}: {value}.")
    parts.append(
        f"A party is named {party}; the docket search does not say in what role, "
        "and gives no date of birth or address."
    )
    listed = docket.get("party") if isinstance(docket.get("party"), list) else []
    others = [_one_line(p, 80) for p in listed if isinstance(p, str)]
    others = [p for p in others if p and p != party]
    if others:
        more = len(others) - MAX_OTHER_PARTIES
        shown = "; ".join(others[:MAX_OTHER_PARTIES])
        parts.append(f"Other parties: {shown}{f'; and {more} more' if more > 0 else ''}.")
    path = docket.get("docket_absolute_url")
    usable = isinstance(path, str) and re.fullmatch(r"/docket/[\w/.-]{1,300}", path)
    return RawRecord(
        text=" ".join(parts),
        url=f"{SITE}{path}" if usable else None,
        person=PersonFacts(name=person),
    )


class FederalCourts:
    """Searches federal dockets for a party of the subject's name."""

    def __init__(
        self,
        api_key: str,
        client: httpx.Client | None = None,
        max_records: int = MAX_RECORDS,
        wait: Callable[[float], None] = time.sleep,
    ):
        self._api_key = api_key
        self._client = client or httpx.Client(timeout=60.0, follow_redirects=False)
        self.max_records = max_records
        self._wait = wait

    def __call__(self, term: str, subject: PersonFacts) -> SourceResult:
        name = SubjectName(term)
        if not name.usable:
            return SourceResult(
                status=SourceStatus.FAILED,
                note="A given name and a surname are needed to search federal dockets.",
            )
        records: list[RawRecord] = []
        total: int | None = None
        left_out = 0
        ended = "the end of the list"
        cursor: str | None = None
        for page in range(MAX_PAGES):
            try:
                data = self._page(name.query(), cursor)
            except _SearchFailed as error:
                if page == 0:
                    return SourceResult(status=SourceStatus.FAILED, note=str(error))
                ended = str(error)
                break
            if page == 0:
                count = data.get("count")
                total = count if type(count) is int and count >= 0 else None
            results = data.get("results")
            full = False
            for docket in results if isinstance(results, list) else []:
                found = None
                if isinstance(docket, dict) and not is_bankruptcy(docket):
                    found = matching_party(docket.get("party"), name)
                if found is None:
                    left_out += 1
                elif len(records) >= self.max_records:
                    full = True
                else:
                    party, middle = found
                    person = " ".join(
                        part
                        for part in (name.given_shown, middle.capitalize(), name.surname_shown)
                        if part
                    )
                    records.append(docket_record(docket, party, person))
            cursor = _cursor(data.get("next"))
            if full or (cursor and len(records) >= self.max_records):
                ended = f"the limit of {self.max_records} dockets"
                break
            if cursor is None:
                break
        else:
            ended = f"the limit of {MAX_PAGES * PAGE_SIZE} dockets looked at"
        return self._result(records, total, left_out, ended)

    def _page(self, query: str, cursor: str | None) -> dict:
        params = {
            "type": "r",
            "party_name": query,
            "order_by": "dateFiled desc",
            "page_size": str(PAGE_SIZE),
        }
        if cursor:
            params["cursor"] = cursor
        for attempt in range(BUSY_RETRIES + 1):
            try:
                reply = self._client.get(
                    SEARCH_URL,
                    params=params,
                    headers={"Authorization": f"Token {self._api_key}"},
                )
            except httpx.HTTPError as error:
                raise _SearchFailed(
                    f"CourtListener could not be reached ({type(error).__name__})."
                ) from None
            if reply.status_code != 429 or attempt == BUSY_RETRIES:
                break
            # The service allows only a few searches a minute: wait as asked
            self._wait(_seconds(reply.headers.get("Retry-After")))
        if reply.status_code in (401, 403):
            raise _SearchFailed("CourtListener refused the key.")
        if reply.status_code == 429:
            raise _SearchFailed("CourtListener's request limit was reached; try later.")
        if reply.status_code != 200:
            raise _SearchFailed(f"CourtListener answered HTTP {reply.status_code}.")
        try:
            data = reply.json()
        except ValueError:
            raise _SearchFailed("CourtListener's answer was not JSON.") from None
        if not isinstance(data, dict):
            raise _SearchFailed("CourtListener's answer was not in the expected form.")
        return data

    @staticmethod
    def _result(
        records: list[RawRecord], total: int | None, left_out: int, ended: str
    ) -> SourceResult:
        complete = ended == "the end of the list"
        looked_at = len(records) + left_out
        parts = [ARCHIVE_NOTE]
        if not looked_at and complete:
            parts.append("It lists no docket for a party of this name.")
        else:
            listed = "an unstated number of" if total is None else str(total)
            parts.append(
                f"It lists {listed} dockets that may name a party like this one. They "
                "can be about other people of the same name."
            )
            parts.append(f"Newest first, {len(records)} were kept.")
        if left_out:
            parts.append(
                f"{left_out} were left out: bankruptcy cases, which this review does not "
                "gather, or dockets where no party's name could be read as this given "
                "name and surname."
            )
        if not complete:
            parts.append(
                f"The search is incomplete: it stopped at {ended.rstrip('.')}, and "
                "older dockets were not looked at."
            )
        if records:
            status = SourceStatus.ANSWERED
        else:
            # Nothing kept from a search cut short is not "searched, nothing found"
            status = SourceStatus.EMPTY if complete else SourceStatus.FAILED
        return SourceResult(status=status, records=records, note=" ".join(parts))


class _SearchFailed(Exception):  # noqa: N818 - reads as what happened
    """The service did not give a usable answer. The message never holds the key."""


def _seconds(asked: str | None) -> float:
    """How long to wait before asking again, kept between 5 and 60 seconds."""
    try:
        return min(max(float(asked or BUSY_WAIT), 5.0), 60.0)
    except ValueError:
        return BUSY_WAIT


def _cursor(following: object) -> str | None:
    """The cursor from the service's link to the next page, and nothing else from it.

    Only the cursor is taken, never the address, so the service cannot send
    the key to another host.
    """
    if not isinstance(following, str):
        return None
    try:
        value = httpx.URL(following).params.get("cursor")
    except (httpx.InvalidURL, ValueError):
        return None
    return value if value and len(value) < 500 else None
