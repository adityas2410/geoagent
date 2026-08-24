"""Connection lifecycle and authorized access to organizational data."""

from __future__ import annotations

import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from .data_source_contracts import DataSourceError
from .data_source_contracts import DataSourceRecord
from .data_source_contracts import QueryResult
from .data_source_contracts import QuerySpec
from .data_source_contracts import SchemaInspection
from .source_files import GcsSourceStorage
from .source_files import LocalSourceStorage
from .source_files import SourceStorage
from .source_records import FirestoreSourceRepository
from .source_records import SourceRepository
from .sqlite_source import SQLiteAdapter


WORKSPACE_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
SQLITE_EXTENSIONS = {".db", ".sqlite", ".sqlite3"}
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024


class AdapterRegistry:
    def __init__(self, adapters: list[SQLiteAdapter] | None = None) -> None:
        self._adapters = {
            adapter.source_type: adapter for adapter in (adapters or [SQLiteAdapter()])
        }

    def get(self, source_type: str) -> SQLiteAdapter:
        adapter = self._adapters.get(source_type)
        if adapter is None:
            raise DataSourceError(
                "SOURCE_TYPE_UNSUPPORTED", "The source type is not supported.", 400
            )
        return adapter


class DataSourceService:
    def __init__(
        self,
        repository: SourceRepository,
        storage: SourceStorage,
        adapter_registry: AdapterRegistry | None = None,
        max_upload_bytes: int = DEFAULT_MAX_UPLOAD_BYTES,
    ) -> None:
        self.repository = repository
        self.storage = storage
        self.adapter_registry = adapter_registry or AdapterRegistry()
        self.max_upload_bytes = max_upload_bytes

    @staticmethod
    def validate_workspace_id(workspace_id: str) -> None:
        if not WORKSPACE_ID_PATTERN.fullmatch(workspace_id):
            raise DataSourceError(
                "INVALID_WORKSPACE", "The Workspace identifier is invalid.", 400
            )

    def connect_sqlite(
        self,
        workspace_id: str,
        name: str,
        upload_path: Path,
        original_filename: str,
    ) -> DataSourceRecord:
        self.validate_workspace_id(workspace_id)
        normalized_name = name.strip()
        if not 1 <= len(normalized_name) <= 100:
            raise DataSourceError(
                "INVALID_SOURCE_NAME", "The source name must contain 1 to 100 characters."
            )
        safe_filename = original_filename.replace("\\", "/").rsplit("/", 1)[-1]
        if Path(safe_filename).suffix.lower() not in SQLITE_EXTENSIONS:
            raise DataSourceError(
                "INVALID_SQLITE", "Upload a .db, .sqlite, or .sqlite3 file."
            )
        try:
            size_bytes = upload_path.stat().st_size
        except OSError as error:
            raise DataSourceError("INVALID_SQLITE", "The uploaded file is unavailable.") from error
        if size_bytes == 0:
            raise DataSourceError("INVALID_SQLITE", "The uploaded file is empty.")
        if size_bytes > self.max_upload_bytes:
            raise DataSourceError(
                "SOURCE_TOO_LARGE",
                f"SQLite uploads are limited to {self.max_upload_bytes} bytes.",
                413,
            )

        adapter = self.adapter_registry.get("sqlite")
        validation = adapter.validate(upload_path)
        source_id = f"src_{uuid4().hex}"
        stored_object = self.storage.put(workspace_id, source_id, upload_path)
        record = DataSourceRecord(
            source_id=source_id,
            workspace_id=workspace_id,
            name=normalized_name,
            provenance=validation.provenance,
            original_filename=safe_filename,
            size_bytes=stored_object.size_bytes,
            table_count=validation.schema_inspection.table_count,
            view_count=validation.schema_inspection.view_count,
            storage_key=stored_object.storage_key,
            storage_generation=stored_object.generation,
            created_at=datetime.now(timezone.utc),
        )
        try:
            self.repository.create(record)
        except Exception:
            self.storage.delete(
                workspace_id,
                source_id,
                stored_object.storage_key,
                stored_object.generation,
            )
            raise
        return record

    def list_sources(self, workspace_id: str) -> list[DataSourceRecord]:
        self.validate_workspace_id(workspace_id)
        return self.repository.list(workspace_id)

    def get_source(self, workspace_id: str, source_id: str) -> DataSourceRecord:
        self.validate_workspace_id(workspace_id)
        record = self.repository.get(workspace_id, source_id)
        if record is None:
            raise DataSourceError("SOURCE_NOT_FOUND", "The source was not found.", 404)
        return record

    def list_authorized(
        self, workspace_id: str, authorized_source_ids: set[str]
    ) -> list[DataSourceRecord]:
        return [
            record
            for record in self.list_sources(workspace_id)
            if record.source_id in authorized_source_ids and record.status == "connected"
        ]

    def require_authorized(
        self, workspace_id: str, authorized_source_ids: set[str], source_id: str
    ) -> DataSourceRecord:
        if source_id not in authorized_source_ids:
            raise DataSourceError(
                "SOURCE_NOT_AUTHORIZED", "The Mission is not authorized to use this source.", 403
            )
        return self.get_source(workspace_id, source_id)

    def inspect_source(self, record: DataSourceRecord) -> SchemaInspection:
        source_path = self.storage.materialize(record)
        return self.adapter_registry.get(record.source_type).inspect_schema(source_path)

    def query_source(
        self, record: DataSourceRecord, query_spec: QuerySpec | dict[str, Any]
    ) -> QueryResult:
        source_path = self.storage.materialize(record)
        return self.adapter_registry.get(record.source_type).query(source_path, query_spec)


_service: DataSourceService | None = None


def build_data_source_service_from_environment() -> DataSourceService:
    backend_directory = Path(__file__).resolve().parents[2]
    project_id = os.getenv("GOOGLE_CLOUD_PROJECT", "geoagent-hackathon")
    database_id = os.getenv("FIRESTORE_DATABASE_ID", "geoagentdb")
    repository = FirestoreSourceRepository(project_id=project_id, database_id=database_id)

    storage_backend = os.getenv("GEOAGENT_SOURCE_STORAGE", "local").lower()
    if storage_backend == "local":
        local_directory_value = os.getenv("GEOAGENT_LOCAL_SOURCE_DIRECTORY", "").strip()
        local_directory = (
            Path(local_directory_value)
            if local_directory_value
            else backend_directory / "data" / "sources"
        )
        if not local_directory.is_absolute():
            local_directory = backend_directory / local_directory
        storage: SourceStorage = LocalSourceStorage(
            local_directory
        )
    elif storage_backend == "gcs":
        storage = GcsSourceStorage(
            bucket_name=os.getenv("GEOAGENT_SOURCE_BUCKET", ""),
            cache_directory=Path(tempfile.gettempdir()) / "geoagent-source-cache",
        )
    else:
        raise RuntimeError("GEOAGENT_SOURCE_STORAGE must be 'local' or 'gcs'")

    try:
        max_upload_bytes = int(
            os.getenv("GEOAGENT_MAX_SQLITE_UPLOAD_BYTES", str(DEFAULT_MAX_UPLOAD_BYTES))
        )
    except ValueError as error:
        raise RuntimeError("GEOAGENT_MAX_SQLITE_UPLOAD_BYTES must be an integer") from error
    if max_upload_bytes <= 0:
        raise RuntimeError("GEOAGENT_MAX_SQLITE_UPLOAD_BYTES must be positive")

    return DataSourceService(
        repository=repository,
        storage=storage,
        max_upload_bytes=max_upload_bytes,
    )


def get_data_source_service() -> DataSourceService:
    global _service
    if _service is None:
        _service = build_data_source_service_from_environment()
    return _service


def configure_data_source_service(service: DataSourceService | None) -> None:
    """Override the lazy service, primarily for isolated tests."""
    global _service
    _service = service
