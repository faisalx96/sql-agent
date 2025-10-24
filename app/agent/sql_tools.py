from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional

from .tools import ToolSpec
from ..db import Database


def _normalize_params(params: Any) -> Any:
    # SQLite supports positional (list/tuple) or named (dict) params.
    if params is None:
        return None
    if isinstance(params, (list, tuple, dict)):
        return params
    # Try not to be picky; pass through scalars in a 1-item list
    return [params]


def make_sql_tools(db: Database) -> List[ToolSpec]:
    def sql_schema(_: Dict[str, Any]) -> Dict[str, Any]:
        return db.schema()

    def sql_query(args: Dict[str, Any]) -> Dict[str, Any]:
        sql = str(args.get("sql", "")).strip()
        params = _normalize_params(args.get("params"))
        max_rows = int(args.get("max_rows", 100))
        result = db.query(sql, params=params, max_rows=max_rows)
        return {
            "columns": result.columns,
            "rows": result.rows,
            "rowcount": result.rowcount,
        }

    return [
        ToolSpec(
            name="sql_schema",
            description="Return database schema (tables, columns, types, primary keys, row counts).",
            schema={"type": "object", "properties": {}},
            func=sql_schema,
        ),
        ToolSpec(
            name="sql_query",
            description="Execute a read-only SQL SELECT/CTE and return rows (up to max_rows).",
            schema={
                "type": "object",
                "required": ["sql"],
                "properties": {
                    "sql": {"type": "string"},
                    "params": {"description": "Positional list or named dict parameters"},
                    "max_rows": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                },
            },
            func=sql_query,
        ),
    ]
