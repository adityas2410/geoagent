from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from fastapi.testclient import TestClient  # noqa: E402
from google.adk.events.event import Event  # noqa: E402
from google.adk.sessions.in_memory_session_service import InMemorySessionService  # noqa: E402
from google.genai import types  # noqa: E402

from build_demo_db import build_database  # noqa: E402
from geoagent.app import app  # noqa: E402
from geoagent.app import data_source_service_dependency  # noqa: E402
from geoagent.app import mission_service_dependency  # noqa: E402
from geoagent.data_sources.source_files import LocalSourceStorage  # noqa: E402
from geoagent.data_sources.source_manager import DataSourceService  # noqa: E402
from geoagent.data_sources.source_records import InMemorySourceRepository  # noqa: E402
from geoagent.missions import APP_NAME  # noqa: E402
from geoagent.missions import ClarificationResponse  # noqa: E402
from geoagent.missions import MissionCreate  # noqa: E402
from geoagent.missions import MissionError  # noqa: E402
from geoagent.missions import MissionEventRecord  # noqa: E402
from geoagent.missions import MissionMapAssignment  # noqa: E402
from geoagent.missions import MissionMapAvailability  # noqa: E402
from geoagent.missions import MissionMapState  # noqa: E402
from geoagent.missions import MissionRecord  # noqa: E402
from geoagent.missions import MissionService  # noqa: E402
from geoagent.missions import OperationalDataRequirements  # noqa: E402
from geoagent.missions import WorkspaceCreate  # noqa: E402
from geoagent.missions import WorkspaceRecord  # noqa: E402
from geoagent.missions import build_mission_service_from_environment  # noqa: E402


NON_GEOGRAPHIC_REQUIREMENTS = OperationalDataRequirements.model_validate(
    {
        "locations": {"status": "not_applicable", "reason": "The fixture has no physical locations."},
        "routes": {"status": "not_applicable", "reason": "The fixture has no travel between locations."},
        "assignments": {"status": "required"},
        "metrics": {"status": "required"},
        "validation": {"status": "required"},
    }
)

GEOGRAPHIC_REQUIREMENTS = OperationalDataRequirements.model_validate(
    {
        "locations": {"status": "required"},
        "routes": {"status": "required"},
        "assignments": {"status": "required"},
        "metrics": {"status": "required"},
        "validation": {"status": "required"},
    }
)


def seed_required_plan_evidence(service: MissionService, workspace_id: str, mission_id: str) -> None:
    """Provide test-only persisted projection evidence for lifecycle runners."""
    key = (workspace_id, mission_id)
    mission = service.store.missions[key]
    state = mission.map_state.model_copy(
        update={
            "availability": MissionMapAvailability(
                assignments="available", metrics="available", validation="available"
            ),
            "assignments": [
                MissionMapAssignment(task_id="TASK-1", resource_id="RESOURCE-1", sequence=1)
            ],
            "metrics": {"assigned_task_count": 1},
            "validation": {"feasible": True, "hard_violations": [], "warnings": []},
        }
    )
    service.store.missions[key] = mission.model_copy(update={"map_state": state})


class InMemoryMissionStore:
    def __init__(self) -> None:
        self.workspaces: dict[str, WorkspaceRecord] = {}
        self.missions: dict[tuple[str, str], MissionRecord] = {}
        self.events: dict[tuple[str, str], list[MissionEventRecord]] = {}

    async def create_workspace(self, workspace: WorkspaceRecord) -> None:
        self.workspaces[workspace.workspace_id] = workspace.model_copy(deep=True)

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        workspace = self.workspaces.get(workspace_id)
        return workspace.model_copy(deep=True) if workspace else None

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        return sorted(
            (item.model_copy(deep=True) for item in self.workspaces.values()),
            key=lambda item: item.created_at,
        )

    async def set_workspace_status(
        self, workspace_id: str, allowed_statuses: set[str], status: str, updated_at: datetime
    ) -> WorkspaceRecord:
        current = self.workspaces.get(workspace_id)
        if current is None:
            raise MissionError("WORKSPACE_NOT_FOUND", "The Workspace was not found.", 404)
        if current.status not in allowed_statuses:
            raise MissionError("INVALID_WORKSPACE_STATUS", "The Workspace cannot perform this action right now.", 409)
        updated = current.model_copy(update={"status": status, "updated_at": updated_at})
        self.workspaces[workspace_id] = updated
        return updated.model_copy(deep=True)

    async def delete_workspace(self, workspace_id: str) -> None:
        self.workspaces.pop(workspace_id, None)
        for key in [key for key in self.missions if key[0] == workspace_id]:
            self.missions.pop(key, None)
            self.events.pop(key, None)

    async def create_mission(
        self, mission: MissionRecord, event: MissionEventRecord
    ) -> None:
        key = (mission.workspace_id, mission.mission_id)
        self.missions[key] = mission.model_copy(deep=True)
        self.events[key] = [event.model_copy(deep=True)]

    async def get_mission(
        self, workspace_id: str, mission_id: str
    ) -> MissionRecord | None:
        mission = self.missions.get((workspace_id, mission_id))
        return mission.model_copy(deep=True) if mission else None

    async def list_missions(self, workspace_id: str) -> list[MissionRecord]:
        return sorted(
            (
                mission.model_copy(deep=True)
                for (stored_workspace_id, _), mission in self.missions.items()
                if stored_workspace_id == workspace_id
            ),
            key=lambda item: item.created_at,
        )

    async def delete_mission(
        self, workspace_id: str, mission_id: str, allowed_statuses: set[str]
    ) -> None:
        key = (workspace_id, mission_id)
        mission = self.missions.get(key)
        if mission is None:
            raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
        if mission.status not in allowed_statuses:
            raise MissionError("MISSION_RUNNING", "A running Mission cannot be deleted. Wait until it pauses or finishes.", 409)
        self.missions.pop(key, None)
        self.events.pop(key, None)

    async def transition_mission(
        self,
        workspace_id: str,
        mission_id: str,
        allowed_statuses: set[str],
        changes: dict,
        event: MissionEventRecord,
    ) -> MissionRecord:
        key = (workspace_id, mission_id)
        current = self.missions.get(key)
        if current is None:
            raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
        if current.status not in allowed_statuses:
            raise MissionError(
                "INVALID_MISSION_STATUS",
                f"The Mission cannot perform this action while {current.status}.",
                409,
            )
        updated = MissionRecord.model_validate(
            {**current.model_dump(mode="python"), **changes}
        )
        self.missions[key] = updated
        self.events.setdefault(key, []).append(event.model_copy(deep=True))
        return updated.model_copy(deep=True)

    async def append_event(
        self, workspace_id: str, event: MissionEventRecord
    ) -> None:
        key = (workspace_id, event.mission_id)
        existing = self.events.setdefault(key, [])
        existing[:] = [item for item in existing if item.event_id != event.event_id]
        existing.append(event.model_copy(deep=True))

    async def append_event_and_map_state(
        self,
        workspace_id: str,
        event: MissionEventRecord,
        map_state: MissionMapState,
    ) -> None:
        key = (workspace_id, event.mission_id)
        current = self.missions.get(key)
        if current is None:
            raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
        self.missions[key] = current.model_copy(
            update={"map_state": map_state.model_copy(deep=True)}
        )
        await self.append_event(workspace_id, event)

    async def update_mission(
        self, workspace_id: str, mission_id: str, changes: dict
    ) -> None:
        key = (workspace_id, mission_id)
        current = self.missions.get(key)
        if current is None:
            raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
        self.missions[key] = MissionRecord.model_validate(
            {**current.model_dump(mode="python"), **changes}
        )

    async def list_events(
        self, workspace_id: str, mission_id: str
    ) -> list[MissionEventRecord]:
        return sorted(
            (
                item.model_copy(deep=True)
                for item in self.events.get((workspace_id, mission_id), [])
            ),
            key=lambda item: item.created_at,
        )


class ClarifyThenPublishRunner:
    def __init__(self) -> None:
        self.service: MissionService | None = None
        self.calls = 0
        self.run_configs = []

    async def run_async(self, *, user_id, session_id, new_message, run_config):
        self.calls += 1
        self.run_configs.append(run_config)
        assert self.service is not None
        if self.calls == 1:
            await self.service.request_clarification(
                user_id,
                session_id,
                session_id,
                "What is the maximum acceptable delay?",
                "The limit is not present in organizational data.",
            )
            yield Event(
                author="mission_manager",
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Clarification requested.")],
                ),
            )
        else:
            seed_required_plan_evidence(self.service, user_id, session_id)
            await self.service.publish_plan(
                user_id,
                session_id,
                session_id,
                "Tomorrow's Operations",
                "A validated operational plan.",
                {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
                NON_GEOGRAPHIC_REQUIREMENTS,
            )
            yield Event(
                author="mission_manager",
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text="Plan published.")],
                ),
            )


class ObjectiveDecisionRunner:
    def __init__(self, *, feasible_after_acceptance: bool) -> None:
        self.service: MissionService | None = None
        self.calls = 0
        self.feasible_after_acceptance = feasible_after_acceptance

    async def run_async(self, *, user_id, session_id, new_message, run_config):
        self.calls += 1
        assert self.service is not None
        if self.calls == 1 or not self.feasible_after_acceptance:
            suffix = "highest-priority work" if self.calls == 1 else "one priority task"
            await self.service.request_objective_decision(
                user_id,
                session_id,
                session_id,
                f"Complete {suffix} within available capacity.",
                "Available capacity cannot satisfy the current objective.",
                [{"code": "CAPACITY_EXCEEDED"}],
            )
            text = "Objective decision requested."
        else:
            seed_required_plan_evidence(self.service, user_id, session_id)
            await self.service.publish_plan(
                user_id,
                session_id,
                session_id,
                "Capacity-Aware Operations",
                "The accepted objective has a validated plan.",
                {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
                NON_GEOGRAPHIC_REQUIREMENTS,
            )
            text = "Plan published."
        yield Event(
            author="mission_manager",
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=text)]
            ),
        )


class ActivityProjectionRunner:
    """Produces the ADK event shapes used by single-turn specialist tools."""

    specialist_sequence = [
        ("organizational_data_agent", "query_source"),
        ("geospatial_intelligence_agent", "compute_routes"),
        ("planning_validation_agent", "validate_plan"),
        ("organizational_data_agent", "inspect_source_schema"),
    ]

    def __init__(self) -> None:
        self.service: MissionService | None = None

    async def run_async(self, *, user_id, session_id, new_message, run_config):
        assert self.service is not None
        for index, (specialist, tool_name) in enumerate(self.specialist_sequence):
            delegation_id = f"delegation-{index}"
            delegated = Event(
                author="mission_manager",
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(
                            function_call=types.FunctionCall(
                                id=delegation_id,
                                name=specialist,
                                args={"objective": "Plan tomorrow's deliveries."},
                            )
                        )
                    ],
                ),
            )
            yield delegated
            if index == 0:
                yield delegated

            specialist_call = Event(
                author=specialist,
                content=types.Content(
                    role="model",
                    parts=[
                        types.Part(text="hidden specialist reasoning", thought=True),
                        types.Part.from_function_call(
                            name=tool_name, args={"request_id": index}
                        ),
                    ],
                ),
            )
            yield specialist_call
            if index == 0:
                yield specialist_call
            yield Event(
                author=specialist,
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part.from_function_response(
                            name=tool_name,
                            response={"status": "success", "request_id": index},
                        )
                    ],
                ),
            )
            yield Event(
                author=specialist,
                content=types.Content(
                    role="model",
                    parts=[types.Part.from_text(text=f"{specialist} findings")],
                ),
            )
            completed = Event(
                author="mission_manager",
                content=types.Content(
                    role="user",
                    parts=[
                        types.Part(
                            function_response=types.FunctionResponse(
                                id=delegation_id,
                                name=specialist,
                                response={"status": "success", "specialist": specialist},
                            )
                        )
                    ],
                ),
            )
            yield completed
            if index == 0:
                yield completed

        seed_required_plan_evidence(self.service, user_id, session_id)
        await self.service.publish_plan(
            user_id,
            session_id,
            session_id,
            "Tomorrow's Operations",
            "A validated operational plan.",
            {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
            NON_GEOGRAPHIC_REQUIREMENTS,
        )
        yield Event(
            author="mission_manager",
            content=types.Content(
                role="model", parts=[types.Part.from_text(text="Plan published.")]
            ),
        )


def build_services(root: Path, runner=None):
    data_service = DataSourceService(
        repository=InMemorySourceRepository(),
        storage=LocalSourceStorage(root / "stored"),
    )
    runner = runner or ClarifyThenPublishRunner()
    session_service = InMemorySessionService()
    mission_service = MissionService(
        store=InMemoryMissionStore(),
        session_service=session_service,
        runner=runner,
        data_source_service=data_service,
    )
    runner.service = mission_service
    return data_service, mission_service, session_service


class MissionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))
        self.data_service, self.service, self.sessions = build_services(self.root)
        self.workspace = await self.service.create_workspace(
            WorkspaceCreate(name="Kerala Operations")
        )
        self.source = self.data_service.connect_sqlite(
            self.workspace.workspace_id,
            "Operations",
            self.database_path,
            "operations.db",
        )

    async def asyncTearDown(self) -> None:
        self.temporary_directory.cleanup()

    async def test_default_sources_and_initial_adk_state(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )
        self.assertEqual(mission.status, "created")
        self.assertEqual(mission.authorized_source_ids, [self.source.source_id])
        self.assertEqual(mission.adk_session_id, mission.mission_id)

        session = await self.sessions.get_session(
            app_name=APP_NAME,
            user_id=self.workspace.workspace_id,
            session_id=mission.mission_id,
        )
        self.assertIsNotNone(session)
        self.assertEqual(session.state["workspace_id"], self.workspace.workspace_id)
        self.assertEqual(session.state["authorized_source_ids"], [self.source.source_id])

        second_source = self.data_service.connect_sqlite(
            self.workspace.workspace_id,
            "Later Source",
            self.database_path,
            "later.db",
        )
        unchanged = await self.service.require_mission(
            self.workspace.workspace_id, mission.mission_id
        )
        self.assertNotIn(second_source.source_id, unchanged.authorized_source_ids)

    async def test_selected_sources_and_invalid_selection(self) -> None:
        second_source = self.data_service.connect_sqlite(
            self.workspace.workspace_id,
            "Inventory",
            self.database_path,
            "inventory.db",
        )
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(
                objective="Plan inventory work.", source_ids=[second_source.source_id]
            ),
        )
        self.assertEqual(mission.authorized_source_ids, [second_source.source_id])
        with self.assertRaises(MissionError) as raised:
            await self.service.create_mission(
                self.workspace.workspace_id,
                MissionCreate(objective="Invalid.", source_ids=["src_missing"]),
            )
        self.assertEqual(raised.exception.code, "SOURCE_NOT_AUTHORIZED")

    async def test_mission_requires_a_connected_source(self) -> None:
        empty_workspace = await self.service.create_workspace(
            WorkspaceCreate(name="Empty Workspace")
        )
        with self.assertRaises(MissionError) as raised:
            await self.service.create_mission(
                empty_workspace.workspace_id,
                MissionCreate(objective="Plan an operation."),
            )
        self.assertEqual(raised.exception.code, "MISSION_HAS_NO_SOURCES")

    async def test_run_clarification_resume_and_status_guards(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )
        waiting = await self.service.run_mission(
            self.workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(waiting.status, "awaiting_input")
        self.assertEqual(waiting.clarification.status, "open")
        self.assertEqual(self.service.runner.run_configs[0].max_llm_calls, 100)

        with self.assertRaises(MissionError) as raised:
            await self.service.run_mission(self.workspace.workspace_id, mission.mission_id)
        self.assertEqual(raised.exception.code, "INVALID_MISSION_STATUS")

        completed = await self.service.respond_to_clarification(
            self.workspace.workspace_id,
            mission.mission_id,
            ClarificationResponse(answer="Thirty minutes."),
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.clarification.answer, "Thirty minutes.")
        self.assertEqual(completed.name, "Tomorrow's Operations")
        self.assertEqual(len(self.service.runner.run_configs), 2)
        self.assertTrue(
            all(config.max_llm_calls == 100 for config in self.service.runner.run_configs)
        )

        with self.assertRaises(MissionError) as raised:
            await self.service.respond_to_clarification(
                self.workspace.workspace_id,
                mission.mission_id,
                ClarificationResponse(answer="Another answer"),
            )
        self.assertEqual(raised.exception.code, "NO_OPEN_CLARIFICATION")

    async def test_safe_event_projection_excludes_thought_text(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )
        event = Event(
            author="organizational_data_agent",
            content=types.Content(
                role="model",
                parts=[
                    types.Part(text="hidden reasoning", thought=True),
                    types.Part.from_text(text="Visible finding"),
                    types.Part.from_function_call(
                        name="query_source", args={"source_id": self.source.source_id}
                    ),
                ],
            ),
        )
        await self.service._record_adk_event(mission, event)
        events = await self.service.list_events(
            self.workspace.workspace_id, mission.mission_id
        )
        serialized = " ".join(str(item.model_dump(mode="json")) for item in events)
        self.assertIn("Visible finding", serialized)
        self.assertIn("query_source", serialized)
        self.assertNotIn("hidden reasoning", serialized)

    async def test_map_projection_uses_only_known_safe_tool_results(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )
        self.service.store.missions[(self.workspace.workspace_id, mission.mission_id)] = (
            mission.model_copy(update={"status": "running"})
        )

        async def record(tool: str, response: dict) -> None:
            await self.service._record_adk_event(
                mission,
                Event(
                    author="geospatial_intelligence_agent",
                    content=types.Content(
                        role="user",
                        parts=[types.Part.from_function_response(name=tool, response=response)],
                    ),
                ),
            )

        await record(
            "geocode_locations",
            {
                "status": "success",
                "resolved_locations": [
                    {
                        "reference_id": "DEPOT-1",
                        "name": "Main Depot",
                        "place_id": "place-depot",
                        "coordinates": {"latitude": 9.9312, "longitude": 76.2673},
                        "source": {"source_id": self.source.source_id, "secret": "omit-me"},
                    }
                ],
                "warnings": [],
                "errors": [],
            },
        )
        await record(
            "compute_routes",
            {
                "status": "success",
                "routes": [
                    {
                        "route_index": 0,
                        "origin_reference_id": "DEPOT-1",
                        "destination_reference_id": "STOP-1",
                        "waypoint_reference_ids": [],
                        "encoded_polyline": "encoded-route",
                        "distance_meters": 12345,
                        "duration_seconds": 900,
                    }
                ],
                "warnings": [],
                "errors": [],
            },
        )
        await record(
            "optimize_assignments",
            {
                "status": "success",
                "plan": {
                    "assignments": [
                        {
                            "task_id": "JOB-1",
                            "resource_id": "VEH-1",
                            "sequence": 1,
                            "start_at": "2026-08-25T09:00:00Z",
                            "end_at": "2026-08-25T10:00:00Z",
                            "origin_location_id": "DEPOT-1",
                            "destination_location_id": "STOP-1",
                            "travel_distance_meters": 12345,
                            "travel_duration_seconds": 900,
                        }
                    ],
                    "resource_schedules": [
                        {"resource_id": "VEH-1", "encoded_polyline": "optimized-route"}
                    ],
                },
            },
        )
        await record(
            "calculate_plan_metrics",
            {"status": "success", "metrics": {"task_count": 1}},
        )
        await record(
            "validate_plan",
            {
                "status": "success",
                "feasible": True,
                "hard_violations": [],
                "warnings": [{"code": "WEATHER", "message": "Monitor rain."}],
            },
        )
        await record(
            "query_source",
            {"status": "success", "rows": [{"secret": "must-not-appear"}]},
        )

        map_response = await self.service.get_map_state(
            self.workspace.workspace_id, mission.mission_id
        )
        state = map_response.map_state
        self.assertEqual(state.availability.locations, "available")
        self.assertEqual(state.availability.routes, "available")
        self.assertEqual(state.availability.assignments, "available")
        self.assertEqual(state.locations[0].label, "Main Depot")
        self.assertEqual(state.routes[0].encoded_polyline, "encoded-route")
        self.assertEqual(state.assignments[0].resource_id, "VEH-1")
        self.assertEqual(state.metrics, {"task_count": 1})
        self.assertTrue(state.validation["feasible"])
        self.assertGreaterEqual(state.revision, 5)
        serialized_map = str(state.model_dump(mode="json"))
        self.assertNotIn("must-not-appear", serialized_map)
        self.assertNotIn("omit-me", serialized_map)

        completed = await self.service.publish_plan(
            self.workspace.workspace_id,
            mission.mission_id,
            mission.adk_session_id,
            "Map Test",
            "A plan with real map data.",
            {"result": "published"},
            GEOGRAPHIC_REQUIREMENTS,
        )
        self.assertTrue(completed.map_state.is_final)
        events = await self.service.list_events(self.workspace.workspace_id, mission.mission_id)
        self.assertTrue(any(event.tool == "validate_plan" for event in events))

    async def test_publish_blocks_missing_required_projection_evidence(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan physical operational work."),
        )
        self.service.store.missions[(self.workspace.workspace_id, mission.mission_id)] = (
            mission.model_copy(update={"status": "running"})
        )
        with self.assertRaises(MissionError) as raised:
            await self.service.publish_plan(
                self.workspace.workspace_id,
                mission.mission_id,
                mission.adk_session_id,
                "Incomplete Plan",
                "This must not publish without structured evidence.",
                {"result": "unsupported"},
                GEOGRAPHIC_REQUIREMENTS,
            )
        self.assertEqual(raised.exception.code, "OPERATIONAL_DATA_INCOMPLETE")
        unchanged = await self.service.require_mission(
            self.workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(unchanged.status, "running")

        seed_required_plan_evidence(self.service, self.workspace.workspace_id, mission.mission_id)
        key = (self.workspace.workspace_id, mission.mission_id)
        seeded = self.service.store.missions[key]
        self.service.store.missions[key] = seeded.model_copy(
            update={"map_state": seeded.map_state.model_copy(update={"assignments": []})}
        )
        with self.assertRaises(MissionError) as empty_raised:
            await self.service.publish_plan(
                self.workspace.workspace_id,
                mission.mission_id,
                mission.adk_session_id,
                "Empty Assignment Plan",
                "This must not publish with an empty required assignment result.",
                {"result": "unsupported"},
                NON_GEOGRAPHIC_REQUIREMENTS,
            )
        self.assertEqual(empty_raised.exception.code, "OPERATIONAL_DATA_INCOMPLETE")

    async def test_publish_persists_not_applicable_reasons(self) -> None:
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Schedule non-geographic operational work."),
        )
        self.service.store.missions[(self.workspace.workspace_id, mission.mission_id)] = (
            mission.model_copy(update={"status": "running"})
        )
        seed_required_plan_evidence(self.service, self.workspace.workspace_id, mission.mission_id)
        completed = await self.service.publish_plan(
            self.workspace.workspace_id,
            mission.mission_id,
            mission.adk_session_id,
            "Non-geographic Operations",
            "A validated assignment plan without physical travel.",
            {"result": "published"},
            NON_GEOGRAPHIC_REQUIREMENTS,
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(completed.map_state.availability.locations, "not_applicable")
        self.assertEqual(
            completed.map_state.availability_reasons["routes"],
            "The fixture has no travel between locations.",
        )

    async def test_specialist_activity_projection(self) -> None:
        runner = ActivityProjectionRunner()
        data_service, service, _sessions = build_services(self.root / "activity", runner)
        workspace = await service.create_workspace(WorkspaceCreate(name="Activity Test"))
        data_service.connect_sqlite(
            workspace.workspace_id,
            "Operations",
            self.database_path,
            "operations.db",
        )
        mission = await service.create_mission(
            workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )

        completed = await service.run_mission(workspace.workspace_id, mission.mission_id)
        self.assertEqual(completed.status, "completed")
        events = await service.list_events(workspace.workspace_id, mission.mission_id)
        activity_types = {
            "task_delegated",
            "specialist_started",
            "tool_called",
            "tool_result",
            "specialist_completed",
            "plan_published",
        }
        activity = [event for event in events if event.type in activity_types]
        expected: list[str] = []
        for _specialist, _tool in runner.specialist_sequence:
            expected.extend(
                [
                    "task_delegated",
                    "specialist_started",
                    "tool_called",
                    "tool_result",
                    "specialist_completed",
                ]
            )
        expected.append("plan_published")
        self.assertEqual([event.type for event in activity], expected)

        for index, (specialist, tool_name) in enumerate(runner.specialist_sequence):
            delegated, started, called, result, finished = activity[
                index * 5 : index * 5 + 5
            ]
            delegation_id = f"delegation-{index}"
            self.assertEqual(delegated.agent, "mission_manager")
            self.assertEqual(delegated.tool, specialist)
            self.assertEqual(delegated.payload["specialist"], specialist)
            self.assertEqual(delegated.payload["delegation_id"], delegation_id)
            self.assertEqual(started.agent, specialist)
            self.assertEqual(started.payload["delegation_id"], delegation_id)
            self.assertEqual(called.agent, specialist)
            self.assertEqual(called.tool, tool_name)
            self.assertEqual(result.agent, specialist)
            self.assertEqual(result.tool, tool_name)
            self.assertEqual(finished.agent, specialist)
            self.assertEqual(finished.payload["delegation_id"], delegation_id)
            self.assertEqual(finished.payload["status"], "success")
            for item in (delegated, started, called, result, finished):
                self.assertIsNotNone(item.source_event_id)

        delegated_specialists = [
            event.payload["specialist"]
            for event in activity
            if event.type == "task_delegated"
        ]
        self.assertEqual(
            delegated_specialists,
            [specialist for specialist, _tool in runner.specialist_sequence],
        )
        self.assertEqual(len(completed.run_metrics), 1)
        self.assertEqual(
            completed.run_metrics[-1].specialist_delegations,
            len(runner.specialist_sequence),
        )
        self.assertEqual(
            completed.run_metrics[-1].tool_calls,
            len(runner.specialist_sequence),
        )
        serialized = " ".join(str(item.model_dump(mode="json")) for item in events)
        self.assertNotIn("hidden specialist reasoning", serialized)

    async def test_accept_revised_objective_replans_once_and_preserves_history(self) -> None:
        runner = ObjectiveDecisionRunner(feasible_after_acceptance=True)
        data_service, service, _sessions = build_services(self.root / "objective", runner)
        workspace = await service.create_workspace(WorkspaceCreate(name="Objective Test"))
        data_service.connect_sqlite(
            workspace.workspace_id,
            "Operations",
            self.database_path,
            "operations.db",
        )
        mission = await service.create_mission(
            workspace.workspace_id,
            MissionCreate(objective="Complete every task with current capacity."),
        )

        waiting = await service.run_mission(workspace.workspace_id, mission.mission_id)
        self.assertEqual(waiting.status, "awaiting_objective_decision")
        self.assertEqual(waiting.objective_decision.status, "pending")

        completed = await service.accept_objective_decision(
            workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(completed.status, "completed")
        self.assertEqual(runner.calls, 2)
        self.assertEqual(len(completed.objective_history), 1)
        self.assertEqual(
            completed.objective_history[0].objective,
            "Complete every task with current capacity.",
        )
        self.assertEqual(
            completed.objective,
            "Complete highest-priority work within available capacity.",
        )

    async def test_second_infeasible_attempt_stops_and_discard_deletes_mission(self) -> None:
        runner = ObjectiveDecisionRunner(feasible_after_acceptance=False)
        data_service, service, sessions = build_services(self.root / "discard", runner)
        workspace = await service.create_workspace(WorkspaceCreate(name="Discard Test"))
        data_service.connect_sqlite(
            workspace.workspace_id,
            "Operations",
            self.database_path,
            "operations.db",
        )
        mission = await service.create_mission(
            workspace.workspace_id,
            MissionCreate(objective="Complete every task with current capacity."),
        )
        await service.run_mission(workspace.workspace_id, mission.mission_id)

        waiting_again = await service.accept_objective_decision(
            workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(waiting_again.status, "awaiting_objective_decision")
        self.assertEqual(runner.calls, 2)
        self.assertEqual(
            waiting_again.objective_decision.proposed_objective,
            "Complete one priority task within available capacity.",
        )

        await service.discard_objective_decision(
            workspace.workspace_id, mission.mission_id
        )
        with self.assertRaises(MissionError) as raised:
            await service.require_mission(workspace.workspace_id, mission.mission_id)
        self.assertEqual(raised.exception.code, "MISSION_NOT_FOUND")
        session = await sessions.get_session(
            app_name=APP_NAME,
            user_id=workspace.workspace_id,
            session_id=mission.mission_id,
        )
        self.assertIsNone(session)

    async def test_delete_non_running_mission_removes_product_and_session_state(self) -> None:
        data_service, service, sessions = build_services(self.root / "delete-mission")
        workspace = await service.create_workspace(WorkspaceCreate(name="Delete Mission"))
        data_service.connect_sqlite(workspace.workspace_id, "Operations", self.database_path, "operations.db")
        mission = await service.create_mission(
            workspace.workspace_id, MissionCreate(objective="Review operational work.")
        )

        await service.delete_mission(workspace.workspace_id, mission.mission_id)

        with self.assertRaises(MissionError) as raised:
            await service.require_mission(workspace.workspace_id, mission.mission_id)
        self.assertEqual(raised.exception.code, "MISSION_NOT_FOUND")
        self.assertIsNone(await sessions.get_session(app_name=APP_NAME, user_id=workspace.workspace_id, session_id=mission.mission_id))

    async def test_running_mission_and_workspace_cannot_be_deleted(self) -> None:
        data_service, service, _sessions = build_services(self.root / "running-delete")
        workspace = await service.create_workspace(WorkspaceCreate(name="Running Delete"))
        data_service.connect_sqlite(workspace.workspace_id, "Operations", self.database_path, "operations.db")
        mission = await service.create_mission(
            workspace.workspace_id, MissionCreate(objective="Review operational work.")
        )
        service.store.missions[(workspace.workspace_id, mission.mission_id)] = mission.model_copy(update={"status": "running"})

        with self.assertRaises(MissionError) as mission_error:
            await service.delete_mission(workspace.workspace_id, mission.mission_id)
        self.assertEqual(mission_error.exception.code, "MISSION_RUNNING")
        with self.assertRaises(MissionError) as workspace_error:
            await service.delete_workspace(workspace.workspace_id, workspace.name)
        self.assertEqual(workspace_error.exception.code, "WORKSPACE_HAS_RUNNING_MISSIONS")

    async def test_delete_workspace_removes_sources_missions_and_sessions(self) -> None:
        data_service, service, sessions = build_services(self.root / "delete-workspace")
        workspace = await service.create_workspace(WorkspaceCreate(name="Delete Workspace"))
        source = data_service.connect_sqlite(
            workspace.workspace_id, "Operations", self.database_path, "operations.db"
        )
        mission = await service.create_mission(
            workspace.workspace_id, MissionCreate(objective="Review operational work.")
        )

        with self.assertRaises(MissionError) as confirmation_error:
            await service.delete_workspace(workspace.workspace_id, "wrong name")
        self.assertEqual(confirmation_error.exception.code, "WORKSPACE_CONFIRMATION_MISMATCH")
        await service.delete_workspace(workspace.workspace_id, workspace.name)

        with self.assertRaises(MissionError) as workspace_error:
            await service.require_workspace(workspace.workspace_id)
        self.assertEqual(workspace_error.exception.code, "WORKSPACE_NOT_FOUND")
        self.assertFalse((data_service.storage.root_directory / source.storage_key).exists())
        self.assertIsNone(await sessions.get_session(app_name=APP_NAME, user_id=workspace.workspace_id, session_id=mission.mission_id))


class MissionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))
        self.data_service, self.mission_service, _ = build_services(self.root)
        app.dependency_overrides[data_source_service_dependency] = lambda: self.data_service
        app.dependency_overrides[mission_service_dependency] = lambda: self.mission_service
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.temporary_directory.cleanup()

    def test_real_api_flow(self) -> None:
        workspace_response = self.client.post(
            "/api/workspaces", json={"name": "Kerala Operations"}
        )
        self.assertEqual(workspace_response.status_code, 201, workspace_response.text)
        workspace_id = workspace_response.json()["workspace_id"]

        upload = self.client.post(
            f"/api/workspaces/{workspace_id}/data-sources/sqlite",
            data={"name": "Operations"},
            files={
                "file": (
                    "operations.db",
                    self.database_path.read_bytes(),
                    "application/vnd.sqlite3",
                )
            },
        )
        self.assertEqual(upload.status_code, 201, upload.text)

        created = self.client.post(
            f"/api/workspaces/{workspace_id}/missions",
            json={"objective": "Plan tomorrow's deliveries.", "source_ids": []},
        )
        self.assertEqual(created.status_code, 201, created.text)
        mission_id = created.json()["mission_id"]

        waiting = self.client.post(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/run"
        )
        self.assertEqual(waiting.status_code, 200, waiting.text)
        self.assertEqual(waiting.json()["status"], "awaiting_input")

        completed = self.client.post(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/responses",
            json={"answer": "Thirty minutes."},
        )
        self.assertEqual(completed.status_code, 200, completed.text)
        self.assertEqual(completed.json()["status"], "completed")

        listed = self.client.get(f"/api/workspaces/{workspace_id}/missions")
        self.assertEqual(len(listed.json()["missions"]), 1)
        events = self.client.get(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/events"
        )
        event_types = [item["type"] for item in events.json()["events"]]
        self.assertIn("mission_created", event_types)
        self.assertIn("clarification_requested", event_types)
        self.assertIn("plan_published", event_types)

        mission_map = self.client.get(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/map"
        )
        self.assertEqual(mission_map.status_code, 200, mission_map.text)
        self.assertTrue(mission_map.json()["map_state"]["is_final"])

        active = self.client.post(
            f"/api/workspaces/{workspace_id}/missions",
            json={"objective": "Plan next week's deliveries."},
        )
        self.assertEqual(active.status_code, 201, active.text)
        workspace_map = self.client.get(f"/api/workspaces/{workspace_id}/map")
        self.assertEqual(workspace_map.status_code, 200, workspace_map.text)
        self.assertEqual(
            [item["mission_id"] for item in workspace_map.json()["missions"]],
            [active.json()["mission_id"]],
        )
        all_missions_map = self.client.get(
            f"/api/workspaces/{workspace_id}/map?include_completed=true"
        )
        self.assertEqual(len(all_missions_map.json()["missions"]), 2)

    def test_local_vite_origin_receives_cors_headers(self) -> None:
        response = self.client.options(
            "/health",
            headers={
                "Origin": "http://localhost:5173",
                "Access-Control-Request-Method": "GET",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(
            response.headers.get("access-control-allow-origin"),
            "http://localhost:5173",
        )

    def test_delete_mission_and_workspace_endpoints(self) -> None:
        workspace = self.client.post(
            "/api/workspaces", json={"name": "Delete API"}
        ).json()
        workspace_id = workspace["workspace_id"]
        upload = self.client.post(
            f"/api/workspaces/{workspace_id}/data-sources/sqlite",
            data={"name": "Operations"},
            files={"file": ("operations.db", self.database_path.read_bytes(), "application/vnd.sqlite3")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        mission = self.client.post(
            f"/api/workspaces/{workspace_id}/missions", json={"objective": "Review operational work."}
        ).json()

        deleted_mission = self.client.delete(
            f"/api/workspaces/{workspace_id}/missions/{mission['mission_id']}"
        )
        self.assertEqual(deleted_mission.status_code, 204, deleted_mission.text)
        deleted_workspace = self.client.request(
            "DELETE",
            f"/api/workspaces/{workspace_id}",
            json={"workspace_name": "Delete API"},
        )
        self.assertEqual(deleted_workspace.status_code, 204, deleted_workspace.text)

    def test_objective_accept_and_discard_endpoints(self) -> None:
        runner = ObjectiveDecisionRunner(feasible_after_acceptance=True)
        self.data_service, self.mission_service, _ = build_services(
            self.root / "objective-api", runner
        )
        app.dependency_overrides[data_source_service_dependency] = lambda: self.data_service
        app.dependency_overrides[mission_service_dependency] = lambda: self.mission_service

        workspace = self.client.post(
            "/api/workspaces", json={"name": "Objective API"}
        ).json()
        workspace_id = workspace["workspace_id"]
        upload = self.client.post(
            f"/api/workspaces/{workspace_id}/data-sources/sqlite",
            data={"name": "Operations"},
            files={"file": ("operations.db", self.database_path.read_bytes(), "application/vnd.sqlite3")},
        )
        self.assertEqual(upload.status_code, 201, upload.text)
        created = self.client.post(
            f"/api/workspaces/{workspace_id}/missions",
            json={"objective": "Complete every task."},
        ).json()
        mission_id = created["mission_id"]
        waiting = self.client.post(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/run"
        )
        self.assertEqual(waiting.json()["status"], "awaiting_objective_decision")
        accepted = self.client.post(
            f"/api/workspaces/{workspace_id}/missions/{mission_id}/objective-decision/accept"
        )
        self.assertEqual(accepted.status_code, 200, accepted.text)
        self.assertEqual(accepted.json()["status"], "completed")

        discard_runner = ObjectiveDecisionRunner(feasible_after_acceptance=False)
        discard_data, discard_service, _ = build_services(
            self.root / "discard-api", discard_runner
        )
        app.dependency_overrides[data_source_service_dependency] = lambda: discard_data
        app.dependency_overrides[mission_service_dependency] = lambda: discard_service
        discard_workspace = self.client.post(
            "/api/workspaces", json={"name": "Discard API"}
        ).json()
        discard_workspace_id = discard_workspace["workspace_id"]
        discard_data.connect_sqlite(
            discard_workspace_id,
            "Operations",
            self.database_path,
            "operations.db",
        )
        discard_mission = self.client.post(
            f"/api/workspaces/{discard_workspace_id}/missions",
            json={"objective": "Complete every task."},
        ).json()
        discard_mission_id = discard_mission["mission_id"]
        self.client.post(
            f"/api/workspaces/{discard_workspace_id}/missions/{discard_mission_id}/run"
        )
        discarded = self.client.delete(
            f"/api/workspaces/{discard_workspace_id}/missions/{discard_mission_id}/objective-decision"
        )
        self.assertEqual(discarded.status_code, 204, discarded.text)
        missing = self.client.get(
            f"/api/workspaces/{discard_workspace_id}/missions/{discard_mission_id}"
        )
        self.assertEqual(missing.status_code, 404)


class MissionBuilderTest(unittest.TestCase):
    def test_named_firestore_database_is_used_for_product_and_adk_state(self) -> None:
        with (
            patch("google.cloud.firestore.AsyncClient") as client_class,
            patch(
                "google.adk.integrations.firestore.firestore_session_service.FirestoreSessionService"
            ) as session_class,
            patch("geoagent.missions.Runner") as runner_class,
            patch("geoagent.missions.get_data_source_service") as data_source_factory,
        ):
            service = build_mission_service_from_environment()

        client_class.assert_called_once_with(
            project="geoagent-hackathon", database="geoagentdb"
        )
        session_class.assert_called_once_with(client=client_class.return_value)
        runner_class.assert_called_once()
        self.assertIs(service.store.client, client_class.return_value)
        self.assertIs(service.data_source_service, data_source_factory.return_value)


if __name__ == "__main__":
    unittest.main()
