"""The governed agent loop.

Every step is checked and recorded:

  user message ─▶ redact PII ─▶ LLM ─▶ tool calls
                                         │
                     policy: allow ──────┼─▶ validate args ─▶ run (timeout, retry) ─▶ redact
                             approve ────┼─▶ hold; run pauses until an approver decides
                             deny ───────┘─▶ error result returned to the model
  budgets (steps, tokens per run, tokens per user per day) are enforced between LLM calls
  every event goes to the hash-chained audit log; spans go to the run trace
"""

from __future__ import annotations

import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import asdict, dataclass, field
from typing import Any

from pydantic import ValidationError

from agentkit.config import Settings, get_settings
from agentkit.policy import Policy
from agentkit.providers import LLMProvider, ToolCall, get_provider
from agentkit.redact import redact, redact_obj
from agentkit.store import Store
from agentkit.tools import Registry, ToolError
from agentkit.tracing import Tracer

SYSTEM = """\
You are an internal operations assistant. Use the tools to act; never invent data.
- Personal data is masked as [EMAIL], [PHONE], [CARD]. Refer to customers by id; tools resolve
  contact details themselves.
- If a tool result says an action was denied or is awaiting approval, tell the user plainly.
- Be concise and state exactly what you did.
"""

_pool = ThreadPoolExecutor(max_workers=8)


@dataclass
class RunResult:
    run_id: str
    status: str  # completed | awaiting_approval | denied_budget | failed | max_steps
    answer: str = ""
    pending_approvals: list[dict[str, Any]] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: dict[str, int] = field(default_factory=dict)
    trace: list[dict[str, Any]] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class BudgetExceeded(Exception):
    pass


class Agent:
    def __init__(
        self,
        registry: Registry,
        policy: Policy,
        store: Store,
        provider: LLMProvider | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.registry = registry
        self.policy = policy
        self.store = store
        self.settings = settings or get_settings()
        self.provider = provider or get_provider(settings=self.settings)
        # a misspelled tool name would silently fall back to the role's "*" rule
        unknown = (
            {t for rules in policy.roles.values() for t in rules} - {"*"} - set(registry.tools)
        )
        if unknown:
            raise ValueError(f"policy names unknown tools: {sorted(unknown)}")

    # ------------------------------------------------------------------ public
    def start(self, user_id: str, role: str, message: str) -> RunResult:
        run_id = uuid.uuid4().hex[:16]
        clean, findings = redact(message)
        state: dict[str, Any] = {
            "messages": [{"role": "user", "content": clean}],
            "usage": {},
            "tool_calls": [],
            "trace": [],
            "held": None,
        }
        self.store.audit(
            "run_started",
            user_id,
            {"role": role, "message": clean, "pii_redacted": len(findings)},
            run_id,
        )
        return self._loop(run_id, user_id, role, state)

    def decide(
        self,
        approval_id: str,
        approver_id: str,
        approver_role: str,
        approve: bool,
        note: str | None = None,
    ) -> RunResult:
        note = redact(note)[0] if note else note
        a = self.store.approval(approval_id)
        run = self.store.load_run(a["run_id"])
        if a["status"] != "pending":
            raise ValueError(f"approval {approval_id} is already {a['status']}")
        if not self.policy.can_approve(approver_role):
            raise PermissionError(f"role {approver_role!r} cannot approve")
        if approver_id == run["user_id"]:
            raise PermissionError("separation of duties: you cannot approve your own request")
        if not self.store.decide_approval(approval_id, approve, approver_id, note):
            raise ValueError(f"approval {approval_id} was already decided")
        self.store.audit(
            "approval_decided",
            approver_id,
            {"approval_id": approval_id, "tool": a["tool"], "approved": approve, "note": note},
            a["run_id"],
        )
        if self.store.approvals("pending", a["run_id"]):
            return RunResult(
                a["run_id"],
                "awaiting_approval",
                pending_approvals=self.store.approvals("pending", a["run_id"]),
            )
        # two approvers finishing at once: only one of them resumes the run
        if not self.store.claim_run(a["run_id"]):
            return RunResult(a["run_id"], "resuming")
        return self._resume(a["run_id"])

    # ------------------------------------------------------------------ loop
    def _resume(self, run_id: str) -> RunResult:
        run = self.store.load_run(run_id)
        state = run["state"]
        held = state.pop("held")
        decisions = {a["id"]: a for a in self.store.approvals(None, run_id)}
        tracer = Tracer()
        results = []
        for item in held["calls"]:
            call = ToolCall(item["id"], item["name"], item["input"])
            if item.get("approval_id"):
                a = decisions[item["approval_id"]]
                if a["status"] == "approved":
                    results.append(
                        self._execute(
                            call, run_id, run["user_id"], state, tracer, decision="approved"
                        )
                    )
                else:
                    state["tool_calls"].append({"tool": call.name, "decision": "denied"})
                    results.append(
                        self._result(
                            call, {"error": "denied by approver", "note": a.get("note")}, True
                        )
                    )
            else:
                results.append(item["result"])
        state["trace"] += tracer.export()
        state["messages"].append({"role": "user", "content": results})
        return self._loop(run_id, run["user_id"], run["role"], state)

    def _loop(self, run_id: str, user_id: str, role: str, state: dict[str, Any]) -> RunResult:
        tracer = Tracer()
        tools = self.registry.schemas(self.policy.visible_tools(role, list(self.registry.tools)))
        status, answer = "max_steps", ""
        try:
            for _ in range(self.settings.max_steps):
                self._check_budget(user_id, state)
                with tracer.span("llm", provider=self.provider.name) as sp:
                    turn = self.provider.complete(SYSTEM, state["messages"], tools)
                    sp.attrs.update(turn.usage)
                self._add_usage(user_id, state, turn.usage)
                self.store.audit(
                    "llm_call", "agent", {"stop_reason": turn.stop_reason, **turn.usage}, run_id
                )
                if turn.stop_reason == "refusal":
                    status, answer = "completed", redact(turn.text)[0]
                    break
                state["messages"].append({"role": "assistant", "content": turn.content})
                if not turn.tool_calls:
                    status, answer = "completed", redact(turn.text)[0]
                    break
                held, results = [], []
                for call in turn.tool_calls:
                    decision = self.policy.decide(role, call.name)
                    if decision == "approve":
                        args, _ = redact_obj(call.input)
                        aid = self.store.create_approval(run_id, call.name, args, user_id)
                        self.store.audit(
                            "approval_requested",
                            "agent",
                            {"approval_id": aid, "tool": call.name, "args": args},
                            run_id,
                        )
                        held.append(
                            {
                                "id": call.id,
                                "name": call.name,
                                "input": call.input,
                                "approval_id": aid,
                            }
                        )
                        state["tool_calls"].append({"tool": call.name, "decision": "held"})
                    else:
                        r = (
                            self._execute(call, run_id, user_id, state, tracer)
                            if decision == "allow"
                            else self._deny(call, run_id, role, state)
                        )
                        results.append(r)
                        held.append(
                            {"id": call.id, "name": call.name, "input": call.input, "result": r}
                        )
                if any(h.get("approval_id") for h in held):
                    state["held"] = {"calls": held}
                    state["trace"] += tracer.export()
                    self.store.save_run(run_id, user_id, role, "awaiting_approval", state)
                    pending = self.store.approvals("pending", run_id)
                    return RunResult(
                        run_id,
                        "awaiting_approval",
                        "This action needs approval before it can run.",
                        pending,
                        state["tool_calls"],
                        state["usage"],
                        state["trace"],
                    )
                state["messages"].append({"role": "user", "content": results})
        except BudgetExceeded as e:
            status, answer = "denied_budget", str(e)
        except Exception as e:  # provider/network failure: record it, never lose the run
            status, answer = "failed", f"{type(e).__name__}: {e}"
        state["trace"] += tracer.export()
        self.store.save_run(run_id, user_id, role, status, state)
        self.store.audit("run_finished", "agent", {"status": status, "answer": answer}, run_id)
        return RunResult(
            run_id, status, answer, [], state["tool_calls"], state["usage"], state["trace"]
        )

    # ------------------------------------------------------------------ tools
    @staticmethod
    def _result(call: ToolCall, payload: dict[str, Any], is_error: bool) -> dict[str, Any]:
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": call.id,
            "content": json.dumps(payload, default=str),
        }
        if is_error:
            block["is_error"] = True
        return block

    def _deny(self, call: ToolCall, run_id: str, role: str, state: dict) -> dict[str, Any]:
        self.store.audit("tool_denied", "policy", {"tool": call.name, "role": role}, run_id)
        state["tool_calls"].append({"tool": call.name, "decision": "denied"})
        return self._result(call, {"error": f"policy: role '{role}' may not use {call.name}"}, True)

    def _execute(
        self,
        call: ToolCall,
        run_id: str,
        user_id: str,
        state: dict,
        tracer: Tracer,
        decision: str = "allowed",
    ) -> dict[str, Any]:
        try:
            tool = self.registry.get(call.name)
        except KeyError:
            self.store.audit("tool_unknown", "agent", {"tool": call.name}, run_id)
            return self._result(call, {"error": f"unknown tool {call.name}"}, True)
        try:
            args = tool.validate(call.input)
        except ValidationError as e:
            self.store.audit(
                "tool_invalid_args", "agent", {"tool": call.name, "errors": e.errors()}, run_id
            )
            return self._result(call, {"error": "invalid arguments", "details": e.errors()}, True)

        payload: dict[str, Any]
        is_error = False
        with tracer.span(f"tool:{call.name}", risk=tool.risk) as sp:
            # a timed-out call may still finish, so never retry side effects (high-risk tools)
            for attempt in range((0 if tool.risk == "high" else tool.retries) + 1):
                fut = _pool.submit(tool.fn, args)
                try:
                    payload = fut.result(timeout=tool.timeout_s)
                    is_error = False
                    break
                except ToolError as e:  # expected failure: tell the model, don't retry
                    payload, is_error = {"error": str(e)}, True
                    break
                except FutureTimeout:
                    fut.cancel()  # only stops it if it hasn't started yet
                    payload, is_error = {"error": f"timed out after {tool.timeout_s}s"}, True
                except Exception as e:  # transient: retry, then report
                    payload, is_error = {"error": f"{type(e).__name__}: {e}"}, True
                sp.attrs["attempts"] = attempt + 1
            safe, n_pii = redact_obj(payload)
            sp.attrs["pii_redacted"] = n_pii
        args_safe, _ = redact_obj(args.model_dump(mode="json"))
        self.store.audit(
            "tool_executed",
            user_id,
            {
                "tool": call.name,
                "decision": decision,
                "args": args_safe,
                "error": is_error,
                "pii_redacted": n_pii,
            },
            run_id,
        )
        state["tool_calls"].append({"tool": call.name, "decision": decision, "error": is_error})
        return self._result(call, safe, is_error)

    # ------------------------------------------------------------------ budgets
    def _check_budget(self, user_id: str, state: dict) -> None:
        used = sum(state["usage"].values())
        if used >= self.settings.max_run_tokens:
            raise BudgetExceeded(f"run token budget reached ({used} tokens)")
        if self.store.usage_today(user_id) >= self.settings.daily_user_tokens:
            raise BudgetExceeded("daily token budget reached for this user")

    def _add_usage(self, user_id: str, state: dict, usage: dict[str, int]) -> None:
        for k, v in usage.items():
            state["usage"][k] = state["usage"].get(k, 0) + v
        if usage:
            self.store.add_usage(user_id, sum(usage.values()))
