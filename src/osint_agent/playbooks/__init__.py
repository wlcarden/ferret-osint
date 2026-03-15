"""Investigation playbooks — structured tool sequences for common OSINT workflows.

Each playbook encodes a specific investigation pattern (e.g., username→identity,
name→digital surface). Playbooks:
  1. Run a predefined sequence of tools via the registry
  2. Ingest all findings into the graph store
  3. Extract leads from findings for follow-up
  4. Optionally follow leads up to a configurable depth

Playbooks are the programmatic core that the Claude investigation agent
orchestrates. The agent picks the right playbook, provides the seed input,
and interprets the results — but the mechanical tool-chaining is code.
"""

from osint_agent.playbooks.base import Playbook, Lead, PlaybookResult

__all__ = ["Playbook", "Lead", "PlaybookResult"]
