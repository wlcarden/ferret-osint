"""Holehe tool adapter — email registration check across 120+ platforms."""

import asyncio
import logging

import httpx
import trio

logger = logging.getLogger(__name__)

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter


class HoleheAdapter(ToolAdapter):
    """Uses holehe's Python API directly (no subprocess needed).

    Checks whether an email is registered on ~120 platforms by
    probing their "forgot password" / registration flows.
    """

    name = "holehe"

    PERMUTATION_DELAY = 0.5  # seconds between checks to avoid rate limiting

    def __init__(self, timeout: int = 10):
        self.timeout = timeout

    def is_available(self) -> bool:
        try:
            from holehe import modules
            from holehe.core import get_functions, import_submodules
            return True
        except ImportError:
            return False

    async def run(self, email: str) -> Finding:
        """Check which platforms an email is registered on.

        Returns a Finding containing:
        - An EMAIL entity for the search target
        - An ACCOUNT entity for each platform where the email is registered
        - HAS_ACCOUNT relationships
        - Additional EMAIL entities if recovery emails are discovered
        """
        from holehe import modules as holehe_modules
        from holehe.core import get_functions, import_submodules, launch_module

        submodules = import_submodules(holehe_modules)
        fns = get_functions(submodules)
        results = []

        async def _run_modules():
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                for fn in fns:
                    try:
                        await launch_module(fn, email, client, results)
                    except Exception as exc:
                        logger.debug("holehe module %s failed: %s", fn.__name__, exc)

        trio.run(_run_modules)
        return self._parse_results(email, results)

    @staticmethod
    def _generate_permutations(
        first_name: str,
        last_name: str,
        domain: str,
    ) -> list[str]:
        """Generate common email format permutations.

        For "Bill" "Beckwith" at "ois.com", produces:
            bbeckwith@ois.com      (first initial + last)
            beckwith@ois.com       (last only)
            billbeckwith@ois.com   (first + last)
            bill.beckwith@ois.com  (first.last)
            beckwithb@ois.com      (last + first initial)
            bill@ois.com           (first only)
            b.beckwith@ois.com     (initial.last)
            beckwith.b@ois.com     (last.initial)
        """
        first = first_name.lower().strip()
        last = last_name.lower().strip()
        initial = first[0] if first else ""
        domain = domain.strip()

        patterns = [
            f"{initial}{last}@{domain}",        # bbeckwith@
            f"{last}@{domain}",                  # beckwith@
            f"{first}{last}@{domain}",           # billbeckwith@
            f"{first}.{last}@{domain}",          # bill.beckwith@
            f"{last}{initial}@{domain}",         # beckwithb@
            f"{first}@{domain}",                 # bill@
            f"{initial}.{last}@{domain}",        # b.beckwith@
            f"{last}.{initial}@{domain}",        # beckwith.b@
        ]

        # Deduplicate while preserving order (e.g. single-char first name
        # would make initial-based patterns identical to first-name ones)
        seen: set[str] = set()
        unique: list[str] = []
        for p in patterns:
            if p not in seen:
                seen.add(p)
                unique.append(p)
        return unique

    async def run_permutations(
        self,
        first_name: str,
        last_name: str,
        domain: str,
    ) -> Finding:
        """Generate email permutations and check each against platforms.

        Combines all sub-Findings into a single Finding with a summary
        noting which permutations produced hits.
        """
        permutations = self._generate_permutations(
            first_name, last_name, domain,
        )

        all_entities: list[Entity] = []
        all_relationships: list[Relationship] = []
        hits: list[str] = []
        entity_ids_seen: set[str] = set()

        for i, email in enumerate(permutations):
            if i > 0:
                await asyncio.sleep(self.PERMUTATION_DELAY)

            finding = await self.run(email=email)

            # Count registered accounts (entities beyond the email itself)
            accounts = [
                e for e in finding.entities
                if e.entity_type == EntityType.ACCOUNT
            ]
            if accounts:
                hits.append(
                    f"{email}: {len(accounts)} registration(s)"
                )

            # Merge entities, deduplicating by id
            for entity in finding.entities:
                if entity.id not in entity_ids_seen:
                    entity_ids_seen.add(entity.id)
                    all_entities.append(entity)

            all_relationships.extend(finding.relationships)

        total_accounts = sum(
            1 for e in all_entities
            if e.entity_type == EntityType.ACCOUNT
        )

        if hits:
            hit_summary = "\n".join(f"  - {h}" for h in hits)
            notes = (
                f"Holehe permutation scan for "
                f"'{first_name} {last_name}' @ {domain}: "
                f"{total_accounts} total registration(s) across "
                f"{len(permutations)} permutations.\n"
                f"Hits:\n{hit_summary}"
            )
        else:
            notes = (
                f"Holehe permutation scan for "
                f"'{first_name} {last_name}' @ {domain}: "
                f"no registrations found across "
                f"{len(permutations)} permutations."
            )

        return Finding(
            entities=all_entities,
            relationships=all_relationships,
            notes=notes,
        )

    def _parse_results(self, email: str, results: list[dict]) -> Finding:
        """Parse holehe results into entities and relationships."""
        entities = []
        relationships = []

        email_entity = Entity(
            id=f"email:{email}",
            entity_type=EntityType.EMAIL,
            label=email,
            sources=[Source(tool=self.name)],
        )
        entities.append(email_entity)

        registered_count = 0
        for result in results:
            if not result.get("exists"):
                continue

            registered_count += 1
            platform = result["name"]
            domain = result.get("domain", "")

            account_id = f"account:{platform}:{email}"
            account = Entity(
                id=account_id,
                entity_type=EntityType.ACCOUNT,
                label=f"{email} on {platform}",
                properties={
                    "platform": platform,
                    "domain": domain,
                    "method": result.get("method"),
                    "rate_limited": result.get("rateLimit", False),
                },
                sources=[Source(
                    tool=self.name,
                    source_url=f"https://{domain}",
                    raw_data=result,
                )],
            )
            entities.append(account)

            relationships.append(Relationship(
                source_id=email_entity.id,
                target_id=account_id,
                relation_type=RelationType.HAS_ACCOUNT,
                properties={"platform": platform},
                sources=[Source(tool=self.name)],
            ))

            # If holehe discovered a recovery email, capture it
            recovery = result.get("emailrecovery")
            if recovery and recovery != email:
                recovery_id = f"email:{recovery}"
                recovery_entity = Entity(
                    id=recovery_id,
                    entity_type=EntityType.EMAIL,
                    label=recovery,
                    sources=[Source(
                        tool=self.name,
                        source_url=f"https://{domain}",
                    )],
                )
                entities.append(recovery_entity)
                relationships.append(Relationship(
                    source_id=account_id,
                    target_id=recovery_id,
                    relation_type=RelationType.HAS_EMAIL,
                    properties={"recovery_email": True},
                    sources=[Source(tool=self.name)],
                ))

            # If holehe discovered a phone number, capture it
            phone = result.get("phoneNumber")
            if phone:
                phone_id = f"phone:{phone}"
                phone_entity = Entity(
                    id=phone_id,
                    entity_type=EntityType.PHONE,
                    label=phone,
                    sources=[Source(
                        tool=self.name,
                        source_url=f"https://{domain}",
                    )],
                )
                entities.append(phone_entity)
                relationships.append(Relationship(
                    source_id=account_id,
                    target_id=phone_id,
                    relation_type=RelationType.HAS_PHONE,
                    properties={"from_recovery": True},
                    sources=[Source(tool=self.name)],
                ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=f"Holehe found {registered_count} registrations for '{email}'",
        )
