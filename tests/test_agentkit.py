import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import BaseModel

from agentkit.agent import Agent
from agentkit.config import Settings
from agentkit.examples import support
from agentkit.examples.mock_planner import MockPlanner
from agentkit.policy import Policy
from agentkit.providers import ToolCall, Turn
from agentkit.redact import find, redact, redact_obj
from agentkit.store import Store
from agentkit.tools import Registry, ToolError

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def store():
    return Store()


@pytest.fixture
def agent(store):
    return Agent(
        support.registry,
        Policy.from_yaml(ROOT / "policies" / "default.yaml"),
        store,
        MockPlanner(),
        Settings(),
    )


# ---------------------------------------------------------------- redaction


def test_redacts_common_pii():
    text = (
        "Mail jane.doe@example.com or call +1 415-555-0134, card 4242 4242 4242 4242, "
        "SSN 123-45-6789"
    )
    red, findings = redact(text)
    assert {f.kind for f in findings} == {"EMAIL", "PHONE", "CARD", "SSN"}
    assert "example.com" not in red and "4242" not in red and "6789" not in red


def test_luhn_and_ssn_checks_avoid_false_positives():
    assert not find("order 1234 5678 9012 3456")  # fails Luhn
    assert not find("ref 000-12-3456")  # invalid SSN area


def test_redact_nested():
    obj, n = redact_obj({"a": ["x@y.io", {"b": "4242 4242 4242 4242"}], "c": 5})
    assert n == 2 and obj["c"] == 5 and obj["a"][0] == "[EMAIL]"


# ---------------------------------------------------------------- policy


def test_policy_default_deny_and_wildcards():
    p = Policy.from_dict(
        {"roles": {"a": {"x": "allow"}, "b": {"*": "allow", "y": "approve"}}, "approvers": ["b"]}
    )
    assert p.decide("a", "x") == "allow" and p.decide("a", "z") == "deny"
    assert p.decide("b", "z") == "allow" and p.decide("b", "y") == "approve"
    assert p.decide("unknown", "x") == "deny"
    assert p.can_approve("b") and not p.can_approve("a")


def test_policy_rejects_typos():
    with pytest.raises(ValueError):
        Policy.from_dict({"roles": {"a": {"x": "alow"}}})


# ---------------------------------------------------------------- audit chain


def test_audit_chain_detects_tampering(store):
    for i in range(3):
        store.audit("e", "u", {"i": i})
    assert store.verify() == {"ok": True, "records": 3, "broken_at": None}
    store.con.execute("UPDATE audit SET data = '{\"i\": 99}' WHERE seq = 2")
    assert store.verify()["broken_at"] == 2


def test_audit_chain_detects_deletion(store):
    for i in range(3):
        store.audit("e", "u", {"i": i})
    store.con.execute("DELETE FROM audit WHERE seq = 2")
    assert not store.verify()["ok"]


# ---------------------------------------------------------------- agent flows


def test_allowed_flow_and_pii_never_reaches_model(agent, store):
    res = agent.start("alice", "support_agent", "Look up Jane, email jane.doe@example.com")
    assert res.status == "completed" and "Jane Doe" in res.answer
    msgs = json.dumps(store.load_run(res.run_id)["state"]["messages"])
    assert "jane.doe@example.com" not in msgs and "4242 4242" not in msgs
    assert "[EMAIL]" in msgs and "[CARD]" in msgs


def test_approval_flow_with_separation_of_duties(agent, store):
    support.OUTBOX.clear()
    res = agent.start("alice", "support_agent", "Send Jane an email that it's done")
    assert res.status == "awaiting_approval" and not support.OUTBOX
    aid = res.pending_approvals[0]["id"]
    with pytest.raises(PermissionError, match="own request"):
        agent.decide(aid, "alice", "support_manager", True)
    with pytest.raises(PermissionError, match="cannot approve"):
        agent.decide(aid, "carol", "support_agent", True)
    done = agent.decide(aid, "bob", "support_manager", True, "fine")
    assert done.status == "completed" and support.OUTBOX[-1]["to"] == "jane.doe@example.com"
    with pytest.raises(ValueError, match="already"):
        agent.decide(aid, "bob", "support_manager", True)
    events = [e["event"] for e in store.audit_log(res.run_id)]
    assert "approval_requested" in events and "approval_decided" in events


def test_denied_approval_is_reported_to_model(agent):
    support.OUTBOX.clear()
    res = agent.start("alice", "support_agent", "Send Ravi an email")
    done = agent.decide(res.pending_approvals[0]["id"], "bob", "support_manager", False, "no")
    assert done.status == "completed" and "denied" in done.answer and not support.OUTBOX


def test_hidden_tools_cannot_run(agent):
    support.REFUNDS.clear()
    res = agent.start("alice", "support_agent", "Refund Maria for her last order")
    assert not support.REFUNDS and "refund_payment" in res.answer


class Rogue:
    """A model that calls a tool it was never offered."""

    name, model = "rogue", "rogue"

    def __init__(self):
        self.n = 0

    def complete(self, system, messages, tools):
        self.n += 1
        if self.n == 1:
            c = ToolCall(
                "t1", "refund_payment", {"order_id": "O-5001", "amount": 10, "reason": "x"}
            )
            return Turn(
                "",
                [c],
                "tool_use",
                [{"type": "tool_use", "id": "t1", "name": c.name, "input": c.input}],
            )
        return Turn("ok", [], "end_turn", [{"type": "text", "text": "ok"}])


def test_policy_enforced_even_if_model_calls_unoffered_tool(store):
    support.REFUNDS.clear()
    a = Agent(
        support.registry,
        Policy.from_yaml(ROOT / "policies" / "default.yaml"),
        store,
        Rogue(),
        Settings(),
    )
    res = a.start("alice", "support_agent", "anything")
    assert res.tool_calls == [{"tool": "refund_payment", "decision": "denied"}]
    assert not support.REFUNDS


def test_invalid_args_rejected_before_running(agent, store):
    class BadArgs(Rogue):
        def complete(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                c = ToolCall("t1", "create_ticket", {"customer_id": "not-an-id", "subject": "x"})
                return Turn(
                    "",
                    [c],
                    "tool_use",
                    [{"type": "tool_use", "id": "t1", "name": c.name, "input": c.input}],
                )
            return Turn("ok", [], "end_turn", [{"type": "text", "text": "ok"}])

    a = Agent(support.registry, agent.policy, store, BadArgs(), Settings())
    res = a.start("alice", "support_agent", "anything")
    assert res.status == "completed"
    assert any(e["event"] == "tool_invalid_args" for e in store.audit_log(res.run_id))


def test_budget_stops_runaway_loops(store):
    class Loop(Rogue):
        def complete(self, system, messages, tools):
            c = ToolCall(f"t{len(messages)}", "search_customers", {"query": "jane"})
            return Turn(
                "",
                [c],
                "tool_use",
                [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.input}],
                {"input_tokens": 5000},
            )

    a = Agent(
        support.registry,
        Policy.from_yaml(ROOT / "policies" / "default.yaml"),
        store,
        Loop(),
        Settings(max_run_tokens=12000),
    )
    res = a.start("alice", "support_agent", "loop")
    assert res.status == "denied_budget"


def test_tool_timeout_and_retry(store):
    reg = Registry()

    class Args(BaseModel):
        x: int = 1

    calls = {"n": 0}

    @reg.tool(description="flaky", timeout_s=0.2, retries=1)
    def flaky(args: Args) -> dict:
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("transient")
        return {"ok": True}

    @reg.tool(description="slow", timeout_s=0.1, retries=0)
    def slow(args: Args) -> dict:
        time.sleep(0.5)
        return {"ok": True}

    @reg.tool(description="expected failure")
    def nope(args: Args) -> dict:
        raise ToolError("not found")

    class Calls(Rogue):
        def complete(self, system, messages, tools):
            self.n += 1
            if self.n == 1:
                cs = [ToolCall(n, n, {}) for n in ("flaky", "slow", "nope")]
                return Turn(
                    "",
                    cs,
                    "tool_use",
                    [{"type": "tool_use", "id": c.id, "name": c.name, "input": {}} for c in cs],
                )
            return Turn("ok", [], "end_turn", [{"type": "text", "text": "ok"}])

    pol = Policy.from_dict({"roles": {"r": {"*": "allow"}}})
    res = Agent(reg, pol, store, Calls(), Settings()).start("u", "r", "go")
    errs = {c["tool"]: c["error"] for c in res.tool_calls}
    assert errs == {"flaky": False, "slow": True, "nope": True} and calls["n"] == 2
    assert any(s["name"] == "tool:slow" for s in res.trace)


# ---------------------------------------------------------------- API


def test_api_end_to_end(tmp_path, monkeypatch):
    monkeypatch.setenv("AGENTKIT_DB", str(tmp_path / "a.db"))
    monkeypatch.setenv("AGENTKIT_PROVIDER", "mock")
    monkeypatch.setenv("AGENTKIT_POLICY", str(ROOT / "policies" / "default.yaml"))
    from agentkit import app as wiring
    from agentkit.api import app

    wiring.agent.cache_clear()
    c = TestClient(app)
    alice = {"X-User-Id": "alice", "X-User-Role": "support_agent"}
    bob = {"X-User-Id": "bob", "X-User-Role": "support_manager"}
    r = c.post("/runs", json={"message": "Send Jane an email"}, headers=alice).json()
    assert r["status"] == "awaiting_approval"
    assert c.get("/approvals", headers=alice).status_code == 403
    pending = c.get("/approvals", headers=bob).json()
    assert (
        c.post(f"/approvals/{pending[0]['id']}", json={"approve": True}, headers=alice).status_code
        == 403
    )
    done = c.post(f"/approvals/{pending[0]['id']}", json={"approve": True}, headers=bob).json()
    assert done["status"] == "completed"
    assert c.get("/audit", headers=bob).json()["verify"]["ok"]
    assert c.get(f"/runs/{r['run_id']}", headers=alice).json()["status"] == "completed"
    assert (
        c.post(
            "/runs", json={"message": "hi"}, headers={"X-User-Id": "x", "X-User-Role": "hacker"}
        ).status_code
        == 403
    )
    assert "agentkit console" in c.get("/").text
    wiring.agent.cache_clear()


def test_eval_passes():
    import sys

    sys.path.insert(0, str(ROOT))
    from evals.run_eval import run

    s = run("mock")["summary"]
    assert s["pass_rate"] == 1.0 and s["audit_chain_ok"]
