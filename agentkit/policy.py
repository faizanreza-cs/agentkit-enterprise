"""Role-based tool policy loaded from YAML.

Each role maps tools to one of: allow | approve | deny. Unlisted tools are denied
(default-deny). "*" sets the role's default. Example:

    roles:
      support_agent:
        "*": deny
        search_customers: allow
        send_email: approve
      support_manager:
        "*": allow
        refund_payment: approve
    approvers: [support_manager]
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml

Decision = Literal["allow", "approve", "deny"]
_VALID = {"allow", "approve", "deny"}


@dataclass
class Policy:
    roles: dict[str, dict[str, Decision]]
    approvers: tuple[str, ...] = ()  # roles allowed to approve held tool calls

    @classmethod
    def from_yaml(cls, path: str | Path) -> Policy:
        data = yaml.safe_load(Path(path).read_text()) or {}
        return cls.from_dict(data)

    @classmethod
    def from_dict(cls, data: dict) -> Policy:
        roles = data.get("roles") or {}
        for role, rules in roles.items():
            for tool, d in (rules or {}).items():
                if d not in _VALID:
                    raise ValueError(f"role {role!r}, tool {tool!r}: invalid decision {d!r}")
        return cls(
            {r: dict(rules or {}) for r, rules in roles.items()}, tuple(data.get("approvers") or ())
        )

    def decide(self, role: str, tool: str) -> Decision:
        rules = self.roles.get(role)
        if rules is None:
            return "deny"
        return rules.get(tool, rules.get("*", "deny"))

    def can_approve(self, role: str) -> bool:
        return role in self.approvers

    def visible_tools(self, role: str, all_tools: list[str]) -> list[str]:
        """Tools the model is told about: everything not denied for this role."""
        return [t for t in all_tools if self.decide(role, t) != "deny"]
