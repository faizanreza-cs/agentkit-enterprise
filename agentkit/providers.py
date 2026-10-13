"""Provider-agnostic LLM interface.

The agent speaks one message format (Anthropic-style content blocks) and every provider
translates to and from it. That keeps the agent loop identical whether it runs on the
Anthropic API (default), OpenAI, or the deterministic mock planner used in tests and CI.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from agentkit.config import Settings, get_settings


@dataclass
class ToolCall:
    id: str
    name: str
    input: dict[str, Any]


@dataclass
class Turn:
    """One assistant response, normalised across providers."""

    text: str
    tool_calls: list[ToolCall]
    stop_reason: str  # "end_turn" | "tool_use" | "refusal" | "max_tokens" | ...
    # Exactly what to append to history as the assistant message content. For Anthropic this
    # is the raw response content (thinking blocks must be echoed back unchanged).
    content: list[Any]
    usage: dict[str, int] = field(default_factory=dict)


class LLMProvider(Protocol):
    name: str
    model: str

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn: ...


# --------------------------------------------------------------------------- Anthropic


def _require_model(s: Settings, provider: str) -> str:
    if not s.model:
        raise ValueError(
            f"AGENTKIT_MODEL is not set; set it to the {provider} model id to use, "
            "or use AGENTKIT_PROVIDER=mock"
        )
    return s.model


class AnthropicProvider:
    """Anthropic Messages API via the official ``anthropic`` SDK."""

    name = "anthropic"

    def __init__(self, settings: Settings | None = None) -> None:
        import anthropic

        s = settings or get_settings()
        self.model = _require_model(s, "Anthropic")
        self.effort = s.effort
        self.fallbacks = s.fallbacks
        self.client = anthropic.Anthropic()

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        kwargs: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 16000,
            "system": system,
            "messages": messages,
            "tools": tools,
            "output_config": {"effort": self.effort},
        }
        if self.fallbacks:
            kwargs["betas"] = ["server-side-fallback-2026-07-01"]
            kwargs["fallbacks"] = "default"
            resp = self.client.beta.messages.create(**kwargs)
        else:
            resp = self.client.messages.create(**kwargs)

        text = "".join(b.text for b in resp.content if b.type == "text")
        calls = [
            ToolCall(id=b.id, name=b.name, input=dict(b.input))
            for b in resp.content
            if b.type == "tool_use"
        ]
        if resp.stop_reason == "refusal":
            text = text or "The model declined to answer this request."
        return Turn(
            text=text,
            tool_calls=calls,
            stop_reason=str(resp.stop_reason),
            content=[b.model_dump(mode="json", exclude_none=True) for b in resp.content],
            usage={
                "input_tokens": resp.usage.input_tokens,
                "output_tokens": resp.usage.output_tokens,
            },
        )


# --------------------------------------------------------------------------- OpenAI


def _block(b: Any, key: str, default: Any = None) -> Any:
    return b.get(key, default) if isinstance(b, dict) else getattr(b, key, default)


class OpenAIProvider:
    """Optional OpenAI backend (``pip install agentkit-enterprise[openai]``)."""

    name = "openai"

    def __init__(self, settings: Settings | None = None) -> None:
        from openai import OpenAI

        s = settings or get_settings()
        self.model = os.environ.get("AGENTKIT_OPENAI_MODEL") or _require_model(s, "OpenAI")
        self.client = OpenAI()

    @staticmethod
    def _convert(system: str, messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = [{"role": "system", "content": system}]
        for m in messages:
            content = m["content"]
            if isinstance(content, str):
                out.append({"role": m["role"], "content": content})
                continue
            if m["role"] == "assistant":
                text = "".join(
                    _block(b, "text", "") for b in content if _block(b, "type") == "text"
                )
                calls = [
                    {
                        "id": _block(b, "id"),
                        "type": "function",
                        "function": {
                            "name": _block(b, "name"),
                            "arguments": json.dumps(_block(b, "input")),
                        },
                    }
                    for b in content
                    if _block(b, "type") == "tool_use"
                ]
                msg: dict[str, Any] = {"role": "assistant", "content": text or None}
                if calls:
                    msg["tool_calls"] = calls
                out.append(msg)
            else:
                for b in content:
                    if _block(b, "type") == "tool_result":
                        out.append(
                            {
                                "role": "tool",
                                "tool_call_id": b["tool_use_id"],
                                "content": b["content"],
                            }
                        )
                    elif _block(b, "type") == "text":
                        out.append({"role": "user", "content": b["text"]})
        return out

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        oa_tools = [
            {
                "type": "function",
                "function": {
                    "name": t["name"],
                    "description": t["description"],
                    "parameters": t["input_schema"],
                },
            }
            for t in tools
        ]
        resp = self.client.chat.completions.create(
            model=self.model, messages=self._convert(system, messages), tools=oa_tools
        )
        msg = resp.choices[0].message
        calls = [
            ToolCall(id=c.id, name=c.function.name, input=json.loads(c.function.arguments or "{}"))
            for c in (msg.tool_calls or [])
        ]
        content: list[Any] = []
        if msg.content:
            content.append({"type": "text", "text": msg.content})
        content += [
            {"type": "tool_use", "id": c.id, "name": c.name, "input": c.input} for c in calls
        ]
        return Turn(
            text=msg.content or "",
            tool_calls=calls,
            stop_reason="tool_use" if calls else "end_turn",
            content=content,
        )


# --------------------------------------------------------------------------- factory


def _has_anthropic_credentials() -> bool:
    return bool(os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("ANTHROPIC_AUTH_TOKEN"))


def get_provider(name: str | None = None, settings: Settings | None = None) -> LLMProvider:
    s = settings or get_settings()
    name = (name or s.provider).lower()
    if name == "auto":
        name = "anthropic" if _has_anthropic_credentials() and s.model else "mock"
    if name == "anthropic":
        return AnthropicProvider(s)
    if name == "openai":
        return OpenAIProvider(s)
    if name == "mock":
        from agentkit.examples.mock_planner import MockPlanner

        return MockPlanner()
    raise ValueError(f"unknown provider {name!r} (anthropic | openai | mock | auto)")
