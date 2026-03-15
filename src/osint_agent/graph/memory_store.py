"""In-memory GraphStore using NetworkX — for testing and Neo4j-free operation."""

import networkx as nx

from osint_agent.graph.store import GraphStore
from osint_agent.models import Entity, Relationship


class MemoryStore(GraphStore):
    """Graph store backed by NetworkX DiGraph.

    Useful for testing, quick exploration, and environments
    where Neo4j isn't running. Data lives only in process memory.
    """

    def __init__(self):
        self.graph = nx.DiGraph()

    async def merge_entity(self, entity: Entity) -> None:
        """Add or update a node in the in-memory graph."""
        self.graph.add_node(
            entity.id,
            label=entity.label,
            entity_type=entity.entity_type.value,
            sources=[s.model_dump(mode="json") for s in entity.sources],
            **entity.properties,
        )

    async def merge_relationship(self, rel: Relationship) -> None:
        """Add or update an edge in the in-memory graph."""
        self.graph.add_edge(
            rel.source_id,
            rel.target_id,
            relation_type=rel.relation_type.value,
            sources=[s.model_dump(mode="json") for s in rel.sources],
            **rel.properties,
        )

    async def query(self, cypher_or_filter: str, params: dict | None = None) -> list[dict]:
        """Basic query support — not Cypher, just simple filters.

        Supports:
          "all_nodes" — returns all nodes
          "all_edges" — returns all edges
          "neighbors:<entity_id>" — returns neighbors
        """
        if cypher_or_filter == "all_nodes":
            return [
                {"id": n, **self.graph.nodes[n]}
                for n in self.graph.nodes
            ]
        elif cypher_or_filter == "all_edges":
            return [
                {"source": u, "target": v, **self.graph.edges[u, v]}
                for u, v in self.graph.edges
            ]
        elif cypher_or_filter.startswith("neighbors:"):
            entity_id = cypher_or_filter.split(":", 1)[1]
            neighbors = []
            for _, target, data in self.graph.out_edges(entity_id, data=True):
                neighbors.append({"id": target, "direction": "outgoing", **data})
            for source, _, data in self.graph.in_edges(entity_id, data=True):
                neighbors.append({"id": source, "direction": "incoming", **data})
            return neighbors
        return []

    async def entity_count(self) -> int:
        return self.graph.number_of_nodes()

    async def relationship_count(self) -> int:
        return self.graph.number_of_edges()

    def summary(self) -> str:
        """Human-readable summary of graph contents."""
        type_counts: dict[str, int] = {}
        for _, data in self.graph.nodes(data=True):
            t = data.get("entity_type", "unknown")
            type_counts[t] = type_counts.get(t, 0) + 1

        rel_counts: dict[str, int] = {}
        for _, _, data in self.graph.edges(data=True):
            t = data.get("relation_type", "unknown")
            rel_counts[t] = rel_counts.get(t, 0) + 1

        lines = [
            f"Graph: {self.graph.number_of_nodes()} entities,"
            f" {self.graph.number_of_edges()} relationships",
            "Entity types:",
        ]
        for t, count in sorted(type_counts.items()):
            lines.append(f"  {t}: {count}")
        lines.append("Relationship types:")
        for t, count in sorted(rel_counts.items()):
            lines.append(f"  {t}: {count}")
        return "\n".join(lines)
