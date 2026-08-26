"""Workspace, Mission, ADK session, and Mission event handling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import types
from pydantic import BaseModel, Field

from .data_sources.source_manager import DataSourceService
from .data_sources.source_manager import get_data_source_service


APP_NAME = "geoagent"
MissionStatus = Literal["created", "running", "awaiting_input", "completed", "failed"]
logger = logging.getLogger(__name__)


class MissionError(Exception):
    """An expected Workspace or Mission error safe to return through the API."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        """Keep a safe error code, message, and HTTP status together."""
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class WorkspaceCreate(BaseModel):
    """Information required to create a Workspace."""

    name: str = Field(min_length=1, max_length=100)


class WorkspaceRecord(BaseModel):
    """Workspace information saved in Firestore and returned by the API."""

    workspace_id: str
    name: str
    status: Literal["active"] = "active"
    created_at: datetime
    updated_at: datetime


class MissionCreate(BaseModel):
    """Objective and optional source selection supplied for a new Mission."""

    objective: str = Field(min_length=1, max_length=4000)
    source_ids: list[str] = Field(default_factory=list)


class ClarificationResponse(BaseModel):
    """Open-ended answer used to continue a paused Mission."""

    answer: str = Field(min_length=1, max_length=4000)


class ClarificationState(BaseModel):
    """The current question and answer state for one Mission."""

    question: str
    reason: str
    status: Literal["open", "answered"]
    requested_at: datetime
    answer: str | None = None
    answered_at: datetime | None = None


class MissionRecord(BaseModel):
    """Complete product-facing state saved for one Mission."""

    mission_id: str
    workspace_id: str
    adk_session_id: str
    objective: str
    authorized_source_ids: list[str]
    status: MissionStatus
    name: str | None = None
    summary: str | None = None
    clarification: ClarificationState | None = None
    plan: dict[str, Any] | None = None
    error: str | None = None
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    completed_at: datetime | None = None


class MissionEventRecord(BaseModel):
    """One safe activity item that the frontend may display."""

    event_id: str
    mission_id: str
    type: str
    agent: str | None = None
    tool: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    source_event_id: str | None = None
    created_at: datetime


class WorkspaceListResponse(BaseModel):
    """API response containing every available Workspace."""

    workspaces: list[WorkspaceRecord]


class MissionListResponse(BaseModel):
    """API response containing a Workspace's Missions."""

    missions: list[MissionRecord]


class MissionEventListResponse(BaseModel):
    """API response containing observable activity for one Mission."""

    events: list[MissionEventRecord]


class MissionStore(Protocol):
    """Storage operations needed by the Mission lifecycle."""

    async def create_workspace(self, workspace: WorkspaceRecord) -> None:
        """Save a new Workspace."""
        ...

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        """Load a Workspace by its ID."""
        ...

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        """Load all Workspaces."""
        ...

    async def create_mission(
        self, mission: MissionRecord, event: MissionEventRecord
    ) -> None:
        """Save a new Mission and its first event."""
        ...

    async def get_mission(
        self, workspace_id: str, mission_id: str
    ) -> MissionRecord | None:
        """Load one Mission from its Workspace."""
        ...

    async def list_missions(self, workspace_id: str) -> list[MissionRecord]:
        """Load all Missions from one Workspace."""
        ...

    async def transition_mission(
        self,
        workspace_id: str,
        mission_id: str,
        allowed_statuses: set[str],
        changes: dict[str, Any],
        event: MissionEventRecord,
    ) -> MissionRecord:
        """Save an allowed status change and its event together."""
        ...

    async def append_event(
        self, workspace_id: str, event: MissionEventRecord
    ) -> None:
        """Save one observable Mission event."""
        ...

    async def list_events(
        self, workspace_id: str, mission_id: str
    ) -> list[MissionEventRecord]:
        """Load every observable event for one Mission."""
        ...


class FirestoreMissionStore:
    """Stores product-facing Workspace, Mission, and safe event records."""

    def __init__(self, client: Any) -> None:
        """Use the supplied named-database Firestore client for all records."""
        self.client = client

    def _workspace_ref(self, workspace_id: str):
        """Point to one Workspace document."""
        return self.client.collection("workspaces").document(workspace_id)

    def _mission_ref(self, workspace_id: str, mission_id: str):
        """Point to one Mission inside its Workspace."""
        return (
            self._workspace_ref(workspace_id)
            .collection("missions")
            .document(mission_id)
        )

    def _event_ref(self, workspace_id: str, mission_id: str, event_id: str):
        """Point to one observable event inside its Mission."""
        return (
            self._mission_ref(workspace_id, mission_id)
            .collection("events")
            .document(event_id)
        )

    async def create_workspace(self, workspace: WorkspaceRecord) -> None:
        """Save a newly created Workspace."""
        await self._workspace_ref(workspace.workspace_id).create(
            workspace.model_dump(mode="python")
        )

    async def get_workspace(self, workspace_id: str) -> WorkspaceRecord | None:
        """Load one Workspace, returning nothing when it does not exist."""
        snapshot = await self._workspace_ref(workspace_id).get()
        if not snapshot.exists:
            return None
        return WorkspaceRecord.model_validate(snapshot.to_dict())

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        """Load all Workspaces in creation order."""
        snapshots = await self.client.collection("workspaces").order_by(
            "created_at"
        ).get()
        return [WorkspaceRecord.model_validate(item.to_dict()) for item in snapshots]

    async def create_mission(
        self, mission: MissionRecord, event: MissionEventRecord
    ) -> None:
        """Save a Mission and its creation event together."""
        batch = self.client.batch()
        batch.set(
            self._mission_ref(mission.workspace_id, mission.mission_id),
            mission.model_dump(mode="python"),
        )
        batch.set(
            self._event_ref(mission.workspace_id, mission.mission_id, event.event_id),
            event.model_dump(mode="python"),
        )
        await batch.commit()

    async def get_mission(
        self, workspace_id: str, mission_id: str
    ) -> MissionRecord | None:
        """Load one Mission from its Workspace."""
        snapshot = await self._mission_ref(workspace_id, mission_id).get()
        if not snapshot.exists:
            return None
        return MissionRecord.model_validate(snapshot.to_dict())

    async def list_missions(self, workspace_id: str) -> list[MissionRecord]:
        """Load every Mission in a Workspace in creation order."""
        snapshots = await (
            self._workspace_ref(workspace_id)
            .collection("missions")
            .order_by("created_at")
            .get()
        )
        return [MissionRecord.model_validate(item.to_dict()) for item in snapshots]

    async def transition_mission(
        self,
        workspace_id: str,
        mission_id: str,
        allowed_statuses: set[str],
        changes: dict[str, Any],
        event: MissionEventRecord,
    ) -> MissionRecord:
        """Change Mission status and record the matching event atomically."""
        from google.cloud import firestore

        mission_ref = self._mission_ref(workspace_id, mission_id)
        event_ref = self._event_ref(workspace_id, mission_id, event.event_id)

        @firestore.async_transactional
        async def update(transaction):
            """Reject stale status changes before writing the state and event."""
            snapshot = await mission_ref.get(transaction=transaction)
            if not snapshot.exists:
                raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
            current = MissionRecord.model_validate(snapshot.to_dict())
            if current.status not in allowed_statuses:
                raise MissionError(
                    "INVALID_MISSION_STATUS",
                    f"The Mission cannot perform this action while {current.status}.",
                    409,
                )
            transaction.update(mission_ref, changes)
            transaction.set(event_ref, event.model_dump(mode="python"))
            return MissionRecord.model_validate(
                {**current.model_dump(mode="python"), **changes}
            )

        return await update(self.client.transaction())

    async def append_event(
        self, workspace_id: str, event: MissionEventRecord
    ) -> None:
        """Save one safe event produced during agent execution."""
        await self._event_ref(workspace_id, event.mission_id, event.event_id).set(
            event.model_dump(mode="python")
        )

    async def list_events(
        self, workspace_id: str, mission_id: str
    ) -> list[MissionEventRecord]:
        """Load the visible Mission activity in time order."""
        snapshots = await (
            self._mission_ref(workspace_id, mission_id)
            .collection("events")
            .order_by("created_at")
            .get()
        )
        return [MissionEventRecord.model_validate(item.to_dict()) for item in snapshots]


def _now() -> datetime:
    """Return one timezone-aware timestamp for Firestore records."""
    return datetime.now(timezone.utc)


def _safe_value(value: Any) -> Any:
    """Keep event payloads JSON-compatible without copying model thoughts."""
    return json.loads(json.dumps(value, default=str))


def _event(
    mission_id: str,
    event_type: str,
    *,
    agent: str | None = None,
    tool: str | None = None,
    payload: dict[str, Any] | None = None,
    source_event_id: str | None = None,
    created_at: datetime | None = None,
    event_id: str | None = None,
) -> MissionEventRecord:
    """Build one consistently shaped frontend-safe Mission event."""
    return MissionEventRecord(
        event_id=event_id or f"evt_{uuid4().hex}",
        mission_id=mission_id,
        type=event_type,
        agent=agent,
        tool=tool,
        payload=_safe_value(payload or {}),
        source_event_id=source_event_id,
        created_at=created_at or _now(),
    )


class MissionService:
    """Runs the real Workspace and Mission lifecycle."""

    def __init__(
        self,
        store: MissionStore,
        session_service: BaseSessionService,
        runner: Any,
        data_source_service: DataSourceService,
    ) -> None:
        """Combine storage, ADK sessions, agent execution, and source access."""
        self.store = store
        self.session_service = session_service
        self.runner = runner
        self.data_source_service = data_source_service

    async def create_workspace(self, request: WorkspaceCreate) -> WorkspaceRecord:
        """Create the container that owns sources and Missions."""
        name = request.name.strip()
        if not name:
            raise MissionError("INVALID_WORKSPACE_NAME", "Workspace name is required.")
        timestamp = _now()
        workspace = WorkspaceRecord(
            workspace_id=f"ws_{uuid4().hex}",
            name=name,
            created_at=timestamp,
            updated_at=timestamp,
        )
        try:
            await self.store.create_workspace(workspace)
        except MissionError:
            raise
        except Exception as error:
            raise MissionError(
                "WORKSPACE_UNAVAILABLE", "The Workspace could not be created.", 503
            ) from error
        return workspace

    async def require_workspace(self, workspace_id: str) -> WorkspaceRecord:
        """Load a Workspace or return a clear not-found error."""
        try:
            workspace = await self.store.get_workspace(workspace_id)
        except Exception as error:
            raise MissionError(
                "WORKSPACE_UNAVAILABLE", "The Workspace could not be loaded.", 503
            ) from error
        if workspace is None:
            raise MissionError("WORKSPACE_NOT_FOUND", "The Workspace was not found.", 404)
        return workspace

    async def list_workspaces(self) -> list[WorkspaceRecord]:
        """Return every existing Workspace."""
        try:
            return await self.store.list_workspaces()
        except Exception as error:
            raise MissionError(
                "WORKSPACE_UNAVAILABLE", "Workspaces could not be listed.", 503
            ) from error

    async def create_mission(
        self, workspace_id: str, request: MissionCreate
    ) -> MissionRecord:
        """Create initial Mission state and its persistent ADK session."""
        await self.require_workspace(workspace_id)
        objective = request.objective.strip()
        if not objective:
            raise MissionError("INVALID_OBJECTIVE", "A Mission objective is required.")

        try:
            sources = await asyncio.to_thread(
                self.data_source_service.list_sources, workspace_id
            )
        except Exception as error:
            raise MissionError(
                "SOURCE_UNAVAILABLE", "Connected sources could not be loaded.", 503
            ) from error
        connected = {source.source_id: source for source in sources if source.status == "connected"}
        requested_ids = list(dict.fromkeys(request.source_ids))
        authorized_ids = requested_ids or list(connected)
        if not authorized_ids:
            raise MissionError(
                "MISSION_HAS_NO_SOURCES",
                "Connect at least one data source before creating a Mission.",
                409,
            )
        invalid_ids = [source_id for source_id in authorized_ids if source_id not in connected]
        if invalid_ids:
            raise MissionError(
                "SOURCE_NOT_AUTHORIZED",
                "Every selected source must be connected to this Workspace.",
                403,
            )

        mission_id = f"msn_{uuid4().hex}"
        timestamp = _now()
        mission = MissionRecord(
            mission_id=mission_id,
            workspace_id=workspace_id,
            adk_session_id=mission_id,
            objective=objective,
            authorized_source_ids=authorized_ids,
            status="created",
            created_at=timestamp,
            updated_at=timestamp,
        )
        session_state = {
            "workspace_id": workspace_id,
            "mission_id": mission_id,
            "objective": objective,
            "authorized_source_ids": authorized_ids,
            "mission_status": "created",
        }
        try:
            await self.session_service.create_session(
                app_name=APP_NAME,
                user_id=workspace_id,
                session_id=mission_id,
                state=session_state,
            )
            await self.store.create_mission(
                mission,
                _event(
                    mission_id,
                    "mission_created",
                    payload={"authorized_source_ids": authorized_ids},
                    created_at=timestamp,
                ),
            )
        except MissionError:
            await self._delete_session_quietly(workspace_id, mission_id)
            raise
        except Exception as error:
            await self._delete_session_quietly(workspace_id, mission_id)
            raise MissionError(
                "MISSION_UNAVAILABLE", "The Mission could not be created.", 503
            ) from error
        return mission

    async def _delete_session_quietly(self, workspace_id: str, session_id: str) -> None:
        """Remove a partially created session when Mission creation fails."""
        try:
            await self.session_service.delete_session(
                app_name=APP_NAME, user_id=workspace_id, session_id=session_id
            )
        except Exception:
            pass

    async def require_mission(
        self, workspace_id: str, mission_id: str
    ) -> MissionRecord:
        """Load a Mission only from the Workspace that owns it."""
        await self.require_workspace(workspace_id)
        try:
            mission = await self.store.get_mission(workspace_id, mission_id)
        except Exception as error:
            raise MissionError(
                "MISSION_UNAVAILABLE", "The Mission could not be loaded.", 503
            ) from error
        if mission is None:
            raise MissionError("MISSION_NOT_FOUND", "The Mission was not found.", 404)
        return mission

    async def list_missions(self, workspace_id: str) -> list[MissionRecord]:
        """Return all Missions belonging to one Workspace."""
        await self.require_workspace(workspace_id)
        try:
            return await self.store.list_missions(workspace_id)
        except Exception as error:
            raise MissionError(
                "MISSION_UNAVAILABLE", "Missions could not be listed.", 503
            ) from error

    async def list_events(
        self, workspace_id: str, mission_id: str
    ) -> list[MissionEventRecord]:
        """Return safe activity records for one existing Mission."""
        await self.require_mission(workspace_id, mission_id)
        try:
            return await self.store.list_events(workspace_id, mission_id)
        except Exception as error:
            raise MissionError(
                "MISSION_UNAVAILABLE", "Mission events could not be listed.", 503
            ) from error

    async def load_for_tool(
        self, workspace_id: str, mission_id: str, session_id: str
    ) -> MissionRecord:
        """Ensure a manager tool is acting on its own Mission and session."""
        mission = await self.require_mission(workspace_id, mission_id)
        if mission.adk_session_id != session_id:
            raise MissionError(
                "MISSION_ACCESS_DENIED",
                "The current ADK session cannot access this Mission.",
                403,
            )
        return mission

    async def request_clarification(
        self,
        workspace_id: str,
        mission_id: str,
        session_id: str,
        question: str,
        reason: str,
    ) -> MissionRecord:
        """Save one essential question and pause the Mission."""
        await self.load_for_tool(workspace_id, mission_id, session_id)
        question = question.strip()
        reason = reason.strip()
        if not question or not reason:
            raise MissionError(
                "INVALID_CLARIFICATION", "Question and reason are required."
            )
        timestamp = _now()
        clarification = ClarificationState(
            question=question,
            reason=reason,
            status="open",
            requested_at=timestamp,
        )
        return await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"running"},
            {
                "status": "awaiting_input",
                "clarification": clarification.model_dump(mode="python"),
                "updated_at": timestamp,
            },
            _event(
                mission_id,
                "clarification_requested",
                agent="mission_manager",
                tool="request_clarification",
                payload={"question": question, "reason": reason},
                created_at=timestamp,
            ),
        )

    async def publish_plan(
        self,
        workspace_id: str,
        mission_id: str,
        session_id: str,
        mission_name: str,
        summary: str,
        plan: dict[str, Any],
    ) -> MissionRecord:
        """Save the manager's final named plan and complete the Mission."""
        await self.load_for_tool(workspace_id, mission_id, session_id)
        mission_name = mission_name.strip()
        summary = summary.strip()
        if not mission_name or not summary or not isinstance(plan, dict) or not plan:
            raise MissionError(
                "INVALID_PLAN", "Mission name, summary, and a non-empty plan are required."
            )
        timestamp = _now()
        return await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"running"},
            {
                "status": "completed",
                "name": mission_name,
                "summary": summary,
                "plan": plan,
                "error": None,
                "updated_at": timestamp,
                "completed_at": timestamp,
            },
            _event(
                mission_id,
                "plan_published",
                agent="mission_manager",
                tool="publish_plan",
                payload={"mission_name": mission_name, "summary": summary},
                created_at=timestamp,
            ),
        )

    async def run_mission(self, workspace_id: str, mission_id: str) -> MissionRecord:
        """Start agent planning for a newly created Mission."""
        mission = await self.require_mission(workspace_id, mission_id)
        timestamp = _now()
        mission = await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"created"},
            {"status": "running", "started_at": timestamp, "updated_at": timestamp},
            _event(mission_id, "mission_started", created_at=timestamp),
        )
        return await self._run_agent(mission, mission.objective)

    async def respond_to_clarification(
        self, workspace_id: str, mission_id: str, response: ClarificationResponse
    ) -> MissionRecord:
        """Save the user's answer and continue the same ADK session."""
        mission = await self.require_mission(workspace_id, mission_id)
        answer = response.answer.strip()
        if not answer:
            raise MissionError("INVALID_RESPONSE", "A clarification answer is required.")
        if mission.clarification is None or mission.clarification.status != "open":
            raise MissionError(
                "NO_OPEN_CLARIFICATION", "The Mission has no open clarification.", 409
            )
        timestamp = _now()
        clarification = mission.clarification.model_copy(
            update={"status": "answered", "answer": answer, "answered_at": timestamp}
        )
        mission = await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"awaiting_input"},
            {
                "status": "running",
                "clarification": clarification.model_dump(mode="python"),
                "updated_at": timestamp,
            },
            _event(
                mission_id,
                "clarification_answered",
                payload={"answer": answer},
                created_at=timestamp,
            ),
        )
        return await self._run_agent(
            mission, f"Answer to your pending clarification: {answer}"
        )

    async def _run_agent(self, mission: MissionRecord, message: str) -> MissionRecord:
        """Run the manager until it completes, pauses, or fails."""
        try:
            content = types.Content(
                role="user", parts=[types.Part.from_text(text=message)]
            )
            async for adk_event in self.runner.run_async(
                user_id=mission.workspace_id,
                session_id=mission.adk_session_id,
                new_message=content,
            ):
                await self._record_adk_event(mission, adk_event)
        except Exception as error:
            logger.exception("Mission Manager run failed for %s", mission.mission_id)
            return await self._fail_running_mission(
                mission, "The Mission Manager run failed."
            )

        current = await self.require_mission(mission.workspace_id, mission.mission_id)
        if current.status == "running":
            current = await self._fail_running_mission(
                current,
                "The Mission Manager stopped without publishing a plan or requesting input.",
            )
        return current

    async def _fail_running_mission(
        self, mission: MissionRecord, message: str
    ) -> MissionRecord:
        """Mark only a still-running Mission as failed."""
        timestamp = _now()
        try:
            return await self.store.transition_mission(
                mission.workspace_id,
                mission.mission_id,
                {"running"},
                {"status": "failed", "error": message, "updated_at": timestamp},
                _event(
                    mission.mission_id,
                    "mission_failed",
                    payload={"message": message},
                    created_at=timestamp,
                ),
            )
        except MissionError:
            return await self.require_mission(mission.workspace_id, mission.mission_id)

    async def _record_adk_event(self, mission: MissionRecord, event: Event) -> None:
        """Copy safe ADK actions and results without copying hidden thoughts."""
        timestamp = datetime.fromtimestamp(event.timestamp, tz=timezone.utc)
        index = 0
        for call in event.get_function_calls():
            await self.store.append_event(
                mission.workspace_id,
                _event(
                    mission.mission_id,
                    "tool_called",
                    agent=event.author or None,
                    tool=call.name,
                    payload={"arguments": _safe_value(call.args or {})},
                    source_event_id=event.id,
                    created_at=timestamp,
                    event_id=f"evt_adk_{event.id}_{index}",
                ),
            )
            index += 1
        for response in event.get_function_responses():
            await self.store.append_event(
                mission.workspace_id,
                _event(
                    mission.mission_id,
                    "tool_result",
                    agent=event.author or None,
                    tool=response.name,
                    payload={"result": _safe_value(response.response or {})},
                    source_event_id=event.id,
                    created_at=timestamp,
                    event_id=f"evt_adk_{event.id}_{index}",
                ),
            )
            index += 1
        visible_text: list[str] = []
        if event.content and event.content.parts:
            visible_text = [
                part.text
                for part in event.content.parts
                if part.text and not getattr(part, "thought", False)
            ]
        if visible_text:
            await self.store.append_event(
                mission.workspace_id,
                _event(
                    mission.mission_id,
                    "agent_message",
                    agent=event.author or None,
                    payload={"text": "".join(visible_text)},
                    source_event_id=event.id,
                    created_at=timestamp,
                    event_id=f"evt_adk_{event.id}_{index}",
                ),
            )
            index += 1
        if event.actions.state_delta:
            await self.store.append_event(
                mission.workspace_id,
                _event(
                    mission.mission_id,
                    "state_changed",
                    agent=event.author or None,
                    payload={"changes": _safe_value(event.actions.state_delta)},
                    source_event_id=event.id,
                    created_at=timestamp,
                    event_id=f"evt_adk_{event.id}_{index}",
                ),
            )


_mission_service: MissionService | None = None


def build_mission_service_from_environment() -> MissionService:
    """Build production services connected to the configured Firestore database."""
    from google.adk.integrations.firestore.firestore_session_service import (
        FirestoreSessionService,
    )
    from google.cloud import firestore

    from .agent import root_agent

    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "geoagent-hackathon")
    database_id = os.getenv("FIRESTORE_DATABASE_ID", "geoagentdb")
    client = firestore.AsyncClient(project=project_id, database=database_id)
    session_service = FirestoreSessionService(client=client)
    runner = Runner(
        agent=root_agent,
        app_name=APP_NAME,
        session_service=session_service,
    )
    return MissionService(
        store=FirestoreMissionStore(client),
        session_service=session_service,
        runner=runner,
        data_source_service=get_data_source_service(),
    )


def get_mission_service() -> MissionService:
    """Reuse one production Mission service across API requests and agent tools."""
    global _mission_service
    if _mission_service is None:
        _mission_service = build_mission_service_from_environment()
    return _mission_service


def configure_mission_service(service: MissionService | None) -> None:
    """Replace the shared service for isolated tests."""
    global _mission_service
    _mission_service = service


__all__ = [
    "ClarificationResponse",
    "MissionCreate",
    "MissionError",
    "MissionEventListResponse",
    "MissionEventRecord",
    "MissionListResponse",
    "MissionRecord",
    "MissionService",
    "WorkspaceCreate",
    "WorkspaceListResponse",
    "WorkspaceRecord",
    "configure_mission_service",
    "get_mission_service",
]
