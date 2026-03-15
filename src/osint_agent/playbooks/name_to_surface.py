"""Playbook: Person Name → Digital Surface Map.

Starting from a real name, map their complete digital surface: public records,
social accounts, financial activity, legal history, and organizational ties.
This is the standard "forward" OSINT direction — name → everything.

Tool sequence:
  1. DuckDuckGo — web search for disambiguation and context
  2. People search — addresses, phones, relatives, aliases
  3. Court records — legal history
  4. Campaign donors — political activity
  5. LittleSis — board seats, donations, lobbying ties
  6. DocumentCloud — FOIA docs, court filings, leaked memos
  7. FARA — foreign agent registrations
  8. Congress.gov — legislator records and sponsored bills
  9. Common username patterns → Maigret
"""


from osint_agent.models import EntityType, Finding
from osint_agent.playbooks.base import Lead, Playbook, ToolStep, extract_leads_from_findings

# Common username patterns generated from a name
_USERNAME_PATTERNS = [
    "{first}{last}",          # janedoe
    "{first}.{last}",         # jane.doe
    "{first}_{last}",         # jane_doe
    "{first}{last_initial}",  # janed
    "{first_initial}{last}",  # jdoe
]


def _generate_username_variants(first: str, last: str) -> list[str]:
    """Generate common username patterns from a name.

    Produces lowercase variants using standard patterns observed
    across platforms. Does not include numbers (too many combinations).
    """
    first = first.lower().strip()
    last = last.lower().strip()
    if not first or not last:
        return []

    variants = []
    for pattern in _USERNAME_PATTERNS:
        username = pattern.format(
            first=first,
            last=last,
            first_initial=first[0],
            last_initial=last[0],
        )
        variants.append(username)
    return variants


class NameToSurface(Playbook):
    """Map a person's complete digital surface from their real name."""

    @property
    def name(self) -> str:
        return "name_to_surface"

    @property
    def description(self) -> str:
        return "Start with a person name, map their full digital footprint"

    @property
    def completeness_criteria(self) -> dict[EntityType, int]:
        return {
            EntityType.PERSON: 1,
            EntityType.ACCOUNT: 1,
            EntityType.EMAIL: 1,
        }

    def steps(self, seed: str, **kwargs) -> list[ToolStep]:
        state = kwargs.get("state", "")
        city = kwargs.get("city", "")

        steps = [
            ToolStep(
                tool_name="ddg_search",
                kwargs={"query": seed},
                description=f"Web search for '{seed}'",
            ),
            ToolStep(
                tool_name="peoplesearch",
                kwargs={"query": seed, "state": state, "city": city},
                description=f"People search for '{seed}'",
            ),
            ToolStep(
                tool_name="courtlistener",
                kwargs={"name": seed},
                description=f"Court records for '{seed}'",
            ),
            ToolStep(
                tool_name="openfec",
                kwargs={"query": seed, "mode": "contributors"},
                description=f"Campaign donations by '{seed}'",
            ),
            ToolStep(
                tool_name="littlesis",
                kwargs={"query": seed},
                description=f"Power network for '{seed}' (LittleSis)",
            ),
            ToolStep(
                tool_name="documentcloud",
                kwargs={"query": seed},
                description=f"FOIA documents mentioning '{seed}'",
            ),
            ToolStep(
                tool_name="fara",
                kwargs={"name": seed},
                description=f"Foreign agent registrations for '{seed}'",
            ),
            ToolStep(
                tool_name="congress",
                kwargs={"query": seed, "mode": "member"},
                description=f"Congress.gov member search for '{seed}'",
            ),
        ]

        # Generate username variants and search the most common one
        parts = seed.strip().split()
        if len(parts) >= 2:
            first, last = parts[0], parts[-1]
            variants = _generate_username_variants(first, last)
            if variants:
                # Search the most common pattern (firstlast)
                steps.append(ToolStep(
                    tool_name="maigret",
                    kwargs={"username": variants[0]},
                    description=f"Username search for '{variants[0]}'",
                ))

        return steps

    def extract_leads(self, findings: list[Finding]) -> list[Lead]:
        """Name playbook generates username variant leads.

        After the initial sweep, any discovered usernames should be
        followed up. Additionally, generate leads for untested username
        variants derived from the name.
        """
        leads = extract_leads_from_findings(findings)

        # Find the seed name from findings to generate more username variants
        for finding in findings:
            for entity in finding.entities:
                if (
                    entity.entity_type == EntityType.PERSON
                    and not entity.properties.get("is_secondary")
                ):
                    parts = entity.label.strip().split()
                    if len(parts) >= 2:
                        first, last = parts[0], parts[-1]
                        variants = _generate_username_variants(first, last)
                        # Skip the first variant (already searched in steps)
                        for variant in variants[1:]:
                            leads.append(Lead(
                                lead_type="username",
                                value=variant,
                                score=0.35,
                                source_entity_id=entity.id,
                                notes=f"Generated username variant from '{entity.label}'",
                            ))
                    break  # Only generate from the primary target
            break

        return leads
