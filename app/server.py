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
if cfg.openai_default_headers:
    client_kwargs["default_headers"] = cfg.openai_default_headers

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
        "Complexity-aware consultant output:\n"
        "- Simple question (narrow, specific): Execute immediately and reply with ONE short paragraph (<= 120 words). Include 0–2 inline charts only if they clarify the answer. End with a single-sentence takeaway.\n"
        "- Report-style request (vague/general like 'give me a report on …'): Do not ask for confirmation; proceed with reasonable defaults and produce a concise, structured mini‑report with short section headings and 2–4 sentences per section. For each key point, place its chart block immediately after the paragraph that introduces it. End with a single‑sentence executive takeaway.\n"
        "- Moderate multi-step questions: Keep responses concise (1–2 short paragraphs) and include 1–3 targeted charts inline.\n\n"
        "Tool call titles:\n"
        "- When calling ANY tool, include a short 'title' string in the function arguments (<= 6 words) that summarizes the step in human terms, e.g., 'Top customers by orders', 'Inspect schema', 'Find file: README'.\n"
        "- Keep titles concise, specific, and user-friendly.\n\n"
        "Result previews in UI:\n"
        "- The UI previews sql_query results as a table automatically. Include a reasonable LIMIT (default 100).\n"
        "Visualization policy (executive audience):\n"
        "- When your answer includes a trend, ranking, breakdown, or numeric comparison, ALWAYS embed one or more inline charts in your final assistant message.\n"
        "- Do NOT call any chart tool; instead, place charts directly in the answer using fenced code blocks with language 'chart' that contain the chart spec and the chart data (columns + rows).\n"
        "- Chart types and defaults: line for time series, bar for rankings/breakdowns, area for share-of-total or stacked series.\n"
        "- Keep charts focused: Simple answers 0–2 charts; moderate 1–3; report-style 3–5 total. Always <= 500 rows per chart; include short, human titles.\n"
        "- Use x + y for single-series or x + series for multi-series.\n"
        "- Do NOT include code blocks other than chart blocks, and never include Markdown tables in the final message.\n"
        "- Self-check before you answer: If your reply contains numeric comparisons or you used sql_query for aggregates, make sure your final text includes at least one ```chart block in-line. If not, add it.\n"
        "- Place each chart block immediately after the paragraph it supports; do not collect charts at the end.\n"
        "- Example inline chart block:\n"
        "\n```chart\n{\\n  \"title\": \"Title\", \"type\": \"bar|line|area\", \"x\": \"label_col\", \"y\": \"value_col\", \"columns\": [..], \"rows\": [..]\\n}\n```\n\n"
        "(The UI renders these blocks inline, in place.)\n"
        "- Always end with one concise executive takeaway sentence.\n\n"
        "Presentation style (friendly, actionable, concise):\n"
        "- If the question is clear and answerable, DO NOT ask for confirmation — immediately use tools to execute and show results (display_result), then reply per the complexity rules above.\n"
        "- If the question is ambiguous or missing key parameters, ask 1–3 short, direct clarifying questions. For report-style requests, prefer reasonable defaults over back-and-forth.\n"
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


def _jsonable(obj: Any) -> Any:
    """Best-effort conversion to JSON-serializable structure for tracing.
    Falls back to string representation when necessary.
    """
    try:
        if obj is None or isinstance(obj, (str, int, float, bool, dict, list)):
            return obj
        # Pydantic v2 style models
        md = getattr(obj, "model_dump", None)
        if callable(md):
            try:
                return md()
            except Exception:
                pass
        # Pydantic v1 style
        dd = getattr(obj, "dict", None)
        if callable(dd):
            try:
                return dd()
            except Exception:
                pass
        # Generic __dict__
        if hasattr(obj, "__dict__"):
            try:
                return json.loads(json.dumps(obj, default=lambda o: getattr(o, "__dict__", str(o))))
            except Exception:
                pass
        # Final fallback
        return str(obj)
    except Exception:
        try:
            return str(obj)
        except Exception:
            return None

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
    """Basic app metadata for the UI.

    Note: We no longer expose a static list of models here. The UI should call
    /api/models to fetch the live provider list. For compatibility, we return
    an empty allowed_models array.
    """
    return {"model": agent.model, "allowed_models": []}


@app.get("/api/models")
def list_models() -> Dict[str, Any]:
    """List available models from the configured provider (OpenAI-compatible).

    - Attempts provider listing via SDK. If it fails, returns a merged list of
      the static models from /api/meta with the current default model.
    - When using OpenRouter (OPENAI_BASE_URL contains openrouter.ai), this will
      return the full list visible to your key.
    """
    models: list[str] = []
    try:
        resp = client.models.list()
        items = getattr(resp, "data", None) or []
        for m in items:
            mid = None
            try:
                mid = getattr(m, "id", None)
            except Exception:
                pass
            if mid is None and isinstance(m, dict):
                mid = m.get("id")
            if mid:
                models.append(str(mid))
    except Exception:
        models = []

    # If provider returns nothing, fall back to current default only (no static list)
    if not models:
        return {"models": [agent.model] if agent.model else []}
    # Otherwise return provider list as-is (deduped, order preserved)
    seen: set[str] = set()
    out: list[str] = []
    for m in models:
        if m not in seen:
            out.append(m); seen.add(m)
    return {"models": out}


# The UI fetches the live model list from /api/models; /api/meta.allowed_models is kept empty for compatibility.


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

    async def event_stream():
        # Iterative tool loop with true streaming of content + thinking deltas
        try:
            # Prime stream to reduce buffering in some clients/proxies
            try:
                yield orjson.dumps({"type": "open"}).decode() + "\n"
                await asyncio.sleep(0)
            except Exception:
                pass
            executed_tools: list[Dict[str, Any]] = []
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

                # Provider streaming (via OpenAI SDK; compatible with OpenRouter). Fallback to non-streaming if rejected.
                lower_model = str(current_model).lower()
                tools_input_snapshot = executed_tools[:] if executed_tools else []
                executed_tools = []
                req_messages = agent.build_messages(history)
                ns_kwargs: Dict[str, Any] = dict(
                    model=current_model,
                    messages=req_messages,
                    tools=tools_payload,
                    tool_choice="auto" if agent.tools else None,
                )
                if not lower_model.startswith("gpt-5"):
                    ns_kwargs["temperature"] = 0.2

                assistant_text = ""
                thinking_text = ""
                tool_calls_state: Dict[int, Dict[str, Any]] = {}
                finish_reason = None
                llm_start = int(time.time() * 1000)
                llm_end: int | None = None
                import threading, queue as _q, time as _t
                out_q: _q.Queue[str] = _q.Queue()

                state_box: Dict[str, Any] = {"assistant_text": "", "thinking_text": "", "tool_calls_state": {}, "finish_reason": None, "llm_end": None, "fallback": False, "error": None}

                def _producer():
                    nonlocal llm_start
                    try:
                        stream = agent.client.chat.completions.create(stream=True, **ns_kwargs)
                        for chunk in stream:  # type: ignore
                            try:
                                chs = getattr(chunk, "choices", None) or []
                                if not chs:
                                    continue
                                c0 = chs[0]
                                delta = getattr(c0, "delta", None)
                                if getattr(c0, "finish_reason", None):
                                    state_box["finish_reason"] = getattr(c0, "finish_reason", None)
                                if delta is None:
                                    continue
                                piece = getattr(delta, "content", None)
                                if isinstance(piece, str) and piece:
                                    state_box["assistant_text"] += piece
                                    out_q.put(orjson.dumps({"chunk": piece}).decode() + "\n")
                                elif isinstance(piece, list):
                                    for blk in piece:
                                        try:
                                            btype = blk.get("type") if isinstance(blk, dict) else getattr(blk, "type", None)
                                            if btype in ("reasoning", "thinking"):
                                                txt = blk.get("text") if isinstance(blk, dict) else getattr(blk, "text", None)
                                                if txt:
                                                    state_box["thinking_text"] += str(txt)
                                                    out_q.put(orjson.dumps({"type": "thinking", "content": str(txt)}).decode() + "\n")
                                            else:
                                                txt = blk.get("text") if isinstance(blk, dict) else getattr(blk, "text", None)
                                                if txt:
                                                    state_box["assistant_text"] += str(txt)
                                                    out_q.put(orjson.dumps({"chunk": str(txt)}).decode() + "\n")
                                        except Exception:
                                            pass
                                r = getattr(delta, "reasoning", None)
                                if r:
                                    try:
                                        if isinstance(r, str):
                                            state_box["thinking_text"] += r
                                            out_q.put(orjson.dumps({"type": "thinking", "content": r}).decode() + "\n")
                                        elif isinstance(r, dict):
                                            for key in ("text", "content", "output_text"):
                                                if r.get(key):
                                                    state_box["thinking_text"] += str(r.get(key))
                                                    out_q.put(orjson.dumps({"type": "thinking", "content": str(r.get(key))}).decode() + "\n")
                                                    break
                                    except Exception:
                                        pass
                                tcd = getattr(delta, "tool_calls", None)
                                if tcd:
                                    try:
                                        for i, tc in enumerate(tcd):
                                            idx = getattr(tc, "index", None)
                                            if idx is None:
                                                idx = i
                                            st = state_box["tool_calls_state"].get(idx) if isinstance(state_box.get("tool_calls_state"), dict) else None
                                            if not isinstance(st, dict):
                                                st = {"id": getattr(tc, "id", None), "name": None, "arguments": ""}
                                            fn = getattr(tc, "function", None)
                                            if fn is not None:
                                                nm = getattr(fn, "name", None)
                                                if nm:
                                                    st["name"] = nm
                                                ap = getattr(fn, "arguments", None)
                                                if ap:
                                                    st["arguments"] = (st.get("arguments") or "") + str(ap)
                                            else:
                                                try:
                                                    fn2 = tc.get("function") if isinstance(tc, dict) else None
                                                    if fn2:
                                                        if fn2.get("name"):
                                                            st["name"] = fn2.get("name")
                                                        if fn2.get("arguments"):
                                                            st["arguments"] = (st.get("arguments") or "") + str(fn2.get("arguments"))
                                                except Exception:
                                                    pass
                                            state_box.setdefault("tool_calls_state", {})[idx] = st
                                    except Exception:
                                        pass
                            except Exception:
                                pass
                        state_box["llm_end"] = int(_t.time() * 1000)
                    except Exception as e:
                        # Fallback: non-stream request
                        if is_stream_unsupported_error(e):
                            try:
                                resp = agent.client.chat.completions.create(**ns_kwargs)
                                state_box["llm_end"] = int(_t.time() * 1000)
                                choice = resp.choices[0]
                                msg = choice.message
                                state_box["assistant_text"] = getattr(msg, "content", None) or ""
                                state_box["thinking_text"] = extract_thinking_from_message(msg) or ""
                                tcs = {}
                                if getattr(msg, "tool_calls", None):
                                    for i, tc in enumerate(msg.tool_calls or []):
                                        try:
                                            tcs[i] = {
                                                "id": getattr(tc, "id", None),
                                                "name": getattr(tc.function, "name", None),
                                                "arguments": getattr(tc.function, "arguments", "") or "",
                                            }
                                        except Exception:
                                            pass
                                state_box["tool_calls_state"] = tcs
                                state_box["finish_reason"] = getattr(choice, "finish_reason", None)
                                # Simulate streaming for UX
                                step = 300
                                content = state_box["assistant_text"] or ""
                                for i in range(0, len(content), step):
                                    out_q.put(orjson.dumps({"chunk": content[i:i+step]}).decode() + "\n")
                                state_box["fallback"] = True
                            except Exception as ie:
                                state_box["error"] = str(ie)
                        else:
                            state_box["error"] = str(e)
                    finally:
                        out_q.put("__STREAM_DONE__")

                th = threading.Thread(target=_producer, daemon=True)
                th.start()

                # Relay produced chunks to client as they arrive
                while True:
                    line = await asyncio.to_thread(out_q.get)
                    if line == "__STREAM_DONE__":
                        break
                    try:
                        yield line
                    except Exception:
                        break

                # Read state produced by the producer
                assistant_text = str(state_box.get("assistant_text") or "")
                thinking_text = str(state_box.get("thinking_text") or "")
                tool_calls_state = dict(state_box.get("tool_calls_state") or {})
                finish_reason = state_box.get("finish_reason")
                llm_end = int(state_box.get("llm_end") or (time.time() * 1000))

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
                        "model": current_model,
                        "llm_start_ms": llm_start,
                        "llm_end_ms": llm_end,
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
                    "model": current_model,
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
            # Ensure traces are flushed to Langfuse backend
            try:
                tracer.flush()
            except Exception:
                pass

    return StreamingResponse(
        event_stream(),
        media_type="application/x-ndjson",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.app_host, port=cfg.app_port, reload=True)
