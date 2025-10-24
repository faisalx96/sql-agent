Agents SDK Example (Python)

What this is
- A minimal, opinionated recipe to spin up an agent quickly using the OpenAI Python SDK and function-calling tools, with a tiny web UI that “just works”.
- Drop it into a repo and start iterating on POCs without wiring a custom UI each time.

What’s included
- FastAPI app with a dead-simple chat UI (no frameworks on the client side).
- Agent core that supports tool-calling and a small standard toolset (list/read/write/search files within a workspace directory), plus SQL tools for schema + querying.
- Environment-driven config and a dedicated workspace folder that’s safe to target with tools.
- Clear extension points to add tools and change the model or system prompt.

Why not Streamlit? This aims for a native-feeling, dependency-light UI that runs anywhere you can start a web server. No ceremony, no extra app shell.

Quickstart
1) Python env
   - python -m venv .venv
   - source .venv/bin/activate  # Windows: .venv\\Scripts\\activate
   - pip install -U pip
   - pip install openai fastapi "uvicorn[standard]" python-dotenv orjson

   Optional (Poetry):
   - pip install poetry
   - poetry install

2) Configure
   - cp .env.example .env
   - Set OPENAI_API_KEY and optionally OPENAI_MODEL (defaults to gpt-4o-mini)

3) Run
   - uvicorn app.server:create_app --factory --reload --host ${APP_HOST:-127.0.0.1} --port ${APP_PORT:-8000}
   - Open http://127.0.0.1:8000

Folder layout
- app/
  - config.py: Loads env, creates workspace directory.
  - agent/core.py: Agent orchestration and tool-call loop.
  - agent/tools.py: Built-in file tools and tool registry.
  - agent/sql_tools.py: SQL tools (schema and read-only query).
  - server.py: FastAPI server + endpoints; minimal streaming.
  - ui/static/: Barebones HTML/CSS/JS chat.
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
- Tools are exposed to the model via OpenAI function calling. The agent loop handles execution and returns results back to the model.

Swapping in the OpenAI Agents SDK
- This scaffold purposely concentrates the model call in Agent.respond(). If your org uses the new OpenAI Agents SDK/runtime, wire it in there:
  - Replace the chat.completions call with your Agents SDK call.
  - Propagate tool results back to the agent runtime as required by that API.
  - Keep the messages list format {role, content} so the UI and session store keep working unchanged.

Notes on streaming
- The UI uses minimal newline-delimited JSON for streaming. The server executes tool-calls first and then streams the final assistant text in small chunks for a responsive feel. This keeps the code simple while remaining robust.

Security considerations
- File tools are constrained to WORKSPACE_DIR and reject path traversal. Still, treat this as a dev/POR scaffold, not a production sandbox.

Common tweaks for POCs
- Change the system prompt in app/server.py when creating the Agent.
- Preload context: Push a system message at session start or call a tool that reads project docs.
- Add custom tools: e.g., run linters, query a DB, call internal APIs (wrap them with careful auth and rate limiting).

Troubleshooting
- ImportError: Ensure dependencies are installed as shown in Quickstart.
- 401 from OpenAI: Verify OPENAI_API_KEY, organization access, and model name.
- Network-restricted env: Point OPENAI_BASE_URL to your internal gateway if applicable.

License
- Intended as an internal template/recipe. Add your org’s standard header/policy if needed.

SQL Agent usage
- The system prompt configures the agent as a read-only data scientist. It queries a SQLite database via tools:
  - sql_schema: discover tables/columns and row counts
  - sql_query: run read-only SELECT/CTE and return rows (defaults to LIMIT 100)
  - No DDL/DML tools are exposed; requests to change data/schema should be declined.

Initial database
- Default DB path: workspace/agent.db (created on first connection).
- Create tables/data manually (the agent is read-only):
  - python - <<'PY'
from app.db import Database
from app.config import Config
cfg = Config.load()
db = Database(cfg.database_url)
db.execute("CREATE TABLE IF NOT EXISTS customers(id INTEGER PRIMARY KEY, name TEXT, city TEXT);")
db.execute("INSERT INTO customers(name, city) VALUES(?, ?)", ["Alice", "NYC"])
print(db.schema())
PY

Examples to try in the UI
- "What tables are available?"
- "How many customers are in NYC?"
- "Show the top 5 orders by amount with the customer name."
- "Can we calculate average order value by city?"
