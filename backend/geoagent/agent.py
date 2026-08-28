"""Defines the Mission Manager and its three specialist agents."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from google.adk import Agent
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
from .mission_manager_tools import request_objective_decision
from .model_fallback import FallbackGemini
from .planning_tools import calculate_plan_metrics
from .planning_tools import optimize_assignments
from .planning_tools import validate_plan


# Load GOOGLE_API_KEY from backend/.env.
load_dotenv(Path(__file__).resolve().parents[1] / ".env")

# Ordered native-Gemini model chain used by all four agents. Every configured
# model satisfies the hackathon's Gemini 3.5+ requirement.
PRIMARY_MODEL = os.getenv("GEOAGENT_PRIMARY_MODEL", "gemini-3.7-flash")
FALLBACK_MODELS = tuple(
    model.strip()
    for model in os.getenv(
        "GEOAGENT_FALLBACK_MODELS", "gemini-3.6-flash,gemini-3.5-flash"
    ).split(",")
    if model.strip()
)
MODEL = FallbackGemini(model=PRIMARY_MODEL, fallback_models=FALLBACK_MODELS)


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
    hard_violations: list[dict[str, Any]] = Field(default_factory=list)
    warnings: list[dict[str, Any]] = Field(default_factory=list)
    feasible: bool = False
    unresolved: list[dict[str, Any]] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)
    proposed_objective: str | None = None
    provenance: list[dict[str, Any]] = Field(default_factory=list)


class MissionManagerResult(BaseModel):
    """[Mission Manager output_schema] Structured JSON returned to the backend."""

    status: Literal[
        "awaiting_input", "awaiting_objective_decision", "completed", "failed"
    ]
    mission_name: str | None = None
    summary: str
    question: str | None = None
    proposed_objective: str | None = None
    plan: dict[str, Any] | None = None


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
operational plans. Translate findings into the typed, domain-neutral task,
resource, constraint, location, and matrix arguments required by your tools.
Use local optimization for generic work. Let optimize_assignments select Google
Route Optimization only for a compatible vehicle-routing problem. Always
calculate metrics and independently validate the best candidate.

Try only a bounded set of supported alternatives. If no feasible candidate
exists, stop and return exact hard violations, grounded recommendations, and
one achievable proposed objective. Never retry indefinitely, invent missing
facts, publish an outcome, ask the user questions, or speak directly to the
user. Return valid structured JSON matching your output schema.
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

Always call load_mission_state first. Before delegating to any specialist,
decide whether the objective is actionable. A short objective such as "Plan
tomorrow's deliveries" is actionable. If the objective is genuinely vague,
call request_clarification exactly once with one concise open-ended objective
question, return status awaiting_input, and stop before delegation. Never ask a
question while a specialist is working. After the answer resumes this same
session, combine it with the original objective and proceed without repeating
the initial clarification gate.

For an actionable objective, investigate organizational data, obtain only the
relevant geospatial context, and delegate planning and validation. Publish only
a feasible, independently validated plan using publish_plan. If bounded
alternatives prove the objective impossible, call request_objective_decision
once with the exact reason, hard violations, and one achievable proposed
objective, return status awaiting_objective_decision, and stop. Do not retry,
publish an invalid plan, or treat infeasibility as clarification. Never create
another Mission.
""".strip(),
    # Structured JSON tells the backend whether to wait, finish, or report failure.
    output_schema=MissionManagerResult,
    tools=[
        load_mission_state,
        request_clarification,
        request_objective_decision,
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
