"""Tests for the playbook system — base, individual playbooks, and runner."""

import pytest
import pytest_asyncio
from unittest.mock import AsyncMock, MagicMock

from osint_agent.models import (
    Entity,
    EntityType,
    Finding,
    Relationship,
    RelationType,
    Source,
)
from osint_agent.playbooks.base import (
    Lead,
    Playbook,
    PlaybookResult,
    ToolStep,
    extract_leads_from_findings,
    _entity_to_lead,
)
from osint_agent.playbooks.username_to_identity import UsernameToldentity
from osint_agent.playbooks.name_to_surface import NameToSurface, _generate_username_variants
from osint_agent.playbooks.org_to_members import OrgToMembers
from osint_agent.playbooks.runner import run_playbook
from osint_agent.graph.sqlite_store import SqliteStore


# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------

@pytest_asyncio.fixture
async def store(tmp_path):
    s = SqliteStore(db_path=str(tmp_path / "test.db"))
    yield s
    await s.close()


@pytest.fixture
def mock_registry():
    """Registry with mock tools that return canned findings."""
    registry = MagicMock()

    def make_mock_tool(name, finding):
        tool = MagicMock()
        tool.name = name
        tool.is_available.return_value = True
        tool.safe_run = AsyncMock(return_value=finding)
        tool.search_party = AsyncMock(return_value=finding)
        return tool

    # Create canned findings for each tool
    maigret_finding = Finding(
        entities=[
            Entity(
                id="account:github:testuser",
                entity_type=EntityType.ACCOUNT,
                label="testuser on GitHub",
                properties={"platform": "GitHub", "username": "testuser"},
                sources=[Source(tool="maigret")],
            ),
            Entity(
                id="account:twitter:testuser",
                entity_type=EntityType.ACCOUNT,
                label="testuser on Twitter",
                properties={"platform": "Twitter", "username": "testuser"},
                sources=[Source(tool="maigret")],
            ),
        ],
    )

    ddg_finding = Finding(
        entities=[
            Entity(
                id="person:ddg:john_doe",
                entity_type=EntityType.PERSON,
                label="John Doe",
                properties={"url": "https://example.com/johndoe"},
                sources=[Source(tool="ddg_search")],
            ),
        ],
    )

    holehe_finding = Finding(
        entities=[
            Entity(
                id="email:john@example.com",
                entity_type=EntityType.EMAIL,
                label="john@example.com",
                sources=[Source(tool="holehe")],
            ),
        ],
    )

    court_finding = Finding(
        entities=[
            Entity(
                id="document:court:case123",
                entity_type=EntityType.DOCUMENT,
                label="Case 123",
                sources=[Source(tool="courtlistener")],
            ),
        ],
    )

    tools = {
        "maigret": make_mock_tool("maigret", maigret_finding),
        "ddg_search": make_mock_tool("ddg_search", ddg_finding),
        "holehe": make_mock_tool("holehe", holehe_finding),
        "courtlistener": make_mock_tool("courtlistener", court_finding),
        "openfec": make_mock_tool("openfec", Finding()),
        "peoplesearch": make_mock_tool("peoplesearch", Finding()),
        "edgar": make_mock_tool("edgar", Finding()),
        "usaspending": make_mock_tool("usaspending", Finding()),
        "sbir": make_mock_tool("sbir", Finding()),
        "patents": make_mock_tool("patents", Finding()),
    }

    registry.get.side_effect = lambda name: tools.get(name)
    return registry


# ------------------------------------------------------------------
# Lead extraction
# ------------------------------------------------------------------

def test_entity_to_lead_email():
    """should extract lead from EMAIL entity"""
    entity = Entity(
        id="email:test@example.com",
        entity_type=EntityType.EMAIL,
        label="test@example.com",
        sources=[Source(tool="holehe")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.lead_type == "email"
    assert lead.value == "test@example.com"
    assert lead.score == 0.8


def test_entity_to_lead_username():
    """should extract lead from USERNAME entity"""
    entity = Entity(
        id="username:johndoe",
        entity_type=EntityType.USERNAME,
        label="johndoe",
        sources=[Source(tool="maigret")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.lead_type == "username"
    assert lead.value == "johndoe"


def test_entity_to_lead_account():
    """should extract username lead from ACCOUNT entity"""
    entity = Entity(
        id="account:github:johndoe",
        entity_type=EntityType.ACCOUNT,
        label="johndoe on GitHub",
        properties={"platform": "GitHub", "username": "johndoe"},
        sources=[Source(tool="maigret")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.lead_type == "username"
    assert lead.value == "johndoe"


def test_entity_to_lead_account_from_label():
    """should extract username from ACCOUNT label when username prop missing"""
    entity = Entity(
        id="account:github:johndoe",
        entity_type=EntityType.ACCOUNT,
        label="johndoe on GitHub",
        properties={"platform": "GitHub"},
        sources=[Source(tool="maigret")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.value == "johndoe"


def test_entity_to_lead_domain():
    """should extract lead from DOMAIN entity"""
    entity = Entity(
        id="domain:example.com",
        entity_type=EntityType.DOMAIN,
        label="example.com",
        sources=[Source(tool="theharvester")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.lead_type == "domain"
    assert lead.score == 0.5


def test_entity_to_lead_person_skipped():
    """should not extract lead from primary PERSON entity"""
    entity = Entity(
        id="person:john",
        entity_type=EntityType.PERSON,
        label="John Doe",
        sources=[Source(tool="manual")],
    )
    lead = _entity_to_lead(entity)
    assert lead is None


def test_entity_to_lead_secondary_person():
    """should extract lead from secondary PERSON entity"""
    entity = Entity(
        id="person:secondary",
        entity_type=EntityType.PERSON,
        label="Jane Smith",
        properties={"is_secondary": True},
        sources=[Source(tool="peoplesearch")],
    )
    lead = _entity_to_lead(entity)
    assert lead is not None
    assert lead.lead_type == "person_name"
    assert lead.value == "Jane Smith"


def test_extract_leads_deduplicates():
    """should not produce duplicate leads"""
    findings = [
        Finding(entities=[
            Entity(
                id="email:a@test.com",
                entity_type=EntityType.EMAIL,
                label="a@test.com",
                sources=[Source(tool="holehe")],
            ),
        ]),
        Finding(entities=[
            Entity(
                id="email:holehe:a@test.com",
                entity_type=EntityType.EMAIL,
                label="a@test.com",
                sources=[Source(tool="holehe")],
            ),
        ]),
    ]
    leads = extract_leads_from_findings(findings)
    assert len(leads) == 1


def test_extract_leads_sorted_by_score():
    """should sort leads by score descending"""
    findings = [
        Finding(entities=[
            Entity(id="d:test.com", entity_type=EntityType.DOMAIN, label="test.com", sources=[Source(tool="a")]),
            Entity(id="e:a@test.com", entity_type=EntityType.EMAIL, label="a@test.com", sources=[Source(tool="a")]),
            Entity(id="u:johndoe", entity_type=EntityType.USERNAME, label="johndoe", sources=[Source(tool="a")]),
        ]),
    ]
    leads = extract_leads_from_findings(findings)
    assert len(leads) == 3
    assert leads[0].lead_type == "email"  # 0.8
    assert leads[1].lead_type == "username"  # 0.7
    assert leads[2].lead_type == "domain"  # 0.5


# ------------------------------------------------------------------
# Username variant generation
# ------------------------------------------------------------------

def test_generate_username_variants():
    """should produce standard username patterns"""
    variants = _generate_username_variants("John", "Doe")
    assert "johndoe" in variants
    assert "john.doe" in variants
    assert "john_doe" in variants
    assert "johnd" in variants
    assert "jdoe" in variants


def test_generate_username_variants_empty():
    """should handle empty input"""
    assert _generate_username_variants("", "") == []
    assert _generate_username_variants("John", "") == []


# ------------------------------------------------------------------
# Playbook step generation
# ------------------------------------------------------------------

def test_username_to_identity_steps():
    """should produce maigret + ddg_search steps"""
    pb = UsernameToldentity()
    steps = pb.steps("testuser")
    tool_names = [s.tool_name for s in steps]
    assert "maigret" in tool_names
    assert "ddg_search" in tool_names


def test_username_to_identity_meta():
    """should have correct name and description"""
    pb = UsernameToldentity()
    assert pb.name == "username_to_identity"
    assert "username" in pb.description.lower()


def test_name_to_surface_steps():
    """should produce search, people, court, donors, and username steps"""
    pb = NameToSurface()
    steps = pb.steps("John Doe", state="Virginia")
    tool_names = [s.tool_name for s in steps]
    assert "ddg_search" in tool_names
    assert "peoplesearch" in tool_names
    assert "courtlistener" in tool_names
    assert "openfec" in tool_names
    assert "maigret" in tool_names  # username variant search

    # Should pass state to people search
    people_step = next(s for s in steps if s.tool_name == "peoplesearch")
    assert people_step.kwargs["state"] == "Virginia"


def test_name_to_surface_single_name():
    """should not add maigret step for single-word name"""
    pb = NameToSurface()
    steps = pb.steps("Madonna")
    tool_names = [s.tool_name for s in steps]
    assert "maigret" not in tool_names


def test_org_to_members_steps():
    """should produce org-focused tool steps"""
    pb = OrgToMembers()
    steps = pb.steps("Acme Corp")
    tool_names = [s.tool_name for s in steps]
    assert "edgar" in tool_names
    assert "usaspending" in tool_names
    assert "patents" in tool_names
    assert "courtlistener" in tool_names


# ------------------------------------------------------------------
# Playbook lead extraction overrides
# ------------------------------------------------------------------

def test_username_playbook_boosts_email_leads():
    """should boost email lead scores for deanonymization"""
    pb = UsernameToldentity()
    findings = [
        Finding(entities=[
            Entity(id="e:a@test.com", entity_type=EntityType.EMAIL, label="a@test.com", sources=[Source(tool="a")]),
            Entity(id="d:test.com", entity_type=EntityType.DOMAIN, label="test.com", sources=[Source(tool="a")]),
        ]),
    ]
    leads = pb.extract_leads(findings)
    email_lead = next(l for l in leads if l.lead_type == "email")
    domain_lead = next(l for l in leads if l.lead_type == "domain")
    # Email should be boosted above its default 0.8
    assert email_lead.score == min(1.0, 0.8 + 0.15)
    assert domain_lead.score == 0.5  # Unchanged


def test_name_playbook_generates_username_variants():
    """should add username variant leads from the seed name"""
    pb = NameToSurface()
    # Provide a finding with a primary person entity
    findings = [
        Finding(entities=[
            Entity(
                id="person:john_doe",
                entity_type=EntityType.PERSON,
                label="John Doe",
                sources=[Source(tool="ddg_search")],
            ),
        ]),
    ]
    leads = pb.extract_leads(findings)
    username_leads = [l for l in leads if l.lead_type == "username"]
    # Should have variants (john.doe, john_doe, johnd, jdoe — skips johndoe since it's step[0])
    values = {l.value for l in username_leads}
    assert "john.doe" in values
    assert "jdoe" in values


def test_org_playbook_generates_person_leads():
    """should generate person leads from discovered officers"""
    pb = OrgToMembers()
    findings = [
        Finding(entities=[
            Entity(
                id="person:officer:jane",
                entity_type=EntityType.PERSON,
                label="Jane Smith",
                sources=[Source(tool="edgar")],
            ),
        ]),
    ]
    leads = pb.extract_leads(findings)
    person_leads = [l for l in leads if l.lead_type == "person_name"]
    assert len(person_leads) >= 1
    assert person_leads[0].value == "Jane Smith"


# ------------------------------------------------------------------
# PlaybookResult
# ------------------------------------------------------------------

def test_playbook_result_summary():
    """should produce a readable summary"""
    result = PlaybookResult(
        playbook_name="test",
        investigation_id=1,
        findings=[Finding(), Finding()],
        leads=[Lead(lead_type="email", value="a@test.com")],
        entity_count=5,
        relationship_count=3,
    )
    summary = result.summary()
    assert "test" in summary
    assert "2" in summary  # findings
    assert "5" in summary  # entities
    assert "1" in summary  # leads


# ------------------------------------------------------------------
# Runner integration
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_run_playbook_creates_investigation(store, mock_registry):
    """should create an investigation and return its ID"""
    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=mock_registry,
        store=store,
        follow_leads=False,
    )
    assert result.investigation_id is not None
    assert result.investigation_id > 0

    investigations = await store.list_investigations()
    assert len(investigations) == 1
    assert "testuser" in investigations[0]["name"]


@pytest.mark.asyncio
async def test_run_playbook_ingests_findings(store, mock_registry):
    """should ingest findings into the store"""
    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=mock_registry,
        store=store,
        follow_leads=False,
    )
    assert result.entity_count > 0
    assert await store.entity_count() > 0


@pytest.mark.asyncio
async def test_run_playbook_generates_leads(store, mock_registry):
    """should extract leads and persist them in the store"""
    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=mock_registry,
        store=store,
        follow_leads=False,
    )
    assert len(result.leads) > 0
    db_leads = await store.get_leads(investigation_id=result.investigation_id)
    assert len(db_leads) > 0


@pytest.mark.asyncio
async def test_run_playbook_follows_leads(store, mock_registry):
    """should follow high-score leads when follow_leads=True"""
    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=mock_registry,
        store=store,
        follow_leads=True,
        max_depth=1,
        lead_score_threshold=0.5,
    )
    # With mock tools, following leads should produce additional findings
    assert len(result.findings) >= 2  # At least initial + follow-up


@pytest.mark.asyncio
async def test_run_playbook_skips_unavailable_tools(store):
    """should skip tools that aren't available"""
    registry = MagicMock()
    unavailable_tool = MagicMock()
    unavailable_tool.name = "maigret"
    unavailable_tool.is_available.return_value = False
    registry.get.return_value = unavailable_tool

    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=registry,
        store=store,
        follow_leads=False,
    )
    assert result.entity_count == 0


@pytest.mark.asyncio
async def test_run_playbook_no_follow_flag(store, mock_registry):
    """should not follow leads when follow_leads=False"""
    pb = UsernameToldentity()
    result = await run_playbook(
        playbook=pb,
        seed="testuser",
        registry=mock_registry,
        store=store,
        follow_leads=False,
    )
    # Should only have findings from initial steps
    # (maigret mock + ddg_search mock = 2)
    initial_step_count = len([s for s in pb.steps("testuser")])
    assert len(result.findings) <= initial_step_count
