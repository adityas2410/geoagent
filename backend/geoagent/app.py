"""FastAPI service for GeoAgent."""

from __future__ import annotations

import os
import tempfile
from pathlib import Path
from typing import Any

from .observability import configure_observability


configure_observability()

from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import Response
from fastapi import UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel
from starlette.middleware.cors import CORSMiddleware
from starlette.concurrency import run_in_threadpool

from .data_sources.data_source_contracts import DataSourceError
from .data_sources.source_manager import DataSourceService
from .data_sources.source_manager import get_data_source_service
from .missions import ClarificationResponse
from .missions import MissionCreate
from .missions import MissionError
from .missions import MissionEventListResponse
from .missions import MissionListResponse
from .missions import MissionMapResponse
from .missions import MissionRecord
from .missions import MissionService
from .missions import WorkspaceCreate
from .missions import WorkspaceDelete
from .missions import WorkspaceListResponse
from .missions import WorkspaceMapResponse
from .missions import WorkspaceRecord
from .missions import get_mission_service
from .workspace_qa import WorkspaceQuestionAnswer
from .workspace_qa import WorkspaceQuestionRequest
from .workspace_qa import WorkspaceQuestionService
from .workspace_qa import get_workspace_question_service


app = FastAPI(title="GeoAgent API", version="0.1.0")


def _cors_origins_from_environment() -> list[str]:
    """Read an explicit, comma-separated browser-origin allow list."""
    configured = os.getenv("GEOAGENT_CORS_ORIGINS", "http://localhost:5173")
    return [origin.strip() for origin in configured.split(",") if origin.strip()]


app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins_from_environment(),
    allow_credentials=False,
    allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type"],
)


class DataSourceResponse(BaseModel):
    source_id: str
    name: str
    source_type: str
    status: str
    provenance: str
    original_filename: str
    size_bytes: int
    table_count: int
    view_count: int
    created_at: str


class DataSourceListResponse(BaseModel):
    sources: list[DataSourceResponse]


def data_source_service_dependency() -> DataSourceService:
    """Provide the shared organizational data service to API endpoints."""
    return get_data_source_service()


def mission_service_dependency() -> MissionService:
    """Provide the shared Workspace and Mission service to API endpoints."""
    return get_mission_service()


def workspace_question_service_dependency() -> WorkspaceQuestionService:
    """Provide the stateless workspace Q&A service."""
    return get_workspace_question_service()


def raise_http_error(error: DataSourceError) -> None:
    """Convert a safe data-source error into an HTTP response."""
    raise HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message},
    ) from error


def raise_mission_http_error(error: MissionError) -> None:
    """Convert a safe Workspace or Mission error into an HTTP response."""
    raise HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message},
    ) from error


@app.get("/health")
def health() -> dict[str, str]:
    """Report that the backend process is available."""
    return {"status": "ok"}


@app.post("/api/workspaces", response_model=WorkspaceRecord, status_code=201)
async def create_workspace(
    request: WorkspaceCreate,
    service: MissionService = Depends(mission_service_dependency),
) -> WorkspaceRecord:
    """Create a Workspace before sources or Missions are added."""
    try:
        return await service.create_workspace(request)
    except MissionError as error:
        raise_mission_http_error(error)


@app.get("/api/workspaces", response_model=WorkspaceListResponse)
async def list_workspaces(
    service: MissionService = Depends(mission_service_dependency),
) -> dict[str, Any]:
    """Return the Workspaces available to the application."""
    try:
        return {"workspaces": await service.list_workspaces()}
    except MissionError as error:
        raise_mission_http_error(error)


@app.delete("/api/workspaces/{workspace_id}", status_code=204)
async def delete_workspace(
    workspace_id: str,
    request: WorkspaceDelete,
    service: MissionService = Depends(mission_service_dependency),
) -> Response:
    """Permanently delete a confirmed Workspace and all of its product data."""
    try:
        await service.delete_workspace(workspace_id, request.workspace_name)
    except MissionError as error:
        raise_mission_http_error(error)
    return Response(status_code=204)


@app.post(
    "/api/workspaces/{workspace_id}/data-sources/sqlite",
    response_model=DataSourceResponse,
    status_code=201,
)
async def connect_sqlite_source(
    workspace_id: str,
    name: str = Form(min_length=1, max_length=100),
    file: UploadFile = File(),
    service: DataSourceService = Depends(data_source_service_dependency),
    mission_service: MissionService = Depends(mission_service_dependency),
) -> dict[str, Any]:
    """Validate and connect one uploaded SQLite source to a Workspace."""
    try:
        await mission_service.require_workspace(workspace_id)
    except MissionError as error:
        raise_mission_http_error(error)
    original_filename = file.filename or "source.sqlite"
    temporary_file = tempfile.NamedTemporaryFile(
        prefix="geoagent-upload-", suffix=".tmp", delete=False
    )
    temporary_path = Path(temporary_file.name)
    bytes_written = 0
    try:
        while chunk := await file.read(1024 * 1024):
            bytes_written += len(chunk)
            if bytes_written > service.max_upload_bytes:
                raise_http_error(
                    DataSourceError(
                        "SOURCE_TOO_LARGE",
                        f"SQLite uploads are limited to {service.max_upload_bytes} bytes.",
                        413,
                    )
                )
            temporary_file.write(chunk)
        temporary_file.close()
        try:
            record = await run_in_threadpool(
                service.connect_sqlite,
                workspace_id,
                name,
                temporary_path,
                original_filename,
            )
        except DataSourceError as error:
            raise_http_error(error)
        return record.public_dict()
    finally:
        if not temporary_file.closed:
            temporary_file.close()
        temporary_path.unlink(missing_ok=True)
        await file.close()


@app.get(
    "/api/workspaces/{workspace_id}/data-sources",
    response_model=DataSourceListResponse,
)
async def list_workspace_sources(
    workspace_id: str,
    service: DataSourceService = Depends(data_source_service_dependency),
    mission_service: MissionService = Depends(mission_service_dependency),
) -> dict[str, Any]:
    """Return every connected organizational source in a Workspace."""
    try:
        await mission_service.require_workspace(workspace_id)
        records = service.list_sources(workspace_id)
    except MissionError as error:
        raise_mission_http_error(error)
    except DataSourceError as error:
        raise_http_error(error)
    return {"sources": [record.public_dict() for record in records]}


@app.post(
    "/api/workspaces/{workspace_id}/missions",
    response_model=MissionRecord,
    status_code=201,
)
async def create_mission(
    workspace_id: str,
    request: MissionCreate,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionRecord:
    """Create initial Mission state without starting agent execution."""
    try:
        return await service.create_mission(workspace_id, request)
    except MissionError as error:
        raise_mission_http_error(error)


@app.get(
    "/api/workspaces/{workspace_id}/missions",
    response_model=MissionListResponse,
)
async def list_missions(
    workspace_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> dict[str, Any]:
    """Return every Mission belonging to one Workspace."""
    try:
        return {"missions": await service.list_missions(workspace_id)}
    except MissionError as error:
        raise_mission_http_error(error)


@app.post(
    "/api/workspaces/{workspace_id}/questions",
    response_model=WorkspaceQuestionAnswer,
)
async def ask_workspace_question(
    workspace_id: str,
    request: WorkspaceQuestionRequest,
    response: Response,
    service: WorkspaceQuestionService = Depends(workspace_question_service_dependency),
) -> WorkspaceQuestionAnswer:
    """Answer one read-only question without retaining its conversation."""
    response.headers["Cache-Control"] = "no-store"
    try:
        return await service.answer(workspace_id, request)
    except MissionError as error:
        raise_mission_http_error(error)


@app.get(
    "/api/workspaces/{workspace_id}/missions/{mission_id}",
    response_model=MissionRecord,
)
async def get_mission(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionRecord:
    """Return the latest saved state for one Mission."""
    try:
        return await service.require_mission(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)


@app.delete(
    "/api/workspaces/{workspace_id}/missions/{mission_id}", status_code=204
)
async def delete_mission(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> Response:
    """Permanently delete a non-running Mission and its safe activity history."""
    try:
        await service.delete_mission(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)
    return Response(status_code=204)


@app.get(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/map",
    response_model=MissionMapResponse,
)
async def get_mission_map(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionMapResponse:
    """Return real, display-ready geography and assignments for one Mission."""
    try:
        return await service.get_map_state(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)


@app.get(
    "/api/workspaces/{workspace_id}/map",
    response_model=WorkspaceMapResponse,
)
async def get_workspace_map(
    workspace_id: str,
    include_completed: bool = False,
    service: MissionService = Depends(mission_service_dependency),
) -> WorkspaceMapResponse:
    """Return representative locations for the All Missions map."""
    try:
        return await service.list_workspace_map(workspace_id, include_completed)
    except MissionError as error:
        raise_mission_http_error(error)


@app.post(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/run",
    response_model=MissionRecord,
)
async def run_mission(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionRecord:
    """Run a newly created Mission until completion, clarification, or failure."""
    try:
        return await service.run_mission(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)


@app.post(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/responses",
    response_model=MissionRecord,
)
async def respond_to_mission(
    workspace_id: str,
    mission_id: str,
    response: ClarificationResponse,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionRecord:
    """Submit an open-ended answer and resume the same Mission session."""
    try:
        return await service.respond_to_clarification(
            workspace_id, mission_id, response
        )
    except MissionError as error:
        raise_mission_http_error(error)


@app.post(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/objective-decision/accept",
    response_model=MissionRecord,
)
async def accept_mission_objective(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> MissionRecord:
    """Accept the Manager's replacement objective and run one new attempt."""
    try:
        return await service.accept_objective_decision(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)


@app.delete(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/objective-decision",
    status_code=204,
)
async def discard_mission_objective(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> Response:
    """Discard the proposed replacement and permanently delete the Mission."""
    try:
        await service.discard_objective_decision(workspace_id, mission_id)
    except MissionError as error:
        raise_mission_http_error(error)
    return Response(status_code=204)


@app.get(
    "/api/workspaces/{workspace_id}/missions/{mission_id}/events",
    response_model=MissionEventListResponse,
)
async def list_mission_events(
    workspace_id: str,
    mission_id: str,
    service: MissionService = Depends(mission_service_dependency),
) -> dict[str, Any]:
    """Return safe agent and lifecycle activity for one Mission."""
    try:
        return {"events": await service.list_events(workspace_id, mission_id)}
    except MissionError as error:
        raise_mission_http_error(error)


def _frontend_directory() -> Path:
    """Return the optional, container-provided Vite build directory."""
    configured = os.getenv("GEOAGENT_FRONTEND_DIRECTORY", "").strip()
    if configured:
        return Path(configured).resolve()
    return Path(__file__).resolve().parents[1] / "frontend_dist"


_frontend_build_directory = _frontend_directory()
_frontend_index = _frontend_build_directory / "index.html"


if _frontend_index.is_file():

    @app.get("/{frontend_path:path}", include_in_schema=False)
    def serve_frontend(frontend_path: str) -> FileResponse:
        """Serve the Vite single-page app without intercepting API routes."""
        requested = (_frontend_build_directory / frontend_path).resolve()
        try:
            requested.relative_to(_frontend_build_directory)
        except ValueError:
            return FileResponse(_frontend_index)
        if frontend_path and requested.is_file():
            return FileResponse(requested)
        return FileResponse(_frontend_index)
