from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any, Dict, Optional


logger = logging.getLogger("sql-agent.tracing")


def _now_ms() -> int:
    return int(time.time() * 1000)


@dataclass
class TraceHandle:
    enabled: bool
    trace_id: str
    _client: Any | None = None
    _trace_obj: Any | None = None

    def event(self, name: str, data: Dict[str, Any] | None = None) -> None:
        if not self.enabled or not self._client:
            return
        try:
            # Prefer an explicit event if available, otherwise a short span
            if self._trace_obj and hasattr(self._trace_obj, "event"):
                self._trace_obj.event(name=name, input=data or {})
            elif self._trace_obj and hasattr(self._trace_obj, "span"):
                # Create-and-end span to ensure persistence
                try:
                    sp = self._trace_obj.span(name=name, input=data or {})
                    if hasattr(sp, "end"):
                        sp.end(output={"ok": True})
                except Exception:
                    # Fallback to direct API
                    if hasattr(self._client, "observations") and hasattr(
                        self._client.observations, "create_span"
                    ):
                        self._client.observations.create_span(
                            trace_id=self.trace_id,
                            name=name,
                            input=data or {},
                            output={"ok": True},
                        )
            else:
                # Older client APIs
                if hasattr(self._client, "observations") and hasattr(
                    self._client.observations, "create_span"
                ):
                    self._client.observations.create_span(
                        trace_id=self.trace_id,
                        name=name,
                        input=data or {},
                        output={"ok": True},
                    )
        except Exception:
            logger.debug("langfuse event emit failed", exc_info=True)

    def generation(
        self,
        *,
        name: str,
        model: str | None,
        input: Any | None,
        output: Any | None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        usage: Dict[str, Any] | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or not self._client:
            return
        try:
            payload = dict(
                name=name,
                model=model,
                input=input,
                output=output,
                metadata=metadata or {},
                usage=usage or None,
            )
            if start_ms:
                payload["start_time"] = start_ms
            if end_ms:
                payload["end_time"] = end_ms

            if self._trace_obj and hasattr(self._trace_obj, "generation"):
                gen = self._trace_obj.generation(
                    name=name,
                    model=model,
                    input=input,
                    metadata=metadata or {},
                )
                if hasattr(gen, "end"):
                    try:
                        gen.end(output=output, usage=usage)
                    except TypeError:
                        # Some SDKs use update() instead of end()
                        try:
                            gen.update(output=output, usage=usage)
                        except Exception:
                            pass
            else:
                if hasattr(self._client, "observations") and hasattr(
                    self._client.observations, "create_generation"
                ):
                    self._client.observations.create_generation(
                        trace_id=self.trace_id, **payload
                    )
        except Exception:
            logger.debug("langfuse generation emit failed", exc_info=True)

    def span(
        self,
        *,
        name: str,
        input: Any | None,
        output: Any | None,
        start_ms: int | None = None,
        end_ms: int | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> None:
        if not self.enabled or not self._client:
            return
        try:
            payload = dict(name=name, input=input, output=output, metadata=metadata or {})
            if start_ms:
                payload["start_time"] = start_ms
            if end_ms:
                payload["end_time"] = end_ms

            if self._trace_obj and hasattr(self._trace_obj, "span"):
                sp = self._trace_obj.span(name=name, input=input, metadata=metadata or {})
                if hasattr(sp, "end"):
                    try:
                        sp.end(output=output)
                    except TypeError:
                        try:
                            sp.update(output=output)
                        except Exception:
                            pass
            else:
                if hasattr(self._client, "observations") and hasattr(
                    self._client.observations, "create_span"
                ):
                    self._client.observations.create_span(
                        trace_id=self.trace_id, **payload
                    )
        except Exception:
            logger.debug("langfuse span emit failed", exc_info=True)


class Tracer:
    def __init__(
        self,
        *,
        enabled: bool,
        public_key: Optional[str] = None,
        secret_key: Optional[str] = None,
        host: Optional[str] = None,
    ) -> None:
        self.enabled = enabled and bool(public_key and secret_key)
        self._client = None
        if self.enabled:
            try:
                from langfuse import Langfuse  # type: ignore

                self._client = Langfuse(
                    public_key=public_key,
                    secret_key=secret_key,
                    host=host,
                )
            except Exception:
                logger.warning(
                    "Langfuse SDK unavailable or failed to initialize; tracing disabled"
                )
                self.enabled = False

    @classmethod
    def from_config(cls, cfg: Any) -> "Tracer":
        return cls(
            enabled=getattr(cfg, "tracing_enabled", True),
            public_key=getattr(cfg, "langfuse_public_key", None),
            secret_key=getattr(cfg, "langfuse_secret_key", None),
            host=getattr(cfg, "langfuse_host", None),
        )

    def start_trace(
        self,
        *,
        trace_id: str,
        name: str,
        session_id: Optional[str] = None,
        user_id: Optional[str] = None,
        input: Any | None = None,
        metadata: Dict[str, Any] | None = None,
    ) -> TraceHandle:
        if not self.enabled or not self._client:
            return TraceHandle(enabled=False, trace_id=trace_id)

        trace_obj = None
        try:
            # Newer SDKs
            trace_obj = self._client.trace(
                id=trace_id,
                name=name,
                input=input,
                session_id=session_id,
                user_id=user_id,
                metadata=metadata or {},
            )
        except Exception:
            try:
                # Older client fallback
                if hasattr(self._client, "observations") and hasattr(
                    self._client.observations, "create_trace"
                ):
                    self._client.observations.create_trace(
                        id=trace_id,
                        name=name,
                        input=input,
                        session_id=session_id,
                        user_id=user_id,
                        metadata=metadata or {},
                    )
            except Exception:
                logger.debug("langfuse create trace failed", exc_info=True)

        return TraceHandle(
            enabled=True,
            trace_id=trace_id,
            _client=self._client,
            _trace_obj=trace_obj,
        )

    def flush(self) -> None:
        try:
            if self.enabled and self._client and hasattr(self._client, "flush"):
                self._client.flush()
        except Exception:
            logger.debug("langfuse flush failed", exc_info=True)
