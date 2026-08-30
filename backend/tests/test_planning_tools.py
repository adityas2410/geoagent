from __future__ import annotations

import json
import os
import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))

from google.adk.tools.function_tool import FunctionTool  # noqa: E402
from google.auth.exceptions import DefaultCredentialsError  # noqa: E402

from geoagent.agent import PlanningFindings, PlanningRequest  # noqa: E402
from geoagent.planning_tools import calculate_plan_metrics  # noqa: E402
from geoagent.planning_tools import compose_resources  # noqa: E402
from geoagent.planning_tools import normalize_operational_rules  # noqa: E402
from geoagent.planning_tools import optimize_assignments  # noqa: E402
from geoagent.planning_tools import validate_plan  # noqa: E402


UTC = timezone.utc
START = datetime(2026, 8, 28, 8, 0, tzinfo=UTC)
END = datetime(2026, 8, 28, 12, 0, tzinfo=UTC)
CONTEXT = SimpleNamespace()


def window(start: datetime = START, end: datetime = END) -> dict:
    return {"start_at": start.isoformat(), "end_at": end.isoformat()}


def resource(**changes) -> dict:
    value = {
        "resource_id": "resource-1",
        "resource_type": "team",
        "available": True,
        "availability": [window()],
        "capabilities": ["cold", "lift"],
        "capacities": {"units": 5},
        "start_location_id": "depot",
    }
    value.update(changes)
    return value


def task(task_id: str, location_id: str, **changes) -> dict:
    value = {
        "task_id": task_id,
        "duration_minutes": 30,
        "priority": 5,
        "mandatory": True,
        "required_resource_type": "team",
        "required_capabilities": ["cold"],
        "demands": {"units": 2},
        "time_windows": [window()],
        "location_id": location_id,
    }
    value.update(changes)
    return value


def matrix() -> dict:
    return {
        "elements": [
            {
                "origin_reference_id": "depot",
                "destination_reference_id": "a",
                "duration_seconds": 600,
                "distance_meters": 5000,
                "condition": "ROUTE_EXISTS",
            },
            {
                "origin_reference_id": "a",
                "destination_reference_id": "b",
                "duration_seconds": 900,
                "distance_meters": 8000,
                "condition": "ROUTE_EXISTS",
            },
        ]
    }


class PlanningToolsTest(unittest.TestCase):
    def test_invalid_planning_input_returns_json_safe_tool_error(self) -> None:
        result = optimize_assignments(
            tasks=[task("task-1", "a", time_windows=[window(start=END, end=START)])],
            resources=[resource()],
            constraints=[],
            solver="local",
            tool_context=CONTEXT,
        )

        self.assertEqual(result["status"], "error")
        self.assertEqual(result["error"]["code"], "INVALID_PLANNING_INPUT")
        self.assertEqual(result["error"]["details"][0]["type"], "value_error")
        json.dumps(result)

    def test_local_optimizer_is_deterministic_and_uses_travel_costs(self) -> None:
        arguments = {
            "tasks": [task("task-1", "a"), task("task-2", "b")],
            "resources": [resource()],
            "constraints": [],
            "travel_matrix": matrix(),
            "solver": "local",
            "tool_context": CONTEXT,
        }
        first = optimize_assignments(**arguments)
        second = optimize_assignments(**arguments)

        self.assertEqual(first["status"], "success")
        self.assertTrue(first["feasible"])
        self.assertEqual(first["plan"], second["plan"])
        assignments = first["plan"]["assignments"]
        self.assertEqual([item["task_id"] for item in assignments], ["task-1", "task-2"])
        self.assertEqual(assignments[0]["travel_duration_seconds"], 600)
        self.assertEqual(assignments[1]["travel_distance_meters"], 8000)

    def test_capacity_capability_and_availability_make_mandatory_work_infeasible(self) -> None:
        result = optimize_assignments(
            tasks=[task("task-1", "a"), task("task-2", "a")],
            resources=[
                resource(capacities={"units": 2}),
                resource(
                    resource_id="resource-2",
                    available=False,
                    capacities={"units": 20},
                ),
                resource(
                    resource_id="resource-3",
                    capabilities=["lift"],
                    capacities={"units": 20},
                ),
            ],
            constraints=[],
            solver="local",
            tool_context=CONTEXT,
        )

        self.assertEqual(result["status"], "infeasible")
        self.assertFalse(result["feasible"])
        self.assertEqual(len(result["plan"]["assignments"]), 1)
        self.assertEqual(result["violations"][0]["code"], "MANDATORY_TASK_UNASSIGNED")

    def test_normalizes_demo_hard_rules_without_unsupported_constraint_names(self) -> None:
        rules = [
            {"rule_id": "return", "rule_code": "RETURN_TO_HOME_FACILITY", "boolean_value": True},
            {"rule_id": "break-after", "rule_code": "DRIVER_BREAK_AFTER_MINUTES", "numeric_value": 240},
            {"rule_id": "break-duration", "rule_code": "DRIVER_BREAK_DURATION", "numeric_value": 30},
            {"rule_id": "loading", "rule_code": "MAX_LOADING_CONCURRENCY", "scope_type": "facility", "scope_id": "facility-1", "numeric_value": 2},
            {"rule_id": "cold", "rule_code": "COLD_CHAIN_CERTIFICATION_REQUIRED", "boolean_value": True},
            {"rule_id": "grace", "rule_code": "DELIVERY_WINDOW_GRACE", "numeric_value": 0},
        ]
        result = normalize_operational_rules(
            rules=rules,
            facility_scopes=[{"scope_id": "facility-1", "location_id": "depot", "service_minutes": 20}],
            tool_context=CONTEXT,
        )

        self.assertEqual(result["status"], "success")
        self.assertEqual(
            {item["kind"] for item in result["constraints"]},
            {"return_to_start", "driver_break", "facility_concurrency", "cold_chain_certification", "allowed_lateness_minutes"},
        )

    def test_composes_asset_and_operator_into_one_feasible_resource(self) -> None:
        result = compose_resources(
            assets=[resource(resource_id="vehicle", capabilities=["refrigerated"], capacities={"units": 5})],
            operators=[resource(resource_id="driver", resource_type="operator", capabilities=["cold_chain_certified"], capacities={}, max_work_minutes=240)],
            tool_context=CONTEXT,
        )

        self.assertEqual(result["status"], "success")
        paired = result["resources"][0]
        self.assertEqual(paired["member_ids"], ["vehicle", "driver"])
        self.assertIn("cold_chain_certified", paired["capabilities"])
        self.assertEqual(paired["max_work_minutes"], 240)

    def test_validator_enforces_cold_chain_break_concurrency_and_return_rules(self) -> None:
        extended_end = START + timedelta(hours=10)
        first_end = START + timedelta(hours=3)
        second_end = first_end + timedelta(hours=3)
        resources = [
            resource(resource_id="resource-1", availability=[window(START, extended_end)], end_location_id="depot"),
            resource(resource_id="resource-2", availability=[window(START, extended_end)], end_location_id="depot"),
        ]
        tasks = [
            task("task-1", "a", source={"requires_refrigeration": True}),
            task("task-2", "b"),
            task("task-3", "a"),
        ]
        plan = {
            "assignments": [
                {"task_id": "task-1", "resource_id": "resource-1", "sequence": 1, "start_at": START, "end_at": first_end, "origin_location_id": "depot", "destination_location_id": "a"},
                {"task_id": "task-2", "resource_id": "resource-1", "sequence": 2, "start_at": first_end, "end_at": second_end, "origin_location_id": "a", "destination_location_id": "b"},
                {"task_id": "task-3", "resource_id": "resource-2", "sequence": 1, "start_at": START, "end_at": START + timedelta(hours=1), "origin_location_id": "depot", "destination_location_id": "a"},
            ]
        }
        constraints = [
            {"constraint_id": "cold", "kind": "cold_chain_certification", "parameters": {"capability": "cold_chain_certified"}},
            {"constraint_id": "break", "kind": "driver_break", "parameters": {"after_minutes": 240, "duration_minutes": 30}},
            {"constraint_id": "facility", "kind": "facility_concurrency", "parameters": {"location_id": "depot", "maximum": 1, "duration_minutes": 20}},
            {"constraint_id": "return", "kind": "return_to_start", "parameters": {"enabled": True}},
        ]
        travel_matrix = {
            "elements": [
                {"origin_reference_id": "a", "destination_reference_id": "b", "duration_seconds": 0, "distance_meters": 0},
                {"origin_reference_id": "a", "destination_reference_id": "depot", "duration_seconds": 0, "distance_meters": 0},
            ]
        }
        result = validate_plan(plan, tasks, resources, constraints, CONTEXT, travel_matrix)
        codes = {item["code"] for item in result["hard_violations"]}

        self.assertIn("COLD_CHAIN_CERTIFICATION_REQUIRED", codes)
        self.assertIn("DRIVER_BREAK_REQUIRED", codes)
        self.assertIn("FACILITY_LOADING_CONCURRENCY_EXCEEDED", codes)
        self.assertIn("RETURN_TO_HOME_VIOLATION", codes)

    def test_metrics_are_exact_and_keep_work_separate_from_travel(self) -> None:
        plan = {
            "assignments": [
                {
                    "task_id": "task-1",
                    "resource_id": "resource-1",
                    "sequence": 1,
                    "start_at": START,
                    "end_at": START + timedelta(minutes=40),
                    "travel_duration_seconds": 600,
                    "travel_distance_meters": 5000,
                    "demands": {"units": 2},
                }
            ]
        }
        result = calculate_plan_metrics(
            plan=plan,
            tasks=[task("task-1", "a"), task("task-2", "b", mandatory=False)],
            resources=[resource()],
            tool_context=CONTEXT,
        )

        self.assertEqual(result["status"], "success")
        metrics = result["metrics"]
        self.assertEqual(metrics["completion_percent"], 50)
        self.assertEqual(metrics["total_travel_distance_meters"], 5000)
        detail = metrics["resources"][0]
        self.assertEqual(detail["working_duration_seconds"], 1800)
        self.assertEqual(detail["scheduled_duration_seconds"], 2400)
        self.assertEqual(detail["capacity_utilization"]["units"]["percent"], 40)

    def test_validation_returns_hard_violations_and_warnings(self) -> None:
        late_start = END + timedelta(minutes=10)
        plan = {
            "assignments": [
                {
                    "task_id": "task-1",
                    "resource_id": "resource-1",
                    "sequence": 1,
                    "start_at": late_start,
                    "end_at": late_start + timedelta(minutes=30),
                    "demands": {"units": 7},
                }
            ]
        }
        result = validate_plan(
            plan=plan,
            tasks=[task("task-1", "a"), task("task-2", "b")],
            resources=[resource()],
            constraints=[
                {
                    "constraint_id": "weather",
                    "kind": "weather_risk",
                    "severity": "warning",
                    "parameters": {"active": True},
                },
                {
                    "constraint_id": "unknown",
                    "kind": "unimplemented_rule",
                    "severity": "hard",
                },
            ],
            tool_context=CONTEXT,
        )

        codes = {item["code"] for item in result["hard_violations"]}
        self.assertFalse(result["feasible"])
        self.assertIn("MANDATORY_TASK_UNASSIGNED", codes)
        self.assertIn("OUTSIDE_RESOURCE_AVAILABILITY", codes)
        self.assertIn("TASK_TIME_WINDOW_VIOLATION", codes)
        self.assertIn("CAPACITY_EXCEEDED", codes)
        self.assertIn("UNSUPPORTED_HARD_CONSTRAINT", codes)
        self.assertEqual(result["warnings"][0]["code"], "WEATHER_RISK")

    def test_route_optimization_request_and_normalized_response(self) -> None:
        response = SimpleNamespace(
            routes=[
                SimpleNamespace(
                    vehicle_label="resource-1",
                    vehicle_index=0,
                    visits=[
                        SimpleNamespace(
                            is_pickup=False,
                            shipment_label="task-1",
                            shipment_index=0,
                            start_time=START + timedelta(minutes=10),
                        )
                    ],
                    transitions=[
                        SimpleNamespace(
                            travel_duration=timedelta(minutes=10),
                            travel_distance_meters=5000,
                            wait_duration=timedelta(0),
                        )
                    ],
                    route_polyline=SimpleNamespace(points="encoded"),
                )
            ],
            skipped_shipments=[],
            metrics=SimpleNamespace(
                used_vehicle_count=1,
                skipped_mandatory_shipment_count=0,
                total_cost=12.5,
            ),
        )
        fake_client = SimpleNamespace()
        fake_client.optimize_tours = unittest.mock.Mock(return_value=response)
        locations = [
            {"location_id": "depot", "coordinates": {"latitude": 9.93, "longitude": 76.26}},
            {"location_id": "a", "coordinates": {"latitude": 10.0, "longitude": 76.3}},
        ]

        with patch(
            "geoagent.planning_tools.routeoptimization.RouteOptimizationClient",
            return_value=fake_client,
        ):
            result = optimize_assignments(
                tasks=[
                    task(
                        "task-1",
                        "a",
                        start_location_id="depot",
                        end_location_id="a",
                    )
                ],
                resources=[resource()],
                constraints=[],
                locations=locations,
                solver="route_optimization",
                consider_traffic=True,
                tool_context=CONTEXT,
            )

        self.assertEqual(result["status"], "success", result)
        self.assertEqual(result["solver"], "route_optimization")
        self.assertEqual(result["plan"]["resource_schedules"][0]["encoded_polyline"], "encoded")
        call = fake_client.optimize_tours.call_args
        request = call.kwargs["request"]
        self.assertEqual(request.parent, "projects/geoagent-hackathon")
        self.assertTrue(request.consider_road_traffic)
        self.assertIn(("x-goog-maps-solution-id", "gmp_git_agentskills_v1"), call.kwargs["metadata"])

    def test_route_optimization_strict_error_and_auto_fallback(self) -> None:
        locations = [
            {"location_id": "depot", "coordinates": {"latitude": 9.93, "longitude": 76.26}},
            {"location_id": "a", "coordinates": {"latitude": 10.0, "longitude": 76.3}},
        ]
        incompatible = optimize_assignments(
            tasks=[task("task-1", "a", predecessor_task_ids=["task-0"])],
            resources=[resource()],
            constraints=[],
            locations=locations,
            solver="route_optimization",
            tool_context=CONTEXT,
        )
        self.assertEqual(incompatible["error"]["code"], "ROUTE_OPTIMIZATION_INCOMPATIBLE")

        with patch(
            "geoagent.planning_tools.routeoptimization.RouteOptimizationClient",
            side_effect=DefaultCredentialsError("missing ADC"),
        ):
            fallback = optimize_assignments(
                tasks=[
                    task(
                        "task-1",
                        "a",
                        start_location_id="depot",
                        end_location_id="a",
                    )
                ],
                resources=[resource()],
                constraints=[],
                locations=locations,
                solver="auto",
                tool_context=CONTEXT,
            )
        self.assertEqual(fallback["solver"], "local")
        self.assertEqual(fallback["warnings"][0]["code"], "ROUTE_OPTIMIZATION_FALLBACK")

    def test_adk_declarations_and_agent_schemas(self) -> None:
        expected = {
            normalize_operational_rules: {"rules", "facility_scopes"},
            compose_resources: {"assets", "operators"},
            optimize_assignments: {
                "tasks",
                "resources",
                "constraints",
                "travel_matrix",
                "locations",
                "solver",
                "time_limit_seconds",
                "consider_traffic",
            },
            calculate_plan_metrics: {"plan", "tasks", "resources"},
            validate_plan: {"plan", "tasks", "resources", "constraints", "travel_matrix"},
        }
        for function, properties in expected.items():
            declaration = FunctionTool(function)._get_declaration()
            self.assertEqual(
                set(declaration.parameters_json_schema["properties"]),
                properties,
            )
            self.assertNotIn("tool_context", declaration.parameters_json_schema["properties"])

        request = PlanningRequest.model_validate(
            {
                "objective": "Schedule field work.",
                "organizational_facts": [{"tasks": [task("task-1", "a")]}],
                "geospatial_facts": [{"travel_matrix": matrix()}],
                "constraints": [{"kind": "weather_risk"}],
            }
        )
        self.assertEqual(request.objective, "Schedule field work.")
        findings = PlanningFindings.model_validate(
            {
                "feasible": False,
                "hard_violations": [{"code": "NO_CAPACITY"}],
                "proposed_objective": "Schedule the highest-priority work.",
            }
        )
        self.assertFalse(findings.feasible)


@unittest.skipUnless(
    os.getenv("RUN_LIVE_ROUTE_OPTIMIZATION_TESTS") == "1",
    "set RUN_LIVE_ROUTE_OPTIMIZATION_TESTS=1 with ADC to run",
)
class LiveRouteOptimizationSmokeTest(unittest.TestCase):
    def test_one_small_route_optimization_request(self) -> None:
        result = optimize_assignments(
            tasks=[task("task-1", "a")],
            resources=[resource()],
            constraints=[],
            locations=[
                {"location_id": "depot", "coordinates": {"latitude": 9.9312, "longitude": 76.2673}},
                {"location_id": "a", "coordinates": {"latitude": 9.9674, "longitude": 76.2454}},
            ],
            solver="route_optimization",
            time_limit_seconds=2,
            tool_context=CONTEXT,
        )
        self.assertIn(result["status"], {"success", "partial", "infeasible"})


if __name__ == "__main__":
    unittest.main()
