"""Deterministic stand-in for an LLM, scripted for the support-desk demo.

It plans a short tool sequence from the first user message, feeds each tool result into the
next call, then summarises. Lets the full governed loop (policy, approvals, audit, budgets)
run in CI and demos with no API key. It is not a measure of model quality.
"""

from __future__ import annotations

import json
import re
from typing import Any

from agentkit.providers import ToolCall, Turn


def _first_user_text(messages: list[dict[str, Any]]) -> str:
    return next(
        m["content"] for m in messages if m["role"] == "user" and isinstance(m["content"], str)
    )


def _tool_results(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for m in messages:
        if m["role"] == "user" and isinstance(m["content"], list):
            for b in m["content"]:
                if b.get("type") == "tool_result":
                    out.append({"error": b.get("is_error", False), **json.loads(b["content"])})
    return out


def _name(text: str) -> str:
    m = re.search(r"\b(jane|ravi|maria)\b", text, re.I)
    return m.group(1) if m else text.split()[-1]


class MockPlanner:
    name = "mock"
    model = "mock-planner-v1"

    def complete(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Turn:
        text = _first_user_text(messages).lower()
        results = _tool_results(messages)
        step = len(results)
        available = {t["name"] for t in tools}

        plan: list[tuple[str, Any]] = [("search_customers", lambda r: {"query": _name(text)})]
        cust = lambda r: r[0]["customers"][0]["id"]  # noqa: E731
        if re.search(r"^email\b|\bsend\b|\b(email|write to) (jane|ravi|maria)\b", text):
            plan.append(
                (
                    "send_email",
                    lambda r: {
                        "customer_id": cust(r),
                        "subject": "Update on your request",
                        "body": "Hi, your request has been processed. Reply if you need anything.",
                    },
                )
            )
        elif "refund" in text:
            plan.append(("get_orders", lambda r: {"customer_id": cust(r)}))
            plan.append(
                (
                    "refund_payment",
                    lambda r: {
                        "order_id": r[1]["orders"][0]["id"],
                        "amount": r[1]["orders"][0]["amount"],
                        "reason": "customer request",
                    },
                )
            )
        elif "ticket" in text:
            plan.append(
                (
                    "create_ticket",
                    lambda r: {
                        "customer_id": cust(r),
                        "subject": "Follow-up requested",
                        "priority": "high",
                    },
                )
            )
        elif "order" in text:
            plan.append(("get_orders", lambda r: {"customer_id": cust(r)}))

        if results and results[-1]["error"] or step >= len(plan):
            return self._final(results)
        if step >= 1 and not results[0].get("customers"):
            return self._final(results)
        tool, make_args = plan[step]
        if tool not in available:
            return self._final(results, f"I'm not permitted to use {tool}.")
        call = ToolCall(f"mock_{step}", tool, make_args(results))
        return Turn(
            "",
            [call],
            "tool_use",
            [{"type": "tool_use", "id": call.id, "name": tool, "input": call.input}],
            {"input_tokens": 400 + 120 * step, "output_tokens": 60},
        )

    @staticmethod
    def _final(results: list[dict[str, Any]], prefix: str = "") -> Turn:
        last = results[-1] if results else {}
        if last.get("error"):
            msg = f"I couldn't complete that: {last['error']}."
        elif "sent" in last:
            msg = "Done. The email was sent to the customer."
        elif "refunded" in last:
            msg = f"Refunded {last['refunded']:.2f} on order {last['order_id']}."
        elif "ticket_id" in last:
            msg = f"Opened ticket #{last['ticket_id']}."
        elif "orders" in last:
            msg = "Orders: " + ", ".join(
                f"{o['id']} {o['amount']:.2f} ({o['status']})" for o in last["orders"]
            )
        elif "customers" in last:
            c = last["customers"]
            msg = (
                "Found: " + ", ".join(f"{x['name']} ({x['id']}, {x['plan']})" for x in c)
                if c
                else "No matching customer."
            )
        else:
            msg = "Done."
        text = f"{prefix} {msg}".strip()
        return Turn(
            text,
            [],
            "end_turn",
            [{"type": "text", "text": text}],
            {"input_tokens": 300, "output_tokens": 40},
        )
