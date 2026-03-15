#!/usr/bin/env python3
"""End-to-end test: username → Maigret → Finding → MemoryStore → summary.

Run: python scripts/test_pipeline.py <username>

This validates the full data flow without requiring Neo4j.
"""

import asyncio
import sys
from pathlib import Path

# Add src to path for direct script execution
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from osint_agent.tools.maigret import MaigretAdapter
from osint_agent.graph.memory_store import MemoryStore


async def main(username: str):
    print(f"=== OSINT Pipeline Test: {username} ===\n")

    # Step 1: Run Maigret
    adapter = MaigretAdapter(timeout=30, top_sites=100)
    if not adapter.is_available():
        print("ERROR: maigret is not installed. Run: pip install maigret")
        sys.exit(1)

    print(f"[1/3] Running Maigret on '{username}'...")
    finding = await adapter.run(username=username)
    print(f"      {finding.notes}")
    print(f"      Entities: {len(finding.entities)}, Relationships: {len(finding.relationships)}")

    # Step 2: Ingest into MemoryStore
    print("\n[2/3] Ingesting into graph...")
    store = MemoryStore()
    await store.ingest_finding(finding)
    print(f"      {await store.entity_count()} nodes, {await store.relationship_count()} edges")

    # Step 3: Query and display
    print(f"\n[3/3] Graph summary:")
    print(f"      {store.summary()}")

    # Show discovered accounts
    print(f"\n=== Accounts found for '{username}' ===")
    for entity in finding.entities:
        if entity.entity_type.value == "account":
            platform = entity.properties.get("platform", "?")
            url = entity.properties.get("url", "")
            tags = entity.properties.get("tags", [])
            tag_str = f" [{', '.join(tags)}]" if tags else ""
            print(f"  {platform}{tag_str}: {url}")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage: python scripts/test_pipeline.py <username>")
        sys.exit(1)
    asyncio.run(main(sys.argv[1]))
