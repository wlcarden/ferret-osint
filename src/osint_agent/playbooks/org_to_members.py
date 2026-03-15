"""Playbook: Organization → Members.

Starting from an organization name (company, nonprofit, political group),
identify its members, officers, and financial connections. Critical for
tracking organizational affiliation networks — e.g., which individuals
are linked to a specific group through SEC filings, donations, or contracts.

Tool sequence:
  1. DuckDuckGo — web search for context and disambiguation
  2. SEC EDGAR — corporate officers and filings
  3. USASpending — federal contract recipients
  4. SBIR — research grants
  5. Patents — IP filings (company as assignee)
  6. Court records — legal actions involving the org
  7. LittleSis — board seats, donations, lobbying ties
  8. FARA — foreign agent registrations
  9. CrossLinked — LinkedIn employee discovery
  10. ProPublica Nonprofit — 990 filings, revenue, exec comp
  11. DocumentCloud — FOIA docs, court filings, leaked memos
  12. MuckRock — existing FOIA requests about the org
"""

from osint_agent.models import EntityType, Finding
from osint_agent.playbooks.base import Lead, Playbook, ToolStep, extract_leads_from_findings


class OrgToMembers(Playbook):
    """Map an organization's members, officers, and financial connections."""

    @property
    def name(self) -> str:
        return "org_to_members"

    @property
    def description(self) -> str:
        return "Start with an organization, find its members and connections"

    @property
    def completeness_criteria(self) -> dict[EntityType, int]:
        return {
            EntityType.PERSON: 2,
            EntityType.ORGANIZATION: 1,
        }

    def steps(self, seed: str, **kwargs) -> list[ToolStep]:
        return [
            ToolStep(
                tool_name="ddg_search",
                kwargs={"query": seed},
                description=f"Web search for '{seed}'",
            ),
            ToolStep(
                tool_name="edgar",
                kwargs={"query": seed, "mode": "company"},
                description=f"SEC EDGAR lookup for '{seed}'",
            ),
            ToolStep(
                tool_name="usaspending",
                kwargs={"query": seed, "mode": "recipient"},
                description=f"Federal contracts for '{seed}'",
            ),
            ToolStep(
                tool_name="sbir",
                kwargs={"query": seed, "mode": "firm"},
                description=f"SBIR/STTR awards for '{seed}'",
            ),
            ToolStep(
                tool_name="patents",
                kwargs={"query": seed, "mode": "assignee"},
                description=f"Patents assigned to '{seed}'",
            ),
            ToolStep(
                tool_name="courtlistener",
                kwargs={"name": seed},
                description=f"Court records for '{seed}'",
            ),
            ToolStep(
                tool_name="littlesis",
                kwargs={"query": seed},
                description=f"Power network for '{seed}' (LittleSis)",
            ),
            ToolStep(
                tool_name="fara",
                kwargs={"name": seed},
                description=f"Foreign agent registrations for '{seed}'",
            ),
            ToolStep(
                tool_name="crosslinked",
                kwargs={"company": seed},
                description=f"LinkedIn employee search for '{seed}'",
            ),
            ToolStep(
                tool_name="propublica_nonprofit",
                kwargs={"query": seed},
                description=f"Nonprofit 990 filings for '{seed}'",
            ),
            ToolStep(
                tool_name="documentcloud",
                kwargs={"query": seed},
                description=f"FOIA documents mentioning '{seed}'",
            ),
            ToolStep(
                tool_name="muckrock",
                kwargs={"query": seed, "mode": "foia"},
                description=f"MuckRock FOIA requests about '{seed}'",
            ),
        ]

    def extract_leads(self, findings: list[Finding]) -> list[Lead]:
        """Org playbook prioritizes person leads — they're the members.

        Discovered persons (officers, grantees, inventors) are the primary
        output of this playbook. Boost their scores.
        """
        leads = extract_leads_from_findings(findings)

        # Collect person entities that aren't already leads
        seen_values = {lead.value for lead in leads}
        for finding in findings:
            for entity in finding.entities:
                if entity.entity_type == EntityType.PERSON and entity.label not in seen_values:
                    leads.append(Lead(
                        lead_type="person_name",
                        value=entity.label,
                        score=0.6,
                        source_entity_id=entity.id,
                        notes="Officer/member discovered from org records",
                    ))
                    seen_values.add(entity.label)

        # Also generate domain leads if we find company domains
        for finding in findings:
            for entity in finding.entities:
                if entity.entity_type == EntityType.DOMAIN and entity.label not in seen_values:
                    leads.append(Lead(
                        lead_type="domain",
                        value=entity.label,
                        score=0.5,
                        source_entity_id=entity.id,
                        notes="Org domain — harvest for employee emails",
                    ))
                    seen_values.add(entity.label)

        return leads
