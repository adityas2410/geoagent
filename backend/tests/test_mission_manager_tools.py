from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from google.adk.tools.function_tool import FunctionTool  # noqa: E402

from build_demo_db import build_database  # noqa: E402
from geoagent.mission_manager_tools import load_mission_state  # noqa: E402
from geoagent.mission_manager_tools import publish_plan  # noqa: E402
from geoagent.mission_manager_tools import request_clarification  # noqa: E402
from geoagent.mission_manager_tools import request_objective_decision  # noqa: E402
from geoagent.missions import MissionCreate  # noqa: E402
from geoagent.missions import MissionEventRecord  # noqa: E402
from geoagent.missions import WorkspaceCreate  # noqa: E402
from geoagent.missions import configure_mission_service  # noqa: E402
from test_missions import build_services  # noqa: E402


class MissionManagerToolsTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))
        self.data_service, self.service, _ = build_services(self.root)
        configure_mission_service(self.service)
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
        configure_mission_service(None)
        self.temporary_directory.cleanup()

    async def create_running_mission(self):
        mission = await self.service.create_mission(
            self.workspace.workspace_id,
            MissionCreate(objective="Plan tomorrow's deliveries."),
        )
        timestamp = datetime.now(timezone.utc)
        return await self.service.store.transition_mission(
            self.workspace.workspace_id,
            mission.mission_id,
            {"created"},
            {"status": "running", "updated_at": timestamp, "started_at": timestamp},
            MissionEventRecord(
                event_id=f"evt_start_{mission.mission_id}",
                mission_id=mission.mission_id,
                type="mission_started",
                created_at=timestamp,
            ),
        )

    def context(self, mission_id: str, session_id: str | None = None):
        return SimpleNamespace(
            state={
                "workspace_id": self.workspace.workspace_id,
                "mission_id": mission_id,
                "authorized_source_ids": [self.source.source_id],
            },
            session=SimpleNamespace(id=session_id or mission_id),
            user_id=self.workspace.workspace_id,
            actions=SimpleNamespace(skip_summarization=False),
        )

    async def test_load_and_request_clarification(self) -> None:
        mission = await self.create_running_mission()
        context = self.context(mission.mission_id)
        loaded = await load_mission_state(context)
        self.assertEqual(loaded["status"], "success")
        self.assertEqual(loaded["mission"]["objective"], mission.objective)

        result = await request_clarification(
            "What is the maximum acceptable delay?",
            "The limit is not present in organizational data.",
            context,
        )
        self.assertEqual(result["status"], "awaiting_input")
        self.assertTrue(context.actions.skip_summarization)
        self.assertEqual(context.state["mission_status"], "awaiting_input")

    async def test_publish_plan_and_reject_wrong_session(self) -> None:
        mission = await self.create_running_mission()
        denied = await load_mission_state(
            self.context(mission.mission_id, session_id="msn_wrong")
        )
        self.assertEqual(denied["error"]["code"], "MISSION_ACCESS_DENIED")

        context = self.context(mission.mission_id)
        result = await publish_plan(
            "Tomorrow's Operations",
            "A validated operational plan.",
            {"assignments": [{"task": "JOB-001", "resource": "VEH-001"}]},
            context,
        )
        self.assertEqual(result["status"], "completed")
        self.assertEqual(context.state["mission_status"], "completed")
        stored = await self.service.require_mission(
            self.workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(stored.name, "Tomorrow's Operations")
        self.assertIsNotNone(stored.plan)

    async def test_request_objective_decision(self) -> None:
        mission = await self.create_running_mission()
        context = self.context(mission.mission_id)
        result = await request_objective_decision(
            "Complete the highest-priority work within available capacity.",
            "Current capacity cannot complete all mandatory work.",
            [{"code": "CAPACITY_EXCEEDED"}],
            context,
        )
        self.assertEqual(result["status"], "awaiting_objective_decision")
        self.assertTrue(context.actions.skip_summarization)
        self.assertEqual(
            context.state["mission_status"], "awaiting_objective_decision"
        )
        stored = await self.service.require_mission(
            self.workspace.workspace_id, mission.mission_id
        )
        self.assertEqual(stored.objective_decision.status, "pending")

    def test_adk_tool_declarations_hide_context(self) -> None:
        clarification = FunctionTool(request_clarification)._get_declaration()
        self.assertEqual(
            set(clarification.parameters_json_schema["properties"]),
            {"question", "reason"},
        )
        published = FunctionTool(publish_plan)._get_declaration()
        self.assertEqual(
            set(published.parameters_json_schema["properties"]),
            {"mission_name", "summary", "plan"},
        )
        objective = FunctionTool(request_objective_decision)._get_declaration()
        self.assertEqual(
            set(objective.parameters_json_schema["properties"]),
            {"proposed_objective", "reason", "hard_violations"},
        )


if __name__ == "__main__":
    unittest.main()
