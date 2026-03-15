"""OpenPoliceData adapter — US police incident data access.

Queries the OpenPoliceData library for police incident data from
236+ US agencies and 11 states. Covers traffic stops, use of force,
officer-involved shootings, complaints, and more.
No API key or authentication required.
"""

import logging

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.tools.base import ToolAdapter

logger = logging.getLogger(__name__)

_SOURCE = lambda: Source(tool="openpolicedata")

# Table types most relevant to accountability investigations.
_PRIORITY_TABLES = [
    "USE OF FORCE",
    "OFFICER-INVOLVED SHOOTINGS",
    "COMPLAINTS",
    "STOPS",
    "ARRESTS",
    "TRAFFIC STOPS",
    "INCIDENTS",
    "EMPLOYEE",
    "DEATHS IN CUSTODY",
]


class OpenPoliceDataAdapter(ToolAdapter):
    """Search police incident data from US agencies."""

    name = "openpolicedata"

    def is_available(self) -> bool:
        try:
            import openpolicedata  # noqa: F401
            return True
        except ImportError:
            return False

    async def run(
        self,
        agency: str,
        state: str = "",
        table_type: str = "",
        **kwargs,
    ) -> Finding:
        """Query police data for an agency.

        Args:
            agency: Agency or source name (e.g., "Norfolk", "Fairfax County").
            state: State name (e.g., "Virginia"). Narrows search.
            table_type: Specific table type (e.g., "USE OF FORCE").
                        If empty, returns a catalog of available data.
        """
        import asyncio

        import openpolicedata as opd

        loop = asyncio.get_event_loop()

        def _query():
            return opd.datasets.query()

        try:
            ds = await loop.run_in_executor(None, _query)
        except Exception as exc:
            logger.warning("OpenPoliceData query failed: %s", exc)
            return Finding(notes=f"OpenPoliceData error: {exc}")

        # Filter to matching agency/state.
        mask = ds["SourceName"].str.contains(agency, case=False, na=False)
        if state:
            mask &= ds["State"].str.contains(state, case=False, na=False)
        matches = ds[mask]

        if matches.empty:
            # Try agency field too.
            mask2 = ds["Agency"].str.contains(agency, case=False, na=False)
            if state:
                mask2 &= ds["State"].str.contains(state, case=False, na=False)
            matches = ds[mask2]

        if matches.empty:
            return Finding(
                notes=f"OpenPoliceData: no datasets found for '{agency}'"
                + (f" in {state}" if state else ""),
            )

        entities: list[Entity] = []
        relationships: list[Relationship] = []

        # Build agency entity.
        source_name = matches.iloc[0]["SourceName"]
        agency_state = matches.iloc[0]["State"]
        agency_ent = Entity(
            id=f"organization:police:{_slug(source_name)}:{_slug(agency_state)}",
            entity_type=EntityType.ORGANIZATION,
            label=f"{source_name} Police" if "police" not in source_name.lower() else source_name,
            properties={
                "state": agency_state,
                "agency_type": "law_enforcement",
            },
            sources=[_SOURCE()],
        )
        entities.append(agency_ent)

        # If a specific table type was requested, try to fetch actual data.
        if table_type:
            finding = await self._fetch_table(
                agency_ent, matches, table_type, agency, state,
            )
            return finding

        # Otherwise, catalog what's available.
        cols = ["TableType", "Year", "coverage_start", "coverage_end"]
        available = matches[cols].drop_duplicates()
        table_types = sorted(available["TableType"].unique().tolist())

        # Create document entities for each available dataset.
        for tt in table_types:
            tt_rows = available[available["TableType"] == tt]
            years = sorted(tt_rows["Year"].dropna().unique().tolist())

            doc = Entity(
                id=f"document:opd:{_slug(source_name)}:{_slug(tt)}",
                entity_type=EntityType.DOCUMENT,
                label=f"{source_name} — {tt}",
                properties={
                    "table_type": tt,
                    "years_available": years if years else None,
                    "coverage_start": (
                        str(tt_rows["coverage_start"].min())
                        if tt_rows["coverage_start"].notna().any()
                        else None
                    ),
                    "coverage_end": (
                        str(tt_rows["coverage_end"].max())
                        if tt_rows["coverage_end"].notna().any()
                        else None
                    ),
                },
                sources=[_SOURCE()],
            )
            entities.append(doc)
            relationships.append(Relationship(
                source_id=agency_ent.id,
                target_id=doc.id,
                relation_type=RelationType.OWNS,
                sources=[_SOURCE()],
            ))

        return Finding(
            entities=entities,
            relationships=relationships,
            notes=(
                f"OpenPoliceData: {source_name} ({agency_state}) — "
                f"{len(table_types)} dataset types available: "
                + ", ".join(table_types)
            ),
        )

    async def _fetch_table(
        self,
        agency_ent: Entity,
        matches,
        table_type: str,
        agency: str,
        state: str,
    ) -> Finding:
        """Fetch actual records from a specific table type."""
        import asyncio

        import openpolicedata as opd

        tt_matches = matches[
            matches["TableType"].str.contains(table_type, case=False, na=False)
        ]
        if tt_matches.empty:
            return Finding(
                entities=[agency_ent],
                notes=f"OpenPoliceData: no '{table_type}' data for {agency}",
            )

        # Use the most recent year available.
        row = tt_matches.sort_values("Year", ascending=False).iloc[0]
        source_name = row["SourceName"]

        def _load():
            src = opd.Source(source_name=source_name, state=row["State"])
            table = src.load(
                table_type=row["TableType"],
                date=row["Year"],
            )
            return table.table

        loop = asyncio.get_event_loop()
        try:
            df = await loop.run_in_executor(None, _load)
        except Exception as exc:
            logger.warning("OPD data load failed: %s", exc)
            return Finding(
                entities=[agency_ent],
                notes=f"OpenPoliceData: failed to load data — {exc}",
            )

        # Summarize the data rather than ingesting every row.
        row_count = len(df)
        columns = list(df.columns)

        props = {
            "table_type": row["TableType"],
            "year": str(row["Year"]),
            "record_count": row_count,
            "columns": columns[:30],
        }

        # Extract summary statistics for key columns.
        for col in df.columns:
            col_lower = col.lower()
            demo_keys = (
                "race", "ethnicity", "gender", "sex",
                "force_type", "reason", "disposition",
            )
            if any(k in col_lower for k in demo_keys):
                try:
                    counts = df[col].value_counts().head(10).to_dict()
                    props[f"breakdown_{col}"] = {str(k): int(v) for k, v in counts.items()}
                except Exception:
                    pass

        doc = Entity(
            id=f"document:opd:{_slug(source_name)}:{_slug(row['TableType'])}:{row['Year']}",
            entity_type=EntityType.DOCUMENT,
            label=f"{source_name} — {row['TableType']} ({row['Year']})",
            properties=props,
            sources=[_SOURCE()],
        )

        return Finding(
            entities=[agency_ent, doc],
            relationships=[Relationship(
                source_id=agency_ent.id,
                target_id=doc.id,
                relation_type=RelationType.OWNS,
                sources=[_SOURCE()],
            )],
            notes=(
                f"OpenPoliceData: {source_name} {row['TableType']} "
                f"({row['Year']}) — {row_count} records"
            ),
        )


def _slug(s: str) -> str:
    import re
    return re.sub(r"[^a-z0-9]+", "_", s.lower()).strip("_")
