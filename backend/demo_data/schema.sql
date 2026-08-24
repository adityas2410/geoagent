PRAGMA foreign_keys = ON;

CREATE TABLE dataset_metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE locations (
    location_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    address_line TEXT NOT NULL,
    locality TEXT NOT NULL,
    district TEXT NOT NULL,
    state TEXT NOT NULL,
    postal_code TEXT NOT NULL,
    country_code TEXT NOT NULL CHECK (length(country_code) = 2),
    latitude REAL,
    longitude REAL,
    coordinate_provenance TEXT NOT NULL DEFAULT 'unresolved',
    access_notes TEXT,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (
        (latitude IS NULL AND longitude IS NULL)
        OR (latitude BETWEEN -90 AND 90 AND longitude BETWEEN -180 AND 180)
    )
);

CREATE TABLE facilities (
    facility_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(location_id),
    facility_type TEXT NOT NULL,
    opens_at TEXT NOT NULL,
    closes_at TEXT NOT NULL,
    loading_bays INTEGER NOT NULL CHECK (loading_bays > 0),
    default_service_minutes INTEGER NOT NULL CHECK (default_service_minutes > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (opens_at < closes_at)
);

CREATE TABLE customers (
    customer_id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    location_id TEXT NOT NULL REFERENCES locations(location_id),
    customer_type TEXT NOT NULL,
    default_service_minutes INTEGER NOT NULL CHECK (default_service_minutes > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE vehicles (
    vehicle_id TEXT PRIMARY KEY,
    registration_number TEXT NOT NULL UNIQUE,
    vehicle_class TEXT NOT NULL,
    home_facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    capacity_kg REAL NOT NULL CHECK (capacity_kg > 0),
    capacity_m3 REAL NOT NULL CHECK (capacity_m3 > 0),
    refrigerated INTEGER NOT NULL CHECK (refrigerated IN (0, 1)),
    max_route_km REAL CHECK (max_route_km IS NULL OR max_route_km > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE drivers (
    driver_id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    employee_code TEXT NOT NULL UNIQUE,
    license_class TEXT NOT NULL,
    cold_chain_certified INTEGER NOT NULL CHECK (cold_chain_certified IN (0, 1)),
    home_facility_id TEXT NOT NULL REFERENCES facilities(facility_id),
    max_shift_minutes INTEGER NOT NULL CHECK (max_shift_minutes > 0),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1))
);

CREATE TABLE resource_availability (
    availability_id TEXT PRIMARY KEY,
    vehicle_id TEXT REFERENCES vehicles(vehicle_id),
    driver_id TEXT REFERENCES drivers(driver_id),
    available_from TEXT NOT NULL,
    available_until TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('available', 'maintenance', 'leave')),
    reason TEXT,
    CHECK ((vehicle_id IS NOT NULL) <> (driver_id IS NOT NULL)),
    CHECK (available_from < available_until)
);

CREATE TABLE delivery_jobs (
    job_id TEXT PRIMARY KEY,
    external_reference TEXT NOT NULL UNIQUE,
    customer_id TEXT NOT NULL REFERENCES customers(customer_id),
    service_date TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('pending', 'completed', 'cancelled')),
    priority INTEGER NOT NULL CHECK (priority BETWEEN 1 AND 5),
    pickup_location_id TEXT NOT NULL REFERENCES locations(location_id),
    delivery_location_id TEXT NOT NULL REFERENCES locations(location_id),
    weight_kg REAL NOT NULL CHECK (weight_kg > 0),
    volume_m3 REAL NOT NULL CHECK (volume_m3 > 0),
    earliest_delivery_at TEXT NOT NULL,
    latest_delivery_at TEXT NOT NULL,
    service_minutes INTEGER NOT NULL CHECK (service_minutes > 0),
    requires_refrigeration INTEGER NOT NULL CHECK (requires_refrigeration IN (0, 1)),
    required_vehicle_class TEXT,
    special_instructions TEXT,
    CHECK (pickup_location_id <> delivery_location_id),
    CHECK (earliest_delivery_at < latest_delivery_at)
);

CREATE TABLE operational_rules (
    rule_id TEXT PRIMARY KEY,
    rule_code TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL,
    scope_type TEXT NOT NULL CHECK (scope_type IN ('organization', 'facility')),
    scope_id TEXT,
    value_type TEXT NOT NULL CHECK (value_type IN ('number', 'text', 'boolean')),
    numeric_value REAL,
    text_value TEXT,
    boolean_value INTEGER CHECK (boolean_value IN (0, 1)),
    unit TEXT,
    severity TEXT NOT NULL CHECK (severity IN ('hard', 'warning')),
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    CHECK (
        (value_type = 'number' AND numeric_value IS NOT NULL AND text_value IS NULL AND boolean_value IS NULL)
        OR (value_type = 'text' AND numeric_value IS NULL AND text_value IS NOT NULL AND boolean_value IS NULL)
        OR (value_type = 'boolean' AND numeric_value IS NULL AND text_value IS NULL AND boolean_value IS NOT NULL)
    )
);

CREATE INDEX idx_locations_locality ON locations(locality);
CREATE INDEX idx_customers_location ON customers(location_id);
CREATE INDEX idx_availability_vehicle ON resource_availability(vehicle_id, available_from, available_until);
CREATE INDEX idx_availability_driver ON resource_availability(driver_id, available_from, available_until);
CREATE INDEX idx_delivery_jobs_work_queue ON delivery_jobs(service_date, status, priority DESC);
CREATE INDEX idx_delivery_jobs_destination ON delivery_jobs(delivery_location_id);
CREATE INDEX idx_operational_rules_scope ON operational_rules(scope_type, scope_id, active);
