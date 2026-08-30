from __future__ import annotations

import asyncio
import os
import logging
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from unittest.mock import patch


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from fastapi.testclient import TestClient  # noqa: E402
from google.adk.events import Event  # noqa: E402
from google.adk.tools.function_tool import FunctionTool  # noqa: E402

from build_demo_db import build_database  # noqa: E402

from geoagent.app import app  # noqa: E402
from geoagent.app import workspace_question_service_dependency  # noqa: E402
from geoagent.missions import MissionCreate  # noqa: E402
from geoagent.missions import MissionError  # noqa: E402
from geoagent.missions import MissionEventRecord  # noqa: E402
from geoagent.missions import WorkspaceCreate  # noqa: E402
from geoagent.missions import configure_mission_service  # noqa: E402
from geoagent.observability import configure_observability  # noqa: E402
from geoagent.workspace_qa import QA_APP_NAME  # noqa: E402
from geoagent.workspace_qa import WorkspaceQuestionAnswer  # noqa: E402
from geoagent.workspace_qa import WorkspaceQuestionReference  # noqa: E402
from geoagent.workspace_qa import WorkspaceQuestionRequest  # noqa: E402
from geoagent.workspace_qa import WorkspaceQuestionService  # noqa: E402
from geoagent.workspace_qa import _validated_answer  # noqa: E402
from geoagent.workspace_qa import get_mission_details  # noqa: E402
from geoagent.workspace_qa import list_mission_events  # noqa: E402
from geoagent.workspace_qa import list_workspace_missions  # noqa: E402
from geoagent.workspace_qa import master_operations_agent  # noqa: E402
from test_missions import build_services  # noqa: E402


class WorkspaceQaToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        database_path = root / "operations.db"
        build_database(database_path, date(2026, 8, 25))
        data_service, self.service, _ = build_services(root)
        configure_mission_service(self.service)
        self.workspace = await self.service.create_workspace(
            WorkspaceCreate(name="Operations")
        )
        self.other_workspace = await self.service.create_workspace(
            WorkspaceCreate(name="Other")
        )
        data_service.connect_sqlite(
            self.workspace.workspace_id,
            "Operations",
            database_path,
            "operations.db",
        )
        data_service.connect_sqlite(
            self.other_workspace.workspace_id,
            "Other operations",
            database_path,
            "operations.db",
        )
        self.mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Review current operations."),
        )
        self.other_mission = await self.service.create_mission(
            self.other_workspace.workspace_id,
            MissionCreate(objective="Private other-workspace objective."),
        )

    async def asyncTearDown(self) -> None:
        configure_mission_service(None)
        self.temporary_directory.cleanup()

    def context(self, workspace_id: str | None = None, user_id: str | None = None):
        selected = workspace_id or self.workspace.workspace_id
        return SimpleNamespace(state={"workspace_id": selected}, user_id=user_id or selected)

    async def test_tools_are_workspace_scoped_and_read_only(self) -> None:
        before_missions = self.service.store.missions.copy()
        before_events = {
            key: list(events) for key, events in self.service.store.events.items()
        }

        listed = await list_workspace_missions(self.context())
        self.assertEqual(
            [item["mission_id"] for item in listed["missions"]],
            [self.mission.mission_id],
        )
        denied = await get_mission_details(self.other_mission.mission_id, self.context())
        self.assertEqual(denied["error"]["code"], "MISSION_NOT_FOUND")
        mismatch = await list_workspace_missions(
            self.context(user_id=self.other_workspace.workspace_id)
        )
        self.assertEqual(mismatch["error"]["code"], "WORKSPACE_ACCESS_DENIED")

        self.assertEqual(self.service.store.missions, before_missions)
        self.assertEqual(self.service.store.events, before_events)

    async def test_event_filter_limit_and_truncation(self) -> None:
        key = (self.workspace.workspace_id, self.mission.mission_id)
        for index in range(105):
            self.service.store.events[key].append(
                MissionEventRecord(
                    event_id=f"evt_{index}",
                    mission_id=self.mission.mission_id,
                    type="tool_result" if index % 2 else "tool_called",
                    created_at=self.mission.created_at,
                )
            )
        result = await list_mission_events(
            self.mission.mission_id,
            self.context(),
            event_types=["tool_result"],
            limit=10,
        )
        self.assertTrue(result["truncated"])
        self.assertEqual(len(result["events"]), 10)
        self.assertTrue(all(item["type"] == "tool_result" for item in result["events"]))
        self.assertEqual(result["events"][-1]["event_id"], "evt_103")

    def test_agent_and_tool_declarations_are_strictly_read_only(self) -> None:
        self.assertEqual(master_operations_agent.sub_agents, [])
        self.assertEqual(
            {getattr(tool, "name", getattr(tool, "__name__", "")) for tool in master_operations_agent.tools},
            {"list_workspace_missions", "get_mission_details", "list_mission_events"},
        )
        declaration = FunctionTool(list_workspace_missions)._get_declaration()
        self.assertNotIn("workspace_id", declaration.parameters_json_schema["properties"])
        detail = FunctionTool(get_mission_details)._get_declaration()
        self.assertEqual(set(detail.parameters_json_schema["properties"]), {"mission_id"})

    def test_reference_validation_filters_unretrieved_evidence(self) -> None:
        answer = WorkspaceQuestionAnswer(
            answer="One Mission is current.",
            references=[
                WorkspaceQuestionReference(
                    mission_id=self.mission.mission_id,
                    mission_name="Invented name",
                    event_ids=["evt_allowed", "evt_invented"],
                ),
                WorkspaceQuestionReference(
                    mission_id="msn_invented", event_ids=["evt_invented"]
                ),
            ],
        )
        validated = _validated_answer(
            answer,
            {self.mission.mission_id: "Recorded name"},
            {self.mission.mission_id: {"evt_allowed"}},
        )
        self.assertEqual(len(validated.references), 1)
        self.assertEqual(validated.references[0].mission_name, "Recorded name")
        self.assertEqual(validated.references[0].event_ids, ["evt_allowed"])

    def test_request_history_limits(self) -> None:
        valid = WorkspaceQuestionRequest(
            question="What is current?",
            history=[{"role": "user", "content": "x"}] * 20,
        )
        self.assertEqual(len(valid.history), 20)
        with self.assertRaises(ValueError):
            WorkspaceQuestionRequest(
                question="What is current?",
                history=[{"role": "user", "content": "x"}] * 21,
            )
        with self.assertRaises(ValueError):
            WorkspaceQuestionRequest(
                question="What is current?",
                history=[{"role": "user", "content": "x" * 8_000}] * 5,
            )


class FakeSessionService:
    instances: list["FakeSessionService"] = []

    def __init__(self) -> None:
        self.created: list[tuple[str, str, str]] = []
        self.deleted: list[tuple[str, str, str]] = []
        self.__class__.instances.append(self)

    async def create_session(self, *, app_name, user_id, session_id, state):
        self.created.append((app_name, user_id, session_id))
        return SimpleNamespace(id=session_id, state=state)

    async def delete_session(self, *, app_name, user_id, session_id):
        self.deleted.append((app_name, user_id, session_id))


class SuccessfulRunner:
    def __init__(self, **_kwargs) -> None:
        pass

    async def run_async(self, **_kwargs):
        yield Event(
            author="master_operations_agent",
            output={"answer": "No Missions are currently running.", "references": []},
        )


class FailingRunner:
    def __init__(self, **_kwargs) -> None:
        pass

    async def run_async(self, **_kwargs):
        if False:
            yield None
        raise RuntimeError("sensitive model failure")


class InvalidRunner:
    def __init__(self, **_kwargs) -> None:
        pass

    async def run_async(self, **_kwargs):
        yield Event(author="master_operations_agent", output={"unexpected": True})


class CancelledRunner:
    def __init__(self, **_kwargs) -> None:
        pass

    async def run_async(self, **_kwargs):
        if False:
            yield None
        raise asyncio.CancelledError


class WorkspaceQuestionServiceTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        _, self.mission_service, _ = build_services(Path(self.temporary_directory.name))
        configure_mission_service(self.mission_service)
        self.workspace = await self.mission_service.create_workspace(
            WorkspaceCreate(name="Operations")
        )
        FakeSessionService.instances.clear()

    async def asyncTearDown(self) -> None:
        configure_mission_service(None)
        self.temporary_directory.cleanup()

    async def test_temporary_session_is_deleted_after_success(self) -> None:
        with patch("geoagent.workspace_qa.InMemorySessionService", FakeSessionService), patch(
            "geoagent.workspace_qa.Runner", SuccessfulRunner
        ):
            answer = await WorkspaceQuestionService().answer(
                self.workspace.workspace_id,
                WorkspaceQuestionRequest(question="What is running?"),
            )
        self.assertIn("No Missions", answer.answer)
        session = FakeSessionService.instances[0]
        self.assertEqual(len(session.created), 1)
        self.assertEqual(session.deleted, session.created)
        self.assertEqual(session.created[0][0], QA_APP_NAME)

    async def test_temporary_session_is_deleted_after_failure(self) -> None:
        with patch("geoagent.workspace_qa.InMemorySessionService", FakeSessionService), patch(
            "geoagent.workspace_qa.Runner", FailingRunner
        ):
            with self.assertRaises(MissionError) as raised:
                await WorkspaceQuestionService().answer(
                    self.workspace.workspace_id,
                    WorkspaceQuestionRequest(question="What is running?"),
                )
        self.assertEqual(raised.exception.code, "WORKSPACE_QA_UNAVAILABLE")
        session = FakeSessionService.instances[0]
        self.assertEqual(session.deleted, session.created)

    async def test_temporary_session_is_deleted_after_invalid_output(self) -> None:
        with patch("geoagent.workspace_qa.InMemorySessionService", FakeSessionService), patch(
            "geoagent.workspace_qa.Runner", InvalidRunner
        ):
            with self.assertRaises(MissionError) as raised:
                await WorkspaceQuestionService().answer(
                    self.workspace.workspace_id,
                    WorkspaceQuestionRequest(question="What is running?"),
                )
        self.assertEqual(raised.exception.code, "WORKSPACE_QA_INVALID_RESPONSE")
        session = FakeSessionService.instances[0]
        self.assertEqual(session.deleted, session.created)

    async def test_temporary_session_is_deleted_after_cancellation(self) -> None:
        with patch("geoagent.workspace_qa.InMemorySessionService", FakeSessionService), patch(
            "geoagent.workspace_qa.Runner", CancelledRunner
        ):
            with self.assertRaises(asyncio.CancelledError):
                await WorkspaceQuestionService().answer(
                    self.workspace.workspace_id,
                    WorkspaceQuestionRequest(question="What is running?"),
                )
        session = FakeSessionService.instances[0]
        self.assertEqual(session.deleted, session.created)


class FakeQuestionService:
    async def answer(self, _workspace_id, _request):
        return WorkspaceQuestionAnswer(
            answer="One Mission is running.", references=[]
        )


class WorkspaceQuestionApiTest(unittest.TestCase):
    def setUp(self) -> None:
        app.dependency_overrides[workspace_question_service_dependency] = (
            lambda: FakeQuestionService()
        )
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()

    def test_endpoint_is_no_store_and_validates_history(self) -> None:
        response = self.client.post(
            "/api/workspaces/ws_1/questions",
            json={"question": "What is running?", "history": []},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.headers["cache-control"], "no-store")
        invalid = self.client.post(
            "/api/workspaces/ws_1/questions",
            json={
                "question": "What is running?",
                "history": [{"role": "user", "content": "x"}] * 21,
            },
        )
        self.assertEqual(invalid.status_code, 422)


class ObservabilityPrivacyTest(unittest.TestCase):
    def test_cloud_telemetry_never_captures_message_content(self) -> None:
        resource = Mock()
        with patch.dict(
            os.environ,
            {
                "GEOAGENT_OTEL_TO_CLOUD": "true",
                "GOOGLE_CLOUD_PROJECT": "geoagent-hackathon",
                "OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT": "EVENT_ONLY",
            },
            clear=False,
        ), patch(
            "google.adk.telemetry.google_cloud.get_gcp_exporters", return_value=[]
        ), patch(
            "google.adk.telemetry.google_cloud.get_gcp_resource",
            return_value=resource,
        ) as get_gcp_resource, patch(
            "google.adk.telemetry.setup.maybe_set_otel_providers"
        ) as set_otel_providers:
            configure_observability()
            get_gcp_resource.assert_called_once_with(project_id="geoagent-hackathon")
            set_otel_providers.assert_called_once_with(
                [[]], otel_resource=resource
            )
            self.assertEqual(
                os.environ["OTEL_INSTRUMENTATION_GENAI_CAPTURE_MESSAGE_CONTENT"],
                "NO_CONTENT",
            )
            self.assertEqual(os.environ["OTEL_INSTRUMENTATION_GENAI_EMIT_EVENT"], "false")
            self.assertGreaterEqual(
                logging.getLogger("google_adk").level, logging.INFO
            )


if __name__ == "__main__":
    unittest.main()
