# GeoAgent

**GeoAgent** is an autonomous multi-agent system for planning and coordinating real-world operations using organizational data and live geospatial intelligence.

A user connects their operational data, creates a Mission with a simple business objective, and GeoAgent determines how to investigate the available information, evaluate constraints, use geographic context, and produce an operational plan.

For example:

> Plan tomorrow's deliveries.

The user specifies the outcome. GeoAgent determines the relevant resources, locations, deadlines, routes, risks, calculations, and follow-up actions.

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
