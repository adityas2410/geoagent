"""The three tools owned by the Mission Manager."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext

from .missions import MissionError
from .missions import get_mission_service


def _error(error: MissionError) -> dict[str, Any]:
    """Return tool errors in a predictable shape the manager can understand."""
    return {
        "status": "error",
        "error": {"code": error.code, "message": error.message},
    }


def _identity(tool_context: ToolContext) -> tuple[str, str, str]:
    """Read the Mission identity set by the backend, never by the model."""
    workspace_id = tool_context.state.get("workspace_id")
    mission_id = tool_context.state.get("mission_id")
    session_id = getattr(getattr(tool_context, "session", None), "id", None)
    user_id = getattr(tool_context, "user_id", None)
    if not all(
        isinstance(value, str) and value.strip()
        for value in (workspace_id, mission_id, session_id)
    ):
        raise MissionError("MISSION_CONTEXT_MISSING", "Mission identity is missing.", 403)
    if user_id is not None and user_id != workspace_id:
        raise MissionError(
            "MISSION_ACCESS_DENIED", "The current session cannot access this Mission.", 403
        )
    return workspace_id, mission_id, session_id


async def load_mission_state(tool_context: ToolContext) -> dict[str, Any]:
    """Load the current Mission's objective, status, clarification, and plan."""
    try:
        workspace_id, mission_id, session_id = _identity(tool_context)
        mission = await get_mission_service().load_for_tool(
            workspace_id, mission_id, session_id
        )
        return {
            "status": "success",
            "mission": mission.model_dump(mode="json"),
        }
    except MissionError as error:
        return _error(error)
    except Exception:
        return _error(
            MissionError("MISSION_UNAVAILABLE", "The Mission could not be loaded.", 503)
        )


async def request_clarification(
    question: str,
    reason: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Save one open-ended question and end the current manager run.

    Args:
        question: The concise question shown to the user.
        reason: Why the answer is required to finish planning.
    """
    try:
        workspace_id, mission_id, session_id = _identity(tool_context)
        mission = await get_mission_service().request_clarification(
            workspace_id,
            mission_id,
            session_id,
            question,
            reason,
        )
        tool_context.state["mission_status"] = "awaiting_input"
        tool_context.state["clarification_question"] = question.strip()
        # ADK treats this function response as the final event of this run.
        tool_context.actions.skip_summarization = True
        return {
            "status": mission.status,
            "mission_id": mission.mission_id,
            "question": question.strip(),
        }
    except MissionError as error:
        return _error(error)
    except Exception:
        return _error(
            MissionError(
                "MISSION_UNAVAILABLE", "The clarification could not be saved.", 503
            )
        )


async def request_objective_decision(
    proposed_objective: str,
    reason: str,
    hard_violations: list[dict[str, Any]],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Pause an infeasible Mission with one achievable replacement objective.

    Args:
        proposed_objective: One achievable objective the user may accept.
        reason: Concise explanation of why the current objective is infeasible.
        hard_violations: Deterministic violations that prevent publication.
    """
    try:
        workspace_id, mission_id, session_id = _identity(tool_context)
        mission = await get_mission_service().request_objective_decision(
            workspace_id,
            mission_id,
            session_id,
            proposed_objective,
            reason,
            hard_violations,
        )
        tool_context.state["mission_status"] = "awaiting_objective_decision"
        tool_context.state["proposed_objective"] = proposed_objective.strip()
        tool_context.actions.skip_summarization = True
        return {
            "status": mission.status,
            "mission_id": mission.mission_id,
            "proposed_objective": proposed_objective.strip(),
        }
    except MissionError as error:
        return _error(error)
    except Exception:
        return _error(
            MissionError(
                "MISSION_UNAVAILABLE",
                "The objective decision could not be saved.",
                503,
            )
        )


async def publish_plan(
    mission_name: str,
    summary: str,
    plan: dict[str, Any],
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Save the Mission name, summary, and validated final plan.

    Args:
        mission_name: Short name generated for this Mission.
        summary: Concise explanation of the final plan.
        plan: Complete operational plan supported by specialist findings.
    """
    try:
        workspace_id, mission_id, session_id = _identity(tool_context)
        mission = await get_mission_service().publish_plan(
            workspace_id,
            mission_id,
            session_id,
            mission_name,
            summary,
            plan,
        )
        tool_context.state["mission_status"] = "completed"
        # The published plan is the authoritative final result. Do not spend
        # another model call summarizing it after the Mission is complete.
        tool_context.actions.skip_summarization = True
        return {
            "status": mission.status,
            "mission_id": mission.mission_id,
            "mission_name": mission.name,
        }
    except MissionError as error:
        return _error(error)
    except Exception:
        return _error(
            MissionError("MISSION_UNAVAILABLE", "The plan could not be saved.", 503)
        )


__all__ = [
    "load_mission_state",
    "publish_plan",
    "request_clarification",
    "request_objective_decision",
]
