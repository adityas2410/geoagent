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
from geoagent.missions import MissionRecord  # noqa: E402
from geoagent.missions import MissionService  # noqa: E402
from geoagent.missions import WorkspaceCreate  # noqa: E402
from geoagent.missions import WorkspaceRecord  # noqa: E402
from geoagent.missions import build_mission_service_from_environment  # noqa: E402


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

    async def delete_mission(self, workspace_id: str, mission_id: str) -> None:
        key = (workspace_id, mission_id)
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

    async def run_async(self, *, user_id, session_id, new_message):
        self.calls += 1
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
            await self.service.publish_plan(
                user_id,
                session_id,
                session_id,
                "Tomorrow's Operations",
                "A validated operational plan.",
                {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
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

    async def run_async(self, *, user_id, session_id, new_message):
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
            await self.service.publish_plan(
                user_id,
                session_id,
                session_id,
                "Capacity-Aware Operations",
                "The accepted objective has a validated plan.",
                {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
            )
            text = "Plan published."
        yield Event(
            author="mission_manager",
            content=types.Content(
                role="model", parts=[types.Part.from_text(text=text)]
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
