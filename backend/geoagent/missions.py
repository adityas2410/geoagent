"""Workspace, Mission, ADK session, and Mission event handling."""

from __future__ import annotations

import asyncio
import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal, Protocol
from uuid import uuid4

from google.adk.agents.run_config import RunConfig
from google.adk.events.event import Event
from google.adk.runners import Runner
from google.adk.sessions.base_session_service import BaseSessionService
from google.genai import types
from pydantic import BaseModel, Field

from .data_sources.source_manager import DataSourceService
from .data_sources.source_manager import get_data_source_service


APP_NAME = "geoagent"
DEFAULT_MAX_LLM_CALLS = 30
SPECIALIST_AGENT_NAMES = frozenset(
    {
        "organizational_data_agent",
        "geospatial_intelligence_agent",
        "planning_validation_agent",
    }
)


@dataclass
class _SpecialistDelegation:
    """One manager-to-specialist call being projected into Mission events."""

    specialist: str
    delegation_id: str
    started: bool = False
    completed: bool = False


@dataclass
class _AgentActivityProjection:
    """Run-local state used to emit each specialist lifecycle event once."""

    delegations: list[_SpecialistDelegation] = field(default_factory=list)
    processed_event_ids: set[str] = field(default_factory=set)

    def begin_event(self, event_id: str) -> bool:
        if event_id in self.processed_event_ids:
            return False
        self.processed_event_ids.add(event_id)
        return True

    def add(self, specialist: str, delegation_id: str) -> _SpecialistDelegation:
        for delegation in self.delegations:
            if delegation.delegation_id == delegation_id:
                return delegation
        delegation = _SpecialistDelegation(specialist, delegation_id)
        self.delegations.append(delegation)
        return delegation

    def active(self, specialist: str) -> _SpecialistDelegation | None:
        for delegation in self.delegations:
            if (
                delegation.specialist == specialist
                and delegation.started
                and not delegation.completed
            ):
                return delegation
        for delegation in self.delegations:
            if delegation.specialist == specialist and not delegation.completed:
                return delegation
        return None

    def matching_response(
        self, specialist: str, delegation_id: str | None
    ) -> _SpecialistDelegation | None:
        if delegation_id:
            for delegation in self.delegations:
                if (
                    delegation.specialist == specialist
                    and delegation.delegation_id == delegation_id
                ):
                    return delegation
        return self.active(specialist)
MissionStatus = Literal[
    "created",
    "running",
    "awaiting_input",
    "awaiting_objective_decision",
    "completed",
    "failed",
]
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


class ObjectiveHistoryEntry(BaseModel):
    """One user-accepted replacement of an infeasible objective."""

    objective: str
    replaced_at: datetime
    reason: str


class ObjectiveDecisionState(BaseModel):
    """The Manager's single proposed objective awaiting user choice."""

    proposed_objective: str
    reason: str
    hard_violations: list[dict[str, Any]] = Field(default_factory=list)
    status: Literal["pending", "accepted"] = "pending"
    requested_at: datetime
    accepted_at: datetime | None = None


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
    objective_history: list[ObjectiveHistoryEntry] = Field(default_factory=list)
    objective_decision: ObjectiveDecisionState | None = None
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

    async def delete_mission(self, workspace_id: str, mission_id: str) -> None:
        """Permanently delete one Mission and its subcollections."""
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

    async def delete_mission(self, workspace_id: str, mission_id: str) -> None:
        """Delete the Mission document and all event subcollection documents."""
        await self.client.recursive_delete(self._mission_ref(workspace_id, mission_id))

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
        max_llm_calls: int = DEFAULT_MAX_LLM_CALLS,
    ) -> None:
        """Combine storage, ADK sessions, agent execution, and source access."""
        if max_llm_calls <= 0:
            raise ValueError("max_llm_calls must be positive")
        self.store = store
        self.session_service = session_service
        self.runner = runner
        self.data_source_service = data_source_service
        self.run_config = RunConfig(max_llm_calls=max_llm_calls)

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
        mission = await self.load_for_tool(workspace_id, mission_id, session_id)
        if mission.clarification is not None:
            raise MissionError(
                "CLARIFICATION_ALREADY_USED",
                "The Mission's initial clarification has already been used.",
                409,
            )
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

    async def request_objective_decision(
        self,
        workspace_id: str,
        mission_id: str,
        session_id: str,
        proposed_objective: str,
        reason: str,
        hard_violations: list[dict[str, Any]],
    ) -> MissionRecord:
        """Stop infeasible planning and wait for an explicit user decision."""
        mission = await self.load_for_tool(workspace_id, mission_id, session_id)
        proposed_objective = proposed_objective.strip()
        reason = reason.strip()
        if not proposed_objective or not reason:
            raise MissionError(
                "INVALID_OBJECTIVE_DECISION",
                "A proposed objective and reason are required.",
            )
        if proposed_objective.casefold() == mission.objective.casefold():
            raise MissionError(
                "OBJECTIVE_NOT_REFINED",
                "The proposed objective must differ from the infeasible objective.",
            )
        timestamp = _now()
        decision = ObjectiveDecisionState(
            proposed_objective=proposed_objective,
            reason=reason,
            hard_violations=hard_violations,
            requested_at=timestamp,
        )
        return await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"running"},
            {
                "status": "awaiting_objective_decision",
                "objective_decision": decision.model_dump(mode="python"),
                "summary": reason,
                "updated_at": timestamp,
            },
            _event(
                mission_id,
                "objective_decision_requested",
                agent="mission_manager",
                tool="request_objective_decision",
                payload={
                    "proposed_objective": proposed_objective,
                    "reason": reason,
                    "hard_violations": hard_violations,
                },
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
            mission,
            (
                f"Answer to the one initial objective clarification: {answer}. "
                "Combine this with the original objective, do not ask another "
                "clarification question, and continue specialist delegation."
            ),
        )

    async def accept_objective_decision(
        self, workspace_id: str, mission_id: str
    ) -> MissionRecord:
        """Accept the proposed objective and start exactly one new planning attempt."""
        mission = await self.require_mission(workspace_id, mission_id)
        decision = mission.objective_decision
        if (
            mission.status != "awaiting_objective_decision"
            or decision is None
            or decision.status != "pending"
        ):
            raise MissionError(
                "NO_OBJECTIVE_DECISION",
                "The Mission has no proposed objective awaiting acceptance.",
                409,
            )
        timestamp = _now()
        accepted = decision.model_copy(
            update={"status": "accepted", "accepted_at": timestamp}
        )
        history = [
            *mission.objective_history,
            ObjectiveHistoryEntry(
                objective=mission.objective,
                replaced_at=timestamp,
                reason=decision.reason,
            ),
        ]
        mission = await self.store.transition_mission(
            workspace_id,
            mission_id,
            {"awaiting_objective_decision"},
            {
                "status": "running",
                "objective": decision.proposed_objective,
                "objective_history": [item.model_dump(mode="python") for item in history],
                "objective_decision": accepted.model_dump(mode="python"),
                "summary": None,
                "updated_at": timestamp,
            },
            _event(
                mission_id,
                "objective_revision_accepted",
                payload={
                    "previous_objective": mission.objective,
                    "objective": decision.proposed_objective,
                },
                created_at=timestamp,
            ),
        )
        return await self._run_agent(
            mission,
            (
                "The user accepted this revised objective: "
                f"{decision.proposed_objective}. Start one new planning attempt. "
                "If it is also infeasible, stop and request another explicit "
                "objective decision; never retry automatically."
            ),
        )

    async def discard_objective_decision(
        self, workspace_id: str, mission_id: str
    ) -> None:
        """Permanently delete a Mission after the user rejects its replacement."""
        mission = await self.require_mission(workspace_id, mission_id)
        if mission.status != "awaiting_objective_decision":
            raise MissionError(
                "NO_OBJECTIVE_DECISION",
                "Only a Mission awaiting an objective decision can be discarded.",
                409,
            )
        try:
            await self.store.delete_mission(workspace_id, mission_id)
        except Exception as error:
            raise MissionError(
                "MISSION_UNAVAILABLE", "The Mission could not be discarded.", 503
            ) from error
        await self._delete_session_quietly(workspace_id, mission.adk_session_id)

    async def _run_agent(self, mission: MissionRecord, message: str) -> MissionRecord:
        """Run the manager until it completes, pauses, or fails."""
        activity = _AgentActivityProjection()
        try:
            content = types.Content(
                role="user", parts=[types.Part.from_text(text=message)]
            )
            async for adk_event in self.runner.run_async(
                user_id=mission.workspace_id,
                session_id=mission.adk_session_id,
                new_message=content,
                run_config=self.run_config,
            ):
                await self._record_adk_event(mission, adk_event, activity)
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

    async def _record_adk_event(
        self,
        mission: MissionRecord,
        event: Event,
        activity: _AgentActivityProjection | None = None,
    ) -> None:
        """Copy safe ADK actions and results without copying hidden thoughts."""
        activity = activity or _AgentActivityProjection()
        if not activity.begin_event(event.id):
            return
        timestamp = datetime.fromtimestamp(event.timestamp, tz=timezone.utc)
        index = 0

        async def append_projected_event(
            event_type: str,
            *,
            agent: str | None = None,
            tool: str | None = None,
            payload: dict[str, Any] | None = None,
        ) -> None:
            nonlocal index
            safe_payload = payload or {}
            await self.store.append_event(
                mission.workspace_id,
                _event(
                    mission.mission_id,
                    event_type,
                    agent=agent,
                    tool=tool,
                    payload=safe_payload,
                    source_event_id=event.id,
                    created_at=timestamp,
                    event_id=f"evt_adk_{event.id}_{index}",
                ),
            )
            logger.info(
                "Mission event mission=%s type=%s agent=%s tool=%s",
                mission.mission_id,
                event_type,
                agent or "-",
                tool or "-",
            )
            logger.debug("Mission event payload=%s", safe_payload)
            index += 1

        specialist_author = event.author if event.author in SPECIALIST_AGENT_NAMES else None
        if not specialist_author and event.node_info.name in SPECIALIST_AGENT_NAMES:
            specialist_author = event.node_info.name
        if specialist_author:
            delegation = activity.active(specialist_author)
            if delegation and not delegation.started:
                delegation.started = True
                await append_projected_event(
                    "specialist_started",
                    agent=specialist_author,
                    payload={
                        "specialist": specialist_author,
                        "delegation_id": delegation.delegation_id,
                    },
                )

        for call in event.get_function_calls():
            if call.name in SPECIALIST_AGENT_NAMES:
                delegation_id = call.id or f"{event.id}:{index}"
                activity.add(call.name, delegation_id)
                await append_projected_event(
                    "task_delegated",
                    agent=event.author or "mission_manager",
                    tool=call.name,
                    payload={
                        "specialist": call.name,
                        "delegation_id": delegation_id,
                        "request": _safe_value(call.args or {}),
                    },
                )
            else:
                await append_projected_event(
                    "tool_called",
                    agent=event.author or None,
                    tool=call.name,
                    payload={"arguments": _safe_value(call.args or {})},
                )

        for response in event.get_function_responses():
            result = _safe_value(
                response.response if response.response is not None else {}
            )
            if response.name in SPECIALIST_AGENT_NAMES:
                delegation = activity.matching_response(response.name, response.id)
                if delegation:
                    if not delegation.started:
                        delegation.started = True
                        await append_projected_event(
                            "specialist_started",
                            agent=response.name,
                            payload={
                                "specialist": response.name,
                                "delegation_id": delegation.delegation_id,
                            },
                        )
                    is_error = (
                        isinstance(result, str)
                        and result.startswith(
                            ("Error running sub-agent:", "Error validating input:")
                        )
                    ) or (
                        isinstance(result, dict) and result.get("status") == "error"
                    )
                    delegation.completed = True
                    await append_projected_event(
                        "specialist_completed",
                        agent=response.name,
                        payload={
                            "specialist": response.name,
                            "delegation_id": delegation.delegation_id,
                            "status": "error" if is_error else "success",
                            "result": result,
                        },
                    )
                else:
                    await append_projected_event(
                        "tool_result",
                        agent=event.author or None,
                        tool=response.name,
                        payload={"result": result},
                    )
            else:
                await append_projected_event(
                    "tool_result",
                    agent=event.author or None,
                    tool=response.name,
                    payload={"result": result},
                )

        visible_text: list[str] = []
        if event.content and event.content.parts:
            visible_text = [
                part.text
                for part in event.content.parts
                if part.text and not getattr(part, "thought", False)
            ]
        if visible_text:
            await append_projected_event(
                "agent_message",
                agent=event.author or None,
                payload={"text": "".join(visible_text)},
            )
        if event.actions.state_delta:
            await append_projected_event(
                "state_changed",
                agent=event.author or None,
                payload={"changes": _safe_value(event.actions.state_delta)},
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
    try:
        max_llm_calls = int(
            os.getenv("GEOAGENT_MAX_LLM_CALLS", str(DEFAULT_MAX_LLM_CALLS))
        )
    except ValueError as error:
        raise RuntimeError("GEOAGENT_MAX_LLM_CALLS must be an integer") from error
    if max_llm_calls <= 0:
        raise RuntimeError("GEOAGENT_MAX_LLM_CALLS must be positive")
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
        max_llm_calls=max_llm_calls,
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
