"""Metadata repositories for Workspace data-source connections."""

from __future__ import annotations

from typing import Protocol

from .data_source_contracts import DataSourceError
from .data_source_contracts import DataSourceRecord


class SourceRepository(Protocol):
    def create(self, record: DataSourceRecord) -> None: ...

    def get(self, workspace_id: str, source_id: str) -> DataSourceRecord | None: ...

    def list(self, workspace_id: str) -> list[DataSourceRecord]: ...

    def delete(self, workspace_id: str, source_id: str) -> None: ...


class InMemorySourceRepository:
    """Small deterministic repository used by unit tests."""

    def __init__(self) -> None:
        self.records: dict[tuple[str, str], DataSourceRecord] = {}

    def create(self, record: DataSourceRecord) -> None:
        key = (record.workspace_id, record.source_id)
        if key in self.records:
            raise DataSourceError("SOURCE_ALREADY_EXISTS", "The source already exists.", 409)
        self.records[key] = record

    def get(self, workspace_id: str, source_id: str) -> DataSourceRecord | None:
        return self.records.get((workspace_id, source_id))

    def list(self, workspace_id: str) -> list[DataSourceRecord]:
        return sorted(
            (
                record
                for (record_workspace_id, _), record in self.records.items()
                if record_workspace_id == workspace_id
            ),
            key=lambda record: record.created_at,
        )

    def delete(self, workspace_id: str, source_id: str) -> None:
        self.records.pop((workspace_id, source_id), None)


class FirestoreSourceRepository:
    """Stores connection metadata in GeoAgent's named Firestore database."""

    def __init__(self, project_id: str, database_id: str, client=None) -> None:
        if client is None:
            try:
                from google.cloud import firestore
            except ImportError as error:
                raise RuntimeError("google-cloud-firestore is not installed") from error
            client = firestore.Client(project=project_id, database=database_id)
        self.client = client

    def _collection(self, workspace_id: str):
        return (
            self.client.collection("workspaces")
            .document(workspace_id)
            .collection("data_sources")
        )

    def create(self, record: DataSourceRecord) -> None:
        try:
            self._collection(record.workspace_id).document(record.source_id).create(
                record.model_dump(mode="json")
            )
        except Exception as error:
            raise DataSourceError(
                "SOURCE_REGISTRATION_FAILED",
                "The source connection could not be registered.",
                503,
            ) from error

    def get(self, workspace_id: str, source_id: str) -> DataSourceRecord | None:
        try:
            snapshot = self._collection(workspace_id).document(source_id).get()
        except Exception as error:
            raise DataSourceError(
                "SOURCE_UNAVAILABLE", "Source metadata could not be loaded.", 503
            ) from error
        if not snapshot.exists:
            return None
        try:
            return DataSourceRecord.model_validate(snapshot.to_dict())
        except Exception as error:
            raise DataSourceError(
                "SOURCE_UNAVAILABLE", "Source metadata is invalid.", 503
            ) from error

    def list(self, workspace_id: str) -> list[DataSourceRecord]:
        try:
            snapshots = self._collection(workspace_id).stream()
            records = [DataSourceRecord.model_validate(item.to_dict()) for item in snapshots]
        except Exception as error:
            raise DataSourceError(
                "SOURCE_UNAVAILABLE", "Source metadata could not be listed.", 503
            ) from error
        return sorted(records, key=lambda record: record.created_at)

    def delete(self, workspace_id: str, source_id: str) -> None:
        try:
            self._collection(workspace_id).document(source_id).delete()
        except Exception as error:
            raise DataSourceError(
                "SOURCE_REGISTRATION_FAILED", "The source connection could not be removed.", 503
            ) from error
