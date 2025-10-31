from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List


@dataclass
class ToolSpec:
    name: str
    description: str
    schema: Dict[str, Any]
    func: Callable[[Dict[str, Any]], Dict[str, Any]]


def _safe_join(base: Path, *parts: str) -> Path:
    candidate = (base / Path(*parts)).resolve()
    if not str(candidate).startswith(str(base.resolve())):
        raise ValueError("Path escapes workspace root")
    return candidate


def make_tools(workspace: Path) -> List[ToolSpec]:
    # UI helper: display a tabular result in the client
    def display_result(args: Dict[str, Any]) -> Dict[str, Any]:
        title = str(args.get("title") or "").strip()
        columns = args.get("columns") or []
        rows = args.get("rows") or []
        rowcount = args.get("rowcount")
        # Normalize types
        if not isinstance(columns, list):
            columns = []
        if not isinstance(rows, list):
            rows = []
        # If rows are objects, convert to arrays in the order of columns (infer columns if missing)
        if rows and isinstance(rows[0], dict):
            # Infer columns from first row if not provided
            if not columns:
                keyset = set()
                for r in rows:
                    if isinstance(r, dict):
                        keyset.update(r.keys())
                columns = list(keyset)
            conv = []
            for r in rows:
                if isinstance(r, dict):
                    conv.append([r.get(c) for c in columns])
                else:
                    conv.append(r)
            rows = conv
        # Cap rows to a reasonable amount to avoid huge payloads
        try:
            max_rows = int(args.get("max_rows") or 200)
        except Exception:
            max_rows = 200
        if len(rows) > max_rows:
            rows = rows[:max_rows]
        out: Dict[str, Any] = {
            "title": title,
            "columns": columns,
            "rows": rows,
        }
        if rowcount is not None:
            out["rowcount"] = rowcount
        return out
    def list_files(_: Dict[str, Any]) -> Dict[str, Any]:
        files = []
        for root, _, filenames in os.walk(workspace):
            for f in filenames:
                p = Path(root) / f
                files.append(str(p.relative_to(workspace)))
        return {"files": files}

    def read_file(args: Dict[str, Any]) -> Dict[str, Any]:
        path = _safe_join(workspace, args["path"])  # type: ignore[index]
        try:
            data = path.read_text(encoding="utf-8")
            return {"path": str(path.relative_to(workspace)), "content": data}
        except FileNotFoundError:
            return {"error": f"File not found: {args['path']}"}

    def write_file(args: Dict[str, Any]) -> Dict[str, Any]:
        path = _safe_join(workspace, args["path"])  # type: ignore[index]
        content = args.get("content", "")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(content), encoding="utf-8")
        return {"path": str(path.relative_to(workspace)), "bytes": len(str(content).encode("utf-8"))}

    def search_files(args: Dict[str, Any]) -> Dict[str, Any]:
        query = str(args.get("query", ""))
        max_hits = int(args.get("max_hits", 20))
        hits = []
        for root, _, filenames in os.walk(workspace):
            for f in filenames:
                p = Path(root) / f
                try:
                    text = p.read_text(encoding="utf-8")
                except Exception:
                    continue
                if query.lower() in text.lower():
                    hits.append({
                        "path": str(p.relative_to(workspace)),
                        "snippet": text[:400],
                    })
                if len(hits) >= max_hits:
                    break
        return {"query": query, "hits": hits}

    def display_chart(args: Dict[str, Any]) -> Dict[str, Any]:
        """Normalize a simple chart spec so the UI can render it.

        Accepts either top-level columns/rows or nested data: {columns, rows}.
        Required: x (column name for labels). Use `y` for a single series or `series` for multiple.
        """
        title = str(args.get("title") or "").strip()
        ctype = str(args.get("type") or "line").lower()
        if ctype not in ("line", "bar", "area"):
            ctype = "line"

        # Data
        data = args.get("data") or {}
        columns = args.get("columns") or data.get("columns") or []
        rows = args.get("rows") or data.get("rows") or []
        if not isinstance(columns, list):
            columns = []
        if not isinstance(rows, list):
            rows = []
        # If rows are dicts, infer/normalize to arrays using column order
        if rows and isinstance(rows[0], dict):
            if not columns:
                # Infer columns from first row, then add any new keys encountered later
                col_order = list(rows[0].keys())
                seen = set(col_order)
                for r in rows:
                    if isinstance(r, dict):
                        for k in r.keys():
                            if k not in seen:
                                col_order.append(k)
                                seen.add(k)
                columns = col_order
            conv = []
            for r in rows:
                if isinstance(r, dict):
                    conv.append([r.get(c) for c in columns])
                else:
                    conv.append(r)
            rows = conv

        # Truncate to keep payload manageable
        try:
            max_rows = int(args.get("max_rows") or 500)
        except Exception:
            max_rows = 500
        truncated = False
        if len(rows) > max_rows:
            rows = rows[: max_rows]
            truncated = True

        # Axis + series
        # Resolve column names case-insensitively when needed
        def _resolve(col: str) -> str:
            if not col or not columns:
                return col
            for c in columns:
                if str(c).lower() == str(col).lower():
                    return c
            return col

        x = _resolve(str(args.get("x") or (columns[0] if columns else "")).strip())
        y = args.get("y")
        series = args.get("series")
        if y and not series:
            series = [y]
        if not isinstance(series, list) or not series:
            # Fallback: use up to 3 non-x columns
            series = [c for c in columns if c != x][:3]
        # Normalize series names case-insensitively
        series = [_resolve(str(s)) for s in series]

        stacked = bool(args.get("stacked") or False)

        return {
            "title": title,
            "type": ctype,
            "x": x,
            "series": series,
            "columns": columns,
            "rows": rows,
            "stacked": stacked,
            "truncated": truncated,
        }

    return [
        # Note: display_result kept available in module but not exposed by default to the model
        # ToolSpec(
        #     name="display_result",
        #     description=(
        #         "Display a final tabular result in the UI. Provide columns (array of strings) and rows (array of arrays). "
        #         "Use after computing an answer to show the table preview. Keep <= 200 rows."
        #     ),
        #     schema={
        #         "type": "object",
        #         "required": ["columns", "rows"],
        #         "properties": {
        #             "title": {"type": "string", "description": "Short title for the result"},
        #             "columns": {"type": "array", "items": {"type": "string"}},
        #             "rows": {"type": "array", "items": {"type": "object"}},
        #             "rowcount": {"type": "integer"},
        #             "max_rows": {"type": "integer", "minimum": 1, "maximum": 500, "default": 200},
        #         },
        #     },
        #     func=display_result,
        # ),
        ToolSpec(
            name="list_files",
            description=(
                "List all files under the project workspace root."
            ),
            schema={
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "Short human title for this step (<= 6 words)", "default": "List files"},
                },
            },
            func=list_files,
        ),
        ToolSpec(
            name="read_file",
            description="Read a UTF-8 text file from the project workspace.",
            schema={
                "type": "object",
                "required": ["path"],
                "properties": {
                    "title": {"type": "string", "description": "Short human title (<= 6 words)"},
                    "path": {"type": "string", "description": "Relative path from workspace root"}
                },
            },
            func=read_file,
        ),
        ToolSpec(
            name="write_file",
            description="Write a UTF-8 text file to the project workspace, creating folders as needed.",
            schema={
                "type": "object",
                "required": ["path", "content"],
                "properties": {
                    "title": {"type": "string", "description": "Short human title (<= 6 words)"},
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
            },
            func=write_file,
        ),
        ToolSpec(
            name="search_files",
            description="Search for a case-insensitive substring within files in the workspace.",
            schema={
                "type": "object",
                "required": ["query"],
                "properties": {
                    "title": {"type": "string", "description": "Short human title (<= 6 words)"},
                    "query": {"type": "string"},
                    "max_hits": {"type": "integer", "minimum": 1, "maximum": 100, "default": 20},
                },
            },
            func=search_files,
        ),
        # display_chart tool removed (redundant with inline chart blocks rendered in message content)
    ]


def as_openai_tools(tools: List[ToolSpec]) -> List[Dict[str, Any]]:
    """Translate ToolSpec into OpenAI function tool format for chat.completions."""
    return [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
            },
        }
        for t in tools
    ]


def dispatch_tool(tools: List[ToolSpec], name: str, arguments_json: str) -> Dict[str, Any]:
    for t in tools:
        if t.name == name:
            args = json.loads(arguments_json or "{}")
            return t.func(args)
    return {"error": f"Unknown tool: {name}"}
