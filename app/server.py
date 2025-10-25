from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
import os
from typing import Any, Dict, List

import orjson
import logging
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from .agent.core import Agent
from .agent.tools import make_tools
from .agent.sql_tools import make_sql_tools
from .config import Config
from .db import Database
from .sessions import SessionStore


cfg = Config.load()
logger = logging.getLogger("sql-agent")
if not logger.handlers:
    logging.basicConfig(level=logging.INFO)

client_kwargs: Dict[str, Any] = {"api_key": cfg.openai_api_key}
if cfg.openai_base_url:
    client_kwargs["base_url"] = cfg.openai_base_url

client = OpenAI(**client_kwargs)

db = Database(cfg.database_url)

agent = Agent(
    model=cfg.openai_model,
    client=client,
    tools=(make_tools(cfg.workspace_dir) + make_sql_tools(db)),
    system_prompt=(
        "You are a data scientist who answers user questions using ONLY the data available in the database via tools. "
        "Read-only policy: never modify data or schema; do not perform DDL/DML; decline such requests.\n\n"
        "Behavior:\n"
        "- Use sql_schema first when unsure about available tables/columns or relationships.\n"
        "- Translate natural language to correct SQLite SQL. Prefer explicit column lists (avoid *).\n"
        "- Use sql_query for SELECT/CTE (read-only). If no LIMIT is present, include a reasonable LIMIT (default 100).\n"
        "- Do not invent tables/columns. If the question cannot be answered with current data or is ambiguous, state that it is not answerable and briefly explain what is missing or what clarification is needed.\n\n"
        "Helpfulness & initiative:\n"
        "- If the request is broad or strategic, propose 2–4 concrete analysis directions with one-line justifications.\n"
        "- Phrase actions in first person (I'll … / I will …) and focus on execution.\n"
        "- When key details are missing (metric, time window, segment, comparison), make a reasonable default assumption (e.g., last 30 days) and state it in parentheses, or ask targeted questions.\n"
        "- Proactively suggest next steps: additional cuts, benchmarks, or simple experiments that could help the user act.\n\n"
        "Tool call titles:\n"
        "- When calling ANY tool, include a short 'title' string in the function arguments (<= 6 words) that summarizes the step in human terms, e.g., 'Top customers by orders', 'Inspect schema', 'Find file: README'.\n"
        "- Keep titles concise, specific, and user-friendly.\n\n"
        "Result previews in UI:\n"
        "- The UI previews sql_query results as a table automatically. Include a reasonable LIMIT (default 100).\n"
        "- Then provide your one-sentence summary as the final assistant message.\n\n"
        "Presentation style (friendly, actionable, concise):\n"
        "- If the question is clear and answerable, DO NOT ask for confirmation — immediately use tools to execute and show results (display_result), then return ONE short, business-friendly summary sentence.\n"
        "- If the question is ambiguous or missing key parameters, ask 1–3 short, direct clarifying questions. Only ask confirmation when you must choose between multiple equally reasonable paths.\n"
        "- Offer 2–4 concise suggestions or next steps when helpful (one line each).\n"
        "- Avoid technical terms and schema/column names; use everyday business wording.\n"
        "- Do NOT include tables or code blocks unless the user explicitly asks.\n"
        "- Use light Markdown to tidy text: short headings when helpful, and **bold** for key phrases.\n"
        "  Bullets are allowed only for clarifying questions or next-step suggestions (never for numeric results). Avoid code blocks and tables unless asked.\n"
        "- Never output Markdown pipe tables.\n"
        "Data naming rules:\n"
        "- Prefer real-world names over IDs. When referring to entities (customers, products, cities, categories), always use their name/label column.\n"
        "- If the current result has only *_id columns, join the appropriate table (e.g., customers, products) to fetch the human-readable name and select it.\n"
        "- Alias columns to short business labels (customer, product, city, orders, revenue). Avoid *_id or technical names.\n"
        "Join patterns (examples):\n"
        "- Orders per customer: join orders.customer_id = customers.id; SELECT customers.name AS customer, COUNT(*) AS orders.\n"
        "- Revenue by product: join order_items.product_id = products.id and order_items.order_id = orders.id; SELECT products.name AS product, SUM(quantity*unit_price) AS revenue.\n"
        "- Orders by city: join orders.customer_id = customers.id; SELECT customers.city AS city, COUNT(*) AS orders.\n"
        "Never answer with raw IDs; if a query returns only IDs, revise the SQL to include names and run again.\n"
        "- Avoid exclamation marks and filler; no pleasantries.\n"
        "- Never use the exact phrase 'Not answerable with current data'.\n"
        "  Instead, use friendly phrasing: 'I couldn’t find X in this dataset. To proceed, could you share Y?'\n"
        "  Always pair the blocker with the single most important follow-up question or a suggested next step."
        "Do not instruct the user to perform actions (no 'you should', 'please provide by doing …'). Ask for confirmation or missing info, then you execute.\n\n"
        "Style example:\n"
        "- I'll start with sales trends (last 30 days): I'll compare this period vs prior 30 days to spot declines.\n"
        "- I'll check customer retention: I'll estimate churn and highlight any spike by cohort.\n"
        "- I'll review inventory health: I'll flag stockouts or slow movers.\n"
        "Shall I run the sales trends analysis now, or prefer a different focus?"
    ),
)

app = FastAPI(title="SQL Agent")

static_dir = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Session store (file-backed JSON in workspace)
store = SessionStore(cfg.workspace_dir / "sessions.json")


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.get("/api/meta")
def meta() -> Dict[str, Any]:
    """Basic app metadata for the UI with a static model list."""
    static_models = [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-5",
        "gpt-5-mini",
        "gpt-5-nano",
    ]
    # Put current default first if present, dedupe while preserving order
    ordered: list[str] = []
    seen: set[str] = set()
    if agent.model in static_models:
        ordered.append(agent.model); seen.add(agent.model)
    for mid in static_models:
        if mid not in seen:
            ordered.append(mid); seen.add(mid)
    return {
        "model": agent.model,
        "allowed_models": ordered,
    }


# The UI uses a static model list from /api/meta.allowed_models; live listing endpoint removed per request.


@app.get("/api/debug/tools")
def debug_tools() -> Dict[str, Any]:
    tools_payload = [
        {
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description,
                "parameters": t.schema,
            },
        }
        for t in agent.tools or []
    ]
    return {"tools": tools_payload}


@app.get("/api/sessions")
def list_sessions() -> Dict[str, Any]:
    metas = store.list()
    # Sort by updated_at desc
    metas_sorted = sorted(metas, key=lambda m: m.updated_at, reverse=True)
    return {
        "sessions": [
            {
                "id": m.id,
                "title": m.title,
                "created_at": m.created_at,
                "updated_at": m.updated_at,
            }
            for m in metas_sorted
        ]
    }


@app.get("/api/sessions/{chat_id}")
def get_session(chat_id: str) -> Dict[str, Any]:
    s = store.get(chat_id)
    if not s:
        raise HTTPException(status_code=404, detail="not found")
    return {
        "id": chat_id,
        "title": s.get("title") or "",
        "created_at": s.get("created_at"),
        "updated_at": s.get("updated_at"),
        "model": s.get("model") or agent.model,
        "messages": s.get("messages", []),
    }


@app.patch("/api/sessions/{chat_id}")
async def rename_session(chat_id: str, req: Request) -> Dict[str, Any]:
    body = await req.json()
    title = body.get("title")
    model_name = body.get("model")
    import time
    if title is not None:
        ok = store.rename(chat_id, str(title).strip(), updated_at=int(time.time() * 1000))
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
    if model_name is not None:
        ok = store.update_model(chat_id, str(model_name).strip(), updated_at=int(time.time() * 1000))
        if not ok:
            raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.delete("/api/sessions/{chat_id}")
def delete_session(chat_id: str) -> Dict[str, Any]:
    ok = store.delete(chat_id)
    if not ok:
        raise HTTPException(status_code=404, detail="not found")
    return {"ok": True}


@app.post("/api/new_chat")
async def new_chat(req: Request) -> Dict[str, str]:
    chat_id = secrets.token_hex(8)
    title = ""
    model_name: str | None = None
    try:
        body = await req.json()
        if isinstance(body, dict):
            title = str(body.get("title") or "")
            if body.get("model"):
                model_name = str(body.get("model"))
    except Exception:
        pass
    # Use wall clock ms
    import time
    now_ms = int(time.time() * 1000)
    store.create(chat_id, title=title, created_at=now_ms, model=(model_name or agent.model))
    return {"chat_id": chat_id}


@app.post("/api/chat")
async def chat(req: Request):
    body = await req.json()
    chat_id = body.get("chat_id")
    user_message = body.get("message")
    if not chat_id:
        raise HTTPException(status_code=400, detail="chat_id required")
    if not user_message:
        raise HTTPException(status_code=400, detail="message required")

    # Append user message and work with a local history list
    import time
    now_ms = int(time.time() * 1000)
    turn_start = now_ms
    store.append(chat_id, {"role": "user", "content": str(user_message)}, updated_at=now_ms)
    history = store.get_messages(chat_id)

    async def event_stream():
        # Iterative tool loop: stream tool calls/results live, then stream final text
        try:
            while True:
                tools_payload = agent.tools and [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.schema,
                        },
                    }
                    for t in agent.tools
                ] or None
                logger.debug("requesting completion with tools=%s", [t.get("function",{}).get("name") for t in (tools_payload or [])])
                # Determine model for this session
                current_model = agent.model
                sess_meta = store.get(chat_id)
                if isinstance(sess_meta, dict):
                    m = sess_meta.get("model")
                    if m:
                        current_model = str(m)

                # Some models (e.g., gpt-5 family) do not accept non-default temperature.
                create_kwargs: Dict[str, Any] = dict(
                    model=current_model,
                    messages=agent.build_messages(history),
                    tools=tools_payload,
                    tool_choice="auto" if agent.tools else None,
                )
                if not str(current_model).lower().startswith("gpt-5"):
                    create_kwargs["temperature"] = 0.2
                resp = agent.client.chat.completions.create(**create_kwargs)

                choice = resp.choices[0]
                msg = choice.message
                if msg.tool_calls:
                    # Emit tool_call events for visibility
                    tool_call_msg = {
                        "role": "assistant",
                        "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                        "content": msg.content,
                    }
                    store.append(chat_id, tool_call_msg, updated_at=int(time.time() * 1000))
                    history.append(tool_call_msg)

                    for tc in msg.tool_calls or []:
                        # Send tool_call event
                        args_raw = tc.function.arguments
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args = args_raw
                        yield orjson.dumps({
                            "type": "tool_call",
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": args,
                        }).decode() + "\n"

                        # Execute the tool
                        from .agent.tools import dispatch_tool

                        # Guard: encourage schema-first workflow
                        try:
                            if tc.function.name == "sql_query":
                                has_schema = any(
                                    (m.get("role") == "tool" and m.get("name") == "sql_schema")
                                    for m in history
                                )
                                if not has_schema:
                                    result = {
                                        "error": "schema_required",
                                        "message": "Call sql_schema first to confirm available tables/columns and then re-issue sql_query.",
                                    }
                                else:
                                    result = dispatch_tool(agent.tools, tc.function.name, tc.function.arguments)
                            else:
                                result = dispatch_tool(agent.tools, tc.function.name, tc.function.arguments)
                        except Exception as e:
                            logger.exception("tool execution failed: %s", tc.function.name)
                            result = {"error": "tool_exception", "message": str(e)}

                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.function.name,
                            "content": json.dumps(result),
                        }
                        store.append(chat_id, tool_msg, updated_at=int(time.time() * 1000))
                        history.append(tool_msg)

                        # Send tool_result event
                        yield orjson.dumps({
                            "type": "tool_result",
                            "id": tc.id,
                            "name": tc.function.name,
                            "output": result,
                        }).decode() + "\n"

                    # After processing tools, continue conversation loop
                    continue

                # Final assistant response; stream text in chunks
                content = msg.content or ""
                duration_ms = int(time.time() * 1000) - turn_start
                final_msg = {"role": "assistant", "content": content, "duration_ms": duration_ms}
                store.append(chat_id, final_msg, updated_at=int(time.time() * 1000))
                history.append(final_msg)

                # Stream content in character chunks, preserving whitespace/newlines for Markdown formatting
                step = 300
                for i in range(0, len(content), step):
                    part = content[i : i + step]
                    yield orjson.dumps({"chunk": part}).decode() + "\n"
                    await asyncio.sleep(0.005)
                yield orjson.dumps({"done": True}).decode() + "\n"
                break
        except Exception as e:
            logger.exception("streaming loop error")
            try:
                yield orjson.dumps({"type": "error", "error": str(e)}).decode() + "\n"
            except Exception:
                # best effort
                pass

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.app_host, port=cfg.app_port, reload=True)
