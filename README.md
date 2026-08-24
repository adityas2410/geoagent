# GeoAgent

**GeoAgent** is an autonomous multi-agent system for planning and coordinating real-world operations using organizational data and live geospatial intelligence.

A user connects their operational data, creates a Mission with a simple business objective, and GeoAgent determines how to investigate the available information, evaluate constraints, use geographic context, and produce an operational plan.

For example:

> Plan tomorrow's deliveries.

The user specifies the outcome. GeoAgent determines the relevant resources, locations, deadlines, routes, risks, calculations, and follow-up actions.

## Synthetic demo data

Generate the reproducible Kerala goods-transport SQLite database from the repository root:

```bash
python backend/demo_data/build_demo_db.py --planning-date 2026-08-25
```

The generated `backend/data/geoagent_demo.db` is intentionally gitignored. Its committed schema and builder contain synthetic organizational records only; routes and Mission outputs are produced by GeoAgent rather than stored in the source database.

## Connect a SQLite source

Start the API from `backend` after configuring `.env`:

```bash
uvicorn geoagent.app:app --reload
```

Connect a real SQLite file to a Workspace:

```bash
curl -F "name=Kerala Operations" -F "file=@backend/data/geoagent_demo.db" http://localhost:8000/api/workspaces/demo-workspace/data-sources/sqlite
```

Local development stores uploaded sources under `backend/data/sources`. Set `GEOAGENT_SOURCE_STORAGE=gcs` and `GEOAGENT_SOURCE_BUCKET` in Cloud Run; connection metadata is always stored under the Workspace in the named Firestore database `geoagentdb`.

## Organizational data architecture

GeoAgent keeps operational data separate from its own application state:

- **SQLite** contains the organization's operational records, such as jobs, resources, locations, availability, and rules.
- **Cloud Storage** holds uploaded SQLite files in production because Cloud Run's local filesystem is temporary. Local development uses `backend/data/sources` instead.
- **Firestore** contains only GeoAgent metadata and state: connected-source records, Mission state, events, clarification state, and generated plans. It does not duplicate operational rows from SQLite.

Connecting a SQLite source follows this path:

```text
UI upload
  -> app.py receives the file
  -> source_manager.py coordinates the connection
  -> sqlite_source.py validates and inspects SQLite
  -> source_files.py stores the database file
  -> source_records.py registers its metadata in Firestore
```

When the Organizational Data Agent reads a source:

```text
agent.py
  -> agent_tools.py checks the Mission's permitted source IDs
  -> source_manager.py coordinates access
  -> source_records.py loads connection metadata
  -> source_files.py retrieves the SQLite file
  -> sqlite_source.py executes a constrained read-only query
```

The database-related Python files have distinct responsibilities:

| File | Responsibility |
|---|---|
| `backend/geoagent/app.py` | HTTP endpoints for uploading and listing connected sources. |
| `backend/geoagent/data_sources/agent_tools.py` | The three ADK tools: list sources, inspect schema, and query a source. |
| `backend/geoagent/data_sources/source_manager.py` | Coordinates validation, storage, registration, Mission source checks, and queries. |
| `backend/geoagent/data_sources/sqlite_source.py` | Validates SQLite, discovers its schema, and compiles safe read-only queries. |
| `backend/geoagent/data_sources/source_files.py` | Stores and retrieves SQLite files locally or through Cloud Storage. |
| `backend/geoagent/data_sources/source_records.py` | Stores and retrieves connection metadata through Firestore. |
| `backend/geoagent/data_sources/data_source_contracts.py` | Defines validated connection, schema, query, result, and error structures. |
| `backend/geoagent/data_sources/__init__.py` | Marks the directory as a Python package and exports common types; it contains no operational logic. |

`agent.py` imports the three functions from `agent_tools.py` and assigns them to the Organizational Data Agent. It does not contain duplicate implementations.

## Agent architecture

Each Mission runs one isolated Google ADK collaborative agent team:

```text
Mission Manager
├── Organizational Data Agent
├── Geospatial Intelligence Agent
└── Operational Planning and Validation Agent
```

The **Mission Manager** is the user-facing coordinator. It delegates work, resolves specialist findings, requests clarification when essential information cannot be discovered, and publishes the final validated plan. Its application tools are `load_mission_state`, `request_clarification`, and `publish_plan`; ADK also generates one delegation tool for each declared subagent.

The three specialists are leaf agents using `mode="single_turn"`. They do not interact with the user:

- **Organizational Data Agent:** uses `list_authorized_sources`, `inspect_source_schema`, and read-only `query_source` to discover relevant organizational facts without hard-coded domain schemas.
- **Geospatial Intelligence Agent:** uses `geocode_locations`, `search_places`, `compute_routes`, and `compute_route_matrix` to obtain physical-world facts from Google Maps capabilities.
- **Operational Planning and Validation Agent:** uses deterministic `optimize_assignments`, `calculate_plan_metrics`, and `validate_plan` functions to build and check candidate plans. It cannot publish a plan.

For human input, only the Mission Manager may call `request_clarification`. That action stores an open-ended question, places the Mission in `awaiting_input`, and stops work until the user responds through the same Mission session. Backend callbacks persist real agent and tool events for the UI; event recording is infrastructure, not an agent-controlled tool.

## Features

- **Mission-based operations:** Each objective runs as its own Mission with isolated state, activity, plan, and multi-agent execution context.
- **Multi-agent planning:** A Mission Manager coordinates capability-based agents for organizational-data investigation, geospatial intelligence, and operational planning/validation.
- **Simple business objectives:** Users describe the desired outcome without needing to understand database schemas, route optimization, or prompt engineering.
- **Flexible organizational data:** Missions work with authorized connected data sources rather than hard-coded domain tables or workflows.
- **Geospatial intelligence:** Google Maps capabilities provide location resolution, routes, journey facts, and operational geographic context.
- **Validated operational plans:** Agents combine their findings into a structured plan that can be persisted, inspected, and visualized.
- **Live operational map:** The map represents real operational state as locations, routes, resources, constraints, assignments, disruptions, and plan changes emerge.
- **Agent observability:** A synchronized activity view shows real agent actions, tool usage, results, validation, and replanning events without exposing private reasoning.
- **Autonomous reassessment:** Missions can revisit changing conditions and replan affected work when a material change occurs.
- **Parallel Mission isolation:** Multiple active Missions can operate independently while sharing only explicitly authorized organizational data.
- **Cross-Mission operations Q&A:** A later Master Operations Agent can retrieve and explain persisted state across Missions without merging their private execution contexts.
