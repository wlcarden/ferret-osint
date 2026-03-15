"""Base playbook class and common types.

Playbooks define investigation workflows as tool sequences. Each playbook
implements `steps()` to declare what tools to run and `extract_leads()`
to pull follow-up targets from findings.
"""

import abc
import re
from dataclasses import dataclass, field

from osint_agent.models import Entity, EntityType, Finding, ToolError


@dataclass
class Lead:
    """A follow-up target extracted from findings."""

    lead_type: str          # username, email, domain, phone, person_name, url
    value: str              # The actual value to investigate
    score: float = 0.5      # Priority (0.0-1.0, higher = more promising)
    source_entity_id: str | None = None  # Entity that generated this lead
    notes: str = ""


@dataclass
class ToolStep:
    """A single tool invocation within a playbook."""

    tool_name: str
    kwargs: dict = field(default_factory=dict)
    description: str = ""   # Human-readable label for progress output


@dataclass
class PlaybookResult:
    """Result of running a playbook."""

    playbook_name: str
    investigation_id: int | None
    findings: list[Finding] = field(default_factory=list)
    leads: list[Lead] = field(default_factory=list)
    errors: list[ToolError] = field(default_factory=list)
    entity_count: int = 0
    relationship_count: int = 0
    started_at: str = ""
    completed_at: str = ""

    def summary(self) -> str:
        lines = [
            f"Playbook: {self.playbook_name}",
            f"  Findings: {len(self.findings)}",
            f"  Entities: {self.entity_count}",
            f"  Relationships: {self.relationship_count}",
            f"  Leads generated: {len(self.leads)}",
        ]
        if self.errors:
            lines.append(f"  Errors: {len(self.errors)}")
            for err in self.errors:
                lines.append(f"    [{err.category.value}] {err.tool}: {err.message}")
                if err.suggestion:
                    lines.append(f"      -> {err.suggestion}")
        return "\n".join(lines)


class Playbook(abc.ABC):
    """Base class for investigation playbooks.

    Subclasses implement:
      - name: str property
      - description: str property
      - steps(seed, **kwargs): yields ToolStep objects defining what to run
      - extract_leads(findings): extracts follow-up leads from findings
    """

    @property
    @abc.abstractmethod
    def name(self) -> str:
        """Short identifier for this playbook."""

    @property
    @abc.abstractmethod
    def description(self) -> str:
        """One-line description of what this playbook does."""

    @abc.abstractmethod
    def steps(self, seed: str, **kwargs) -> list[ToolStep]:
        """Define the tool steps for this playbook.

        Args:
            seed: The initial input (username, name, email, etc.)
            **kwargs: Additional parameters (state, domain, etc.)

        Returns:
            List of ToolStep objects to execute.
        """

    def extract_leads(self, findings: list[Finding]) -> list[Lead]:
        """Extract follow-up leads from findings.

        Default implementation uses entity-type-based extraction.
        Override for playbook-specific lead logic.
        """
        return extract_leads_from_findings(findings)


def extract_leads_from_findings(findings: list[Finding]) -> list[Lead]:
    """Extract follow-up leads from a list of findings.

    Scans entities for actionable types (emails, usernames, domains, etc.)
    and creates Lead objects for each.
    """
    leads: list[Lead] = []
    seen: set[tuple[str, str]] = set()

    for finding in findings:
        for entity in finding.entities:
            lead = _entity_to_lead(entity)
            if lead is None:
                continue
            key = (lead.lead_type, lead.value)
            if key in seen:
                continue
            seen.add(key)
            leads.append(lead)

    # Sort by score descending
    leads.sort(key=lambda l: l.score, reverse=True)
    return leads


def _entity_to_lead(entity: Entity) -> Lead | None:
    """Convert an entity to a lead if it represents a follow-up target."""
    etype = entity.entity_type

    if etype == EntityType.EMAIL:
        # Email addresses are high-value leads
        return Lead(
            lead_type="email",
            value=entity.label,
            score=0.8,
            source_entity_id=entity.id,
            notes="Discovered email — check platform registrations",
        )

    if etype == EntityType.USERNAME:
        return Lead(
            lead_type="username",
            value=entity.label,
            score=0.7,
            source_entity_id=entity.id,
            notes="Discovered username — search across platforms",
        )

    if etype == EntityType.ACCOUNT:
        # Extract username from account entity
        username = entity.properties.get("username", "")
        if not username:
            # Try to extract from label like "janedoe on GitHub"
            match = re.match(r"^(\S+)\s+on\s+", entity.label)
            if match:
                username = match.group(1)
        if username:
            return Lead(
                lead_type="username",
                value=username,
                score=0.6,
                source_entity_id=entity.id,
                notes=f"Account on {entity.properties.get('platform', 'unknown')}",
            )

    if etype == EntityType.DOMAIN:
        return Lead(
            lead_type="domain",
            value=entity.label,
            score=0.5,
            source_entity_id=entity.id,
            notes="Discovered domain — harvest emails/subdomains",
        )

    if etype == EntityType.PHONE:
        return Lead(
            lead_type="phone",
            value=entity.label,
            score=0.4,
            source_entity_id=entity.id,
            notes="Discovered phone number",
        )

    if etype == EntityType.PERSON:
        # Only generate person leads for secondary targets (not the seed)
        # Score lower since these require more work
        if entity.properties.get("is_secondary"):
            return Lead(
                lead_type="person_name",
                value=entity.label,
                score=0.3,
                source_entity_id=entity.id,
                notes="Related person — potential cross-reference target",
            )

    if etype == EntityType.ORGANIZATION:
        # Organizations discovered during investigations can be followed up
        # via LittleSis, FARA, DocumentCloud, MuckRock, nonprofits
        return Lead(
            lead_type="organization",
            value=entity.label,
            score=0.35,
            source_entity_id=entity.id,
            notes=(
                "Discovered organization — check power networks,"
                " foreign registrations, FOIA docs"
            ),
        )

    return None
