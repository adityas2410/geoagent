"""Persistent object storage backends for uploaded source files."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from pathlib import Path
from typing import Protocol

from .data_source_contracts import DataSourceError
from .data_source_contracts import DataSourceRecord
from .data_source_contracts import StoredObject


class SourceStorage(Protocol):
    def put(self, workspace_id: str, source_id: str, source_path: Path) -> StoredObject: ...

    def materialize(self, record: DataSourceRecord) -> Path: ...

    def delete(
        self, workspace_id: str, source_id: str, storage_key: str, generation: str
    ) -> None: ...


def object_key(workspace_id: str, source_id: str) -> str:
    return f"workspaces/{workspace_id}/data-sources/{source_id}/source.sqlite"


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as source_file:
        for chunk in iter(lambda: source_file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


class LocalSourceStorage:
    """Development storage with the same object-key behavior as Cloud Storage."""

    def __init__(self, root_directory: Path) -> None:
        self.root_directory = root_directory.resolve()

    def _resolve_key(self, storage_key: str) -> Path:
        candidate = (self.root_directory / storage_key).resolve()
        try:
            candidate.relative_to(self.root_directory)
        except ValueError as error:
            raise DataSourceError("SOURCE_UNAVAILABLE", "The source storage key is invalid.", 503) from error
        return candidate

    def put(self, workspace_id: str, source_id: str, source_path: Path) -> StoredObject:
        key = object_key(workspace_id, source_id)
        destination = self._resolve_key(key)
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary_file = tempfile.NamedTemporaryFile(
            prefix=".source-", suffix=".tmp", dir=destination.parent, delete=False
        )
        temporary_path = Path(temporary_file.name)
        temporary_file.close()
        try:
            shutil.copyfile(source_path, temporary_path)
            os.replace(temporary_path, destination)
        except OSError as error:
            temporary_path.unlink(missing_ok=True)
            raise DataSourceError(
                "SOURCE_STORAGE_FAILED", "The source could not be stored.", 503
            ) from error
        return StoredObject(
            storage_key=key,
            generation=file_sha256(destination),
            size_bytes=destination.stat().st_size,
        )

    def materialize(self, record: DataSourceRecord) -> Path:
        source_path = self._resolve_key(record.storage_key)
        if not source_path.is_file():
            raise DataSourceError("SOURCE_UNAVAILABLE", "The source file is unavailable.", 503)
        if file_sha256(source_path) != record.storage_generation:
            raise DataSourceError("SOURCE_UNAVAILABLE", "The source file has changed unexpectedly.", 503)
        return source_path

    def delete(
        self, workspace_id: str, source_id: str, storage_key: str, generation: str
    ) -> None:
        del workspace_id, source_id, generation
        self._resolve_key(storage_key).unlink(missing_ok=True)


class GcsSourceStorage:
    """Production Cloud Storage backend with generation-specific local caching."""

    def __init__(self, bucket_name: str, cache_directory: Path, client=None) -> None:
        if not bucket_name:
            raise ValueError("GEOAGENT_SOURCE_BUCKET is required for GCS storage")
        if client is None:
            try:
                from google.cloud import storage
            except ImportError as error:
                raise RuntimeError("google-cloud-storage is not installed") from error
            client = storage.Client()
        self.bucket = client.bucket(bucket_name)
        self.cache_directory = cache_directory.resolve()

    def put(self, workspace_id: str, source_id: str, source_path: Path) -> StoredObject:
        key = object_key(workspace_id, source_id)
        blob = self.bucket.blob(key)
        try:
            blob.upload_from_filename(
                str(source_path),
                content_type="application/vnd.sqlite3",
                if_generation_match=0,
            )
            blob.reload()
        except Exception as error:
            raise DataSourceError(
                "SOURCE_STORAGE_FAILED", "The source could not be stored.", 503
            ) from error
        return StoredObject(
            storage_key=key,
            generation=str(blob.generation),
            size_bytes=int(blob.size or source_path.stat().st_size),
        )

    def materialize(self, record: DataSourceRecord) -> Path:
        self.cache_directory.mkdir(parents=True, exist_ok=True)
        cache_path = self.cache_directory / (
            f"{record.source_id}-{record.storage_generation}.sqlite"
        )
        if cache_path.is_file():
            return cache_path

        temporary_file = tempfile.NamedTemporaryFile(
            prefix=f".{record.source_id}-",
            suffix=".tmp",
            dir=self.cache_directory,
            delete=False,
        )
        temporary_path = Path(temporary_file.name)
        temporary_file.close()
        try:
            blob = self.bucket.blob(record.storage_key)
            blob.download_to_filename(
                str(temporary_path),
                if_generation_match=int(record.storage_generation),
            )
            os.replace(temporary_path, cache_path)
        except Exception as error:
            temporary_path.unlink(missing_ok=True)
            raise DataSourceError(
                "SOURCE_UNAVAILABLE", "The source file could not be loaded.", 503
            ) from error
        return cache_path

    def delete(
        self, workspace_id: str, source_id: str, storage_key: str, generation: str
    ) -> None:
        del workspace_id, source_id
        try:
            self.bucket.blob(storage_key).delete(if_generation_match=int(generation))
        except Exception:
            # This is compensating cleanup after a failed connection. The
            # original registration error remains the actionable failure.
            return
