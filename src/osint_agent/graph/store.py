"""Graph store abstraction — merges Findings into a persistent graph."""

import abc

from osint_agent.models import Entity, Finding, Relationship


class GraphStore(abc.ABC):
    """Abstract graph storage backend."""

    @abc.abstractmethod
    async def merge_entity(self, entity: Entity) -> None:
        """Insert or update an entity node. Merge by id."""

    @abc.abstractmethod
    async def merge_relationship(self, rel: Relationship) -> None:
        """Insert or update a relationship edge."""

    @abc.abstractmethod
    async def query(self, cypher_or_filter: str, params: dict | None = None) -> list[dict]:
        """Run a query against the graph."""

    async def ingest_finding(
        self, finding: Finding, investigation_id: int | None = None,
    ) -> None:
        """Ingest a complete Finding (entities + relationships) into the graph.

        Subclasses that support investigation scoping (e.g. SqliteStore)
        override this to link entities to the given investigation.
        """
        for entity in finding.entities:
            await self.merge_entity(entity)
        for rel in finding.relationships:
            await self.merge_relationship(rel)
