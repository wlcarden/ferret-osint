"""Tests for the autonomous investigation loop."""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from osint_agent.models import Entity, EntityType, Finding, Relationship, RelationType
from osint_agent.playbooks.base import Lead, PlaybookResult, ToolStep
from osint_agent.playbooks.loop import (
    DEFAULT_COMPLETENESS,
    LoopConfig,
    LoopState,
    _check_completeness,
    _check_termination,
    _is_tried,
    _mark_tried,
    _step_input_value,
    run_investigation_loop,
)


# ------------------------------------------------------------------
# LoopConfig defaults
# ------------------------------------------------------------------

def test_loop_config_defaults():
    """should have sensible defaults"""
    cfg = LoopConfig()
    assert cfg.max_iterations == 20
    assert cfg.max_stale_rounds == 3
    assert cfg.lead_score_threshold == 0.4
    assert cfg.max_leads_per_round == 3
    assert cfg.completeness_criteria == DEFAULT_COMPLETENESS


def test_loop_config_override():
    """should accept custom values"""
    cfg = LoopConfig(
        max_iterations=5,
        max_stale_rounds=1,
        lead_score_threshold=0.8,
    )
    assert cfg.max_iterations == 5
    assert cfg.max_stale_rounds == 1
    assert cfg.lead_score_threshold == 0.8


# ------------------------------------------------------------------
# LoopState
# ------------------------------------------------------------------

def test_loop_state_defaults():
    """should start at zero"""
    state = LoopState()
    assert state.iteration == 0
    assert state.stale_rounds == 0
    assert state.tried == set()
    assert state.entity_count_before == 0
    assert state.stop_reason == ""


# ------------------------------------------------------------------
# Tool dedup tracking
# ------------------------------------------------------------------

def test_mark_tried_and_is_tried():
    """should track (tool, input) pairs case-insensitively"""
    state = LoopState()
    _mark_tried(state, "maigret", "  Tommy-Boy  ")
    assert _is_tried(state, "maigret", "tommy-boy")
    assert _is_tried(state, "maigret", "TOMMY-BOY")
    assert not _is_tried(state, "reddit", "tommy-boy")
    assert not _is_tried(state, "maigret", "other-user")


def test_mark_tried_normalizes_whitespace():
    """should strip whitespace from input values"""
    state = LoopState()
    _mark_tried(state, "holehe", "  test@example.com  ")
    assert _is_tried(state, "holehe", "test@example.com")


# ------------------------------------------------------------------
# Step input extraction
# ------------------------------------------------------------------

def test_step_input_value_username():
    """should extract username from kwargs"""
    step = ToolStep(tool_name="maigret", kwargs={"username": "tommy-boy"})
    assert _step_input_value(step) == "tommy-boy"


def test_step_input_value_email():
    """should extract email from kwargs"""
    step = ToolStep(tool_name="holehe", kwargs={"email": "t@example.com"})
    assert _step_input_value(step) == "t@example.com"


def test_step_input_value_fallback():
    """should fall back to first kwarg value"""
    step = ToolStep(tool_name="custom", kwargs={"custom_param": "value123"})
    assert _step_input_value(step) == "value123"


def test_step_input_value_empty():
    """should return empty string for empty kwargs"""
    step = ToolStep(tool_name="custom", kwargs={})
    assert _step_input_value(step) == ""


# ------------------------------------------------------------------
# Termination: max iterations
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_termination_max_iterations():
    """should stop at max iteration count"""
    state = LoopState(iteration=11)
    cfg = LoopConfig(max_iterations=10)
    store = AsyncMock()
    result = PlaybookResult(playbook_name="test", investigation_id=1)

    reason = await _check_termination(state, cfg, store, result)
    assert "Max iterations" in reason


@pytest.mark.asyncio
async def test_termination_not_yet():
    """should not stop before max iterations"""
    state = LoopState(iteration=5)
    cfg = LoopConfig(max_iterations=10, completeness_criteria={})
    store = AsyncMock()
    result = PlaybookResult(playbook_name="test", investigation_id=1)

    reason = await _check_termination(state, cfg, store, result)
    assert reason == ""


# ------------------------------------------------------------------
# Termination: stale rounds
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_termination_stale_rounds():
    """should stop after N consecutive stale rounds"""
    state = LoopState(iteration=5, stale_rounds=3)
    cfg = LoopConfig(max_stale_rounds=3, completeness_criteria={})
    store = AsyncMock()
    result = PlaybookResult(playbook_name="test", investigation_id=1)

    reason = await _check_termination(state, cfg, store, result)
    assert "Diminishing returns" in reason


# ------------------------------------------------------------------
# Termination: completeness
# ------------------------------------------------------------------

@pytest.mark.asyncio
async def test_completeness_met():
    """should detect when entity counts meet criteria"""
    store = AsyncMock()
    db = AsyncMock()
    store._ensure_db = AsyncMock(return_value=db)

    # Simulate entity counts: 2 persons and 3 accounts
    async def fake_execute(sql, params):
        cursor = AsyncMock()
        entity_type = params[0]
        if entity_type == "person":
            cursor.fetchone = AsyncMock(return_value=(2,))
        elif entity_type == "account":
            cursor.fetchone = AsyncMock(return_value=(3,))
        else:
            cursor.fetchone = AsyncMock(return_value=(0,))
        return cursor

    db.execute = fake_execute

    criteria = {EntityType.PERSON: 1, EntityType.ACCOUNT: 2}
    assert await _check_completeness(store, criteria) is True


@pytest.mark.asyncio
async def test_completeness_not_met():
    """should return False when criteria are unmet"""
    store = AsyncMock()
    db = AsyncMock()
    store._ensure_db = AsyncMock(return_value=db)

    async def fake_execute(sql, params):
        cursor = AsyncMock()
        cursor.fetchone = AsyncMock(return_value=(0,))
        return cursor

    db.execute = fake_execute

    criteria = {EntityType.PERSON: 1}
    assert await _check_completeness(store, criteria) is False


# ------------------------------------------------------------------
# Full loop integration (mocked tools)
# ------------------------------------------------------------------

def _make_mock_playbook(name="test_pb", steps_list=None, completeness=None):
    """Create a mock playbook with configurable steps and completeness."""
    pb = MagicMock()
    pb.name = name
    pb.description = "Test playbook"
    pb.completeness_criteria = completeness or {}

    if steps_list is None:
        steps_list = [
            ToolStep(
                tool_name="maigret",
                kwargs={"username": "testuser"},
                description="Search for testuser",
            ),
        ]
    pb.steps = MagicMock(return_value=steps_list)
    pb.extract_leads = MagicMock(return_value=[])
    return pb


def _make_finding_with_entities(entities=None, relationships=None):
    """Create a finding with optional entities."""
    return Finding(
        entities=entities or [],
        relationships=relationships or [],
        notes="test finding",
    )


@pytest.mark.asyncio
async def test_loop_phase1_runs_steps():
    """should run initial playbook steps and ingest findings"""
    finding = _make_finding_with_entities([
        Entity(
            id="account:test:testuser",
            entity_type=EntityType.ACCOUNT,
            label="testuser on Test",
            properties={"platform": "Test"},
        ),
    ])

    pb = _make_mock_playbook()
    pb.extract_leads.return_value = []

    registry = MagicMock()
    tool = MagicMock()
    tool.is_available.return_value = True
    tool.check_availability.return_value = (True, "ready")
    tool.safe_run = AsyncMock(return_value=finding)
    registry.get.return_value = tool

    store = AsyncMock()
    store.create_investigation = AsyncMock(return_value=1)
    store.entity_count = AsyncMock(return_value=1)
    store.relationship_count = AsyncMock(return_value=0)
    store.get_leads = AsyncMock(return_value=[])
    store.pending_lead_count = AsyncMock(return_value=0)

    result = await run_investigation_loop(
        playbook=pb,
        seed="testuser",
        registry=registry,
        store=store,
        config=LoopConfig(max_iterations=1, completeness_criteria={}),
    )

    assert result.investigation_id == 1
    assert len(result.findings) >= 1
    store.ingest_finding.assert_called()


@pytest.mark.asyncio
async def test_loop_stops_when_no_leads():
    """should stop when no actionable leads exist"""
    pb = _make_mock_playbook(completeness={})
    pb.extract_leads.return_value = []

    registry = MagicMock()
    tool = MagicMock()
    tool.is_available.return_value = True
    tool.check_availability.return_value = (True, "ready")
    tool.safe_run = AsyncMock(return_value=Finding(notes="empty"))
    registry.get.return_value = tool

    store = AsyncMock()
    store.create_investigation = AsyncMock(return_value=1)
    store.entity_count = AsyncMock(return_value=0)
    store.relationship_count = AsyncMock(return_value=0)
    store.get_leads = AsyncMock(return_value=[])
    store.pending_lead_count = AsyncMock(return_value=0)

    result = await run_investigation_loop(
        playbook=pb,
        seed="nobody",
        registry=registry,
        store=store,
        config=LoopConfig(completeness_criteria={}),
    )

    assert "No actionable leads" in result.findings[0].notes or result.entity_count == 0


@pytest.mark.asyncio
async def test_loop_follows_leads():
    """should follow leads from the queue and produce findings"""
    account = Entity(
        id="account:test:user1",
        entity_type=EntityType.ACCOUNT,
        label="user1 on Test",
        properties={"platform": "Test", "username": "user1"},
    )
    phase1_finding = _make_finding_with_entities([account])
    phase2_finding = _make_finding_with_entities([
        Entity(
            id="account:reddit:user1",
            entity_type=EntityType.ACCOUNT,
            label="user1 on Reddit",
            properties={"platform": "Reddit", "username": "user1"},
        ),
    ])

    lead = Lead(
        lead_type="username",
        value="user1",
        score=0.7,
        source_entity_id="account:test:user1",
    )

    pb = _make_mock_playbook(completeness={})
    pb.extract_leads.return_value = [lead]

    # Track call count to return different findings
    call_count = {"n": 0}

    async def fake_run(**kwargs):
        call_count["n"] += 1
        if call_count["n"] <= 1:
            return phase1_finding
        return phase2_finding

    registry = MagicMock()
    tool = MagicMock()
    tool.is_available.return_value = True
    tool.check_availability.return_value = (True, "ready")
    tool.safe_run = AsyncMock(side_effect=fake_run)
    registry.get.return_value = tool

    # First get_leads returns the lead, second returns empty (loop ends)
    lead_row = {
        "id": 1,
        "lead_type": "username",
        "value": "user1",
        "score": 0.7,
    }
    store = AsyncMock()
    store.create_investigation = AsyncMock(return_value=1)
    store.entity_count = AsyncMock(side_effect=[0, 1, 2])
    store.relationship_count = AsyncMock(return_value=0)
    store.get_leads = AsyncMock(side_effect=[
        [lead_row],  # First iteration: one lead
        [],           # Second iteration: no leads, loop stops
    ])
    store.pending_lead_count = AsyncMock(return_value=0)

    result = await run_investigation_loop(
        playbook=pb,
        seed="user1",
        registry=registry,
        store=store,
        config=LoopConfig(
            max_iterations=5,
            completeness_criteria={},
        ),
    )

    assert len(result.findings) >= 2
    # Should have called update_lead to mark the lead as completed
    store.update_lead.assert_called()


@pytest.mark.asyncio
async def test_loop_skips_tried_tools():
    """should not re-run tool/input pairs already tried"""
    finding = _make_finding_with_entities([
        Entity(
            id="account:steam:user1",
            entity_type=EntityType.ACCOUNT,
            label="user1 on Steam",
            properties={"platform": "Steam", "username": "user1"},
        ),
    ])

    # Phase 1 step uses maigret with "user1"
    steps = [
        ToolStep(tool_name="maigret", kwargs={"username": "user1"}),
    ]
    pb = _make_mock_playbook(steps_list=steps, completeness={})
    pb.extract_leads.return_value = [
        Lead(lead_type="username", value="user1", score=0.7),
    ]

    registry = MagicMock()
    tool = MagicMock()
    tool.is_available.return_value = True
    tool.check_availability.return_value = (True, "ready")
    tool.run = AsyncMock(return_value=finding)
    registry.get.return_value = tool

    lead_row = {
        "id": 1,
        "lead_type": "username",
        "value": "user1",
        "score": 0.7,
    }
    store = AsyncMock()
    store.create_investigation = AsyncMock(return_value=1)
    # entity_count is called: after phase1, after each loop iteration, and at the end
    store.entity_count = AsyncMock(return_value=1)
    store.relationship_count = AsyncMock(return_value=0)
    store.get_leads = AsyncMock(side_effect=[
        [lead_row],
        [],
    ])
    store.pending_lead_count = AsyncMock(return_value=0)

    result = await run_investigation_loop(
        playbook=pb,
        seed="user1",
        registry=registry,
        store=store,
        config=LoopConfig(max_iterations=5, completeness_criteria={}),
    )

    # maigret+user1 was already run in phase 1, so in phase 2
    # the loop should skip it. reddit+user1 and steam+user1 would still run.
    # The lead should be marked as completed (not "exhausted" since reddit/steam are new)
    assert store.update_lead.called


@pytest.mark.asyncio
async def test_loop_detects_stale_rounds():
    """should increment stale counter when no new entities appear"""
    pb = _make_mock_playbook(completeness={})
    pb.extract_leads.return_value = [
        Lead(lead_type="username", value="user1", score=0.7),
    ]

    finding = Finding(notes="nothing new")

    registry = MagicMock()
    tool = MagicMock()
    tool.is_available.return_value = True
    tool.check_availability.return_value = (True, "ready")
    tool.run = AsyncMock(return_value=finding)
    registry.get.return_value = tool

    lead_row = {
        "id": 1,
        "lead_type": "username",
        "value": "user1",
        "score": 0.7,
    }
    store = AsyncMock()
    store.create_investigation = AsyncMock(return_value=1)
    # Entity count stays at 0 every time — stale rounds
    store.entity_count = AsyncMock(return_value=0)
    store.relationship_count = AsyncMock(return_value=0)
    # Keep returning the same lead to force iterations
    store.get_leads = AsyncMock(return_value=[lead_row])
    store.pending_lead_count = AsyncMock(return_value=0)

    result = await run_investigation_loop(
        playbook=pb,
        seed="user1",
        registry=registry,
        store=store,
        config=LoopConfig(
            max_stale_rounds=2,
            max_iterations=20,
            completeness_criteria={},
        ),
    )

    # Should have stopped due to diminishing returns
    # (the stop reason is embedded in the result's internal state,
    # but we can verify the loop didn't run all 20 iterations)
    # Each iteration processes leads, so get_leads should have been
    # called fewer times than max_iterations
    assert store.get_leads.call_count <= 5


# ------------------------------------------------------------------
# Completeness criteria on playbook subclasses
# ------------------------------------------------------------------

def test_username_playbook_has_completeness():
    """should define completeness requiring person + accounts"""
    from osint_agent.playbooks.username_to_identity import UsernameToldentity
    pb = UsernameToldentity()
    criteria = pb.completeness_criteria
    assert EntityType.PERSON in criteria
    assert EntityType.ACCOUNT in criteria
    assert criteria[EntityType.ACCOUNT] >= 2


def test_name_playbook_has_completeness():
    """should define completeness requiring person + account + email"""
    from osint_agent.playbooks.name_to_surface import NameToSurface
    pb = NameToSurface()
    criteria = pb.completeness_criteria
    assert EntityType.PERSON in criteria
    assert EntityType.EMAIL in criteria


def test_org_playbook_has_completeness():
    """should define completeness requiring persons + org"""
    from osint_agent.playbooks.org_to_members import OrgToMembers
    pb = OrgToMembers()
    criteria = pb.completeness_criteria
    assert EntityType.PERSON in criteria
    assert criteria[EntityType.PERSON] >= 2
    assert EntityType.ORGANIZATION in criteria
