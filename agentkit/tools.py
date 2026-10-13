"""Tool registry. Tools are plain functions with a Pydantic argument model.

    @registry.tool(risk="high", description="Send an email to a customer")
    def send_email(args: SendEmail) -> dict: ...

The argument model gives the LLM a JSON schema and validates every call before it runs.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from pydantic import BaseModel, ValidationError

Risk = Literal["low", "medium", "high"]


@dataclass
class Tool:
    name: str
    description: str
    args_model: type[BaseModel]
    fn: Callable[[Any], dict[str, Any]]
    risk: Risk = "low"
    timeout_s: float = 15.0
    retries: int = 1

    def schema(self) -> dict[str, Any]:
        s = self.args_model.model_json_schema()
        s.pop("title", None)
        return {"name": self.name, "description": self.description, "input_schema": s}

    def validate(self, raw: dict[str, Any]) -> BaseModel:
        return self.args_model.model_validate(raw)


class ToolError(Exception):
    """Raised by tools for expected failures; the message is returned to the model."""


@dataclass
class Registry:
    tools: dict[str, Tool] = field(default_factory=dict)

    def tool(self, *, description: str, risk: Risk = "low", name: str | None = None,
             timeout_s: float = 15.0, retries: int = 1) -> Callable:  # fmt: skip
        def deco(fn: Callable[[Any], dict[str, Any]]) -> Callable[[Any], dict[str, Any]]:
            ann = [v for k, v in fn.__annotations__.items() if k != "return"]
            if not ann:
                raise TypeError(f"{fn.__name__} needs a Pydantic args parameter")
            model = ann[0]
            if isinstance(model, str):  # postponed annotations
                model = fn.__globals__[model]
            self.tools[name or fn.__name__] = Tool(
                name or fn.__name__, description, model, fn, risk, timeout_s, retries
            )
            return fn

        return deco

    def schemas(self, names: list[str] | None = None) -> list[dict[str, Any]]:
        return [t.schema() for n, t in self.tools.items() if names is None or n in names]

    def get(self, name: str) -> Tool:
        if name not in self.tools:
            raise KeyError(name)
        return self.tools[name]


__all__ = ["Registry", "Tool", "ToolError", "ValidationError"]
