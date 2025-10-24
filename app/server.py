from __future__ import annotations

import asyncio
import json
import secrets
from pathlib import Path
from typing import Any, Dict, List

import orjson
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from openai import OpenAI

from .agent.core import Agent
from .agent.tools import make_tools
from .agent.sql_tools import make_sql_tools
from .config import Config
from .db import Database


cfg = Config.load()

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
        "Presentation style (strict):\n"
        "- Return ONLY one short, business-friendly summary sentence (<= 20 words) stating the key result in plain language.\n"
        "- Avoid technical terms and schema/column names; use everyday business wording.\n"
        "- Do NOT include tables, code blocks, or extra paragraphs unless the user explicitly asks.\n"
        "- Never output Markdown tables (no '|' pipe syntax) or bullet lists unless explicitly requested.\n"
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
        "- When not answerable with current data, reply exactly: Not answerable with current data. Follow with one short reason."
    ),
)

app = FastAPI(title="SQL Agent")

static_dir = Path(__file__).parent / "ui" / "static"
app.mount("/static", StaticFiles(directory=static_dir), name="static")


# In-memory chat sessions. For production, swap with a store.
SESSIONS: Dict[str, List[Dict[str, Any]]] = {}


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    html = (static_dir / "index.html").read_text(encoding="utf-8")
    return HTMLResponse(html)


@app.post("/api/new_chat")
def new_chat() -> Dict[str, str]:
    chat_id = secrets.token_hex(8)
    SESSIONS[chat_id] = []
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

    history = SESSIONS.setdefault(chat_id, [])
    history.append({"role": "user", "content": str(user_message)})

    async def event_stream():
        # Iterative tool loop: stream tool calls/results live, then stream final text
        while True:
            resp = agent.client.chat.completions.create(
                model=agent.model,
                messages=agent.build_messages(history),
                tools=agent.tools and [
                    {
                        "type": "function",
                        "function": {
                            "name": t.name,
                            "description": t.description,
                            "parameters": t.schema,
                        },
                    }
                    for t in agent.tools
                ] or None,
                tool_choice="auto" if agent.tools else None,
                temperature=0.2,
            )

            choice = resp.choices[0]
            msg = choice.message
            if msg.tool_calls:
                # Emit tool_call events for visibility
                history.append({
                    "role": "assistant",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                    "content": msg.content,
                })
                SESSIONS[chat_id] = history
                for tc in msg.tool_calls:
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

                    result = dispatch_tool(agent.tools, tc.function.name, tc.function.arguments)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": json.dumps(result),
                    })
                    SESSIONS[chat_id] = history

                    # Send tool_result event
                    yield orjson.dumps({
                        "type": "tool_result",
                        "id": tc.id,
                        "name": tc.function.name,
                        "output": result,
                    }).decode() + "\n"
                # Continue the loop; model will see tool results
                continue

            # Final assistant response; stream text in chunks
            content = msg.content or ""
            history.append({"role": "assistant", "content": content})
            SESSIONS[chat_id] = history

            words = content.split()
            chunk = []
            for w in words:
                chunk.append(w)
                if len(chunk) >= 20:
                    yield orjson.dumps({"chunk": " ".join(chunk)}).decode() + "\n"
                    await asyncio.sleep(0.005)
                    chunk = []
            if chunk:
                yield orjson.dumps({"chunk": " ".join(chunk)}).decode() + "\n"
            yield orjson.dumps({"done": True}).decode() + "\n"
            break

    return StreamingResponse(event_stream(), media_type="application/x-ndjson")


def create_app() -> FastAPI:
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host=cfg.app_host, port=cfg.app_port, reload=True)
