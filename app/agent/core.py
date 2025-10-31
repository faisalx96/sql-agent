from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from openai import OpenAI

from .tools import ToolSpec, as_openai_tools, dispatch_tool


RoleMessage = Dict[str, Any]


@dataclass
class Agent:
    model: str
    client: OpenAI
    tools: List[ToolSpec] = field(default_factory=list)
    system_prompt: Optional[str] = None

    def build_messages(self, messages: List[RoleMessage]) -> List[RoleMessage]:
        msgs: List[RoleMessage] = []
        if self.system_prompt:
            msgs.append({"role": "system", "content": self.system_prompt})
        msgs.extend(messages)
        return msgs

    def respond(self, messages: List[RoleMessage]) -> Dict[str, Any]:
        """Blocking call that executes any tool calls until final assistant message is ready.

        Returns a dict with keys: {content: str, messages: List[...]} where messages is the full
        message history including tool call messages appended.
        """
        history = self.build_messages(messages)

        while True:
            resp = self.client.chat.completions.create(
                model=self.model,
                messages=history,
                tools=as_openai_tools(self.tools) if self.tools else None,
                tool_choice="auto" if self.tools else None,
                temperature=0.2,
            )

            choice = resp.choices[0]
            msg = choice.message
            if msg.tool_calls:
                # Execute tools and append results
                history.append({
                    "role": "assistant",
                    "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
                    "content": msg.content,
                    "model": self.model,
                })
                for tc in msg.tool_calls:
                    result = dispatch_tool(self.tools, tc.function.name, tc.function.arguments)
                    history.append({
                        "role": "tool",
                        "tool_call_id": tc.id,
                        "name": tc.function.name,
                        "content": json.dumps(result),
                    })
                # Loop; the model will receive tool results in the next turn
                continue

            content = msg.content or ""
            history.append({"role": "assistant", "content": content, "model": self.model})
            return {"content": content, "messages": history}
