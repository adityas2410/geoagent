from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from fastapi.testclient import TestClient  # noqa: E402

from build_demo_db import build_database  # noqa: E402
from geoagent.app import app  # noqa: E402
from geoagent.app import data_source_service_dependency  # noqa: E402
from geoagent.app import mission_service_dependency  # noqa: E402
from geoagent.data_sources.source_files import LocalSourceStorage  # noqa: E402
from geoagent.data_sources.source_manager import DataSourceService  # noqa: E402
from geoagent.data_sources.source_records import InMemorySourceRepository  # noqa: E402
from geoagent.missions import MissionError  # noqa: E402
from geoagent.missions import WorkspaceRecord  # noqa: E402


class WorkspaceGate:
    async def require_workspace(self, workspace_id: str) -> WorkspaceRecord:
        if workspace_id != "demo-workspace":
            raise MissionError("WORKSPACE_NOT_FOUND", "The Workspace was not found.", 404)
        timestamp = datetime.now(timezone.utc)
        return WorkspaceRecord(
            workspace_id=workspace_id,
            name="Demo Workspace",
            created_at=timestamp,
            updated_at=timestamp,
        )


class DataSourceApiTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        self.database_path = root / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))
        self.service = DataSourceService(
            repository=InMemorySourceRepository(),
            storage=LocalSourceStorage(root / "stored"),
        )
        app.dependency_overrides[data_source_service_dependency] = lambda: self.service
        app.dependency_overrides[mission_service_dependency] = WorkspaceGate
        self.client = TestClient(app)

    def tearDown(self) -> None:
        self.client.close()
        app.dependency_overrides.clear()
        self.temporary_directory.cleanup()

    def upload(self, filename: str = "operations.db"):
        return self.client.post(
            "/api/workspaces/demo-workspace/data-sources/sqlite",
            data={"name": "Kerala Operations"},
            files={"file": (filename, self.database_path.read_bytes(), "application/vnd.sqlite3")},
        )

    def test_health_upload_and_list(self) -> None:
        self.assertEqual(self.client.get("/health").json(), {"status": "ok"})
        response = self.upload("../../operations.db")
        self.assertEqual(response.status_code, 201, response.text)
        source = response.json()
        self.assertEqual(source["table_count"], 9)
        self.assertEqual(source["provenance"], "synthetic")
        self.assertEqual(source["original_filename"], "operations.db")
        self.assertNotIn("storage_key", source)

        listed = self.client.get(
            "/api/workspaces/demo-workspace/data-sources"
        )
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(listed.json()["sources"], [source])

    def test_invalid_and_empty_uploads_are_rejected(self) -> None:
        invalid = self.client.post(
            "/api/workspaces/demo-workspace/data-sources/sqlite",
            data={"name": "Invalid"},
            files={"file": ("invalid.db", b"not sqlite", "application/octet-stream")},
        )
        self.assertEqual(invalid.status_code, 400)
        self.assertEqual(invalid.json()["detail"]["code"], "INVALID_SQLITE")

        empty = self.client.post(
            "/api/workspaces/demo-workspace/data-sources/sqlite",
            data={"name": "Empty"},
            files={"file": ("empty.db", b"", "application/octet-stream")},
        )
        self.assertEqual(empty.status_code, 400)
        self.assertEqual(empty.json()["detail"]["code"], "INVALID_SQLITE")

    def test_oversized_upload_is_rejected_before_storage(self) -> None:
        self.service.max_upload_bytes = 16
        response = self.client.post(
            "/api/workspaces/demo-workspace/data-sources/sqlite",
            data={"name": "Too Large"},
            files={"file": ("large.db", b"x" * 17, "application/octet-stream")},
        )
        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"]["code"], "SOURCE_TOO_LARGE")
        self.assertEqual(self.service.list_sources("demo-workspace"), [])

    def test_unknown_workspace_is_rejected(self) -> None:
        response = self.client.post(
            "/api/workspaces/missing/data-sources/sqlite",
            data={"name": "Operations"},
            files={
                "file": (
                    "operations.db",
                    self.database_path.read_bytes(),
                    "application/vnd.sqlite3",
                )
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"]["code"], "WORKSPACE_NOT_FOUND")


if __name__ == "__main__":
    unittest.main()
