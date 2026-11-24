"""Regex-based entity extraction from federal acquisition documents.

Operates on extracted plain text (from docs_ingest.extract). Produces a
Finding with entities and relationships populated, plus a DOCUMENT entity
that anchors every other extraction via MENTIONED_IN.

Targets (in approximate order of precision):
- UEIs: 12-char base36-ish identifiers. Validated against SAM's
  documented exclusions (no leading 0, no I/O in allowed set per FAR
  52.204-6 conventions). We err on the side of recall and let
  downstream validation catch false positives.
- Contract / solicitation / IDV numbers: DHS/ICE-specific formats
  (70CDCR**, 70CMSD**, ICE ERO) plus GSA lease IDs (e.g. 6MN0187).
- Gov/gsa emails + the personal-name token immediately preceding.
- Dollar amounts with million/billion normalization.
- sam.gov and usaspending.gov URLs (back-references for cross-doc linkage).
- FAR / USC citations (context signals for J&A vs RFP).

Entity ID scheme (must match existing adapters' conventions to let entity
resolution merge naturally across tools):
    document:corpus:<sha1(relpath)>
    organization:name:<slug>
    person:email:<email>
    email:<email>
    government_office:<slug>
    contract:<normalized_contract_id>
    facility:<slug>           (LLM pass will add these; regex is conservative)
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)

# --- Patterns ---------------------------------------------------------------

# DHS/ICE contract IDs: agency prefix (e.g. 70CDCR) + fiscal year (2 digits) +
# document-type letter + 8-digit serial. Observed types: D (IDV), F (task order),
# R (solicitation), S (order off IDV), C (award).
_CONTRACT_ID = re.compile(
    r"\b(70[A-Z]{4}\d{2}[A-Z]\d{5,8})\b"
)

# GSA lease IDs: fiscal year digit + state + 4-digit serial (e.g. 6MN0187)
_LEASE_ID = re.compile(r"\b(\d[A-Z]{2}\d{4})\b")

# RFI solicitation IDs embedded in text (e.g. 2026RFIUASMOS, 2020RFIHQFLDUAS)
_RFI_ID = re.compile(r"\b(20\d{2}RFI[A-Z]{3,20})\b")

# UEIs: 12 chars, uppercase letters + digits, excluding O and I per SAM convention.
# Must not be all-numeric (that's usually a ZIP or serial). Must not start with 0.
_UEI_CANDIDATE = re.compile(r"\b([A-HJ-NP-Z1-9][A-HJ-NP-Z0-9]{11})\b")

# Government emails we care about for this corpus
_GOV_EMAIL = re.compile(
    r"\b([A-Za-z][A-Za-z0-9._+-]*@(?:ice\.dhs\.gov|dhs\.gov|cbp\.dhs\.gov|gsa\.gov|hq\.dhs\.gov))\b",
    re.IGNORECASE,
)

# Dollar amounts. Three variants:
#   $12.2 million / $12.2M / $100‑million
#   $12,345,678
#   $12.5 billion / $12.5B
_DOLLARS_MAG = re.compile(
    r"\$\s*([\d,]+(?:\.\d+)?)\s*[-‑\s]*(million|billion|thousand|M|B|K)\b",
    re.IGNORECASE,
)
_DOLLARS_LITERAL = re.compile(r"\$\s*([\d]{1,3}(?:,\d{3}){1,})(?:\.\d{2})?\b")

# sam.gov and usaspending.gov links
_SAM_URL = re.compile(
    r"https?://(?:beta\.)?sam\.gov/[^\s\"<>)]+", re.IGNORECASE
)
_USASPENDING_URL = re.compile(
    r"https?://(?:www\.)?usaspending\.gov/[^\s\"<>)]+", re.IGNORECASE
)

# FAR / USC citations — weak entities (context only, not nodes)
_FAR_CITE = re.compile(r"\bFAR\s+(\d+\.\d{3}-\d+|\d+\.\d{3})\b")
_USC_CITE = re.compile(r"\b(\d+)\s*U\.?S\.?C\.?\s*§?\s*(\d+[a-z]?)\b")

# Magnitude multipliers for dollar normalization
_MAG_MULT = {
    "k": 1_000, "thousand": 1_000,
    "m": 1_000_000, "million": 1_000_000,
    "b": 1_000_000_000, "billion": 1_000_000_000,
}

# Names directly bound to gov emails: captures (name, email) as one match
# so we never mis-pair the name from a preceding email with a following one.
# Name must be case-sensitive proper-noun capitalization (otherwise "is/or/at"
# get swallowed into the name when IGNORECASE is applied to the whole pattern).
# Only the email-domain side is case-insensitive, via inline flags.
_NAME_EMAIL_PAIR = re.compile(
    r"([A-Z][a-z]+(?:\s+[A-Z]\.?)?(?:\s+[A-Z][a-z]+){1,2})"
    r"\s+(?:at\s+)?"
    r"((?i:[A-Za-z][A-Za-z0-9._+-]*@"
    r"(?:ice\.dhs\.gov|dhs\.gov|cbp\.dhs\.gov|gsa\.gov|hq\.dhs\.gov)))"
)

# Awardee attribution — accepts multiple preambles for sole-source style.
# "sole source... to X LLC"
# "open competition to X LLC"
# "award[ed] to X LLC"
# "contract to X LLC"
# "sole source this acquisition to X LLC"
# The organization side is case-sensitive (must start with a capital) since
# awardee names are proper nouns and a case-insensitive match swallows
# sentence-initial pronouns.
# Binds an organization name to a UEI when the doc says "X's UEI is Y" — this
# is how JOFOC/Notice-of-Intent docs consistently disclose the UEI.
_ORG_UEI_LINK = re.compile(
    r"([A-Z][\w&.\- ]{1,80}?"
    r"(?:LLC|L\.L\.C\.|Inc\.?|Corp\.?|Corporation|Company|Co\.?|"
    r"LLP|LP|Ltd\.?|Group|Services|Solutions|Partners|Holdings|Associates|"
    r"Technologies|Systems|Industries|International))"
    r"['’]?s?\s+"
    r"(?:Unique\s+Entity\s+Identification(?:\s*\(UEI\))?|UEI)\s*"
    r"(?:number|#|No\.?)?\s*(?:is\s+)?"
    r"([A-HJ-NP-Z1-9][A-HJ-NP-Z0-9]{11})",
    re.IGNORECASE,
)

_AWARDEE = re.compile(
    r"(?i:sole\s+source(?:\s+(?:contract|award|this\s+acquisition))?\s+to\s+|"
    r"open\s+competition\s+to\s+|"
    r"award(?:ed)?\s+(?:a\s+contract\s+)?to\s+|"
    r"contract\s+to\s+)"
    r"([A-Z][\w&.\- ]{1,80}?"
    r"(?:LLC|L\.L\.C\.|Inc\.?|Incorporated|Corp\.?|Corporation|Company|Co\.?|"
    r"LLP|LP|Ltd\.?|Group|Services|Solutions|Partners|Holdings|Associates|"
    r"Technologies|Systems|Industries|International))"
    r"(?=[.,\s'])"
)


# --- Helpers ----------------------------------------------------------------


_ADDR_PREFIX = re.compile(
    r"^(?:\d+\s+\w[\w\s]*?\s*,\s*)*"  # "3222 Sunrise Drive, "
    r"(?:[A-Z]{2}\s+\d{5}(?:-\d{4})?\s*\.?\s*)?"  # "MD 21755. "
    r"(?:[A-Z][a-z]+\s*,\s*)*"  # "Jefferson, "
)


def _clean_org_name(raw: str) -> str:
    """Strip leading address fragments from a captured organization name.

    Awardee patterns often end up anchored earlier than the real name start
    when preceded by a company address — e.g. "...Jefferson, MD 21755.
    Edge Ops LLC's UEI..." can match starting at 'MD'. This function walks
    the match forward until the substring begins with a plausible org name
    token (capitalized word that isn't a 2-letter state).
    """
    s = raw.strip().strip(".,")
    # Drop leading tokens while the first word looks like part of an address
    while s:
        first = s.split(" ", 1)[0]
        # 2-letter state abbreviation
        if re.fullmatch(r"[A-Z]{2}", first):
            s = s[len(first):].lstrip(" .,")
            continue
        # Zip code
        if re.fullmatch(r"\d{5}(?:-\d{4})?\.?", first):
            s = s[len(first):].lstrip(" .,")
            continue
        # Any token starting with a digit
        if first[:1].isdigit():
            s = s[len(first):].lstrip(" .,")
            continue
        break
    return s


def _slug(s: str) -> str:
    s = s.lower().strip()
    s = re.sub(r"[^\w\s-]", "", s)
    s = re.sub(r"\s+", "_", s)
    return s[:80]


def _doc_id(path: Path) -> str:
    """Stable ID keyed on the absolute path so relative/absolute calls
    converge to the same document entity."""
    digest = hashlib.sha1(
        str(path.resolve()).encode("utf-8"),
    ).hexdigest()[:10]
    return f"document:corpus:{digest}"


def _is_plausible_uei(candidate: str, context: str) -> bool:
    """Heuristic filter — UEI must be accompanied by "UEI" or "Unique Entity"
    in a ±120 char window, OR the doc must be a JOFOC/J&A (where UEIs are
    required by FAR). We're conservative: better to miss than to coin a
    false UEI entity.
    """
    if not (candidate[0].isalpha() or candidate[0].isdigit()):
        return False
    # Context window check
    idx = context.find(candidate)
    if idx < 0:
        return False
    window = context[max(0, idx - 120):idx + 120].lower()
    return ("uei" in window or "unique entity" in window)


def _normalize_dollars(num_str: str, mag: str | None) -> int | None:
    try:
        val = float(num_str.replace(",", ""))
    except ValueError:
        return None
    if mag:
        mult = _MAG_MULT.get(mag.lower())
        if mult:
            val *= mult
    return int(val)


def _determine_stage(filename: str, text_head: str) -> str:
    """Classify contract stage from filename + first ~500 chars."""
    name = filename.lower()
    head = text_head.lower()
    if "notice of intent" in head or "notice of intent" in name:
        return "notice_of_intent"
    if "jofoc" in name or "jofoc" in head or "justification and approval" in head:
        return "j_and_a"
    if "j&a" in name or "j_a" in name:
        return "j_and_a"
    if "draft pws" in head or "draft_pws" in name or "draft-pws" in name:
        return "draft_pws"
    if " rfp " in f" {name} " or "rfp" in head[:200]:
        return "rfp"
    if " rfi " in f" {name} " or "rfi" in head[:200]:
        return "rfi"
    if "award" in name or "awarded" in head[:300]:
        return "award"
    if "solicitation" in name or "solicitation" in head[:300]:
        return "solicitation"
    return "unknown"


# --- Main entry -------------------------------------------------------------


@dataclass
class RegexExtraction:
    finding: Finding
    stats: dict  # counts per entity type for logging


def _normalize_for_regex(text: str) -> str:
    """Collapse runs of whitespace (including newlines) to single spaces.

    Organization names and attribution phrases frequently wrap across PDF
    line breaks ("Edge Ops\nLLC's Unique Entity..."). Every pattern in this
    module is designed against single-line input; normalizing once is
    simpler and more reliable than adding \\s to every character class.
    Double newlines are preserved as paragraph boundaries since awardee and
    name-email patterns shouldn't span paragraph breaks.
    """
    # Preserve paragraph breaks (2+ newlines → sentinel) before collapsing
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    # Collapse single newlines + surrounding whitespace into a single space
    text = re.sub(r"(?<!\n)\n(?!\n)", " ", text)
    # Collapse runs of non-newline whitespace
    text = re.sub(r"[ \t]+", " ", text)
    return text


def extract_from_document(
    path: Path,
    text: str,
    corpus_root: Path | None = None,
) -> RegexExtraction:
    """Produce a Finding from a single document's extracted text.

    The Finding contains one DOCUMENT entity and many entities + relationships
    extracted via regex. Each source-URL points back to the document file path
    so provenance survives ingestion.
    """
    if corpus_root is not None:
        try:
            rel = path.relative_to(corpus_root)
        except ValueError:
            rel = path
    else:
        rel = path
    raw_text = text
    text = _normalize_for_regex(text)

    doc_id = _doc_id(path)
    doc_src = Source(
        tool="docs_ingest.regex",
        source_url=f"file://{path}",
        confidence=0.9,
    )

    entities: list[Entity] = []
    relationships: list[Relationship] = []
    seen_ids: set[str] = set()

    def add_entity(e: Entity) -> bool:
        if e.id in seen_ids:
            return False
        seen_ids.add(e.id)
        entities.append(e)
        return True

    def link_to_doc(
        target_id: str,
        rel_type: RelationType = RelationType.MENTIONED_IN,
        properties: dict | None = None,
    ) -> None:
        relationships.append(Relationship(
            source_id=target_id,
            target_id=doc_id,
            relation_type=rel_type,
            properties=properties or {},
            sources=[doc_src],
        ))

    # 1. Document entity itself
    stage = _determine_stage(path.name, raw_text[:800])
    add_entity(Entity(
        id=doc_id,
        entity_type=EntityType.DOCUMENT,
        label=path.name,
        properties={
            "rel_path": str(rel),
            "abs_path": str(path),
            "stage_hint": stage,
            "char_count": len(raw_text),
        },
        sources=[doc_src],
    ))

    # 2. Contract IDs (DHS + lease + RFI)
    contract_ids: list[str] = []
    for match in _CONTRACT_ID.finditer(text):
        cid = match.group(1)
        if cid not in contract_ids:
            contract_ids.append(cid)
    for match in _LEASE_ID.finditer(text):
        lid = match.group(1)
        # Filter: need "lease" or "RLP" nearby
        idx = match.start()
        window = text[max(0, idx - 80):idx + 80].lower()
        if "lease" in window or "rlp" in window or "rfp" in window:
            if lid not in contract_ids:
                contract_ids.append(lid)
    for match in _RFI_ID.finditer(text):
        rid = match.group(1)
        if rid not in contract_ids:
            contract_ids.append(rid)

    for cid in contract_ids:
        ent_id = f"contract:{cid.lower()}"
        if add_entity(Entity(
            id=ent_id,
            entity_type=EntityType.CONTRACT,
            label=cid,
            properties={"stage": stage, "raw_id": cid},
            sources=[doc_src],
        )):
            link_to_doc(ent_id)

    # 3. UEIs
    for match in _UEI_CANDIDATE.finditer(text):
        cand = match.group(1)
        if not _is_plausible_uei(cand, text):
            continue
        # UEI becomes a property on the organization — but without knowing
        # which org yet, stash as a standalone "uei:*" entity that the LLM
        # pass or entity resolution can merge into an organization.
        ent_id = f"organization:uei:{cand}"
        if add_entity(Entity(
            id=ent_id,
            entity_type=EntityType.ORGANIZATION,
            label=f"UEI {cand}",
            properties={"uei": cand, "source": "regex"},
            sources=[doc_src],
        )):
            link_to_doc(ent_id)

    # 4. Gov emails — first pass: emit every email entity.
    for match in _GOV_EMAIL.finditer(text):
        email = match.group(1).lower()
        email_id = f"email:{email}"
        if add_entity(Entity(
            id=email_id,
            entity_type=EntityType.EMAIL,
            label=email,
            properties={"agency_domain": email.split("@")[1]},
            sources=[doc_src],
        )):
            link_to_doc(email_id)

    # 4b. Name↔email pairs — captures name and email in one regex so we
    # never mis-pair (e.g. attributing a second email to the first name).
    _NAME_FALSE_POSITIVES = {
        "point of contact", "contracting officer", "the government",
        "attention to", "sole source", "notice of intent",
    }
    for match in _NAME_EMAIL_PAIR.finditer(text):
        raw_name = re.sub(r"\s+", " ", match.group(1)).strip()
        email = match.group(2).lower()
        if raw_name.lower() in _NAME_FALSE_POSITIVES:
            continue
        person_id = f"person:name:{_slug(raw_name)}"
        email_id = f"email:{email}"
        if add_entity(Entity(
            id=person_id,
            entity_type=EntityType.PERSON,
            label=raw_name,
            properties={
                "email_observed": email,
                "agency": email.split("@")[1],
            },
            sources=[doc_src],
        )):
            link_to_doc(person_id)
        relationships.append(Relationship(
            source_id=person_id,
            target_id=email_id,
            relation_type=RelationType.HAS_EMAIL,
            sources=[doc_src],
        ))
        for cid in contract_ids:
            relationships.append(Relationship(
                source_id=person_id,
                target_id=f"contract:{cid.lower()}",
                relation_type=RelationType.POC_FOR,
                sources=[doc_src],
            ))

    # 4c. Org↔UEI bindings from "<Company>'s UEI is <12-char>" patterns.
    # Merges the two separate entities (one keyed by name, one by UEI) by
    # (a) setting UEI as a property on the named org, and (b) issuing
    # ALSO_KNOWN_AS so downstream entity resolution treats them as one.
    for match in _ORG_UEI_LINK.finditer(text):
        raw_name = _clean_org_name(
            re.sub(r"\s+", " ", match.group(1)).strip()
        )
        uei = match.group(2)
        if not raw_name:
            continue
        org_id = f"organization:name:{_slug(raw_name)}"
        uei_ent_id = f"organization:uei:{uei}"
        if add_entity(Entity(
            id=org_id,
            entity_type=EntityType.ORGANIZATION,
            label=raw_name,
            properties={"uei": uei, "extraction": "org_uei_binding"},
            sources=[doc_src],
        )):
            link_to_doc(org_id)
        else:
            # Org exists — set UEI property if not already set
            for e in entities:
                if e.id == org_id and "uei" not in e.properties:
                    e.properties["uei"] = uei
        # If the UEI entity was created independently, link them
        if uei_ent_id in seen_ids:
            relationships.append(Relationship(
                source_id=org_id,
                target_id=uei_ent_id,
                relation_type=RelationType.ALSO_KNOWN_AS,
                sources=[doc_src],
            ))

    # 5. Awardee attributions → Organization + AWARDED_TO
    for match in _AWARDEE.finditer(text):
        raw_name = _clean_org_name(
            re.sub(r"\s+", " ", match.group(1)).strip()
        )
        if not raw_name:
            continue
        org_id = f"organization:name:{_slug(raw_name)}"
        if add_entity(Entity(
            id=org_id,
            entity_type=EntityType.ORGANIZATION,
            label=raw_name,
            properties={"extraction": "sole_source_pattern"},
            sources=[doc_src],
        )):
            link_to_doc(org_id)
        for cid in contract_ids:
            relationships.append(Relationship(
                source_id=f"contract:{cid.lower()}",
                target_id=org_id,
                relation_type=RelationType.AWARDED_TO,
                properties={"method": "sole_source"},
                sources=[doc_src],
            ))

    # 6. Dollar amounts — attach to document, best-effort also to contracts
    dollar_values: list[int] = []
    for match in _DOLLARS_MAG.finditer(text):
        val = _normalize_dollars(match.group(1), match.group(2))
        if val:
            dollar_values.append(val)
    for match in _DOLLARS_LITERAL.finditer(text):
        val = _normalize_dollars(match.group(1), None)
        if val and val >= 1_000_000:  # Only keep literal amounts >= $1M
            dollar_values.append(val)
    if dollar_values:
        max_value = max(dollar_values)
        # Attach as properties on each CONTRACT entity (mutate in place)
        for e in entities:
            if e.entity_type == EntityType.CONTRACT:
                e.properties.setdefault("max_value_usd", max_value)
                e.properties.setdefault(
                    "all_values_usd_seen", sorted(set(dollar_values))[-5:]
                )

    # 7. sam.gov and usaspending URLs — attach to contracts
    sam_urls = sorted({
        m.group(0).rstrip(".,;)'\"") for m in _SAM_URL.finditer(text)
    })
    uspending_urls = sorted({
        m.group(0).rstrip(".,;)'\"") for m in _USASPENDING_URL.finditer(text)
    })
    for e in entities:
        if e.entity_type == EntityType.CONTRACT:
            if sam_urls:
                e.properties.setdefault("sam_urls", sam_urls)
            if uspending_urls:
                e.properties.setdefault("usaspending_urls", uspending_urls)
        if e.entity_type == EntityType.DOCUMENT:
            if sam_urls:
                e.properties.setdefault("sam_urls", sam_urls)
            if uspending_urls:
                e.properties.setdefault("usaspending_urls", uspending_urls)

    # 8. FAR / USC citations — attach as document property, no entities
    far_cites = sorted({m.group(1) for m in _FAR_CITE.finditer(text)})
    usc_cites = sorted({
        f"{m.group(1)} USC {m.group(2)}" for m in _USC_CITE.finditer(text)
    })
    if far_cites or usc_cites:
        for e in entities:
            if e.entity_type == EntityType.DOCUMENT:
                if far_cites:
                    e.properties["far_citations"] = far_cites
                if usc_cites:
                    e.properties["usc_citations"] = usc_cites

    # Stats for logging
    stats: dict[str, int] = {}
    for e in entities:
        k = e.entity_type.value
        stats[k] = stats.get(k, 0) + 1
    stats["_relationships"] = len(relationships)

    return RegexExtraction(
        finding=Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"Regex extraction from {path.name}: "
                f"{len(entities)} entities, {len(relationships)} relationships "
                f"(stage={stage})"
            ),
        ),
        stats=stats,
    )
