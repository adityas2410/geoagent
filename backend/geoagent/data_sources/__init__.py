"""Connected organizational data-source subsystem."""

from .data_source_contracts import QuerySpec
from .source_manager import DataSourceService

__all__ = ["DataSourceService", "QuerySpec"]
