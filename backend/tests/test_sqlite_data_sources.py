from __future__ import annotations

import hashlib
import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND_DIRECTORY))
sys.path.insert(0, str(BACKEND_DIRECTORY / "demo_data"))

from build_demo_db import build_database  # noqa: E402
from geoagent.data_sources.data_source_contracts import DataSourceError  # noqa: E402
from geoagent.data_sources.data_source_contracts import QuerySpec  # noqa: E402
from geoagent.data_sources.sqlite_source import SQLiteAdapter  # noqa: E402


class SQLiteAdapterTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "operations.db"
        build_database(self.database_path, date(2026, 8, 25))
        self.adapter = SQLiteAdapter()

    def tearDown(self) -> None:
        self.temporary_directory.cleanup()

    def test_validation_and_schema_discovery(self) -> None:
        validation = self.adapter.validate(self.database_path)
        self.assertEqual(validation.provenance, "synthetic")
        self.assertEqual(validation.schema_inspection.table_count, 9)
        jobs = next(
            entity
            for entity in validation.schema_inspection.entities
            if entity.name == "delivery_jobs"
        )
        self.assertIn("weight_kg", {column.name for column in jobs.columns})
        self.assertIn(
            ("customer_id", "customers", "customer_id"),
            {
                (
                    relationship.from_column,
                    relationship.referenced_entity,
                    relationship.referenced_column,
                )
                for relationship in jobs.foreign_keys
            },
        )

    def test_filter_order_pagination_and_parameterization(self) -> None:
        result = self.adapter.query(
            self.database_path,
            QuerySpec.model_validate(
                {
                    "entity": "delivery_jobs",
                    "columns": ["job_id", "priority"],
                    "filters": [{"column": "priority", "operator": "gte", "value": 4}],
                    "order_by": [{"column": "priority", "direction": "desc"}],
                    "limit": 2,
                }
            ),
        )
        self.assertEqual(result.returned_count, 2)
        self.assertTrue(result.has_more)
        self.assertEqual([row["priority"] for row in result.rows], [5, 5])

        injected_value = self.adapter.query(
            self.database_path,
            {
                "entity": "customers",
                "columns": ["customer_id"],
                "filters": [
                    {
                        "column": "name",
                        "operator": "eq",
                        "value": "x' OR 1=1 --",
                    }
                ],
            },
        )
        self.assertEqual(injected_value.rows, [])

    def test_grouped_aggregation(self) -> None:
        result = self.adapter.query(
            self.database_path,
            {
                "entity": "delivery_jobs",
                "columns": ["requires_refrigeration"],
                "group_by": ["requires_refrigeration"],
                "aggregates": [
                    {"function": "count", "alias": "job_count"}
                ],
                "order_by": [{"column": "job_count", "direction": "desc"}],
            },
        )
        self.assertEqual(
            result.rows,
            [
                {"requires_refrigeration": 0, "job_count": 8},
                {"requires_refrigeration": 1, "job_count": 2},
            ],
        )

    def test_invalid_identifiers_and_shapes_are_rejected(self) -> None:
        invalid_specs = [
            {"entity": "delivery_jobs; DROP TABLE delivery_jobs"},
            {"entity": "delivery_jobs", "raw_sql": "DELETE FROM delivery_jobs"},
            {"entity": "delivery_jobs", "columns": ["missing_column"]},
            {
                "entity": "delivery_jobs",
                "columns": ["job_id"],
                "group_by": ["job_id"],
            },
            {
                "entity": "delivery_jobs",
                "aggregates": [{"function": "sum", "alias": "total"}],
            },
        ]
        for spec in invalid_specs:
            with self.subTest(spec=spec):
                with self.assertRaises(DataSourceError) as raised:
                    self.adapter.query(self.database_path, spec)
                self.assertEqual(raised.exception.code, "INVALID_QUERY")

    def test_queries_do_not_change_database(self) -> None:
        before = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.adapter.query(
            self.database_path,
            {"entity": "delivery_jobs", "limit": 5},
        )
        after = hashlib.sha256(self.database_path.read_bytes()).hexdigest()
        self.assertEqual(before, after)

    def test_invalid_corrupt_and_empty_databases_are_rejected(self) -> None:
        invalid_path = Path(self.temporary_directory.name) / "invalid.db"
        invalid_path.write_bytes(b"not sqlite")
        with self.assertRaises(DataSourceError) as invalid_error:
            self.adapter.validate(invalid_path)
        self.assertEqual(invalid_error.exception.code, "INVALID_SQLITE")

        corrupt_path = Path(self.temporary_directory.name) / "corrupt.db"
        corrupt_path.write_bytes(b"SQLite format 3\x00" + b"corrupt" * 32)
        with self.assertRaises(DataSourceError) as corrupt_error:
            self.adapter.validate(corrupt_path)
        self.assertEqual(corrupt_error.exception.code, "INVALID_SQLITE")

        empty_path = Path(self.temporary_directory.name) / "empty.db"
        connection = sqlite3.connect(empty_path)
        connection.execute("PRAGMA user_version = 1")
        connection.close()
        with self.assertRaises(DataSourceError) as empty_error:
            self.adapter.validate(empty_path)
        self.assertEqual(empty_error.exception.code, "EMPTY_SQLITE")


if __name__ == "__main__":
    unittest.main()
