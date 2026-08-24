"""FastAPI service for GeoAgent."""

from __future__ import annotations

import tempfile
from pathlib import Path
from typing import Any

from fastapi import Depends
from fastapi import FastAPI
from fastapi import File
from fastapi import Form
from fastapi import HTTPException
from fastapi import UploadFile
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from .data_sources.data_source_contracts import DataSourceError
from .data_sources.source_manager import DataSourceService
from .data_sources.source_manager import get_data_source_service


app = FastAPI(title="GeoAgent API", version="0.1.0")


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
    return get_data_source_service()


def raise_http_error(error: DataSourceError) -> None:
    raise HTTPException(
        status_code=error.http_status,
        detail={"code": error.code, "message": error.message},
    ) from error


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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
) -> dict[str, Any]:
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
def list_workspace_sources(
    workspace_id: str,
    service: DataSourceService = Depends(data_source_service_dependency),
) -> dict[str, Any]:
    try:
        records = service.list_sources(workspace_id)
    except DataSourceError as error:
        raise_http_error(error)
    return {"sources": [record.public_dict() for record in records]}
