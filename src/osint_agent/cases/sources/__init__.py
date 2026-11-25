"""Sources a program can search for records about a case's subject.

A source is called with the search term the engine allows, which is the
subject's name, and the subject's stated facts. It returns a SourceResult. Only
sources whose keys are present are installed; the rest report "not available
here" in the brief.
"""

from collections.abc import Mapping

from osint_agent.cases.engine import SourceFunction
from osint_agent.cases.sources.federal_courts import FederalCourts


def default_sources(environ: Mapping[str, str]) -> dict[str, SourceFunction]:
    """The sources this environment holds keys for, by the id perspectives use."""
    sources: dict[str, SourceFunction] = {}
    key = (environ.get("COURTLISTENER_API_KEY") or "").strip()
    if key:
        sources["federal_courts"] = FederalCourts(key)
    return sources


__all__ = ["FederalCourts", "default_sources"]
