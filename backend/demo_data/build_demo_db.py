"""Build the reproducible synthetic SQLite database used by the Kerala demo."""

from __future__ import annotations

import argparse
import os
import sqlite3
import tempfile
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


DEMO_DIRECTORY = Path(__file__).resolve().parent
DEFAULT_OUTPUT = DEMO_DIRECTORY.parent / "data" / "geoagent_demo.db"
SCHEMA_PATH = DEMO_DIRECTORY / "schema.sql"
TIMEZONE = "Asia/Kolkata"


def tomorrow_in_kerala() -> date:
    """Return tomorrow according to the demo organization's local timezone."""
    return datetime.now(ZoneInfo(TIMEZONE)).date() + timedelta(days=1)


def parse_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("planning date must use YYYY-MM-DD") from error


def local_datetime(planning_date: date, time_value: str) -> str:
    """Create a consistently sortable ISO datetime with Kerala's UTC offset."""
    return f"{planning_date.isoformat()}T{time_value}:00+05:30"


def seed_database(connection: sqlite3.Connection, planning_date: date) -> None:
    metadata = [
        ("dataset_name", "GeoAgent Kerala goods-transport demonstration"),
        ("provenance", "synthetic"),
        ("planning_date", planning_date.isoformat()),
        ("timezone", TIMEZONE),
    ]
    connection.executemany("INSERT INTO dataset_metadata VALUES (?, ?)", metadata)

    locations = [
        ("LOC-DEPOT-01", "Central Distribution Depot", "Industrial Development Area, Kalamassery", "Kalamassery", "Ernakulam", "Kerala", "683104", "IN", None, None, "unresolved", "Enter through the main freight gate", 1),
        ("LOC-CUST-01", "Kakkanad Retail Centre", "Kakkanad", "Kakkanad", "Ernakulam", "Kerala", "682030", "IN", None, None, "unresolved", None, 1),
        ("LOC-CUST-02", "Aluva Medical Store", "Aluva", "Aluva", "Ernakulam", "Kerala", "683101", "IN", None, None, "unresolved", "Use the receiving entrance", 1),
        ("LOC-CUST-03", "Edappally Supermarket", "Edappally", "Edappally", "Ernakulam", "Kerala", "682024", "IN", None, None, "unresolved", None, 1),
        ("LOC-CUST-04", "Tripunithura Foods", "Thrippunithura", "Thrippunithura", "Ernakulam", "Kerala", "682301", "IN", None, None, "unresolved", None, 1),
        ("LOC-CUST-05", "Fort Kochi Hotel", "Fort Kochi", "Kochi", "Ernakulam", "Kerala", "682001", "IN", None, None, "unresolved", "Call receiving desk on arrival", 1),
        ("LOC-CUST-06", "Vyttila Convenience Store", "Vyttila", "Kochi", "Ernakulam", "Kerala", "682019", "IN", None, None, "unresolved", None, 1),
        ("LOC-CUST-07", "Maradu Wholesale Outlet", "Maradu", "Maradu", "Ernakulam", "Kerala", "682304", "IN", None, None, "unresolved", "Rear loading area accommodates light trucks", 1),
        ("LOC-CUST-08", "Angamaly Pharmacy", "Angamaly", "Angamaly", "Ernakulam", "Kerala", "683572", "IN", None, None, "unresolved", "Temperature-controlled handoff required", 1),
        ("LOC-CUST-09", "Perumbavoor Retail Store", "Perumbavoor", "Perumbavoor", "Ernakulam", "Kerala", "683542", "IN", None, None, "unresolved", None, 1),
        ("LOC-CUST-10", "North Paravur Market", "North Paravur", "North Paravur", "Ernakulam", "Kerala", "683513", "IN", None, None, "unresolved", None, 1),
    ]
    connection.executemany(
        "INSERT INTO locations VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        locations,
    )

    connection.execute(
        "INSERT INTO facilities VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("FAC-001", "Central Distribution Depot", "LOC-DEPOT-01", "distribution_depot", "06:00", "20:00", 2, 20, 1),
    )

    customer_details = [
        ("CUST-001", "Kakkanad Retail Centre", "LOC-CUST-01", "retail", 20, 1),
        ("CUST-002", "Aluva Medical Store", "LOC-CUST-02", "pharmacy", 20, 1),
        ("CUST-003", "Edappally Supermarket", "LOC-CUST-03", "supermarket", 25, 1),
        ("CUST-004", "Tripunithura Foods", "LOC-CUST-04", "retail", 20, 1),
        ("CUST-005", "Fort Kochi Hotel", "LOC-CUST-05", "hospitality", 30, 1),
        ("CUST-006", "Vyttila Convenience Store", "LOC-CUST-06", "retail", 15, 1),
        ("CUST-007", "Maradu Wholesale Outlet", "LOC-CUST-07", "wholesale", 30, 1),
        ("CUST-008", "Angamaly Pharmacy", "LOC-CUST-08", "pharmacy", 20, 1),
        ("CUST-009", "Perumbavoor Retail Store", "LOC-CUST-09", "retail", 20, 1),
        ("CUST-010", "North Paravur Market", "LOC-CUST-10", "market", 25, 1),
    ]
    connection.executemany("INSERT INTO customers VALUES (?, ?, ?, ?, ?, ?)", customer_details)

    vehicles = [
        ("VEH-001", "KL-07-GA-1010", "light_truck", "FAC-001", 1800.0, 10.0, 0, 300.0, 1),
        ("VEH-002", "KL-07-GA-2020", "cargo_van", "FAC-001", 900.0, 5.0, 0, 250.0, 1),
        ("VEH-003", "KL-07-GA-3030", "refrigerated_van", "FAC-001", 750.0, 4.0, 1, 250.0, 1),
        ("VEH-004", "KL-07-GA-4040", "mini_truck", "FAC-001", 1200.0, 7.0, 0, 275.0, 1),
        ("VEH-005", "KL-07-GA-5050", "cargo_van", "FAC-001", 700.0, 4.0, 0, 225.0, 1),
    ]
    connection.executemany("INSERT INTO vehicles VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", vehicles)

    drivers = [
        ("DRV-001", "Arun Nair", "EMP-101", "HGV", 1, "FAC-001", 600, 1),
        ("DRV-002", "Meera Das", "EMP-102", "LMV", 1, "FAC-001", 540, 1),
        ("DRV-003", "Nikhil Kumar", "EMP-103", "HGV", 0, "FAC-001", 600, 1),
        ("DRV-004", "Fathima Rahman", "EMP-104", "LMV", 0, "FAC-001", 540, 1),
        ("DRV-005", "Joseph Mathew", "EMP-105", "HGV", 0, "FAC-001", 600, 1),
        ("DRV-006", "Anjali Menon", "EMP-106", "LMV", 0, "FAC-001", 540, 1),
    ]
    connection.executemany("INSERT INTO drivers VALUES (?, ?, ?, ?, ?, ?, ?, ?)", drivers)

    availability = []
    for vehicle_number in range(1, 6):
        vehicle_id = f"VEH-{vehicle_number:03d}"
        is_maintenance = vehicle_id == "VEH-004"
        availability.append(
            (
                f"AVL-{vehicle_id}",
                vehicle_id,
                None,
                local_datetime(planning_date, "06:30"),
                local_datetime(planning_date, "18:00"),
                "maintenance" if is_maintenance else "available",
                "Scheduled brake inspection" if is_maintenance else None,
            )
        )
    for driver_number in range(1, 7):
        driver_id = f"DRV-{driver_number:03d}"
        is_on_leave = driver_id == "DRV-006"
        availability.append(
            (
                f"AVL-{driver_id}",
                None,
                driver_id,
                local_datetime(planning_date, "06:30"),
                local_datetime(planning_date, "18:00"),
                "leave" if is_on_leave else "available",
                "Approved personal leave" if is_on_leave else None,
            )
        )
    connection.executemany(
        "INSERT INTO resource_availability VALUES (?, ?, ?, ?, ?, ?, ?)",
        availability,
    )

    job_values = [
        ("JOB-001", "ORD-1001", "CUST-002", 5, "LOC-CUST-02", 280.0, 1.4, "08:00", "09:30", 20, 1, "refrigerated_van", "Maintain cold-chain handoff"),
        ("JOB-002", "ORD-1002", "CUST-008", 5, "LOC-CUST-08", 220.0, 1.1, "09:00", "11:00", 20, 1, "refrigerated_van", "Temperature-controlled handoff required"),
        ("JOB-003", "ORD-1003", "CUST-001", 3, "LOC-CUST-01", 600.0, 3.0, "09:00", "13:00", 20, 0, None, None),
        ("JOB-004", "ORD-1004", "CUST-003", 4, "LOC-CUST-03", 450.0, 2.2, "08:30", "12:00", 25, 0, None, None),
        ("JOB-005", "ORD-1005", "CUST-004", 3, "LOC-CUST-04", 520.0, 2.8, "11:00", "15:00", 20, 0, None, None),
        ("JOB-006", "ORD-1006", "CUST-005", 4, "LOC-CUST-05", 300.0, 1.8, "10:00", "12:00", 30, 0, None, "Call receiving desk on arrival"),
        ("JOB-007", "ORD-1007", "CUST-006", 2, "LOC-CUST-06", 250.0, 1.2, "13:00", "17:00", 15, 0, None, None),
        ("JOB-008", "ORD-1008", "CUST-007", 3, "LOC-CUST-07", 1050.0, 5.5, "09:00", "16:00", 30, 0, "light_truck", "Light-truck delivery bay only"),
        ("JOB-009", "ORD-1009", "CUST-009", 2, "LOC-CUST-09", 500.0, 2.5, "12:00", "16:00", 20, 0, None, None),
        ("JOB-010", "ORD-1010", "CUST-010", 2, "LOC-CUST-10", 350.0, 1.9, "14:00", "17:00", 25, 0, None, None),
    ]
    jobs = [
        (
            job_id,
            reference,
            customer_id,
            planning_date.isoformat(),
            "pending",
            priority,
            "LOC-DEPOT-01",
            destination_id,
            weight,
            volume,
            local_datetime(planning_date, earliest),
            local_datetime(planning_date, latest),
            service_minutes,
            refrigerated,
            vehicle_class,
            instructions,
        )
        for (
            job_id,
            reference,
            customer_id,
            priority,
            destination_id,
            weight,
            volume,
            earliest,
            latest,
            service_minutes,
            refrigerated,
            vehicle_class,
            instructions,
        ) in job_values
    ]
    connection.executemany(
        "INSERT INTO delivery_jobs VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        jobs,
    )

    rules = [
        ("RULE-001", "RETURN_TO_HOME_FACILITY", "Vehicles must return to their home facility", "organization", None, "boolean", None, None, 1, None, "hard", 1),
        ("RULE-002", "DRIVER_BREAK_AFTER_MINUTES", "A driver must stop for a break after this much continuous work", "organization", None, "number", 240.0, None, None, "minutes", "hard", 1),
        ("RULE-003", "DRIVER_BREAK_DURATION", "Required driver break duration", "organization", None, "number", 30.0, None, None, "minutes", "hard", 1),
        ("RULE-004", "MAX_LOADING_CONCURRENCY", "Maximum vehicles that may load simultaneously", "facility", "FAC-001", "number", 2.0, None, None, "vehicles", "hard", 1),
        ("RULE-005", "COLD_CHAIN_CERTIFICATION_REQUIRED", "Refrigerated loads require a cold-chain-certified driver", "organization", None, "boolean", None, None, 1, None, "hard", 1),
        ("RULE-006", "DELIVERY_WINDOW_GRACE", "Allowed lateness beyond a delivery window", "organization", None, "number", 0.0, None, None, "minutes", "hard", 1),
        ("RULE-007", "TARGET_VEHICLE_UTILIZATION", "Preferred minimum loaded-capacity utilization", "organization", None, "number", 70.0, None, None, "percent", "warning", 1),
        ("RULE-008", "PREFER_FEWER_ACTIVE_VEHICLES", "Prefer plans using fewer vehicles when otherwise equivalent", "organization", None, "boolean", None, None, 1, None, "warning", 1),
    ]
    connection.executemany(
        "INSERT INTO operational_rules VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        rules,
    )


def build_database(output_path: Path, planning_date: date) -> Path:
    """Build and atomically replace a demo database at ``output_path``."""
    output_path = output_path.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_file = tempfile.NamedTemporaryFile(
        prefix=f".{output_path.stem}-",
        suffix=".tmp",
        dir=output_path.parent,
        delete=False,
    )
    temporary_path = Path(temporary_file.name)
    temporary_file.close()

    try:
        connection = sqlite3.connect(temporary_path)
        try:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
            seed_database(connection, planning_date)
            foreign_key_errors = connection.execute("PRAGMA foreign_key_check").fetchall()
            if foreign_key_errors:
                raise RuntimeError(f"seed data violates foreign keys: {foreign_key_errors}")
            connection.commit()
        finally:
            connection.close()
        os.replace(temporary_path, output_path)
    except Exception:
        temporary_path.unlink(missing_ok=True)
        raise

    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--planning-date",
        type=parse_date,
        default=tomorrow_in_kerala(),
        help="delivery date in YYYY-MM-DD format (default: tomorrow in Asia/Kolkata)",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=DEFAULT_OUTPUT,
        help=f"database output path (default: {DEFAULT_OUTPUT})",
    )
    arguments = parser.parse_args()

    output_path = build_database(arguments.output, arguments.planning_date)
    print(f"Built synthetic demo database: {output_path}")
    print(f"Planning date: {arguments.planning_date.isoformat()}")


if __name__ == "__main__":
    main()
