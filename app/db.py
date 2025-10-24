from __future__ import annotations

import sqlite3
import threading
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Tuple
from urllib.parse import urlparse


def _parse_sqlite_path(database_url: str) -> str:
    """Parse a sqlite:/// URL into a filesystem path.

    Accepts forms like:
    - sqlite:///absolute/path.db
    - sqlite:////absolute/path.db (also ok)
    - file:/absolute/path.db
    - Direct filesystem paths are returned unchanged.
    """
    if database_url.startswith("sqlite"):
        parsed = urlparse(database_url)
        path = parsed.path
        if path.startswith("//"):
            # urlparse on 'sqlite:////abs.db' yields path='//abs.db'
            path = path[1:]
        return path
    if database_url.startswith("file:"):
        parsed = urlparse(database_url)
        return parsed.path
    # Treat as a direct path
    return database_url


@dataclass
class QueryResult:
    columns: List[str]
    rows: List[List[Any]]
    rowcount: int


class Database:
    def __init__(self, database_url: str):
        path = _parse_sqlite_path(database_url)
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._conn:
            self._conn.execute("PRAGMA foreign_keys = ON;")
        self._lock = threading.Lock()

    def _execute(self, sql: str, params: Optional[Iterable[Any]] | Optional[Dict[str, Any]] = None):
        cur = self._conn.cursor()
        cur.execute(sql, params or [])
        return cur

    def query(self, sql: str, params: Optional[Iterable[Any]] | Optional[Dict[str, Any]] = None, max_rows: int = 100) -> QueryResult:
        sql_stripped = sql.lstrip().lower()
        if not (sql_stripped.startswith("select") or sql_stripped.startswith("with")):
            raise ValueError("sql_query only allows read-only SELECT/CTE statements")
        with self._lock:
            cur = self._execute(sql, params)
            rows = cur.fetchmany(max_rows)
            cols = [d[0] for d in cur.description] if cur.description else []
            return QueryResult(columns=cols, rows=[list(r) for r in rows], rowcount=len(rows))

    def execute(self, sql: str, params: Optional[Iterable[Any]] | Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        with self._lock, self._conn:
            cur = self._execute(sql, params)
            last_id = cur.lastrowid
            count = cur.rowcount
            return {"rows_affected": count, "last_row_id": last_id}

    def schema(self) -> Dict[str, Any]:
        with self._lock:
            cur = self._conn.cursor()
            tables = []
            for (name,) in cur.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name;"):
                sanitized = name.replace("'", "''")
                info_cur = self._conn.execute(f"PRAGMA table_info('{sanitized}')")
                cols = []
                for row in info_cur.fetchall():
                    cols.append({
                        "cid": row[0],
                        "name": row[1],
                        "type": row[2],
                        "notnull": bool(row[3]),
                        "default": row[4],
                        "pk": bool(row[5]),
                    })
                try:
                    (count,) = self._conn.execute(f"SELECT COUNT(*) FROM '{sanitized}'").fetchone()
                except sqlite3.Error:
                    count = None
                tables.append({"name": name, "columns": cols, "row_count": count})
            return {"dialect": "sqlite", "tables": tables}
