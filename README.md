SQL Agent (Python)

What this is
- A minimal, opinionated recipe to spin up an agent quickly using the OpenAI Python SDK and function-calling tools, with a tiny web UI that “just works”.
- Drop it into a repo and start iterating on POCs without wiring a custom UI each time.

What’s included
- FastAPI app with a lightweight, modern UI (Vue 3 + Tailwind via CDN; no build step).
- Agent core that supports tool-calling and a small standard toolset (list/read/write/search files within a workspace directory), plus SQL tools for schema + querying (read-only).
- Live streaming: tool calls/results stream as they happen, followed by the assistant’s reply.
- Pretty SQL view in the tool panel, plus a compact “Result preview” table for quick inspection.
- Environment-driven config and a dedicated workspace folder for file operations.
- Clear extension points to add tools and change the model or system prompt.
 - Server-side sessions: JSON-backed store under the workspace with APIs to list/get/rename/delete sessions.

Why not Streamlit? This aims for a native-feeling, dependency-light UI that runs anywhere you can start a web server. No ceremony, no extra app shell.

Quickstart
1) One environment (no mixing)
   - Use the provided helper to create and use a local venv just for this project:
     - bash scripts/dev.sh
   - It will create `.venv/`, install dependencies from `requirements.txt` (includes langfuse), and run the server with that interpreter.
   - Tip: Prefer `python -m uvicorn ...` through this script vs a global `uvicorn` to avoid PATH mixups.

2) Configure
   - cp .env.example .env
   - Set OPENAI_API_KEY and optionally OPENAI_MODEL (defaults to gpt-4o-mini)

3) Run
   - Simple: bash scripts/dev.sh
   - Manual (if you already activated .venv):
     - python -m uvicorn app.server:app --reload --host ${APP_HOST:-127.0.0.1} --port ${APP_PORT:-8000}
   - Open http://127.0.0.1:8000

Folder layout
- app/
  - config.py: Loads env, creates workspace directory.
  - agent/core.py: Agent orchestration and tool-call loop (utility; server currently streams directly).
  - agent/tools.py: Built-in file tools and tool registry.
  - agent/sql_tools.py: SQL tools (schema and read-only query).
  - server.py: FastAPI server + endpoints; live tool + text streaming.
  - ui/static/: Vue + Tailwind assets (CDN) for the chat UI.
- scripts/
  - seed_db.py: Seed/reset the SQLite DB with demo data (customers/products/orders).
- workspace/: Safe area for file tools; persisted locally.

Config
- OPENAI_API_KEY: Your API key
- OPENAI_BASE_URL: Use custom base URL if you run via a proxy/gateway
- OPENAI_MODEL: Default gpt-4o-mini; switch to your org’s standard model
- APP_HOST / APP_PORT: Web server bind
- WORKSPACE_DIR: Filesystem root agents tools can touch
- DATABASE_URL: Defaults to sqlite:///workspace/agent.db. Set to your DB if needed. The agent uses SQLite by default.

Extending tools
- Add or modify tools in app/agent/tools.py. Each ToolSpec has name, description, JSON schema, and a Python function.
- Tools are exposed to the model via OpenAI function calling. The server’s streaming loop executes tool calls and streams back results.

Swapping in the OpenAI Agents SDK
- Integration point: the chat.completions call inside app/server.py (the streaming loop). Swap it for your Agents SDK runtime and wire tool events accordingly.
- Keep the messages list format {role, content} so the UI and session store keep working unchanged.

Notes on streaming
- The server streams newline-delimited JSON (NDJSON) events:
  - {"type":"tool_call", "id", "name", "arguments"}
  - {"type":"tool_result", "id", "name", "output"}
  - {"chunk":"..."} and {"done":true} for the assistant’s text
- The UI shows live tool panels (with pretty SQL and JSON I/O), a “Result preview” grid for sql_query, and a live timer.

Session APIs
- POST /api/new_chat {title?} → {chat_id}
- GET  /api/sessions → {sessions:[{id,title,created_at,updated_at}]}
- GET  /api/sessions/{chat_id} → {id,title,created_at,updated_at,messages:[...]}
- PATCH /api/sessions/{chat_id} {title} → {ok:true}
- DELETE /api/sessions/{chat_id} → {ok:true}

Security considerations
- File tools are constrained to WORKSPACE_DIR and reject path traversal. Still, treat this as a dev/POR scaffold, not a production sandbox.

Common tweaks for POCs
- Change the system prompt in app/server.py when creating the Agent.
- Preload context: Push a system message at session start or call a tool that reads project docs.
- Add custom tools: e.g., run linters, query a DB, call internal APIs (wrap them with careful auth and rate limiting).

Troubleshooting
- ImportError / ModuleNotFoundError: Ensure you started via `bash scripts/dev.sh` or activated `.venv` in this repo. If your prompt shows another venv (or conda `base`) at the same time, deactivate it first, then run the script again.
- 401 from OpenAI: Verify OPENAI_API_KEY, organization access, and model name.
- Network-restricted env: Point OPENAI_BASE_URL to your internal gateway if applicable.

License
- Intended as an internal template/recipe. Add your org’s standard header/policy if needed.

SQL Agent usage
- The system prompt configures a read-only “data scientist” persona that answers with one short, business-friendly summary sentence (no tables by default).
- Tools:
  - sql_schema: discover tables/columns and row counts
  - sql_query: run read-only SELECT/CTE and return rows (defaults to LIMIT 100)
  - No DDL/DML tools are exposed; requests to change data/schema should be declined.
- Naming guidance: Prefer human-readable names over IDs; join customers/products to include name/label columns; alias to short business labels.

Initial database
- Default DB path: workspace/agent.db (created on first connection).
- Seed or reset with demo data:
  - PYTHONPATH=. python scripts/seed_db.py --reset --customers 60 --products 50 --orders 500
  - Omit --reset to keep existing rows and only fill if empty.

Examples to try in the UI
- "What tables are available?"
- "How many orders per customer?"
- "Which product generates the most revenue?"
- "Average order value by city (summary only)."
