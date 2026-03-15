"""Neo4j implementation of the GraphStore."""

import os

from neo4j import AsyncGraphDatabase

from osint_agent.graph.store import GraphStore
from osint_agent.models import Entity, Relationship


class Neo4jStore(GraphStore):
    """Graph store backed by Neo4j.

    Entities become nodes labeled with their EntityType.
    Relationships become typed edges.
    Sources are stored as properties on nodes/edges for provenance.
    """

    def __init__(
        self,
        uri: str | None = None,
        user: str | None = None,
        password: str | None = None,
    ):
        self.uri = uri or os.getenv("NEO4J_URI", "bolt://localhost:7687")
        self.user = user or os.getenv("NEO4J_USER", "neo4j")
        self.password = password or os.getenv("NEO4J_PASSWORD", "changeme")
        self._driver = None

    async def connect(self):
        self._driver = AsyncGraphDatabase.driver(
            self.uri,
            auth=(self.user, self.password),
        )

    async def close(self):
        if self._driver:
            await self._driver.close()

    async def merge_entity(self, entity: Entity) -> None:
        """Merge an entity into Neo4j. Uses entity.id as the stable key.

        MERGE ensures idempotency — running the same Finding twice
        updates properties rather than creating duplicates.
        """
        label = entity.entity_type.value.capitalize()
        # Store sources as serialized JSON for queryability
        source_dicts = [s.model_dump(mode="json") for s in entity.sources]

        query = f"""
        MERGE (n:{label} {{id: $id}})
        SET n.label = $label,
            n.entity_type = $entity_type,
            n += $properties,
            n.sources = $sources,
            n.updated_at = datetime()
        """
        params = {
            "id": entity.id,
            "label": entity.label,
            "entity_type": entity.entity_type.value,
            "properties": self._flatten_properties(entity.properties),
            "sources": [str(s) for s in source_dicts],
        }
        async with self._driver.session() as session:
            await session.run(query, params)

    async def merge_relationship(self, rel: Relationship) -> None:
        """Merge a relationship between two entities.

        Both endpoints must already exist (or be created in the same
        Finding via ingest_finding, which merges entities first).
        """
        rel_type = rel.relation_type.value.upper()
        source_dicts = [s.model_dump(mode="json") for s in rel.sources]

        query = f"""
        MATCH (a {{id: $source_id}})
        MATCH (b {{id: $target_id}})
        MERGE (a)-[r:{rel_type}]->(b)
        SET r += $properties,
            r.sources = $sources,
            r.updated_at = datetime()
        """
        params = {
            "source_id": rel.source_id,
            "target_id": rel.target_id,
            "properties": self._flatten_properties(rel.properties),
            "sources": [str(s) for s in source_dicts],
        }
        async with self._driver.session() as session:
            await session.run(query, params)

    async def query(self, cypher: str, params: dict | None = None) -> list[dict]:
        """Run an arbitrary Cypher query and return results as dicts."""
        async with self._driver.session() as session:
            result = await session.run(cypher, params or {})
            return [record.data() async for record in result]

    async def get_entity(self, entity_id: str) -> dict | None:
        """Retrieve a single entity by its id."""
        results = await self.query(
            "MATCH (n {id: $id}) RETURN n",
            {"id": entity_id},
        )
        if results:
            return results[0]["n"]
        return None

    async def get_neighbors(
        self,
        entity_id: str,
        rel_type: str | None = None,
        direction: str = "both",
    ) -> list[dict]:
        """Get all entities connected to a given entity.

        Args:
            entity_id: The entity to find neighbors of.
            rel_type: Optional relationship type filter (e.g., "HAS_ACCOUNT").
            direction: "outgoing", "incoming", or "both".
        """
        rel_pattern = f"[r:{rel_type.upper()}]" if rel_type else "[r]"
        if direction == "outgoing":
            pattern = f"(a {{id: $id}})-{rel_pattern}->(b)"
        elif direction == "incoming":
            pattern = f"(a {{id: $id}})<-{rel_pattern}-(b)"
        else:
            pattern = f"(a {{id: $id}})-{rel_pattern}-(b)"

        return await self.query(
            f"MATCH {pattern} RETURN b, type(r) as rel_type, properties(r) as rel_props",
            {"id": entity_id},
        )

    async def entity_count(self) -> int:
        """Return total number of entities in the graph."""
        results = await self.query("MATCH (n) RETURN count(n) as count")
        return results[0]["count"] if results else 0

    async def relationship_count(self) -> int:
        """Return total number of relationships in the graph."""
        results = await self.query("MATCH ()-[r]->() RETURN count(r) as count")
        return results[0]["count"] if results else 0

    def _flatten_properties(self, props: dict) -> dict:
        """Flatten nested properties for Neo4j storage.

        Neo4j properties must be primitives or lists of primitives.
        Nested dicts and complex lists get JSON-serialized.
        """
        import json

        flat = {}
        for key, value in props.items():
            if isinstance(value, (str, int, float, bool)):
                flat[key] = value
            elif isinstance(value, list) and all(isinstance(v, (str, int, float)) for v in value):
                flat[key] = value
            else:
                flat[key] = json.dumps(value)
        return flat
