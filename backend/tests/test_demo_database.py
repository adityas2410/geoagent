from __future__ import annotations

import sqlite3
import sys
import tempfile
import unittest
from datetime import date
from pathlib import Path


BACKEND_DIRECTORY = Path(__file__).resolve().parents[1]
DEMO_DIRECTORY = BACKEND_DIRECTORY / "demo_data"
sys.path.insert(0, str(DEMO_DIRECTORY))

from build_demo_db import build_database  # noqa: E402


PLANNING_DATE = date(2026, 8, 25)
EXPECTED_COUNTS = {
    "dataset_metadata": 4,
    "locations": 11,
    "facilities": 1,
    "customers": 10,
    "vehicles": 5,
    "drivers": 6,
    "resource_availability": 11,
    "delivery_jobs": 10,
    "operational_rules": 8,
}


class DemoDatabaseTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "demo.db"
        build_database(self.database_path, PLANNING_DATE)
        self.connection = sqlite3.connect(self.database_path)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")

    def tearDown(self) -> None:
        self.connection.close()
        self.temporary_directory.cleanup()

    def test_schema_and_seed_counts(self) -> None:
        table_names = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        self.assertEqual(table_names, set(EXPECTED_COUNTS))
        for table_name, expected_count in EXPECTED_COUNTS.items():
            count = self.connection.execute(
                f"SELECT COUNT(*) FROM {table_name}"
            ).fetchone()[0]
            self.assertEqual(count, expected_count, table_name)

    def test_provenance_and_planning_date_are_explicit(self) -> None:
        metadata = dict(
            self.connection.execute("SELECT key, value FROM dataset_metadata")
        )
        self.assertEqual(metadata["provenance"], "synthetic")
        self.assertEqual(metadata["planning_date"], PLANNING_DATE.isoformat())
        self.assertEqual(metadata["timezone"], "Asia/Kolkata")
        stored_dates = {
            row[0] for row in self.connection.execute("SELECT service_date FROM delivery_jobs")
        }
        self.assertEqual(stored_dates, {PLANNING_DATE.isoformat()})

    def test_foreign_keys_and_delivery_relationships_are_valid(self) -> None:
        self.assertEqual(self.connection.execute("PRAGMA foreign_key_check").fetchall(), [])
        valid_deliveries = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM delivery_jobs AS jobs
            JOIN customers ON customers.customer_id = jobs.customer_id
            JOIN locations AS pickup ON pickup.location_id = jobs.pickup_location_id
            JOIN locations AS destination ON destination.location_id = jobs.delivery_location_id
            WHERE customers.location_id = jobs.delivery_location_id
            """
        ).fetchone()[0]
        self.assertEqual(valid_deliveries, EXPECTED_COUNTS["delivery_jobs"])

    def test_refrigerated_work_has_feasible_resources(self) -> None:
        cold_work = self.connection.execute(
            """
            SELECT SUM(weight_kg) AS weight_kg, SUM(volume_m3) AS volume_m3
            FROM delivery_jobs
            WHERE requires_refrigeration = 1
            """
        ).fetchone()
        cold_vehicle = self.connection.execute(
            """
            SELECT capacity_kg, capacity_m3
            FROM vehicles
            JOIN resource_availability USING (vehicle_id)
            WHERE refrigerated = 1 AND status = 'available'
            """
        ).fetchone()
        certified_driver_count = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM drivers
            JOIN resource_availability USING (driver_id)
            WHERE cold_chain_certified = 1 AND status = 'available'
            """
        ).fetchone()[0]

        self.assertLessEqual(cold_work["weight_kg"], cold_vehicle["capacity_kg"])
        self.assertLessEqual(cold_work["volume_m3"], cold_vehicle["capacity_m3"])
        self.assertGreaterEqual(certified_driver_count, 1)

    def test_seed_contains_real_planning_tradeoffs(self) -> None:
        maintenance_count = self.connection.execute(
            "SELECT COUNT(*) FROM resource_availability WHERE status = 'maintenance'"
        ).fetchone()[0]
        leave_count = self.connection.execute(
            "SELECT COUNT(*) FROM resource_availability WHERE status = 'leave'"
        ).fetchone()[0]
        restricted_job = self.connection.execute(
            """
            SELECT weight_kg, volume_m3
            FROM delivery_jobs
            WHERE required_vehicle_class = 'light_truck'
            """
        ).fetchone()
        available_light_trucks = self.connection.execute(
            """
            SELECT COUNT(*)
            FROM vehicles
            JOIN resource_availability USING (vehicle_id)
            WHERE vehicle_class = 'light_truck' AND status = 'available'
            """
        ).fetchone()[0]

        self.assertEqual(maintenance_count, 1)
        self.assertEqual(leave_count, 1)
        self.assertIsNotNone(restricted_job)
        self.assertEqual(available_light_trucks, 1)

    def test_expected_query_indexes_exist(self) -> None:
        index_names = {
            row["name"]
            for row in self.connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'index'"
            )
        }
        self.assertTrue(
            {
                "idx_availability_vehicle",
                "idx_availability_driver",
                "idx_delivery_jobs_work_queue",
                "idx_delivery_jobs_destination",
                "idx_operational_rules_scope",
            }.issubset(index_names)
        )

    def test_rebuild_is_deterministic_except_for_date(self) -> None:
        second_path = Path(self.temporary_directory.name) / "second.db"
        second_date = date(2026, 8, 26)
        build_database(second_path, second_date)

        second_connection = sqlite3.connect(second_path)
        try:
            first_dump = "\n".join(self.connection.iterdump()).replace(
                PLANNING_DATE.isoformat(), "<PLANNING_DATE>"
            )
            second_dump = "\n".join(second_connection.iterdump()).replace(
                second_date.isoformat(), "<PLANNING_DATE>"
            )
        finally:
            second_connection.close()

        self.assertEqual(first_dump, second_dump)


if __name__ == "__main__":
    unittest.main()
