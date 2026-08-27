"""Deterministic planning tools owned by the Planning and Validation Agent."""

from __future__ import annotations

import os
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from typing import Any, Literal

from google.adk.tools.tool_context import ToolContext
from google.api_core.exceptions import GoogleAPICallError
from google.auth.exceptions import DefaultCredentialsError
from google.maps import routeoptimization_v1 as routeoptimization
from ortools.sat.python import cp_model
from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator


SOLUTION_ID = "gmp_git_agentskills_v1"
LOCAL_SOLVER_TIME_LIMIT_SECONDS = 5.0
SUPPORTED_CONSTRAINTS = {
    "allowed_lateness_minutes",
    "max_tasks_per_resource",
    "max_travel_distance",
    "max_work_minutes",
    "minimum_utilization",
    "prefer_fewer_resources",
    "return_to_start",
    "tight_schedule_margin",
    "weather_risk",
}


class PlanningTimeWindow(BaseModel):
    """An inclusive operational interval."""

    start_at: datetime
    end_at: datetime

    @model_validator(mode="after")
    def validate_order(self) -> PlanningTimeWindow:
        if self.end_at <= self.start_at:
            raise ValueError("end_at must be after start_at")
        return self


class PlanningCoordinate(BaseModel):
    """Coordinates used only when Google Route Optimization is selected."""

    latitude: float = Field(ge=-90, le=90)
    longitude: float = Field(ge=-180, le=180)


class PlanningLocation(BaseModel):
    """A location resolved by the Geospatial Intelligence Agent."""

    location_id: str = Field(min_length=1)
    coordinates: PlanningCoordinate | None = None
    source: dict[str, Any] = Field(default_factory=dict)


class PlanningTask(BaseModel):
    """A domain-neutral work item supplied to the optimizer."""

    task_id: str = Field(min_length=1)
    duration_minutes: int = Field(default=0, ge=0)
    priority: int = Field(default=1, ge=1, le=100)
    mandatory: bool = True
    required_resource_type: str | None = None
    required_capabilities: list[str] = Field(default_factory=list)
    demands: dict[str, float] = Field(default_factory=dict)
    time_windows: list[PlanningTimeWindow] = Field(default_factory=list)
    location_id: str | None = None
    start_location_id: str | None = None
    end_location_id: str | None = None
    allowed_resource_ids: list[str] = Field(default_factory=list)
    forbidden_resource_ids: list[str] = Field(default_factory=list)
    predecessor_task_ids: list[str] = Field(default_factory=list)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("demands")
    @classmethod
    def validate_demands(cls, value: dict[str, float]) -> dict[str, float]:
        if any(amount < 0 for amount in value.values()):
            raise ValueError("task demands cannot be negative")
        return value


class PlanningResource(BaseModel):
    """A person, vehicle, team, machine, or composed operational unit."""

    resource_id: str = Field(min_length=1)
    resource_type: str = Field(min_length=1)
    member_ids: list[str] = Field(default_factory=list)
    available: bool = True
    availability: list[PlanningTimeWindow] = Field(default_factory=list)
    capabilities: list[str] = Field(default_factory=list)
    capacities: dict[str, float] = Field(default_factory=dict)
    start_location_id: str | None = None
    end_location_id: str | None = None
    max_work_minutes: int | None = Field(default=None, gt=0)
    cost_per_hour: float = Field(default=1.0, gt=0)
    source: dict[str, Any] = Field(default_factory=dict)

    @field_validator("capacities")
    @classmethod
    def validate_capacities(cls, value: dict[str, float]) -> dict[str, float]:
        if any(amount < 0 for amount in value.values()):
            raise ValueError("resource capacities cannot be negative")
        return value


class PlanningConstraint(BaseModel):
    """A generic constraint whose meaning is explicit in its parameters."""

    constraint_id: str = Field(min_length=1)
    kind: str = Field(min_length=1)
    severity: Literal["hard", "warning"] = "hard"
    parameters: dict[str, Any] = Field(default_factory=dict)
    description: str | None = None
    source: dict[str, Any] = Field(default_factory=dict)


class TravelMatrixElement(BaseModel):
    """One travel cost between organizational location references."""

    origin_reference_id: str
    destination_reference_id: str
    duration_seconds: float | None = Field(default=None, ge=0)
    distance_meters: float | None = Field(default=None, ge=0)
    condition: str | None = None


class PlanningTravelMatrix(BaseModel):
    """Travel costs returned by the Geospatial Intelligence Agent."""

    elements: list[TravelMatrixElement] = Field(default_factory=list)


class PlanAssignment(BaseModel):
    """A scheduled task/resource decision shared by all three tools."""

    task_id: str
    resource_id: str
    resource_member_ids: list[str] = Field(default_factory=list)
    sequence: int = Field(ge=1)
    start_at: datetime
    end_at: datetime
    origin_location_id: str | None = None
    destination_location_id: str | None = None
    travel_duration_seconds: float = Field(default=0, ge=0)
    travel_distance_meters: float = Field(default=0, ge=0)
    waiting_duration_seconds: float = Field(default=0, ge=0)
    demands: dict[str, float] = Field(default_factory=dict)


class CandidatePlan(BaseModel):
    """Normalized plan exchanged between optimization, metrics, and validation."""

    assignments: list[PlanAssignment] = Field(default_factory=list)
    unassigned_tasks: list[dict[str, Any]] = Field(default_factory=list)
    resource_schedules: list[dict[str, Any]] = Field(default_factory=list)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _provenance(provider: str, product: str) -> dict[str, Any]:
    result = {
        "provider": provider,
        "product": product,
        "retrieved_at": _now(),
    }
    if provider == "google_maps_platform":
        result["attribution"] = SOLUTION_ID
    return result


def _coerce_list(values: list[Any], model: type[BaseModel]) -> list[Any]:
    return [value if isinstance(value, model) else model.model_validate(value) for value in values]


def _coerce_optional(value: Any, model: type[BaseModel]) -> Any:
    if value is None or isinstance(value, model):
        return value
    return model.model_validate(value)


def _validation_error(error: ValidationError) -> dict[str, Any]:
    return {
        "status": "error",
        "error": {
            "code": "INVALID_PLANNING_INPUT",
            "message": "Planning input failed validation.",
            "details": error.errors(include_url=False),
        },
    }


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _task_locations(task: PlanningTask) -> tuple[str | None, str | None]:
    start = task.start_location_id or task.location_id or task.end_location_id
    end = task.end_location_id or task.location_id or task.start_location_id
    return start, end


def _matrix_lookup(
    matrix: PlanningTravelMatrix | None,
) -> dict[tuple[str, str], TravelMatrixElement]:
    if matrix is None:
        return {}
    return {
        (item.origin_reference_id, item.destination_reference_id): item
        for item in matrix.elements
    }


def _travel(
    lookup: dict[tuple[str, str], TravelMatrixElement],
    origin: str | None,
    destination: str | None,
) -> tuple[float, float, bool]:
    if not origin or not destination or origin == destination:
        return 0.0, 0.0, True
    element = lookup.get((origin, destination))
    if element is None or element.duration_seconds is None:
        return 0.0, 0.0, False
    condition = (element.condition or "ROUTE_EXISTS").upper()
    return (
        float(element.duration_seconds),
        float(element.distance_meters or 0),
        condition not in {"ROUTE_NOT_FOUND", "FAILED"},
    )


def _constraint_value(
    constraints: list[PlanningConstraint], kind: str, key: str, default: Any
) -> Any:
    for constraint in constraints:
        if constraint.kind == kind and key in constraint.parameters:
            return constraint.parameters[key]
    return default


def _eligibility_reason(task: PlanningTask, resource: PlanningResource) -> str | None:
    if not resource.available:
        return "resource_unavailable"
    if task.required_resource_type and task.required_resource_type != resource.resource_type:
        return "resource_type_mismatch"
    if not set(task.required_capabilities).issubset(resource.capabilities):
        return "missing_capability"
    if task.allowed_resource_ids and resource.resource_id not in task.allowed_resource_ids:
        return "resource_not_allowed"
    if resource.resource_id in task.forbidden_resource_ids:
        return "resource_forbidden"
    if any(task.demands.get(key, 0) > resource.capacities.get(key, 0) for key in task.demands):
        return "insufficient_capacity"
    if task.time_windows and resource.availability:
        duration = timedelta(minutes=task.duration_minutes)
        if not any(
            max(_as_utc(task_window.start_at), _as_utc(resource_window.start_at)) + duration
            <= min(_as_utc(task_window.end_at), _as_utc(resource_window.end_at))
            for task_window in task.time_windows
            for resource_window in resource.availability
        ):
            return "no_shared_availability"
    return None


def _unsupported_hard_constraints(
    constraints: list[PlanningConstraint],
) -> list[dict[str, Any]]:
    return [
        {
            "code": "UNSUPPORTED_HARD_CONSTRAINT",
            "constraint_id": item.constraint_id,
            "message": f"Hard constraint '{item.kind}' is not supported.",
        }
        for item in constraints
        if item.severity == "hard" and item.kind not in SUPPORTED_CONSTRAINTS
    ]


def _schedule_local_assignments(
    selected: dict[str, str],
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    constraints: list[PlanningConstraint],
    matrix: PlanningTravelMatrix | None,
) -> tuple[list[PlanAssignment], list[dict[str, Any]]]:
    task_by_id = {item.task_id: item for item in tasks}
    resource_by_id = {item.resource_id: item for item in resources}
    lookup = _matrix_lookup(matrix)
    allowed_lateness = float(
        _constraint_value(constraints, "allowed_lateness_minutes", "minutes", 0)
    )
    grouped: dict[str, list[PlanningTask]] = defaultdict(list)
    for task_id, resource_id in selected.items():
        grouped[resource_id].append(task_by_id[task_id])

    assignments: list[PlanAssignment] = []
    unscheduled: list[dict[str, Any]] = []
    for resource_id, assigned_tasks in grouped.items():
        resource = resource_by_id[resource_id]
        assigned_tasks.sort(
            key=lambda item: (
                min((_as_utc(window.end_at) for window in item.time_windows), default=datetime.max.replace(tzinfo=timezone.utc)),
                -item.priority,
                item.task_id,
            )
        )
        availability = sorted(resource.availability, key=lambda item: _as_utc(item.start_at))
        if not availability:
            task_starts = [
                _as_utc(window.start_at)
                for assigned_task in assigned_tasks
                for window in assigned_task.time_windows
            ]
            deterministic_start = min(
                task_starts,
                default=datetime(1970, 1, 1, tzinfo=timezone.utc),
            )
            availability = [
                PlanningTimeWindow(
                    start_at=deterministic_start,
                    end_at=deterministic_start + timedelta(days=3650),
                )
            ]
        window_index = 0
        cursor = _as_utc(availability[0].start_at)
        current_location = resource.start_location_id
        sequence = 1
        for task in assigned_tasks:
            origin, destination = _task_locations(task)
            before_seconds, before_meters, before_found = _travel(
                lookup, current_location, origin
            )
            inside_seconds, inside_meters, inside_found = _travel(
                lookup, origin, destination
            )
            if matrix is not None and not (before_found and inside_found):
                unscheduled.append(
                    {"task_id": task.task_id, "reason": "travel_cost_missing"}
                )
                continue
            travel_seconds = before_seconds + inside_seconds
            travel_meters = before_meters + inside_meters
            duration = timedelta(seconds=travel_seconds) + timedelta(
                minutes=task.duration_minutes
            )
            task_windows = sorted(task.time_windows, key=lambda item: _as_utc(item.start_at))
            scheduled: tuple[datetime, datetime] | None = None
            while window_index < len(availability) and scheduled is None:
                resource_window = availability[window_index]
                resource_start = _as_utc(resource_window.start_at)
                resource_end = _as_utc(resource_window.end_at)
                candidate_windows = task_windows or [resource_window]
                for task_window in candidate_windows:
                    start = max(
                        cursor,
                        resource_start,
                        _as_utc(task_window.start_at),
                    )
                    end = start + duration
                    latest = min(
                        resource_end,
                        _as_utc(task_window.end_at) + timedelta(minutes=allowed_lateness),
                    )
                    if end <= latest:
                        scheduled = (start, end)
                        break
                if scheduled is None:
                    window_index += 1
                    if window_index < len(availability):
                        cursor = _as_utc(availability[window_index].start_at)
            if scheduled is None:
                unscheduled.append(
                    {"task_id": task.task_id, "reason": "time_window_unavailable"}
                )
                continue
            start, end = scheduled
            assignment = PlanAssignment(
                task_id=task.task_id,
                resource_id=resource_id,
                resource_member_ids=resource.member_ids,
                sequence=sequence,
                start_at=start,
                end_at=end,
                origin_location_id=origin,
                destination_location_id=destination,
                travel_duration_seconds=travel_seconds,
                travel_distance_meters=travel_meters,
                demands=task.demands,
            )
            assignments.append(assignment)
            cursor = end
            current_location = destination
            sequence += 1
    return assignments, unscheduled


def _optimize_local(
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    constraints: list[PlanningConstraint],
    matrix: PlanningTravelMatrix | None,
    time_limit_seconds: float,
) -> dict[str, Any]:
    unsupported = _unsupported_hard_constraints(constraints)
    if unsupported:
        return {
            "status": "error",
            "feasible": False,
            "plan": CandidatePlan().model_dump(mode="json"),
            "violations": unsupported,
            "warnings": [],
            "provenance": _provenance("geoagent", "OR-Tools CP-SAT"),
        }
    if not tasks or not resources:
        return {
            "status": "error",
            "feasible": False,
            "plan": CandidatePlan().model_dump(mode="json"),
            "violations": [
                {
                    "code": "PLANNING_INPUT_EMPTY",
                    "message": "At least one task and one resource are required.",
                }
            ],
            "warnings": [],
            "provenance": _provenance("geoagent", "OR-Tools CP-SAT"),
        }

    model = cp_model.CpModel()
    assignment_vars: dict[tuple[int, int], cp_model.IntVar] = {}
    eligibility: dict[str, dict[str, str]] = defaultdict(dict)
    for task_index, task in enumerate(tasks):
        for resource_index, resource in enumerate(resources):
            reason = _eligibility_reason(task, resource)
            if reason:
                eligibility[task.task_id][resource.resource_id] = reason
                continue
            assignment_vars[(task_index, resource_index)] = model.new_bool_var(
                f"assign_{task_index}_{resource_index}"
            )

    for task_index, _task in enumerate(tasks):
        variables = [
            variable
            for (current_task, _), variable in assignment_vars.items()
            if current_task == task_index
        ]
        if variables:
            model.add(sum(variables) <= 1)

    scale = 1000
    for resource_index, resource in enumerate(resources):
        for dimension, capacity in resource.capacities.items():
            terms = []
            for task_index, task in enumerate(tasks):
                variable = assignment_vars.get((task_index, resource_index))
                if variable is not None and task.demands.get(dimension, 0):
                    terms.append(round(task.demands[dimension] * scale) * variable)
            if terms:
                model.add(sum(terms) <= round(capacity * scale))

    max_tasks = _constraint_value(
        constraints, "max_tasks_per_resource", "maximum", None
    )
    if max_tasks is not None:
        for resource_index, _resource in enumerate(resources):
            variables = [
                variable
                for (_, current_resource), variable in assignment_vars.items()
                if current_resource == resource_index
            ]
            if variables:
                model.add(sum(variables) <= int(max_tasks))

    active_variables: list[cp_model.IntVar] = []
    for resource_index, _resource in enumerate(resources):
        assigned = [
            variable
            for (_, current_resource), variable in assignment_vars.items()
            if current_resource == resource_index
        ]
        if assigned:
            active = model.new_bool_var(f"active_{resource_index}")
            for variable in assigned:
                model.add(variable <= active)
            model.add(active <= sum(assigned))
            active_variables.append(active)

    objective_terms = []
    for (task_index, _resource_index), variable in assignment_vars.items():
        task = tasks[task_index]
        weight = task.priority * 100 + (100_000 if task.mandatory else 0)
        objective_terms.append(weight * variable)
    prefer_fewer = any(
        item.kind == "prefer_fewer_resources" and item.parameters.get("enabled", True)
        for item in constraints
    )
    if prefer_fewer:
        objective_terms.extend(-10 * item for item in active_variables)
    model.maximize(sum(objective_terms))

    solver = cp_model.CpSolver()
    solver.parameters.max_time_in_seconds = max(0.1, min(time_limit_seconds, 30.0))
    solver.parameters.num_search_workers = 1
    solver.parameters.random_seed = 0
    status = solver.solve(model)
    if status not in {cp_model.OPTIMAL, cp_model.FEASIBLE}:
        return {
            "status": "error",
            "feasible": False,
            "plan": CandidatePlan().model_dump(mode="json"),
            "violations": [
                {"code": "NO_ASSIGNMENT_SOLUTION", "message": "No assignment solution was found."}
            ],
            "warnings": [],
            "provenance": _provenance("geoagent", "OR-Tools CP-SAT"),
        }

    selected: dict[str, str] = {}
    for (task_index, resource_index), variable in assignment_vars.items():
        if solver.value(variable):
            selected[tasks[task_index].task_id] = resources[resource_index].resource_id
    assignments, unscheduled = _schedule_local_assignments(
        selected, tasks, resources, constraints, matrix
    )
    scheduled_ids = {item.task_id for item in assignments}
    unassigned = list(unscheduled)
    for task in tasks:
        if task.task_id not in scheduled_ids and not any(
            item["task_id"] == task.task_id for item in unassigned
        ):
            reasons = Counter(eligibility.get(task.task_id, {}).values())
            reason = reasons.most_common(1)[0][0] if reasons else "not_selected"
            unassigned.append({"task_id": task.task_id, "reason": reason})
    mandatory_unassigned = [
        item for item in unassigned if next(task for task in tasks if task.task_id == item["task_id"]).mandatory
    ]
    schedules = [
        {
            "resource_id": resource.resource_id,
            "assignment_task_ids": [
                item.task_id
                for item in sorted(assignments, key=lambda value: (value.resource_id, value.sequence))
                if item.resource_id == resource.resource_id
            ],
        }
        for resource in resources
    ]
    plan = CandidatePlan(
        assignments=assignments,
        unassigned_tasks=unassigned,
        resource_schedules=schedules,
    )
    return {
        "status": "infeasible" if mandatory_unassigned else "partial" if unassigned else "success",
        "feasible": not mandatory_unassigned,
        "optimal": status == cp_model.OPTIMAL,
        "solver": "local",
        "plan": plan.model_dump(mode="json"),
        "violations": [
            {
                "code": "MANDATORY_TASK_UNASSIGNED",
                "task_id": item["task_id"],
                "message": "A mandatory task could not be scheduled.",
                "reason": item["reason"],
            }
            for item in mandatory_unassigned
        ],
        "warnings": [],
        "provenance": _provenance("geoagent", "OR-Tools CP-SAT"),
    }


def _route_optimization_compatible(
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    locations: list[PlanningLocation],
    constraints: list[PlanningConstraint],
) -> tuple[bool, str | None]:
    location_map = {item.location_id: item for item in locations}
    if not tasks or not resources:
        return False, "tasks_and_resources_required"
    if any(item.predecessor_task_ids for item in tasks):
        return False, "task_precedence_requires_local_solver"
    if any(item.max_work_minutes is not None for item in resources):
        return False, "max_work_minutes_requires_local_solver"
    unsupported_hard = [
        item.kind
        for item in constraints
        if item.severity == "hard" and item.kind != "return_to_start"
    ]
    if unsupported_hard:
        return False, "hard_constraints_require_local_solver"
    if any(
        item.kind == "return_to_start"
        and item.severity == "hard"
        and any(
            resource.end_location_id not in {None, resource.start_location_id}
            for resource in resources
        )
        for item in constraints
    ):
        return False, "return_to_start_locations_required"
    for task in tasks:
        start, end = _task_locations(task)
        if not start or not end:
            return False, "task_locations_required"
        if not location_map.get(start) or not location_map[start].coordinates:
            return False, "resolved_task_coordinates_required"
        if not location_map.get(end) or not location_map[end].coordinates:
            return False, "resolved_task_coordinates_required"
    for resource in resources:
        if not resource.start_location_id:
            return False, "resource_start_locations_required"
        location = location_map.get(resource.start_location_id)
        if not location or not location.coordinates:
            return False, "resolved_resource_coordinates_required"
    if any(
        not any(_eligibility_reason(task, resource) is None for resource in resources)
        for task in tasks
    ):
        return False, "eligible_resource_required"
    return True, None


def _lat_lng(location: PlanningLocation) -> dict[str, Any]:
    assert location.coordinates is not None
    return {
        "latitude": location.coordinates.latitude,
        "longitude": location.coordinates.longitude,
    }


def _optimize_with_google(
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    locations: list[PlanningLocation],
    time_limit_seconds: float,
    consider_traffic: bool,
) -> dict[str, Any]:
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "geoagent-hackathon").strip()
    if not project_id:
        raise ValueError("GOOGLE_CLOUD_PROJECT is not configured")
    location_map = {item.location_id: item for item in locations}
    all_starts = [
        _as_utc(window.start_at)
        for item in [*tasks, *resources]
        for window in (item.time_windows if isinstance(item, PlanningTask) else item.availability)
    ]
    all_ends = [
        _as_utc(window.end_at)
        for item in [*tasks, *resources]
        for window in (item.time_windows if isinstance(item, PlanningTask) else item.availability)
    ]
    global_start = min(all_starts, default=datetime.now(timezone.utc))
    global_end = max(all_ends, default=global_start + timedelta(days=1))
    vehicle_index = {item.resource_id: index for index, item in enumerate(resources)}

    shipments: list[dict[str, Any]] = []
    for task in tasks:
        start, end = _task_locations(task)
        assert start and end
        visits = {
            "arrival_location": _lat_lng(location_map[start]),
            "duration": timedelta(minutes=task.duration_minutes),
            "label": task.task_id,
            "time_windows": [
                {
                    "start_time": _as_utc(window.start_at),
                    "end_time": _as_utc(window.end_at),
                }
                for window in task.time_windows
            ],
        }
        delivery = {
            **visits,
            "arrival_location": _lat_lng(location_map[end]),
        }
        shipment: dict[str, Any] = {
            "label": task.task_id,
            "deliveries": [delivery],
            "load_demands": {
                key: {"amount": round(value)} for key, value in task.demands.items()
            },
        }
        if start != end:
            shipment["pickups"] = [{**visits, "duration": timedelta(0)}]
        if not task.mandatory:
            shipment["penalty_cost"] = float(task.priority * 1000)
        eligible_vehicle_indices = [
            vehicle_index[resource.resource_id]
            for resource in resources
            if _eligibility_reason(task, resource) is None
        ]
        if len(eligible_vehicle_indices) != len(resources):
            shipment["allowed_vehicle_indices"] = eligible_vehicle_indices
        shipments.append(shipment)

    vehicles: list[dict[str, Any]] = []
    for resource in resources:
        start_location = location_map[resource.start_location_id or ""]
        end_location = location_map.get(resource.end_location_id or "") or start_location
        vehicles.append(
            {
                "label": resource.resource_id,
                "start_location": _lat_lng(start_location),
                "end_location": _lat_lng(end_location),
                "start_time_windows": [
                    {
                        "start_time": _as_utc(window.start_at),
                        "end_time": _as_utc(window.end_at),
                    }
                    for window in resource.availability
                ],
                "end_time_windows": [
                    {
                        "start_time": _as_utc(window.start_at),
                        "end_time": _as_utc(window.end_at),
                    }
                    for window in resource.availability
                ],
                "load_limits": {
                    key: {"max_load": round(value)}
                    for key, value in resource.capacities.items()
                },
                "cost_per_hour": resource.cost_per_hour,
            }
        )

    request = routeoptimization.OptimizeToursRequest(
        parent=f"projects/{project_id}",
        timeout=timedelta(
            seconds=max(1, min(round(time_limit_seconds), 30))
        ),
        model={
            "shipments": shipments,
            "vehicles": vehicles,
            "global_start_time": global_start,
            "global_end_time": global_end,
        },
        consider_road_traffic=consider_traffic,
        populate_polylines=True,
    )
    client = routeoptimization.RouteOptimizationClient()
    response = client.optimize_tours(
        request=request,
        timeout=max(5.0, min(time_limit_seconds + 5, 40.0)),
        metadata=(("x-goog-maps-solution-id", SOLUTION_ID),),
    )

    task_by_id = {item.task_id: item for item in tasks}
    resource_by_id = {item.resource_id: item for item in resources}
    assignments: list[PlanAssignment] = []
    schedules: list[dict[str, Any]] = []
    for route in response.routes:
        resource_id = route.vehicle_label or resources[route.vehicle_index].resource_id
        task_ids: list[str] = []
        for sequence, visit in enumerate(route.visits, start=1):
            if visit.is_pickup:
                continue
            task_id = visit.shipment_label or tasks[visit.shipment_index].task_id
            task = task_by_id[task_id]
            transition = route.transitions[max(0, sequence - 1)] if route.transitions else None
            start_at = _as_utc(visit.start_time)
            origin, destination = _task_locations(task)
            assignments.append(
                PlanAssignment(
                    task_id=task_id,
                    resource_id=resource_id,
                    resource_member_ids=resource_by_id[resource_id].member_ids,
                    sequence=len(task_ids) + 1,
                    start_at=start_at,
                    end_at=start_at + timedelta(minutes=task.duration_minutes),
                    origin_location_id=origin,
                    destination_location_id=destination,
                    travel_duration_seconds=(transition.travel_duration.total_seconds() if transition else 0),
                    travel_distance_meters=(transition.travel_distance_meters if transition else 0),
                    waiting_duration_seconds=(transition.wait_duration.total_seconds() if transition else 0),
                    demands=task.demands,
                )
            )
            task_ids.append(task_id)
        schedules.append(
            {
                "resource_id": resource_id,
                "assignment_task_ids": task_ids,
                "encoded_polyline": getattr(getattr(route, "route_polyline", None), "points", None),
            }
        )
    unassigned = [
        {
            "task_id": item.label or tasks[item.index].task_id,
            "reason": "route_optimization_skipped",
        }
        for item in response.skipped_shipments
    ]
    mandatory_ids = {item.task_id for item in tasks if item.mandatory}
    mandatory_unassigned = [item for item in unassigned if item["task_id"] in mandatory_ids]
    if response.metrics.skipped_mandatory_shipment_count:
        mandatory_unassigned = mandatory_unassigned or [
            {"task_id": "unknown", "reason": "route_optimization_skipped_mandatory"}
        ]
    plan = CandidatePlan(
        assignments=assignments,
        unassigned_tasks=unassigned,
        resource_schedules=schedules,
    )
    return {
        "status": "infeasible" if mandatory_unassigned else "partial" if unassigned else "success",
        "feasible": not mandatory_unassigned,
        "optimal": None,
        "solver": "route_optimization",
        "plan": plan.model_dump(mode="json"),
        "violations": [
            {
                "code": "MANDATORY_TASK_UNASSIGNED",
                "task_id": item["task_id"],
                "message": "Google Route Optimization skipped mandatory work.",
            }
            for item in mandatory_unassigned
        ],
        "warnings": [],
        "cloud_metrics": {
            "used_resource_count": response.metrics.used_vehicle_count,
            "skipped_mandatory_task_count": response.metrics.skipped_mandatory_shipment_count,
            "total_cost": response.metrics.total_cost,
        },
        "provenance": _provenance("google_maps_platform", "Route Optimization API"),
    }


def optimize_assignments(
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    constraints: list[PlanningConstraint],
    tool_context: ToolContext,
    travel_matrix: PlanningTravelMatrix | None = None,
    locations: list[PlanningLocation] | None = None,
    solver: Literal["auto", "local", "route_optimization"] = "auto",
    time_limit_seconds: float = LOCAL_SOLVER_TIME_LIMIT_SECONDS,
    consider_traffic: bool = False,
) -> dict[str, Any]:
    """Create a generic candidate plan using local or Google optimization.

    Args:
        tasks: Domain-neutral work items to schedule.
        resources: Available people, vehicles, teams, machines, or composed units.
        constraints: Explicit hard rules and warnings.
        travel_matrix: Optional travel costs from the Geospatial Agent.
        locations: Resolved coordinates needed only by Route Optimization.
        solver: Automatic, local OR-Tools, or strict Google Route Optimization.
        time_limit_seconds: Bounded solver time from 0.1 to 30 seconds.
        consider_traffic: Whether Google Route Optimization should consider traffic.
    """
    _ = tool_context
    try:
        task_models = _coerce_list(tasks, PlanningTask)
        resource_models = _coerce_list(resources, PlanningResource)
        constraint_models = _coerce_list(constraints, PlanningConstraint)
        location_models = _coerce_list(locations or [], PlanningLocation)
        matrix_model = _coerce_optional(travel_matrix, PlanningTravelMatrix)
    except ValidationError as error:
        return _validation_error(error)
    if len({item.task_id for item in task_models}) != len(task_models):
        return {"status": "error", "error": {"code": "DUPLICATE_TASK_ID", "message": "Task IDs must be unique."}}
    if len({item.resource_id for item in resource_models}) != len(resource_models):
        return {"status": "error", "error": {"code": "DUPLICATE_RESOURCE_ID", "message": "Resource IDs must be unique."}}

    compatible, compatibility_reason = _route_optimization_compatible(
        task_models, resource_models, location_models, constraint_models
    )
    should_use_google = solver == "route_optimization" or (
        solver == "auto" and compatible and any(item.start_location_id != item.end_location_id for item in task_models)
    )
    if solver == "route_optimization" and not compatible:
        return {
            "status": "error",
            "error": {
                "code": "ROUTE_OPTIMIZATION_INCOMPATIBLE",
                "message": "The request is missing vehicle-routing locations or coordinates.",
                "reason": compatibility_reason,
            },
        }
    if should_use_google:
        try:
            return _optimize_with_google(
                task_models,
                resource_models,
                location_models,
                time_limit_seconds,
                consider_traffic,
            )
        except (
            DefaultCredentialsError,
            GoogleAPICallError,
            TypeError,
            ValueError,
        ) as error:
            if solver == "route_optimization":
                return {
                    "status": "error",
                    "error": {
                        "code": "ROUTE_OPTIMIZATION_UNAVAILABLE",
                        "message": "Google Route Optimization could not complete the request.",
                        "retryable": isinstance(error, GoogleAPICallError),
                    },
                    "provenance": _provenance("google_maps_platform", "Route Optimization API"),
                }
            local = _optimize_local(
                task_models,
                resource_models,
                constraint_models,
                matrix_model,
                time_limit_seconds,
            )
            local.setdefault("warnings", []).append(
                {
                    "code": "ROUTE_OPTIMIZATION_FALLBACK",
                    "message": "Google Route Optimization was unavailable; local optimization was used.",
                }
            )
            return local
    return _optimize_local(
        task_models,
        resource_models,
        constraint_models,
        matrix_model,
        time_limit_seconds,
    )


def calculate_plan_metrics(
    plan: CandidatePlan,
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Calculate exact completion, travel, workload, and utilization metrics."""
    _ = tool_context
    try:
        plan_model = _coerce_optional(plan, CandidatePlan)
        task_models = _coerce_list(tasks, PlanningTask)
        resource_models = _coerce_list(resources, PlanningResource)
    except ValidationError as error:
        return _validation_error(error)
    task_by_id = {item.task_id: item for item in task_models}
    resource_by_id = {item.resource_id: item for item in resource_models}
    assigned_ids = {item.task_id for item in plan_model.assignments}
    total_priority = sum(item.priority for item in task_models)
    assigned_priority = sum(task_by_id[item].priority for item in assigned_ids if item in task_by_id)
    per_resource: list[dict[str, Any]] = []
    for resource in resource_models:
        assignments = sorted(
            [item for item in plan_model.assignments if item.resource_id == resource.resource_id],
            key=lambda item: item.sequence,
        )
        scheduled_seconds = sum(
            (item.end_at - item.start_at).total_seconds() for item in assignments
        )
        work_seconds = sum(
            task_by_id[item.task_id].duration_minutes * 60
            for item in assignments
            if item.task_id in task_by_id
        )
        travel_seconds = sum(item.travel_duration_seconds for item in assignments)
        waiting_seconds = sum(item.waiting_duration_seconds for item in assignments)
        available_seconds = sum(
            (_as_utc(item.end_at) - _as_utc(item.start_at)).total_seconds()
            for item in resource.availability
        )
        capacity_utilization = {}
        for dimension, capacity in resource.capacities.items():
            used = sum(item.demands.get(dimension, 0) for item in assignments)
            capacity_utilization[dimension] = {
                "used": used,
                "capacity": capacity,
                "percent": round((used / capacity * 100) if capacity else 0, 2),
            }
        per_resource.append(
            {
                "resource_id": resource.resource_id,
                "assignment_count": len(assignments),
                "travel_distance_meters": sum(item.travel_distance_meters for item in assignments),
                "travel_duration_seconds": travel_seconds,
                "working_duration_seconds": work_seconds,
                "scheduled_duration_seconds": scheduled_seconds,
                "waiting_duration_seconds": waiting_seconds,
                "idle_duration_seconds": max(available_seconds - scheduled_seconds, 0),
                "utilization_percent": round((scheduled_seconds / available_seconds * 100) if available_seconds else 0, 2),
                "capacity_utilization": capacity_utilization,
            }
        )
    counts = [item["assignment_count"] for item in per_resource]
    metrics = {
        "task_count": len(task_models),
        "assigned_task_count": len(assigned_ids),
        "unassigned_task_count": len(task_models) - len(assigned_ids),
        "mandatory_unassigned_count": sum(item.mandatory and item.task_id not in assigned_ids for item in task_models),
        "completion_percent": round((len(assigned_ids) / len(task_models) * 100) if task_models else 0, 2),
        "priority_weighted_completion_percent": round((assigned_priority / total_priority * 100) if total_priority else 0, 2),
        "total_travel_distance_meters": sum(item.travel_distance_meters for item in plan_model.assignments),
        "total_travel_duration_seconds": sum(item.travel_duration_seconds for item in plan_model.assignments),
        "total_waiting_duration_seconds": sum(item.waiting_duration_seconds for item in plan_model.assignments),
        "active_resource_count": sum(bool(item["assignment_count"]) for item in per_resource),
        "unused_resource_count": sum(not item["assignment_count"] for item in per_resource),
        "workload_assignment_spread": (max(counts) - min(counts)) if counts else 0,
        "resources": per_resource,
    }
    warnings = []
    unknown_tasks = sorted(assigned_ids - set(task_by_id))
    unknown_resources = sorted(
        {item.resource_id for item in plan_model.assignments} - set(resource_by_id)
    )
    if unknown_tasks or unknown_resources:
        warnings.append(
            {
                "code": "METRIC_REFERENCE_MISSING",
                "message": "Some plan references were absent from the supplied catalogs.",
                "task_ids": unknown_tasks,
                "resource_ids": unknown_resources,
            }
        )
    return {
        "status": "partial" if warnings else "success",
        "metrics": metrics,
        "warnings": warnings,
        "provenance": _provenance("geoagent", "deterministic metrics"),
    }


def _issue(
    code: str,
    message: str,
    *,
    task_id: str | None = None,
    resource_id: str | None = None,
    constraint_id: str | None = None,
    details: dict[str, Any] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {"code": code, "message": message}
    if task_id:
        result["task_id"] = task_id
    if resource_id:
        result["resource_id"] = resource_id
    if constraint_id:
        result["constraint_id"] = constraint_id
    if details:
        result["details"] = details
    return result


def validate_plan(
    plan: CandidatePlan,
    tasks: list[PlanningTask],
    resources: list[PlanningResource],
    constraints: list[PlanningConstraint],
    tool_context: ToolContext,
    travel_matrix: PlanningTravelMatrix | None = None,
) -> dict[str, Any]:
    """Independently validate a candidate plan against every supplied rule."""
    _ = tool_context
    try:
        plan_model = _coerce_optional(plan, CandidatePlan)
        task_models = _coerce_list(tasks, PlanningTask)
        resource_models = _coerce_list(resources, PlanningResource)
        constraint_models = _coerce_list(constraints, PlanningConstraint)
        matrix_model = _coerce_optional(travel_matrix, PlanningTravelMatrix)
    except ValidationError as error:
        return _validation_error(error)
    task_by_id = {item.task_id: item for item in task_models}
    resource_by_id = {item.resource_id: item for item in resource_models}
    hard: list[dict[str, Any]] = _unsupported_hard_constraints(constraint_models)
    warnings: list[dict[str, Any]] = []
    assignment_counts = Counter(item.task_id for item in plan_model.assignments)
    for task_id, count in assignment_counts.items():
        if count > 1:
            hard.append(_issue("DUPLICATE_TASK_ASSIGNMENT", "A task appears more than once.", task_id=task_id))
    assigned_ids = set(assignment_counts)
    for task in task_models:
        if task.mandatory and task.task_id not in assigned_ids:
            hard.append(_issue("MANDATORY_TASK_UNASSIGNED", "A mandatory task is unassigned.", task_id=task.task_id))

    grouped: dict[str, list[PlanAssignment]] = defaultdict(list)
    for assignment in plan_model.assignments:
        task = task_by_id.get(assignment.task_id)
        resource = resource_by_id.get(assignment.resource_id)
        if task is None:
            hard.append(_issue("UNKNOWN_TASK", "The plan references an unknown task.", task_id=assignment.task_id))
            continue
        if resource is None:
            hard.append(_issue("UNKNOWN_RESOURCE", "The plan references an unknown resource.", resource_id=assignment.resource_id))
            continue
        grouped[resource.resource_id].append(assignment)
        reason = _eligibility_reason(task, resource)
        if reason:
            hard.append(
                _issue(
                    reason.upper(),
                    "The assigned resource does not satisfy the task requirements.",
                    task_id=task.task_id,
                    resource_id=resource.resource_id,
                )
            )
        if resource.availability and not any(
            assignment.start_at >= _as_utc(window.start_at)
            and assignment.end_at <= _as_utc(window.end_at)
            for window in resource.availability
        ):
            hard.append(_issue("OUTSIDE_RESOURCE_AVAILABILITY", "The assignment is outside resource availability.", task_id=task.task_id, resource_id=resource.resource_id))
        allowed_lateness = float(_constraint_value(constraint_models, "allowed_lateness_minutes", "minutes", 0))
        if task.time_windows and not any(
            assignment.start_at >= _as_utc(window.start_at)
            and assignment.end_at <= _as_utc(window.end_at) + timedelta(minutes=allowed_lateness)
            for window in task.time_windows
        ):
            hard.append(_issue("TASK_TIME_WINDOW_VIOLATION", "The assignment violates its task time window.", task_id=task.task_id, resource_id=resource.resource_id))

    lookup = _matrix_lookup(matrix_model)
    for resource_id, assignments in grouped.items():
        assignments.sort(key=lambda item: (item.start_at, item.sequence))
        resource = resource_by_id[resource_id]
        capacity_used: dict[str, float] = defaultdict(float)
        for assignment in assignments:
            for dimension, amount in assignment.demands.items():
                capacity_used[dimension] += amount
        for dimension, amount in capacity_used.items():
            if amount > resource.capacities.get(dimension, 0):
                hard.append(_issue("CAPACITY_EXCEEDED", "Assigned demand exceeds resource capacity.", resource_id=resource_id, details={"dimension": dimension, "used": amount, "capacity": resource.capacities.get(dimension, 0)}))
        for previous, current in zip(assignments, assignments[1:]):
            if current.start_at < previous.end_at:
                hard.append(_issue("RESOURCE_OVERLAP", "Resource assignments overlap.", task_id=current.task_id, resource_id=resource_id))
            duration, _distance, found = _travel(
                lookup,
                previous.destination_location_id,
                current.origin_location_id,
            )
            if matrix_model is not None and not found:
                hard.append(_issue("TRAVEL_ROUTE_MISSING", "No valid travel route connects consecutive tasks.", task_id=current.task_id, resource_id=resource_id))
            elif current.start_at < previous.end_at + timedelta(seconds=duration):
                hard.append(_issue("TRAVEL_TIME_IMPOSSIBLE", "There is not enough time to travel between consecutive tasks.", task_id=current.task_id, resource_id=resource_id))
        max_work = resource.max_work_minutes
        rule_max = _constraint_value(constraint_models, "max_work_minutes", "minutes", None)
        effective_max = min([item for item in (max_work, rule_max) if item is not None], default=None)
        work_minutes = sum((item.end_at - item.start_at).total_seconds() / 60 for item in assignments)
        if effective_max is not None and work_minutes > float(effective_max):
            hard.append(_issue("MAX_WORK_EXCEEDED", "Resource work exceeds the allowed maximum.", resource_id=resource_id, details={"work_minutes": work_minutes, "maximum": effective_max}))

    assignment_by_task = {item.task_id: item for item in plan_model.assignments}
    for task in task_models:
        current = assignment_by_task.get(task.task_id)
        if current is None:
            continue
        for predecessor_id in task.predecessor_task_ids:
            predecessor = assignment_by_task.get(predecessor_id)
            if predecessor is None or predecessor.end_at > current.start_at:
                hard.append(_issue("PRECEDENCE_VIOLATION", "A predecessor task is missing or finishes too late.", task_id=task.task_id, details={"predecessor_task_id": predecessor_id}))

    for constraint in constraint_models:
        target = hard if constraint.severity == "hard" else warnings
        if constraint.kind == "weather_risk" and constraint.parameters.get("active", False):
            target.append(_issue("WEATHER_RISK", constraint.description or "Weather may affect the plan.", constraint_id=constraint.constraint_id, details=constraint.parameters))
        elif constraint.kind == "max_travel_distance":
            maximum = float(constraint.parameters.get("meters", 0))
            distance = sum(item.travel_distance_meters for item in plan_model.assignments)
            if maximum and distance > maximum:
                target.append(_issue("MAX_TRAVEL_EXCEEDED", "Plan travel exceeds the configured limit.", constraint_id=constraint.constraint_id, details={"distance_meters": distance, "maximum_meters": maximum}))
        elif constraint.kind == "minimum_utilization":
            minimum = float(constraint.parameters.get("percent", 0))
            for resource_id, assignments in grouped.items():
                resource = resource_by_id[resource_id]
                for dimension, capacity in resource.capacities.items():
                    used = sum(item.demands.get(dimension, 0) for item in assignments)
                    percent = (used / capacity * 100) if capacity else 0
                    if percent < minimum:
                        target.append(_issue("LOW_UTILIZATION", "Resource utilization is below the target.", resource_id=resource_id, constraint_id=constraint.constraint_id, details={"dimension": dimension, "percent": round(percent, 2), "minimum_percent": minimum}))
        elif constraint.kind == "tight_schedule_margin":
            threshold = float(constraint.parameters.get("minutes", 15))
            for task in task_models:
                assignment = assignment_by_task.get(task.task_id)
                if assignment and task.time_windows:
                    margins = [
                        (_as_utc(window.end_at) - assignment.end_at).total_seconds()
                        / 60
                        for window in task.time_windows
                        if assignment.end_at <= _as_utc(window.end_at)
                    ]
                    if margins and min(margins) < threshold:
                        margin = min(margins)
                        target.append(_issue("TIGHT_SCHEDULE_MARGIN", "The assignment has little time-window margin.", task_id=task.task_id, constraint_id=constraint.constraint_id, details={"margin_minutes": round(margin, 2)}))

    return {
        "status": "invalid" if hard else "valid_with_warnings" if warnings else "valid",
        "feasible": not hard,
        "hard_violations": hard,
        "warnings": warnings,
        "provenance": _provenance("geoagent", "deterministic validation"),
    }


__all__ = [
    "CandidatePlan",
    "PlanAssignment",
    "PlanningConstraint",
    "PlanningCoordinate",
    "PlanningLocation",
    "PlanningResource",
    "PlanningTask",
    "PlanningTimeWindow",
    "PlanningTravelMatrix",
    "TravelMatrixElement",
    "calculate_plan_metrics",
    "optimize_assignments",
    "validate_plan",
]
