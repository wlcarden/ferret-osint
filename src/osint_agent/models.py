"""Core data models for OSINT entities and relationships.

These models define the unified schema that all tool outputs normalize into.
Designed to map naturally to Neo4j nodes and relationships.
"""

from datetime import UTC, datetime
from enum import Enum

from pydantic import BaseModel, Field


class EntityType(str, Enum):
    """Types of entities in the investigation graph."""

    PERSON = "person"
    ORGANIZATION = "organization"
    DOMAIN = "domain"
    EMAIL = "email"
    PHONE = "phone"
    USERNAME = "username"
    ADDRESS = "address"
    ACCOUNT = "account"           # Social media or service account
    DOCUMENT = "document"         # Court filing, SEC filing, etc.
    PROPERTY = "property"
    VEHICLE = "vehicle"


class RelationType(str, Enum):
    """Types of relationships between entities."""

    # Identity
    HAS_EMAIL = "has_email"
    HAS_PHONE = "has_phone"
    HAS_USERNAME = "has_username"
    HAS_ACCOUNT = "has_account"
    HAS_ADDRESS = "has_address"
    ALSO_KNOWN_AS = "also_known_as"

    # Organizational
    WORKS_AT = "works_at"
    OFFICER_OF = "officer_of"
    OWNS = "owns"
    CONTROLS = "controls"
    AFFILIATED_WITH = "affiliated_with"

    # Financial
    DONATED_TO = "donated_to"
    TRANSACTED_WITH = "transacted_with"

    # Legal
    PARTY_TO = "party_to"          # Party to a court case
    FILED = "filed"

    # Social
    FOLLOWS = "follows"
    CONNECTED_TO = "connected_to"
    MENTIONED = "mentioned"

    # Temporal
    PRECEDED_BY = "preceded_by"    # For timeline events


class Source(BaseModel):
    """Provenance tracking for any piece of data."""

    tool: str                      # Which tool produced this
    source_url: str | None = None  # Where the data came from
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    confidence: float = 1.0        # 0.0 to 1.0
    raw_data: dict | None = None   # Original tool output for audit trail


class Entity(BaseModel):
    """A node in the investigation graph."""

    id: str                        # Stable identifier (e.g., "email:jane@example.com")
    entity_type: EntityType
    label: str                     # Display name
    properties: dict = {}          # Flexible key-value attributes
    sources: list[Source] = []     # Where we learned about this entity


class Relationship(BaseModel):
    """An edge in the investigation graph."""

    source_id: str                 # Entity id
    target_id: str                 # Entity id
    relation_type: RelationType
    properties: dict = {}          # e.g., {"role": "CEO", "start_date": "2020-01"}
    sources: list[Source] = []


class ErrorCategory(str, Enum):
    """Classification of tool errors for actionable user guidance."""

    RATE_LIMIT = "rate_limit"
    AUTH = "auth"
    NOT_FOUND = "not_found"
    NETWORK = "network"
    TIMEOUT = "timeout"
    SERVER = "server"
    PARSE = "parse"
    UNKNOWN = "unknown"


# Maps HTTP status code → (category, suggestion template)
_STATUS_MAP: dict[int, tuple[ErrorCategory, str]] = {
    401: (ErrorCategory.AUTH, "Check the API key environment variable for this service"),
    403: (ErrorCategory.AUTH, "API key invalid or lacks permissions"),
    404: (ErrorCategory.NOT_FOUND, "Resource does not exist"),
    429: (ErrorCategory.RATE_LIMIT, "Rate limited — wait before retrying"),
    500: (ErrorCategory.SERVER, "Internal server error — service may be temporarily down"),
    502: (ErrorCategory.SERVER, "Bad gateway — upstream service unreachable"),
    503: (ErrorCategory.SERVER, "Service unavailable — try again later"),
    504: (ErrorCategory.SERVER, "Gateway timeout — service is overloaded"),
}


class ToolError(BaseModel):
    """Structured error from a tool execution."""

    tool: str
    category: ErrorCategory
    message: str
    http_status: int | None = None
    retry_after: float | None = None
    suggestion: str = ""

    @classmethod
    def for_http_status(
        cls,
        tool: str,
        status: int,
        headers: dict | None = None,
    ) -> "ToolError":
        """Create a ToolError from an HTTP status code."""
        category, suggestion = _STATUS_MAP.get(
            status, (ErrorCategory.UNKNOWN, f"HTTP {status}"),
        )
        retry_after = None
        if status == 429 and headers:
            raw = headers.get("retry-after") or headers.get("Retry-After")
            if raw:
                try:
                    retry_after = float(raw)
                    suggestion = f"Rate limited — wait {retry_after:.0f}s before retrying"
                except ValueError:
                    pass
        return cls(
            tool=tool,
            category=category,
            message=f"HTTP {status}",
            http_status=status,
            retry_after=retry_after,
            suggestion=suggestion,
        )


class Finding(BaseModel):
    """A discrete investigation finding — an entity, relationship, or observation."""

    entities: list[Entity] = []
    relationships: list[Relationship] = []
    notes: str | None = None       # Agent observations or context
    error: ToolError | None = None  # Structured error info (None = success)
