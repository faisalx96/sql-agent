from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    openai_api_key: str
    openai_base_url: str | None
    openai_model: str
    # Optional default headers for OpenAI-compatible providers (e.g., OpenRouter)
    openai_default_headers: dict | None
    # Extra body params for OpenRouter (e.g., provider preferences)
    openrouter_extra_body: dict | None
    app_host: str
    app_port: int
    workspace_dir: Path
    database_url: str
    # Langfuse tracing
    langfuse_public_key: str | None
    langfuse_secret_key: str | None
    langfuse_host: str | None
    tracing_enabled: bool

    @staticmethod
    def load() -> "Config":
        # Load .env if present
        load_dotenv(override=False)

        # Prefer OPENAI_API_KEY but fall back to OPENROUTER_API_KEY for convenience
        api_key = os.getenv("OPENAI_API_KEY") or os.getenv("OPENROUTER_API_KEY") or ""
        base_url = os.getenv("OPENAI_BASE_URL") or None
        model = os.getenv("OPENAI_MODEL", "gpt-5-mini")
        host = os.getenv("APP_HOST", "127.0.0.1")
        port = int(os.getenv("APP_PORT", "8000"))
        workspace = Path(os.getenv("WORKSPACE_DIR", "workspace")).resolve()

        # Ensure workspace exists
        workspace.mkdir(parents=True, exist_ok=True)

        # Default to a local SQLite DB inside the workspace
        default_db = f"sqlite:///{(workspace / 'agent.db').resolve()}"
        db_url = os.getenv("DATABASE_URL", default_db)

        # Langfuse config
        lf_pub = os.getenv("LANGFUSE_PUBLIC_KEY") or None
        lf_sec = os.getenv("LANGFUSE_SECRET_KEY") or None
        # Support both LANGFUSE_HOST (current) and LANGFUSE_BASE_URL (legacy/docs)
        lf_host = os.getenv("LANGFUSE_HOST") or os.getenv("LANGFUSE_BASE_URL") or None
        tracing_on = bool(os.getenv("TRACING_ENABLED", "1").strip() != "0")

        # Optional default headers for OpenRouter (recommended: HTTP-Referer and X-Title)
        default_headers = None
        openrouter_extra_body = None
        try:
            if base_url and "openrouter.ai" in str(base_url):
                ref = (os.getenv("OPENROUTER_SITE_URL") or "").strip()
                title = (os.getenv("OPENROUTER_APP_NAME") or "SQL Agent").strip()
                headers = {}
                if ref:
                    headers["HTTP-Referer"] = ref
                if title:
                    headers["X-Title"] = title
                default_headers = headers or None
                # Prioritize low-latency providers
                openrouter_extra_body = {"provider": {"sort": "latency"}}
        except Exception:
            default_headers = None

        return Config(
            openai_api_key=api_key,
            openai_base_url=base_url,
            openai_model=model,
            openai_default_headers=default_headers,
            openrouter_extra_body=openrouter_extra_body,
            app_host=host,
            app_port=port,
            workspace_dir=workspace,
            database_url=db_url,
            langfuse_public_key=lf_pub,
            langfuse_secret_key=lf_sec,
            langfuse_host=lf_host,
            tracing_enabled=tracing_on,
        )
