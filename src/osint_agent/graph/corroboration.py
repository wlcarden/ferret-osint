"""Weighted corroboration model for entity attribution.

Prevents false entity linkage by requiring multiple independent
corroborating factors before attributing records to the same individual.

Factor weights (PERSON):
  - Unique identifiers (email, phone, DOB): 2.0 each
  - Semi-unique (full address, employer+role, 3+ token name): 1.0 each
  - Source diversity (distinctive name from different tools): 1.0
  - Weak (common 2-token name, city, state): 0.5 each

Thresholds (PERSON):
  - Probable link: cumulative weight >= 2.0
  - Confirmed link: cumulative weight >= 3.0
  - Below probable: no link created

Source diversity bonus: a distinctive name (3+ tokens) appearing
independently in different tool databases is itself corroborating
evidence. Common 2-token names (e.g., "John Smith") don't qualify
because false-positive rates are too high.

Factor weights (ORGANIZATION):
  - Unique identifiers (EIN, DUNS, registration_number, bioguide_id): 2.0
  - Semi-unique (address, jurisdiction, agency_type): 1.0
  - Weak (city, state): 0.5

Thresholds (ORGANIZATION) — lower than PERSON because org names are
structurally more distinctive in government databases:
  - Probable link: cumulative weight >= 1.5
  - Confirmed link: cumulative weight >= 2.5
"""

from dataclasses import dataclass, field

from osint_agent.models import Entity

# Fields classified by discriminative power (PERSON entities)
UNIQUE_FIELDS = frozenset({
    "email", "phone", "phone_number", "dob", "date_of_birth",
    "ssn", "steam_id64", "tax_id", "ein",
})

SEMI_UNIQUE_FIELDS = frozenset({
    "address", "full_address", "employer", "company",
    "occupation", "title", "duns", "zip",
})

WEAK_FIELDS = frozenset({
    "city", "state", "country", "location",
})

# Fields classified by discriminative power (ORGANIZATION entities)
ORG_UNIQUE_FIELDS = frozenset({
    "ein", "duns", "registration_number", "fara_registration_number",
    "bioguide_id", "cik", "ticker", "fec_id",
})

ORG_SEMI_UNIQUE_FIELDS = frozenset({
    "address", "full_address", "jurisdiction",
    "agency_type", "parent_org", "website", "url",
})

ORG_WEAK_FIELDS = frozenset({
    "city", "state", "country", "jurisdiction_level",
})

# Weight per factor category
WEIGHT_UNIQUE = 2.0
WEIGHT_SEMI_UNIQUE = 1.0
WEIGHT_WEAK = 0.5

# Thresholds (PERSON)
CONFIRMED_THRESHOLD = 3.0
PROBABLE_THRESHOLD = 2.0

# Thresholds (ORGANIZATION) — lower because org names are more distinctive
ORG_CONFIRMED_THRESHOLD = 2.5
ORG_PROBABLE_THRESHOLD = 1.5


@dataclass
class CorroborationFactor:
    """A single piece of matching evidence between two entities."""

    field: str       # Which field matched (e.g., "email", "city", "name")
    value: str       # The matching value
    weight: float    # Factor weight (2.0, 1.0, or 0.5)
    category: str    # "unique", "semi_unique", "weak", "name"


@dataclass
class CorroborationResult:
    """Outcome of corroboration analysis between two entities."""

    factors: list[CorroborationFactor] = field(default_factory=list)
    total_weight: float = 0.0
    level: str = "insufficient"  # "confirmed", "probable", "insufficient"
    confidence: float = 0.0      # Mapped to 0.0-1.0 for backward compatibility


class CorroborationPolicy:
    """Evaluates entity pairs using weighted corroboration.

    Two entities are linked only when the cumulative weight of
    independent corroborating factors meets the threshold.

    Supports both PERSON and ORGANIZATION entity evaluation with
    type-appropriate thresholds and field classifications.
    """

    def __init__(
        self,
        probable_threshold: float = PROBABLE_THRESHOLD,
        confirmed_threshold: float = CONFIRMED_THRESHOLD,
        org_probable_threshold: float = ORG_PROBABLE_THRESHOLD,
        org_confirmed_threshold: float = ORG_CONFIRMED_THRESHOLD,
    ):
        self.probable_threshold = probable_threshold
        self.confirmed_threshold = confirmed_threshold
        self.org_probable_threshold = org_probable_threshold
        self.org_confirmed_threshold = org_confirmed_threshold

    def evaluate(
        self,
        e1: Entity,
        e2: Entity,
        name_similarity: float = 1.0,
        entity_type: str = "person",
    ) -> CorroborationResult:
        """Evaluate corroboration between two entities.

        Args:
            e1, e2: Entities to compare.
            name_similarity: Pre-computed name similarity (0.0-1.0).
            entity_type: "person" or "organization" — determines thresholds
                and field classifications.

        Returns:
            CorroborationResult with factors, weight, and classification.
        """
        is_org = entity_type == "organization"
        factors: list[CorroborationFactor] = []

        # Score name match
        if name_similarity > 0.0:
            if is_org:
                name_factor = self._score_org_name(e1, e2, name_similarity)
            else:
                name_factor = self._score_name(e1, e2, name_similarity)
            if name_factor:
                factors.append(name_factor)

        # Score property matches
        if is_org:
            factors.extend(self._score_org_properties(e1, e2))
        else:
            factors.extend(self._score_properties(e1, e2))

        # Cross-source diversity bonus (PERSON only): a distinctive
        # name (3+ tokens) appearing independently in different tool
        # databases is corroborating evidence.  Common 2-token names
        # don't qualify — too many false positives.
        if not is_org and name_similarity >= 0.9:
            tokens1 = e1.label.lower().split()
            tokens2 = e2.label.lower().split()
            max_tokens = max(len(tokens1), len(tokens2))
            if max_tokens >= 3 and _are_different_sources(e1, e2):
                factors.append(CorroborationFactor(
                    field="source_diversity",
                    value=(
                        f"{_source_tool(e1)} + {_source_tool(e2)}"
                    ),
                    weight=WEIGHT_SEMI_UNIQUE,
                    category="semi_unique",
                ))

        total = sum(f.weight for f in factors)

        prob_thresh = self.org_probable_threshold if is_org else self.probable_threshold
        conf_thresh = self.org_confirmed_threshold if is_org else self.confirmed_threshold

        if total >= conf_thresh:
            level = "confirmed"
        elif total >= prob_thresh:
            level = "probable"
        else:
            level = "insufficient"

        confidence = self._weight_to_confidence(total, prob_thresh, conf_thresh)

        return CorroborationResult(
            factors=factors,
            total_weight=round(total, 2),
            level=level,
            confidence=round(confidence, 3),
        )

    def _score_name(
        self,
        e1: Entity,
        e2: Entity,
        similarity: float,
    ) -> CorroborationFactor | None:
        """Score a name match based on specificity.

        A 3+ token name (suggesting a middle name/initial) is more
        discriminative than a 2-token first+last.
        """
        if similarity < 0.5:
            return None

        tokens1 = e1.label.lower().split()
        tokens2 = e2.label.lower().split()
        max_tokens = max(len(tokens1), len(tokens2))

        if max_tokens >= 3:
            # 3+ tokens (includes middle name/initial) — semi-unique
            weight = WEIGHT_SEMI_UNIQUE * similarity
            category = "semi_unique"
        else:
            # 2-token name — weak
            weight = WEIGHT_WEAK * similarity
            category = "weak"

        return CorroborationFactor(
            field="name",
            value=e1.label,
            weight=round(weight, 2),
            category=category,
        )

    def _score_properties(
        self,
        e1: Entity,
        e2: Entity,
    ) -> list[CorroborationFactor]:
        """Find matching properties and assign weights by field type."""
        factors = []

        shared_fields = set(e1.properties.keys()) & set(e2.properties.keys())

        for field_name in shared_fields:
            v1 = str(e1.properties[field_name]).lower().strip()
            v2 = str(e2.properties[field_name]).lower().strip()

            if not v1 or not v2 or v1 != v2:
                continue

            if field_name in UNIQUE_FIELDS:
                weight = WEIGHT_UNIQUE
                category = "unique"
            elif field_name in SEMI_UNIQUE_FIELDS:
                weight = WEIGHT_SEMI_UNIQUE
                category = "semi_unique"
            elif field_name in WEAK_FIELDS:
                weight = WEIGHT_WEAK
                category = "weak"
            else:
                # Unknown field — treat as weak
                weight = WEIGHT_WEAK
                category = "weak"

            factors.append(CorroborationFactor(
                field=field_name,
                value=v1,
                weight=weight,
                category=category,
            ))

        return factors

    def _score_org_name(
        self,
        e1: Entity,
        e2: Entity,
        similarity: float,
    ) -> CorroborationFactor | None:
        """Score an organization name match.

        Org names in government databases are more structurally distinctive
        than person names. An exact org name match across two independent
        federal databases (e.g., "Acme Corp" in SBIR + "Acme Corporation"
        in USASpending) is strong evidence — these normalize to the same
        blocked key and the suffix stripping accounts for the variation.
        """
        if similarity < 0.5:
            return None

        tokens1 = e1.label.lower().split()
        tokens2 = e2.label.lower().split()
        max_tokens = max(len(tokens1), len(tokens2))

        if similarity >= 0.95:
            # Near-exact or suffix-normalized match — org names that
            # match this closely across sources are strong signals
            weight = WEIGHT_SEMI_UNIQUE * 1.5 * similarity
            category = "semi_unique"
        elif max_tokens >= 3:
            # Multi-word org name with partial match
            weight = WEIGHT_SEMI_UNIQUE * similarity
            category = "semi_unique"
        else:
            # Short partial match (e.g., 2-word name at 0.7 similarity)
            weight = WEIGHT_WEAK * similarity
            category = "weak"

        return CorroborationFactor(
            field="name",
            value=e1.label,
            weight=round(weight, 2),
            category=category,
        )

    def _score_org_properties(
        self,
        e1: Entity,
        e2: Entity,
    ) -> list[CorroborationFactor]:
        """Find matching org properties and assign weights."""
        factors = []

        shared_fields = set(e1.properties.keys()) & set(e2.properties.keys())

        for field_name in shared_fields:
            v1 = str(e1.properties[field_name]).lower().strip()
            v2 = str(e2.properties[field_name]).lower().strip()

            if not v1 or not v2 or v1 != v2:
                continue

            if field_name in ORG_UNIQUE_FIELDS:
                weight = WEIGHT_UNIQUE
                category = "unique"
            elif field_name in ORG_SEMI_UNIQUE_FIELDS:
                weight = WEIGHT_SEMI_UNIQUE
                category = "semi_unique"
            elif field_name in ORG_WEAK_FIELDS:
                weight = WEIGHT_WEAK
                category = "weak"
            else:
                weight = WEIGHT_WEAK
                category = "weak"

            factors.append(CorroborationFactor(
                field=field_name,
                value=v1,
                weight=weight,
                category=category,
            ))

        return factors

    def _weight_to_confidence(
        self,
        weight: float,
        prob_thresh: float | None = None,
        conf_thresh: float | None = None,
    ) -> float:
        """Map corroboration weight to a 0.0-1.0 confidence score.

        - Confirmed (>= conf_thresh): maps to 0.8 - 1.0
        - Probable (>= prob_thresh): maps to 0.6 - 0.79
        - Insufficient (< prob_thresh): maps to 0.0 - 0.59
        """
        if prob_thresh is None:
            prob_thresh = self.probable_threshold
        if conf_thresh is None:
            conf_thresh = self.confirmed_threshold

        if weight >= conf_thresh:
            excess = weight - conf_thresh
            return min(1.0, 0.8 + excess * 0.05)
        elif weight >= prob_thresh:
            ratio = (weight - prob_thresh) / (conf_thresh - prob_thresh)
            return 0.6 + ratio * 0.19
        else:
            ratio = weight / prob_thresh if prob_thresh > 0 else 0.0
            return ratio * 0.59


def _are_different_sources(e1: Entity, e2: Entity) -> bool:
    """Check if two entities come from different tool sources."""
    tools1 = {s.tool for s in e1.sources if s.tool}
    tools2 = {s.tool for s in e2.sources if s.tool}
    return bool(tools1) and bool(tools2) and not tools1.intersection(tools2)


def _source_tool(entity: Entity) -> str:
    """Extract the primary tool name from an entity's sources."""
    for s in entity.sources:
        if s.tool:
            return s.tool
    return "unknown"
