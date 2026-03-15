"""Playbook: Username → Identity.

Starting from an anonymous username or handle, attempt to deanonymize
by cross-referencing platform accounts, associated emails, and public
records. This is the most automatable antifascist OSINT pattern —
username pivoting exploits reuse of handles across platforms.

Tool sequence:
  1. Maigret — find the username across 2500+ platforms
  2. DuckDuckGo — web search for the username in context
  3. Holehe — if emails are discovered, check platform registrations
  4. People search — if a real name surfaces, search public records
"""

from osint_agent.models import EntityType
from osint_agent.playbooks.base import Playbook, ToolStep


class UsernameToldentity(Playbook):
    """Deanonymize a username by tracing it across platforms and records."""

    @property
    def name(self) -> str:
        return "username_to_identity"

    @property
    def description(self) -> str:
        return "Start with a username/handle, find the real identity behind it"

    @property
    def completeness_criteria(self) -> dict[EntityType, int]:
        return {
            EntityType.PERSON: 1,
            EntityType.ACCOUNT: 2,
        }

    def steps(self, seed: str, **kwargs) -> list[ToolStep]:
        return [
            ToolStep(
                tool_name="maigret",
                kwargs={"username": seed},
                description=f"Search 2500+ platforms for '{seed}'",
            ),
            ToolStep(
                tool_name="reddit",
                kwargs={"username": seed},
                description=f"Reddit profile + post history for '{seed}'",
            ),
            ToolStep(
                tool_name="steam",
                kwargs={"username": seed},
                description=f"Steam profile for '{seed}'",
            ),
            ToolStep(
                tool_name="ddg_search",
                kwargs={"query": seed},
                description=f"Web search for '{seed}'",
            ),
        ]

    def extract_leads(self, findings: list):
        """Username playbook generates higher-priority email leads.

        When deanonymizing, discovered emails are the most critical
        follow-up — they bridge anonymous handles to real identities.
        """
        from osint_agent.playbooks.base import extract_leads_from_findings

        leads = extract_leads_from_findings(findings)

        # Boost email leads — they're the bridge to real identity
        for lead in leads:
            if lead.lead_type == "email":
                lead.score = min(1.0, lead.score + 0.15)
                lead.notes = "Email from username pivot — high-value for identity resolution"

        return leads
