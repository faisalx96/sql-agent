# SQL Agent

A minimal Python agent scaffold for querying databases via natural language. Uses OpenAI function calling + FastAPI with a lightweight Vue/Tailwind UI (no build step).

## Features

- **Read-only SQL agent** — asks schema, writes SELECT/CTEs, returns human-friendly answers with inline charts
- **Multi-database** — SQLite (default) or PostgreSQL via SQLAlchemy
- **Live streaming** — tool calls/results stream as NDJSON, then assistant reply
- **File tools** — list/read/write/search files in a sandboxed workspace
- **Session management** — JSON-backed chat history with rename/delete APIs
- **Optional tracing** — Langfuse integration for observability
- **OpenRouter compatible** — swap providers without code changes

## Quickstart

```bash
# 1. Setup env
cp .env.example .env
# Edit .env: set OPENAI_API_KEY

# 2. Run (creates venv, installs deps, starts server)
bash scripts/dev.sh

# 3. Open http://127.0.0.1:8000
```

### Seed demo data (optional)

```bash
PYTHONPATH=. python scripts/seed_db.py --reset --customers 60 --products 50 --orders 500
```

## Configuration

| Variable | Default | Description |
|----------|---------|-------------|
| `OPENAI_API_KEY` | — | Required |
| `OPENAI_MODEL` | `gpt-5-mini` | Model name |
| `OPENAI_BASE_URL` | — | Custom endpoint (OpenRouter, Azure, etc.) |
| `DATABASE_URL` | `sqlite:///workspace/agent.db` | SQLite or PostgreSQL connection string |
| `APP_HOST` / `APP_PORT` | `127.0.0.1` / `8000` | Server bind |
| `WORKSPACE_DIR` | `workspace` | Sandboxed file area |
| `LANGFUSE_*` | — | Optional tracing (see `.env.example`) |
| `TRACING_ENABLED` | `1` | Set `0` to disable |

### PostgreSQL

```env
DATABASE_URL=postgresql://user:pass@host:5432/dbname
```

### OpenRouter

```env
OPENAI_BASE_URL=https://openrouter.ai/api/v1
OPENAI_API_KEY=sk-or-v1-...
OPENAI_MODEL=anthropic/claude-3.5-sonnet
```

## Project Structure

```
app/
├── server.py          # FastAPI app, streaming chat endpoint
├── config.py          # Env loading
├── db.py              # SQLAlchemy database abstraction
├── sessions.py        # JSON session store
├── tracing.py         # Langfuse wrapper
├── agent/
│   ├── core.py        # Agent loop (blocking)
│   ├── tools.py       # File tools + ToolSpec registry
│   └── sql_tools.py   # sql_schema, sql_query tools
└── ui/static/         # Vue 3 + Tailwind (CDN)
scripts/
└── seed_db.py         # Seed/reset demo data
```

## Tools

| Tool | Description |
|------|-------------|
| `sql_schema` | Returns tables, columns, types, PKs, row counts |
| `sql_query` | Executes read-only SELECT/CTE (max 1000 rows) |
| `list_files` | Lists workspace files |
| `read_file` | Reads UTF-8 file from workspace |
| `write_file` | Writes file to workspace |
| `search_files` | Case-insensitive substring search |

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/chat` | Streaming chat (NDJSON) |
| `POST` | `/api/new_chat` | Create session |
| `GET` | `/api/sessions` | List sessions |
| `GET` | `/api/sessions/{id}` | Get session + messages |
| `PATCH` | `/api/sessions/{id}` | Rename session |
| `DELETE` | `/api/sessions/{id}` | Delete session |

## Extending

**Add tools:** Edit `app/agent/tools.py`. Each `ToolSpec` has name, description, JSON schema, and handler function.

**Change system prompt:** Edit the `system_prompt` in `app/server.py` where `Agent` is instantiated.

**Swap to Agents SDK:** Replace the streaming loop in `server.py` with your runtime; keep `{role, content}` message format for UI compatibility.

## Troubleshooting

- **ImportError**: Run via `bash scripts/dev.sh` or ensure `.venv` is activated
- **401 from OpenAI**: Check `OPENAI_API_KEY` and model access
- **Decimal serialization error (Postgres)**: Already handled in `db.py`