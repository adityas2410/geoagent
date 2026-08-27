"""Defines the Mission Manager and its three specialist agents."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from google.adk import Agent
from google.adk.tools.tool_context import ToolContext
from pydantic import BaseModel, Field

from .data_sources.organizational_data_tools import inspect_source_schema
from .data_sources.organizational_data_tools import list_authorized_sources
from .data_sources.organizational_data_tools import query_source
from .geospatial_tools import GeospatialJourney
from .geospatial_tools import LocationReference
from .geospatial_tools import PlanningWindow
from .geospatial_tools import compute_route_matrix
from .geospatial_tools import compute_routes
from .geospatial_tools import geocode_locations
from .geospatial_tools import get_weather_context
from .geospatial_tools import inspect_roads
from .geospatial_tools import search_places
from .mission_manager_tools import load_mission_state
from .mission_manager_tools import publish_plan
from .mission_manager_tools import request_clarification


# Load GOOGLE_API_KEY from backend/.env.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Gemini model used by all four agents.
MODEL = "gemini-3.5-flash"


# JSON schemas used when the Mission Manager calls each agent.


class OrganizationalDataRequest(BaseModel):
    """[Organizational Data Agent input_schema] JSON sent by the manager."""

    objective: str
    questions: list[str] = Field(default_factory=list)


class OrganizationalDataFindings(BaseModel):
    """[Organizational Data Agent output_schema] Structured JSON returned."""

    sources_inspected: list[str] = Field(default_factory=list)
    facts: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)
    locations: list[dict[str, Any]] = Field(default_factory=list)
    unresolved: list[str] = Field(default_factory=list)


class GeospatialRequest(BaseModel):
    """[Geospatial Intelligence Agent input_schema] JSON sent by the manager."""

    objective: str
    locations: list[LocationReference] = Field(default_factory=list)
    journeys: list[GeospatialJourney] = Field(default_factory=list)
    planning_window: PlanningWindow | None = None
    questions: list[str] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)


class GeospatialIssue(BaseModel):
    """A warning or unresolved geospatial fact returned to the manager."""

    code: str
    message: str
    input_ref: str | None = None
    details: dict[str, Any] = Field(default_factory=dict)


class GeospatialFindings(BaseModel):
    """[Geospatial Intelligence Agent output_schema] Structured JSON returned."""

    resolved_locations: list[dict[str, Any]] = Field(default_factory=list)
    places: list[dict[str, Any]] = Field(default_factory=list)
    routes: list[dict[str, Any]] = Field(default_factory=list)
    travel_matrices: list[dict[str, Any]] = Field(default_factory=list)
    weather_context: list[dict[str, Any]] = Field(default_factory=list)
    road_context: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[GeospatialIssue] = Field(default_factory=list)
    unresolved: list[GeospatialIssue] = Field(default_factory=list)
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class PlanningRequest(BaseModel):
    """[Planning and Validation Agent input_schema] JSON sent by the manager."""

    objective: str
    organizational_facts: list[dict[str, Any]] = Field(default_factory=list)
    geospatial_facts: list[dict[str, Any]] = Field(default_factory=list)
    constraints: list[dict[str, Any]] = Field(default_factory=list)


class PlanningFindings(BaseModel):
    """[Planning and Validation Agent output_schema] Structured JSON returned."""

    candidate_plan: dict[str, Any] | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)
    violations: list[dict[str, Any]] = Field(default_factory=list)
    feasible: bool = False
    unresolved: list[str] = Field(default_factory=list)


class MissionManagerResult(BaseModel):
    """[Mission Manager output_schema] Structured JSON returned to the backend."""

    status: Literal["awaiting_input", "completed", "failed"]
    mission_name: str | None = None
    summary: str
    question: str | None = None
    plan: dict[str, Any] | None = None


# Operational Planning and Validation Agent tools
# These use normal code for optimization, calculations, and validation.


def optimize_assignments(
    tasks: list[dict[str, Any]],
    resources: list[dict[str, Any]],
    constraints: list[dict[str, Any]],
    travel_matrix: dict[str, Any] | None,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Generate candidate assignments using deterministic optimization.

    Args:
        tasks: Work items discovered from organizational data.
        resources: Available resources discovered from organizational data.
        constraints: Operational constraints to enforce.
        travel_matrix: Optional geospatial cost matrix.
    """
    raise NotImplementedError("Deterministic assignment optimization is not implemented yet.")


def calculate_plan_metrics(
    plan: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Calculate deterministic performance and utilization metrics for a plan.

    Args:
        plan: Candidate operational plan to measure.
    """
    raise NotImplementedError("Plan metric calculation is not implemented yet.")


def validate_plan(
    plan: dict[str, Any],
    constraints: list[dict[str, Any]],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Return hard violations, warnings, and feasibility for a candidate plan.

    Args:
        plan: Candidate operational plan to validate.
        constraints: Discovered organizational and geospatial constraints.
    """
    raise NotImplementedError("Deterministic plan validation is not implemented yet.")


# Agents
# single_turn means an agent returns its JSON result to the Mission Manager and
# cannot chat with the user. ADK automatically lets the manager call each agent.


organizational_data_agent = Agent(
    name="organizational_data_agent",
    model=MODEL,
    mode="single_turn",
    description=(
        "Discovers and investigates organizational data authorized for the "
        "current Mission without assuming a domain or database schema."
    ),
    instruction="""
You are the Organizational Data Agent for one isolated Mission.
Investigate only the data sources authorized for this Mission. Discover schemas
before querying and use constrained read-only queries. Extract facts,
constraints, resources, work items, priorities, deadlines, availability, and
locations that are relevant to the delegated objective. Never assume logistics
tables, column names, industries, or Kerala-specific facts. Never ask the user
questions. Return structured findings and explicitly list unresolved facts for
the Mission Manager.
""".strip(),
    # JSON received from the manager / structured JSON returned to the manager.
    input_schema=OrganizationalDataRequest,
    output_schema=OrganizationalDataFindings,
    tools=[
        list_authorized_sources,
        inspect_source_schema,
        query_source,
    ],
)


geospatial_intelligence_agent = Agent(
    name="geospatial_intelligence_agent",
    model=MODEL,
    mode="single_turn",
    description=(
        "Resolves locations, routes, travel matrices, and relevant "
        "physical-world context for the current Mission."
    ),
    instruction="""
You are the Geospatial Intelligence Agent for one isolated Mission. Call only
the tools relevant to the delegated objective; do not call every tool by
default. Use geocoding for unresolved organizational locations, Places API
(New) for place discovery or verification, Routes for selected journeys, and a
route matrix for candidate travel costs. Fetch Weather API context for
time-bound physical operations even when it is informational, but treat weather
as a planning constraint only when organizational rules or the objective make
it operationally relevant. Use Roads API only for GPS correction, nearest-road,
road-access, or speed-limit questions; ordinary route distance does not require
Roads API.

Never guess coordinates, places, routes, distances, travel times, weather, or
road facts. Preserve organizational reference IDs and the provenance returned
by every tool. Partial tool failures must become structured warnings or
unresolved items, not invented replacements. Never ask the user questions or
publish a plan. Return valid structured JSON matching your output schema.
""".strip(),
    input_schema=GeospatialRequest,
    output_schema=GeospatialFindings,
    tools=[
        geocode_locations,
        search_places,
        compute_routes,
        compute_route_matrix,
        get_weather_context,
        inspect_roads,
    ],
)


planning_validation_agent = Agent(
    name="planning_validation_agent",
    model=MODEL,
    mode="single_turn",
    description=(
        "Builds feasible operational candidates and validates them with "
        "deterministic calculation and optimization tools."
    ),
    instruction="""
You are the Operational Planning and Validation Agent for one isolated Mission.
Use the supplied organizational and geospatial findings to construct candidate
operational plans. Use deterministic tools for assignment optimization,
calculation, and constraint validation. Do not invent missing facts, publish a
final plan, or ask the user questions. Return the best supported candidate,
metrics, violations, feasibility, and unresolved requirements to the Mission
Manager.
""".strip(),
    input_schema=PlanningRequest,
    output_schema=PlanningFindings,
    tools=[
        optimize_assignments,
        calculate_plan_metrics,
        validate_plan,
    ],
)


root_agent = Agent(
    # The Mission Manager is the parent agent and the only user-facing agent.
    name="mission_manager",
    model=MODEL,
    description=(
        "Owns one Mission objective, coordinates capability-based specialists, "
        "and publishes one validated operational plan."
    ),
    instruction="""
You are the Mission Manager for exactly one isolated Mission. Own its objective,
lifecycle, specialist delegation, and final operational plan.

Investigate before deciding. Delegate organizational-data investigation,
geospatial investigation, and planning/validation to the appropriate specialist
agents. Resolve conflicts between their structured findings and delegate focused
follow-up work when needed. Do not expose hidden reasoning.

Only request user clarification after permitted organizational data, existing
Mission state, geospatial context, and deterministic validation cannot resolve
an essential fact. When clarification is unavoidable, call
request_clarification exactly once with one concise open-ended question and
return status awaiting_input. Otherwise publish only a feasible, validated plan
using publish_plan with a generated Mission name, concise summary, and complete
plan, then return status completed. Never create another Mission.
""".strip(),
    # Structured JSON tells the backend whether to wait, finish, or report failure.
    output_schema=MissionManagerResult,
    tools=[
        load_mission_state,
        request_clarification,
        publish_plan,
    ],
    # ADK automatically gives the manager a tool for each agent in this list.
    sub_agents=[
        organizational_data_agent,
        geospatial_intelligence_agent,
        planning_validation_agent,
    ],
)


__all__ = [
    "geospatial_intelligence_agent",
    "organizational_data_agent",
    "planning_validation_agent",
    "root_agent",
]
