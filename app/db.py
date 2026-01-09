from __future__ import annotations

import threading
from dataclasses import dataclass
from decimal import Decimal
from datetime import date, datetime, time
from typing import Any, Dict, List, Optional
from uuid import UUID

from sqlalchemy import create_engine, text, inspect
from sqlalchemy.engine import Engine


def _jsonify(val: Any) -> Any:
    """Convert DB types to JSON-serializable equivalents."""
    if val is None:
        return None
    if isinstance(val, Decimal):
        return float(val)
    if isinstance(val, (datetime, date, time)):
        return val.isoformat()
    if isinstance(val, UUID):
        return str(val)
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


@dataclass
class QueryResult:
    columns: List[str]
    rows: List[List[Any]]
    rowcount: int


class Database:
    def __init__(self, database_url: str):
        self._engine: Engine = create_engine(database_url, pool_pre_ping=True)
        self._lock = threading.Lock()

    @property
    def dialect(self) -> str:
        return self._engine.dialect.name

    def query(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
        max_rows: int = 100,
    ) -> QueryResult:
        sql_stripped = sql.lstrip().lower()
        if not (sql_stripped.startswith("select") or sql_stripped.startswith("with")):
            raise ValueError("sql_query only allows read-only SELECT/CTE statements")

        with self._lock, self._engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            cols = list(result.keys())
            rows = [[_jsonify(v) for v in row] for row in result.fetchmany(max_rows)]
            return QueryResult(columns=cols, rows=rows, rowcount=len(rows))

    def execute(
        self,
        sql: str,
        params: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock, self._engine.connect() as conn:
            result = conn.execute(text(sql), params or {})
            conn.commit()
            return {
                "rows_affected": result.rowcount,
                "last_row_id": getattr(result, "lastrowid", None),
            }

    def schema(self) -> Dict[str, Any]:
        with self._lock, self._engine.connect() as conn:
            insp = inspect(conn)
            tables = []
            for table_name in insp.get_table_names():
                # Get primary key columns
                pk_info = insp.get_pk_constraint(table_name)
                pk_cols = set(pk_info.get("constrained_columns", []) if pk_info else [])

                cols = []
                for col in insp.get_columns(table_name):
                    cols.append({
                        "name": col["name"],
                        "type": str(col["type"]),
                        "nullable": col.get("nullable", True),
                        "default": col.get("default"),
                        "primary_key": col["name"] in pk_cols,
                    })
                # Row count
                try:
                    count_result = conn.execute(
                        text(f'SELECT COUNT(*) FROM "{table_name}"')
                    )
                    count = count_result.scalar()
                except Exception:
                    count = None
                tables.append({"name": table_name, "columns": cols, "row_count": count})
            return {"dialect": self.dialect, "tables": tables}
