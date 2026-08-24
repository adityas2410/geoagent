"""Shared models for connected organizational data sources."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class DataSourceError(Exception):
    """A safe, user-facing failure raised by the data-source subsystem."""

    def __init__(self, code: str, message: str, http_status: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status


class DataSourceRecord(BaseModel):
    """Private persisted representation of a connected source."""

    model_config = ConfigDict(extra="forbid")

    source_id: str
    workspace_id: str
    name: str
    source_type: Literal["sqlite"] = "sqlite"
    status: Literal["connected"] = "connected"
    provenance: Literal["synthetic", "unverified_user_provided"]
    original_filename: str
    size_bytes: int
    table_count: int
    view_count: int
    storage_key: str
    storage_generation: str
    created_at: datetime

    def public_dict(self) -> dict[str, Any]:
        return {
            "source_id": self.source_id,
            "name": self.name,
            "source_type": self.source_type,
            "status": self.status,
            "provenance": self.provenance,
            "original_filename": self.original_filename,
            "size_bytes": self.size_bytes,
            "table_count": self.table_count,
            "view_count": self.view_count,
            "created_at": self.created_at.isoformat(),
        }


class StoredObject(BaseModel):
    storage_key: str
    generation: str
    size_bytes: int


class SchemaColumn(BaseModel):
    name: str
    declared_type: str
    nullable: bool
    primary_key_position: int


class SchemaForeignKey(BaseModel):
    from_column: str
    referenced_entity: str
    referenced_column: str


class SchemaEntity(BaseModel):
    name: str
    entity_type: Literal["table", "view"]
    columns: list[SchemaColumn]
    foreign_keys: list[SchemaForeignKey] = Field(default_factory=list)


class SchemaInspection(BaseModel):
    entities: list[SchemaEntity]

    @property
    def table_count(self) -> int:
        return sum(entity.entity_type == "table" for entity in self.entities)

    @property
    def view_count(self) -> int:
        return sum(entity.entity_type == "view" for entity in self.entities)


class SQLiteValidation(BaseModel):
    schema_inspection: SchemaInspection
    provenance: Literal["synthetic", "unverified_user_provided"]


FilterOperator = Literal[
    "eq",
    "ne",
    "lt",
    "lte",
    "gt",
    "gte",
    "in",
    "is_null",
    "contains",
]


class QueryFilter(BaseModel):
    """One parameterized predicate in a constrained source query."""

    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=128)
    operator: FilterOperator
    value: Any = None

    @model_validator(mode="after")
    def validate_operator_value(self) -> QueryFilter:
        if self.operator == "in":
            if not isinstance(self.value, list) or not 1 <= len(self.value) <= 100:
                raise ValueError("the 'in' operator requires a list of 1 to 100 values")
            if any(isinstance(item, (list, dict)) for item in self.value):
                raise ValueError("the 'in' operator accepts scalar values only")
        elif self.operator == "is_null":
            if not isinstance(self.value, bool):
                raise ValueError("the 'is_null' operator requires a boolean value")
        elif self.operator == "contains":
            if not isinstance(self.value, str):
                raise ValueError("the 'contains' operator requires a string value")
        elif isinstance(self.value, (list, dict)):
            raise ValueError(f"the '{self.operator}' operator requires a scalar value")
        return self


class QueryAggregate(BaseModel):
    """An allowlisted aggregate expression."""

    model_config = ConfigDict(extra="forbid")

    function: Literal["count", "sum", "avg", "min", "max"]
    column: str | None = Field(default=None, max_length=128)
    alias: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")

    @model_validator(mode="after")
    def validate_aggregate_column(self) -> QueryAggregate:
        if self.function != "count" and self.column is None:
            raise ValueError(f"{self.function} requires a column")
        return self


class QueryOrder(BaseModel):
    model_config = ConfigDict(extra="forbid")

    column: str = Field(min_length=1, max_length=128)
    direction: Literal["asc", "desc"] = "asc"


class QuerySpec(BaseModel):
    """Structured, read-only query accepted by the Organizational Data Agent."""

    model_config = ConfigDict(extra="forbid")

    entity: str = Field(min_length=1, max_length=128)
    columns: list[str] = Field(default_factory=list, max_length=50)
    filters: list[QueryFilter] = Field(default_factory=list, max_length=20)
    group_by: list[str] = Field(default_factory=list, max_length=20)
    aggregates: list[QueryAggregate] = Field(default_factory=list, max_length=20)
    order_by: list[QueryOrder] = Field(default_factory=list, max_length=10)
    limit: int = Field(default=50, ge=1, le=200)
    offset: int = Field(default=0, ge=0, le=10_000)

    @field_validator("columns", "group_by")
    @classmethod
    def reject_duplicate_columns(cls, value: list[str]) -> list[str]:
        if len(value) != len(set(value)):
            raise ValueError("duplicate columns are not allowed")
        return value


class QueryResult(BaseModel):
    columns: list[str]
    rows: list[dict[str, Any]]
    returned_count: int
    has_more: bool
