from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from build_demo_db import build_database  # noqa: E402
from geoagent.data_sources.data_source_contracts import DataSourceError  # noqa: E402
from geoagent.data_sources.data_source_contracts import DataSourceRecord  # noqa: E402
from geoagent.data_sources.source_files import GcsSourceStorage  # noqa: E402
from geoagent.data_sources.source_files import LocalSourceStorage  # noqa: E402
from geoagent.data_sources.source_manager import DataSourceService  # noqa: E402
from geoagent.data_sources.source_records import FirestoreSourceRepository  # noqa: E402


class FailingRepository:
    def create(self, record):
        raise DataSourceError("SOURCE_REGISTRATION_FAILED", "failed", 503)

    def get(self, workspace_id, source_id):
        return None

    def list(self, workspace_id):
        return []


class FakeBlob:
    def __init__(self, key: str, objects: dict[str, dict]) -> None:
        self.key = key
        self.objects = objects
        self.generation = None
        self.size = None

    def upload_from_filename(self, filename, content_type, if_generation_match):
        self.assertions = (content_type, if_generation_match)
        self.objects[self.key] = {
            "data": Path(filename).read_bytes(),
            "generation": 1,
        }

    def reload(self):
        stored = self.objects[self.key]
        self.generation = stored["generation"]
        self.size = len(stored["data"])

    def download_to_filename(self, filename, if_generation_match):
        stored = self.objects[self.key]
        if stored["generation"] != if_generation_match:
            raise RuntimeError("wrong generation")
        Path(filename).write_bytes(stored["data"])

    def delete(self, if_generation_match):
        stored = self.objects[self.key]
        if stored["generation"] == if_generation_match:
            del self.objects[self.key]


class FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, dict] = {}

    def blob(self, key: str) -> FakeBlob:
        return FakeBlob(key, self.objects)


class FakeStorageClient:
    def __init__(self) -> None:
        self.fake_bucket = FakeBucket()

    def bucket(self, bucket_name: str) -> FakeBucket:
        self.bucket_name = bucket_name
        return self.fake_bucket


class SourcePersistenceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary_directory.name)
        self.database_path = self.root / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_stored_file_is_removed_when_registration_fails(self) -> None:
        storage_root = self.root / "stored"
        service = DataSourceService(
            repository=FailingRepository(),
            storage=LocalSourceStorage(storage_root),
        )
        with self.assertRaises(DataSourceError) as raised:
            service.connect_sqlite(
                "workspace-1", "Operations", self.database_path, "operations.db"
            )
        self.assertEqual(raised.exception.code, "SOURCE_REGISTRATION_FAILED")
        self.assertEqual(list(storage_root.rglob("*.sqlite")), [])

    def test_gcs_storage_uses_generated_key_and_generation_cache(self) -> None:
        client = FakeStorageClient()
        storage = GcsSourceStorage(
            "geoagent-sources",
            self.root / "cache",
            client=client,
        )
        stored = storage.put("workspace-1", "src_123", self.database_path)
        self.assertEqual(
            stored.storage_key,
            "workspaces/workspace-1/data-sources/src_123/source.sqlite",
        )
        self.assertEqual(stored.generation, "1")
        record = DataSourceRecord(
            source_id="src_123",
            workspace_id="workspace-1",
            name="Operations",
            provenance="synthetic",
            original_filename="../../private.db",
            size_bytes=stored.size_bytes,
            table_count=9,
            view_count=0,
            storage_key=stored.storage_key,
            storage_generation=stored.generation,
            created_at=datetime.now(timezone.utc),
        )
        materialized = storage.materialize(record)
        self.assertEqual(materialized.read_bytes(), self.database_path.read_bytes())
        storage.delete(
            record.workspace_id,
            record.source_id,
            record.storage_key,
            record.storage_generation,
        )
        self.assertEqual(client.fake_bucket.objects, {})

    def test_firestore_repository_targets_named_database(self) -> None:
        with patch("google.cloud.firestore.Client") as client_class:
            FirestoreSourceRepository("geoagent-hackathon", "geoagentdb")
        client_class.assert_called_once_with(
            project="geoagent-hackathon", database="geoagentdb"
        )


if __name__ == "__main__":
    unittest.main()
