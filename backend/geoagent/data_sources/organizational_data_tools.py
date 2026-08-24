"""Tools owned by the Organizational Data Agent."""

from __future__ import annotations

from typing import Any

from google.adk.tools.tool_context import ToolContext
from pydantic import ValidationError

from .data_source_contracts import DataSourceError
from .data_source_contracts import QuerySpec
from .source_manager import get_data_source_service


def _mission_access(tool_context: ToolContext) -> tuple[str, set[str]]:
    workspace_id = tool_context.state.get("workspace_id")
    authorized_source_ids = tool_context.state.get("authorized_source_ids")
    if not isinstance(workspace_id, str) or not isinstance(authorized_source_ids, list):
        raise DataSourceError(
            "SOURCE_CONTEXT_MISSING",
            "This Mission has no organizational data-source authorization context.",
            403,
        )
    if not all(isinstance(source_id, str) for source_id in authorized_source_ids):
        raise DataSourceError(
            "SOURCE_CONTEXT_MISSING",
            "This Mission has invalid organizational data-source authorization context.",
            403,
        )
    return workspace_id, set(authorized_source_ids)


def _error_result(error: DataSourceError) -> dict[str, Any]:
    return {
        "status": "error",
        "error": {"code": error.code, "message": error.message},
    }


def list_authorized_sources(tool_context: ToolContext) -> dict[str, Any]:
    """List organizational data sources authorized for the current Mission."""
    try:
        workspace_id, authorized_source_ids = _mission_access(tool_context)
        records = get_data_source_service().list_authorized(
            workspace_id, authorized_source_ids
        )
        return {
            "status": "success",
            "sources": [record.public_dict() for record in records],
        }
    except DataSourceError as error:
        return _error_result(error)
    except Exception:
        return _error_result(
            DataSourceError(
                "SOURCE_UNAVAILABLE", "Authorized sources could not be listed.", 503
            )
        )


def inspect_source_schema(
    source_id: str,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Inspect a permitted source's entities, fields, types, and relationships.

    Args:
        source_id: Identifier returned by ``list_authorized_sources``.
    """
    try:
        workspace_id, authorized_source_ids = _mission_access(tool_context)
        service = get_data_source_service()
        record = service.require_authorized(
            workspace_id, authorized_source_ids, source_id
        )
        schema = service.inspect_source(record)
        return {
            "status": "success",
            "source_id": source_id,
            "entities": [entity.model_dump(mode="json") for entity in schema.entities],
        }
    except DataSourceError as error:
        return _error_result(error)
    except Exception:
        return _error_result(
            DataSourceError(
                "SOURCE_UNAVAILABLE", "The source schema could not be inspected.", 503
            )
        )


def query_source(
    source_id: str,
    query_spec: QuerySpec,
    tool_context: ToolContext,
) -> dict[str, Any]:
    """Run a constrained read-only query against an authorized source.

    The query may select columns, filter values, group rows, calculate count,
    sum, average, minimum, or maximum aggregates, order results, and paginate.
    Raw SQL and joins are not accepted.

    Args:
        source_id: Identifier returned by ``list_authorized_sources``.
        query_spec: Structured query containing entity, columns, filters,
            group_by, aggregates, order_by, limit, and offset.
    """
    try:
        if not isinstance(query_spec, QuerySpec):
            query_spec = QuerySpec.model_validate(query_spec)
        workspace_id, authorized_source_ids = _mission_access(tool_context)
        service = get_data_source_service()
        record = service.require_authorized(
            workspace_id, authorized_source_ids, source_id
        )
        result = service.query_source(record, query_spec)
        return {
            "status": "success",
            "source_id": source_id,
            "entity": query_spec.entity,
            **result.model_dump(mode="json"),
        }
    except ValidationError:
        return _error_result(
            DataSourceError("INVALID_QUERY", "The query specification is invalid.")
        )
    except DataSourceError as error:
        return _error_result(error)
    except Exception:
        return _error_result(
            DataSourceError("SOURCE_UNAVAILABLE", "The source query failed.", 503)
        )


__all__ = ["inspect_source_schema", "list_authorized_sources", "query_source"]
