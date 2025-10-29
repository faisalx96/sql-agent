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
from .tracing import Tracer
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
        "Visualization policy (executive audience):\n"
        "- When your answer includes a trend, ranking, breakdown, or numeric comparison, ALWAYS embed one or more inline charts in your final assistant message.\n"
        "- Use fenced code blocks with language 'chart' that contain the chart spec and the chart data (columns + rows).\n"
        "- If you called display_chart or produced chartable data with sql_query, you MUST also include matching inline chart blocks in your final message (same x/series and the data you used).\n"
        "- Chart types and defaults: line for time series, bar for rankings/breakdowns, area for share-of-total or stacked series.\n"
        "- Keep charts focused: 1–3 charts per reply, <= 500 rows per chart; include short, human titles.\n"
        "- Use x + y for single-series or x + series for multi-series.\n"
        "- Do NOT include code blocks other than chart blocks, and never include Markdown tables in the final message.\n"
        "- Self-check before you answer: If your reply contains numeric comparisons or you used display_chart/sql_query for aggregates, make sure your final text includes at least one ```chart block in-line. If not, add it.\n"
        "- Example inline chart block:\n"
        "\n```chart\n{\\n  \"title\": \"Title\", \"type\": \"bar|line|area\", \"x\": \"label_col\", \"y\": \"value_col\", \"columns\": [..], \"rows\": [..]\\n}\n```\n\n"
        "(The UI renders these blocks inline, in place.)\n"
        "- Then provide your one-sentence summary as the final assistant message.\n\n"
        "Presentation style (friendly, actionable, concise):\n"
        "- If the question is clear and answerable, DO NOT ask for confirmation — immediately use tools to execute and show results (display_result), then return ONE short, business-friendly summary sentence.\n"
        "- If the question is ambiguous or missing key parameters, ask 1–3 short, direct clarifying questions. Only ask confirmation when you must choose between multiple equally reasonable paths.\n"
        "- Offer 2–4 concise suggestions or next steps when helpful (one line each).\n"
        "- Avoid technical terms and schema/column names; use everyday business wording.\n"
        "- Avoid code blocks except for chart blocks as described above. Do NOT include tables or other code blocks unless the user explicitly asks.\n"
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


def _safe_json(obj: Any) -> Any:
    """Parse JSON strings to objects; leave other types as-is.
    Avoids throwing on invalid JSON, returns original if parsing fails.
    """
    try:
        if isinstance(obj, str):
            return json.loads(obj)
    except Exception:
        return obj
    return obj

app = FastAPI(title="SQL Agent")

static_dir = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# Session store (file-backed JSON in workspace)
store = SessionStore(cfg.workspace_dir / "sessions.json")

# Tracing (Langfuse wrapper; gracefully no-ops if unavailable)
tracer = Tracer.from_config(cfg)


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

    # Start trace for this turn
    turn_id = secrets.token_hex(8)
    trace = tracer.start_trace(
        trace_id=turn_id,
        name="chat-turn",
        session_id=chat_id,
        input={"user": str(user_message)},
        metadata={"route": "/api/chat"},
    )
    try:
        logger.info("trace started: turn_id=%s chat_id=%s", turn_id, chat_id)
    except Exception:
        pass
    trace.event("user_message", {"message": str(user_message)})

    async def event_stream():
        # Iterative tool loop with true streaming of content + thinking deltas
        try:
            def extract_thinking_from_message(message: Any) -> str | None:
                try:
                    content = getattr(message, "content", None)
                    if isinstance(content, list):
                        for blk in content:
                            btype = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
                            if btype in ("reasoning", "thinking"):
                                txt = (
                                    blk.get("text") if isinstance(blk, dict) else getattr(blk, "text", None)
                                ) or (
                                    blk.get("content") if isinstance(blk, dict) else getattr(blk, "content", None)
                                )
                                if txt:
                                    return str(txt)
                    reasoning = getattr(message, "reasoning", None)
                    if reasoning:
                        if isinstance(reasoning, str):
                            return reasoning
                        if isinstance(reasoning, dict):
                            for key in ("text", "content", "output_text"):
                                if reasoning.get(key):
                                    return str(reasoning.get(key))
                        else:
                            for key in ("text", "content", "output_text"):
                                try:
                                    v = getattr(reasoning, key, None)
                                    if v:
                                        return str(v)
                                except Exception:
                                    pass
                except Exception:
                    pass
                return None

            def is_stream_unsupported_error(e: Exception) -> bool:
                try:
                    # Catch common OpenAI 400 errors for stream unsupported/verification
                    msg = str(getattr(e, "message", "")) or str(e)
                    if any(p in msg for p in (
                        "must be verified to stream this model",
                        '"param": "stream"',
                        "'param': 'stream'",
                        "unsupported_value",
                    )):
                        return True
                    status = getattr(e, "status_code", None)
                    if status == 400:
                        return True
                except Exception:
                    pass
                return False

            while True:
                # Prepare tools payload
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

                # Determine model for this session
                current_model = agent.model
                sess_meta = store.get(chat_id)
                if isinstance(sess_meta, dict):
                    m = sess_meta.get("model")
                    if m:
                        current_model = str(m)

                # Non-streaming request to OpenAI (org-friendly)
                lower_model = str(current_model).lower()
                ns_kwargs: Dict[str, Any] = dict(
                    model=current_model,
                    messages=agent.build_messages(history),
                    tools=tools_payload,
                    tool_choice="auto" if agent.tools else None,
                )
                if not lower_model.startswith("gpt-5"):
                    ns_kwargs["temperature"] = 0.2

                llm_start = int(time.time() * 1000)
                resp = agent.client.chat.completions.create(**ns_kwargs)
                llm_end = int(time.time() * 1000)
                choice = resp.choices[0]
                msg = choice.message
                thinking_text = extract_thinking_from_message(msg) or ""
                if thinking_text:
                    yield orjson.dumps({"type": "thinking", "content": thinking_text}).decode() + "\n"

                # Record LLM generation
                usage_dict: Dict[str, Any] | None = None
                try:
                    u = getattr(resp, "usage", None)
                    if u:
                        # Compatible with both dict-like and object-like usage
                        usage_dict = {
                            k: getattr(u, k)
                            for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                            if getattr(u, k, None) is not None
                        }
                except Exception:
                    usage_dict = None
                try:
                    trace.generation(
                        name="assistant",
                        model=current_model,
                        input=None,
                        output=getattr(msg, "content", None),
                        start_ms=llm_start,
                        end_ms=llm_end,
                        usage=usage_dict,
                        metadata={"thinking": thinking_text or None},
                    )
                except Exception:
                    pass

                if msg.tool_calls:
                    # Persist + emit tool calls
                    tool_call_msg = {
                        "role": "assistant",
                        "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                        "content": msg.content,
                        "thinking": (thinking_text or None),
                    }
                    store.append(chat_id, tool_call_msg, updated_at=int(time.time() * 1000))
                    history.append(tool_call_msg)

                    for tc in msg.tool_calls or []:
                        args_raw = tc.function.arguments
                        try:
                            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                        except Exception:
                            args = args_raw
                        # Send tool_call event
                        yield orjson.dumps({
                            "type": "tool_call",
                            "id": tc.id,
                            "name": tc.function.name,
                            "arguments": args,
                        }).decode() + "\n"

                        from .agent.tools import dispatch_tool
                        # Guard: encourage schema-first workflow
                        try:
                            if tc.function.name == "sql_query":
                                has_schema = any((m.get("role") == "tool" and m.get("name") == "sql_schema") for m in history)
                                if not has_schema:
                                    result = {"error": "schema_required", "message": "Call sql_schema first to confirm available tables/columns and then re-issue sql_query."}
                                else:
                                    t_start = int(time.time() * 1000)
                                    result = dispatch_tool(agent.tools, tc.function.name, tc.function.arguments)
                                    t_end = int(time.time() * 1000)
                                    # Trace SQL tool
                                    try:
                                        args_json = tc.function.arguments
                                        trace.span(
                                            name=f"tool:{tc.function.name}",
                                            input=_safe_json(args_json),
                                            output=_safe_json(result),
                                            start_ms=t_start,
                                            end_ms=t_end,
                                        )
                                    except Exception:
                                        pass
                            else:
                                t_start = int(time.time() * 1000)
                                result = dispatch_tool(agent.tools, tc.function.name, tc.function.arguments)
                                t_end = int(time.time() * 1000)
                                try:
                                    args_json = tc.function.arguments
                                    trace.span(
                                        name=f"tool:{tc.function.name}",
                                        input=_safe_json(args_json),
                                        output=_safe_json(result),
                                        start_ms=t_start,
                                        end_ms=t_end,
                                    )
                                except Exception:
                                    pass
                        except Exception as e:
                            logger.exception("tool execution failed: %s", tc.function.name)
                            result = {"error": "tool_exception", "message": str(e)}
                            try:
                                trace.span(
                                    name=f"tool:{tc.function.name}",
                                    input=_safe_json(tc.function.arguments),
                                    output={"error": str(e)},
                                    metadata={"status": "error"},
                                )
                            except Exception:
                                pass

                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.id,
                            "name": tc.function.name,
                            "content": json.dumps(result),
                        }
                        store.append(chat_id, tool_msg, updated_at=int(time.time() * 1000))
                        history.append(tool_msg)

                        yield orjson.dumps({
                            "type": "tool_result",
                            "id": tc.id,
                            "name": tc.function.name,
                            "output": result,
                        }).decode() + "\n"
                    # Next assistant turn
                    continue

                # Final answer path (chunked to the UI but not streamed from provider)
                content = msg.content or ""
                duration_ms = int(time.time() * 1000) - turn_start
                final_msg = {"role": "assistant", "content": content, "duration_ms": duration_ms, "thinking": (thinking_text or None)}
                store.append(chat_id, final_msg, updated_at=int(time.time() * 1000))
                history.append(final_msg)

                # Chunk out content to emulate streaming UX
                step = 300
                for i in range(0, len(content), step):
                    part = content[i : i + step]
                    yield orjson.dumps({"chunk": part}).decode() + "\n"
                    await asyncio.sleep(0.005)
                yield orjson.dumps({"done": True}).decode() + "\n"
                try:
                    trace.event("turn_done", {"duration_ms": duration_ms})
                except Exception:
                    pass
                return

                # After stream closes, decide next step
                duration_ms = int(time.time() * 1000) - turn_start

                if tool_calls_state and (finish_reason in ("tool_calls", "tool", "function_call")):
                    # Persist assistant intermediate with tool calls + any thinking
                    tool_calls_list = []
                    for idx, st in sorted(tool_calls_state.items()):
                        tool_calls_list.append({
                            "id": st.get("id") or f"call_{idx}",
                            "type": "function",
                            "function": {
                                "name": st.get("name") or "",
                                "arguments": st.get("arguments") or "",
                            },
                        })
                    import time as _t
                    tool_call_msg = {
                        "role": "assistant",
                        "tool_calls": tool_calls_list,
                        "content": assistant_text,
                        "thinking": thinking_text or None,
                    }
                    store.append(chat_id, tool_call_msg, updated_at=int(_t.time() * 1000))
                    history.append(tool_call_msg)

                    # Execute each tool and emit results
                    from .agent.tools import dispatch_tool

                    for tc in tool_calls_list:
                        try:
                            fname = tc["function"]["name"]
                            fargs = tc["function"]["arguments"]
                            # Guard: encourage schema-first workflow
                            start_ms = int(time.time() * 1000)
                            if fname == "sql_query":
                                has_schema = any((m.get("role") == "tool" and m.get("name") == "sql_schema") for m in history)
                                if not has_schema:
                                    result = {"error": "schema_required", "message": "Call sql_schema first to confirm available tables/columns and then re-issue sql_query."}
                                    end_ms = int(time.time() * 1000)
                                else:
                                    result = dispatch_tool(agent.tools, fname, fargs)
                                    end_ms = int(time.time() * 1000)
                            else:
                                result = dispatch_tool(agent.tools, fname, fargs)
                                end_ms = int(time.time() * 1000)
                        except Exception as e:
                            logger.exception("tool execution failed: %s", tc)
                            result = {"error": "tool_exception", "message": str(e)}
                            end_ms = int(time.time() * 1000)

                        tool_msg = {
                            "role": "tool",
                            "tool_call_id": tc.get("id"),
                            "name": tc["function"]["name"],
                            "content": json.dumps(result),
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                        }
                        store.append(chat_id, tool_msg, updated_at=int(time.time() * 1000))
                        history.append(tool_msg)

                        # Send tool_result event
                        yield orjson.dumps({
                            "type": "tool_result",
                            "id": tc.get("id"),
                            "name": tc["function"]["name"],
                            "output": result,
                            "start_ms": start_ms,
                            "end_ms": end_ms,
                            "duration_ms": (end_ms - start_ms) if (end_ms and start_ms) else None,
                        }).decode() + "\n"


                    # Continue outer loop for next assistant turn
                    continue

                # Otherwise, final assistant message
                final_msg = {
                    "role": "assistant",
                    "content": assistant_text,
                    "duration_ms": duration_ms,
                    "thinking": thinking_text or None,
                }
                store.append(chat_id, final_msg, updated_at=int(time.time() * 1000))
                history.append(final_msg)
                yield orjson.dumps({"done": True}).decode() + "\n"
                break
        except Exception as e:
            logger.exception("streaming loop error")
            try:
                yield orjson.dumps({"type": "error", "error": str(e)}).decode() + "\n"
            except Exception:
                # best effort
                pass
        finally:
            try:
                trace.event("turn_close", None)
            except Exception:
                pass
            # Ensure traces are flushed to Langfuse backend
            try:
                tracer.flush()
            except Exception:
                pass

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.app_host, port=cfg.app_port, reload=True)
