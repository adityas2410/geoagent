"""Strict read-only SQLite adapter and structured query compiler."""

from __future__ import annotations

import base64
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from .data_source_contracts import DataSourceError
from .data_source_contracts import QueryResult
from .data_source_contracts import QuerySpec
from .data_source_contracts import SchemaColumn
from .data_source_contracts import SchemaEntity
from .data_source_contracts import SchemaForeignKey
from .data_source_contracts import SchemaInspection
from .data_source_contracts import SQLiteValidation


SQLITE_HEADER = b"SQLite format 3\x00"


def quote_identifier(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


class SQLiteAdapter:
    source_type = "sqlite"

    def open_read_only(self, database_path: Path) -> sqlite3.Connection:
        uri = f"{database_path.resolve().as_uri()}?mode=ro&immutable=1"
        try:
            connection = sqlite3.connect(uri, uri=True)
        except sqlite3.Error as error:
            raise DataSourceError(
                "SOURCE_UNAVAILABLE",
                "The SQLite source could not be opened.",
                503,
            ) from error
        try:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA query_only = ON")
            connection.execute("PRAGMA trusted_schema = OFF")
        except sqlite3.Error as error:
            connection.close()
            raise DataSourceError(
                "SOURCE_UNAVAILABLE",
                "The SQLite source could not be initialized.",
                503,
            ) from error
        return connection

    def validate(self, database_path: Path) -> SQLiteValidation:
        try:
            with database_path.open("rb") as database_file:
                header = database_file.read(len(SQLITE_HEADER))
        except OSError as error:
            raise DataSourceError("INVALID_SQLITE", "The uploaded file could not be read.") from error

        if header != SQLITE_HEADER:
            raise DataSourceError(
                "INVALID_SQLITE",
                "The uploaded file is not an unencrypted SQLite database.",
            )

        try:
            connection = self.open_read_only(database_path)
        except DataSourceError as error:
            raise DataSourceError(
                "INVALID_SQLITE", "The SQLite database is corrupt or unsupported."
            ) from error
        try:
            try:
                check_result = connection.execute("PRAGMA quick_check").fetchone()
            except sqlite3.Error as error:
                raise DataSourceError(
                    "INVALID_SQLITE", "The SQLite database is corrupt or unsupported."
                ) from error
            if check_result is None or check_result[0] != "ok":
                raise DataSourceError("INVALID_SQLITE", "The SQLite integrity check failed.")

            schema = self._inspect_with_connection(connection)
            if not schema.entities:
                raise DataSourceError(
                    "EMPTY_SQLITE", "The SQLite database contains no user tables or views."
                )
            provenance = self._detect_provenance(connection, schema)
            return SQLiteValidation(schema_inspection=schema, provenance=provenance)
        finally:
            connection.close()

    def inspect_schema(self, database_path: Path) -> SchemaInspection:
        connection = self.open_read_only(database_path)
        try:
            return self._inspect_with_connection(connection)
        finally:
            connection.close()

    def _inspect_with_connection(self, connection: sqlite3.Connection) -> SchemaInspection:
        try:
            entity_rows = connection.execute(
                """
                SELECT name, type
                FROM sqlite_schema
                WHERE type IN ('table', 'view') AND name NOT LIKE 'sqlite_%'
                ORDER BY name
                """
            ).fetchall()
            entities: list[SchemaEntity] = []
            for entity_row in entity_rows:
                name = entity_row["name"]
                column_rows = connection.execute(
                    f"PRAGMA table_info({quote_identifier(name)})"
                ).fetchall()
                columns = [
                    SchemaColumn(
                        name=row["name"],
                        declared_type=row["type"] or "",
                        nullable=not bool(row["notnull"]) and not bool(row["pk"]),
                        primary_key_position=row["pk"],
                    )
                    for row in column_rows
                ]
                foreign_keys: list[SchemaForeignKey] = []
                if entity_row["type"] == "table":
                    foreign_key_rows = connection.execute(
                        f"PRAGMA foreign_key_list({quote_identifier(name)})"
                    ).fetchall()
                    foreign_keys = [
                        SchemaForeignKey(
                            from_column=row["from"],
                            referenced_entity=row["table"],
                            referenced_column=row["to"],
                        )
                        for row in foreign_key_rows
                    ]
                entities.append(
                    SchemaEntity(
                        name=name,
                        entity_type=entity_row["type"],
                        columns=columns,
                        foreign_keys=foreign_keys,
                    )
                )
            return SchemaInspection(entities=entities)
        except sqlite3.Error as error:
            raise DataSourceError(
                "SOURCE_UNAVAILABLE", "The SQLite schema could not be inspected.", 503
            ) from error

    def _detect_provenance(
        self, connection: sqlite3.Connection, schema: SchemaInspection
    ) -> str:
        metadata = next(
            (entity for entity in schema.entities if entity.name == "dataset_metadata"),
            None,
        )
        if metadata and {column.name for column in metadata.columns} >= {"key", "value"}:
            try:
                row = connection.execute(
                    'SELECT "value" FROM "dataset_metadata" WHERE "key" = ? LIMIT 1',
                    ("provenance",),
                ).fetchone()
                if row and row[0] == "synthetic":
                    return "synthetic"
            except sqlite3.Error:
                pass
        return "unverified_user_provided"

    def query(self, database_path: Path, query_spec: QuerySpec | dict[str, Any]) -> QueryResult:
        try:
            spec = (
                query_spec
                if isinstance(query_spec, QuerySpec)
                else QuerySpec.model_validate(query_spec)
            )
        except ValidationError as error:
            raise DataSourceError("INVALID_QUERY", "The query specification is invalid.") from error

        connection = self.open_read_only(database_path)
        try:
            schema = self._inspect_with_connection(connection)
            sql, parameters, output_columns = self._compile_query(schema, spec)
            try:
                fetched_rows = connection.execute(sql, parameters).fetchall()
            except sqlite3.Error as error:
                raise DataSourceError("INVALID_QUERY", "The source query could not be executed.") from error
            has_more = len(fetched_rows) > spec.limit
            rows = [
                {key: self._json_safe_value(row[key]) for key in row.keys()}
                for row in fetched_rows[: spec.limit]
            ]
            return QueryResult(
                columns=output_columns,
                rows=rows,
                returned_count=len(rows),
                has_more=has_more,
            )
        finally:
            connection.close()

    def _compile_query(
        self, schema: SchemaInspection, spec: QuerySpec
    ) -> tuple[str, list[Any], list[str]]:
        entity = next((item for item in schema.entities if item.name == spec.entity), None)
        if entity is None:
            raise DataSourceError("INVALID_QUERY", "The requested entity does not exist.")

        available_columns = {column.name for column in entity.columns}

        def require_column(column: str) -> None:
            if column not in available_columns:
                raise DataSourceError(
                    "INVALID_QUERY", f"Column '{column}' does not exist on the requested entity."
                )

        for column in spec.columns + spec.group_by:
            require_column(column)
        for query_filter in spec.filters:
            require_column(query_filter.column)
        for aggregate in spec.aggregates:
            if aggregate.column is not None:
                require_column(aggregate.column)

        if spec.aggregates:
            if any(column not in spec.group_by for column in spec.columns):
                raise DataSourceError(
                    "INVALID_QUERY", "Selected non-aggregate columns must appear in group_by."
                )
            selected_columns = spec.columns
        else:
            if spec.group_by:
                raise DataSourceError(
                    "INVALID_QUERY", "group_by requires at least one aggregate."
                )
            selected_columns = spec.columns or [column.name for column in entity.columns]

        select_parts = [quote_identifier(column) for column in selected_columns]
        aggregate_aliases: set[str] = set()
        for aggregate in spec.aggregates:
            if aggregate.alias in available_columns or aggregate.alias in aggregate_aliases:
                raise DataSourceError("INVALID_QUERY", "Aggregate aliases must be unique.")
            aggregate_aliases.add(aggregate.alias)
            aggregate_target = (
                "*" if aggregate.column is None else quote_identifier(aggregate.column)
            )
            select_parts.append(
                f"{aggregate.function.upper()}({aggregate_target}) AS {quote_identifier(aggregate.alias)}"
            )
        if not select_parts:
            raise DataSourceError("INVALID_QUERY", "The query selects no output columns.")

        parameters: list[Any] = []
        predicates: list[str] = []
        operator_sql = {
            "eq": "=",
            "ne": "!=",
            "lt": "<",
            "lte": "<=",
            "gt": ">",
            "gte": ">=",
        }
        for query_filter in spec.filters:
            column_sql = quote_identifier(query_filter.column)
            if query_filter.operator in {"eq", "ne"} and query_filter.value is None:
                predicates.append(
                    f"{column_sql} IS {'NOT ' if query_filter.operator == 'ne' else ''}NULL"
                )
            elif query_filter.operator in operator_sql:
                predicates.append(f"{column_sql} {operator_sql[query_filter.operator]} ?")
                parameters.append(query_filter.value)
            elif query_filter.operator == "in":
                placeholders = ", ".join("?" for _ in query_filter.value)
                predicates.append(f"{column_sql} IN ({placeholders})")
                parameters.extend(query_filter.value)
            elif query_filter.operator == "is_null":
                predicates.append(f"{column_sql} IS {'NULL' if query_filter.value else 'NOT NULL'}")
            elif query_filter.operator == "contains":
                escaped = (
                    query_filter.value.replace("\\", "\\\\")
                    .replace("%", "\\%")
                    .replace("_", "\\_")
                )
                predicates.append(f"{column_sql} LIKE ? ESCAPE '\\'")
                parameters.append(f"%{escaped}%")

        output_columns = selected_columns + [aggregate.alias for aggregate in spec.aggregates]
        for order in spec.order_by:
            if order.column not in output_columns:
                raise DataSourceError(
                    "INVALID_QUERY", "Ordering is limited to selected columns and aggregate aliases."
                )

        sql_parts = [
            "SELECT " + ", ".join(select_parts),
            "FROM " + quote_identifier(entity.name),
        ]
        if predicates:
            sql_parts.append("WHERE " + " AND ".join(predicates))
        if spec.group_by:
            sql_parts.append(
                "GROUP BY " + ", ".join(quote_identifier(column) for column in spec.group_by)
            )
        if spec.order_by:
            sql_parts.append(
                "ORDER BY "
                + ", ".join(
                    f"{quote_identifier(order.column)} {order.direction.upper()}"
                    for order in spec.order_by
                )
            )
        sql_parts.append("LIMIT ? OFFSET ?")
        parameters.extend([spec.limit + 1, spec.offset])
        return " ".join(sql_parts), parameters, output_columns

    @staticmethod
    def _json_safe_value(value: Any) -> Any:
        if isinstance(value, bytes):
            return {
                "encoding": "base64",
                "data": base64.b64encode(value).decode("ascii"),
            }
        return value
