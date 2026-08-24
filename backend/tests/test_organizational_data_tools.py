from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from google.adk.tools.function_tool import FunctionTool  # noqa: E402

from build_demo_db import build_database  # noqa: E402
from geoagent.data_sources.organizational_data_tools import inspect_source_schema  # noqa: E402
from geoagent.data_sources.organizational_data_tools import list_authorized_sources  # noqa: E402
from geoagent.data_sources.organizational_data_tools import query_source  # noqa: E402
from geoagent.data_sources.source_files import LocalSourceStorage  # noqa: E402
from geoagent.data_sources.source_manager import DataSourceService  # noqa: E402
from geoagent.data_sources.source_manager import configure_data_source_service  # noqa: E402
from geoagent.data_sources.source_records import InMemorySourceRepository  # noqa: E402


class DataSourceToolsTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        root = Path(self.temporary_directory.name)
        database_path = root / "operations.db"
        build_database(database_path, date(2026, 8, 25))
        self.service = DataSourceService(
            repository=InMemorySourceRepository(),
            storage=LocalSourceStorage(root / "stored"),
        )
        self.record = self.service.connect_sqlite(
            "workspace-1", "Operations", database_path, "operations.db"
        )
        configure_data_source_service(self.service)
        self.context = SimpleNamespace(
            state={
                "workspace_id": "workspace-1",
                "authorized_source_ids": [self.record.source_id],
            }
        )

    def tearDown(self) -> None:
        configure_data_source_service(None)
        self.temporary_directory.cleanup()

    def test_authorized_list_schema_and_query(self) -> None:
        listed = list_authorized_sources(self.context)
        self.assertEqual(listed["status"], "success")
        self.assertEqual(
            [source["source_id"] for source in listed["sources"]],
            [self.record.source_id],
        )

        schema = inspect_source_schema(self.record.source_id, self.context)
        self.assertEqual(schema["status"], "success")
        self.assertEqual(len(schema["entities"]), 9)

        result = query_source(
            self.record.source_id,
            {
                "entity": "vehicles",
                "columns": ["vehicle_id", "refrigerated"],
                "filters": [
                    {"column": "refrigerated", "operator": "eq", "value": 1}
                ],
            },
            self.context,
        )
        self.assertEqual(result["status"], "success")
        self.assertEqual(result["rows"], [{"vehicle_id": "VEH-003", "refrigerated": 1}])

    def test_missing_and_unauthorized_context_fail_closed(self) -> None:
        missing = list_authorized_sources(SimpleNamespace(state={}))
        self.assertEqual(missing["error"]["code"], "SOURCE_CONTEXT_MISSING")

        unauthorized_context = SimpleNamespace(
            state={"workspace_id": "workspace-1", "authorized_source_ids": []}
        )
        denied = inspect_source_schema(self.record.source_id, unauthorized_context)
        self.assertEqual(denied["error"]["code"], "SOURCE_NOT_AUTHORIZED")

    def test_query_tool_has_an_adk_function_declaration(self) -> None:
        declaration = FunctionTool(query_source)._get_declaration()
        self.assertEqual(declaration.name, "query_source")
        self.assertIn("query_spec", declaration.parameters_json_schema["properties"])


if __name__ == "__main__":
    unittest.main()
