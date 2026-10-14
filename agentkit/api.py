"""REST API + operator console. Run: uvicorn agentkit.api:app --reload

Identity comes from `X-User-Id` / `X-User-Role` headers in this demo. In production, replace
`identity()` with your SSO / OIDC dependency; nothing else changes.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Any

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from agentkit import __version__
from agentkit.app import agent

UI = Path(__file__).resolve().parents[1] / "ui" / "index.html"

app = FastAPI(
    title="agentkit-enterprise",
    version=__version__,
    description="Governed AI agent: policies, approvals, redaction, audit.",
)


class Identity(BaseModel):
    user_id: str
    role: str


def identity(x_user_id: str = Header(...), x_user_role: str = Header(...)) -> Identity:
    return Identity(user_id=x_user_id, role=x_user_role)


Who = Annotated[Identity, Depends(identity)]


class RunRequest(BaseModel):
    message: str = Field(min_length=2, max_length=4000)


class Decision(BaseModel):
    approve: bool
    note: str | None = None


@app.get("/health")
def health() -> dict[str, Any]:
    a = agent()
    return {
        "status": "ok",
        "provider": a.provider.name,
        "tools": list(a.registry.tools),
    }


@app.post("/runs")
def start_run(req: RunRequest, who: Who) -> dict[str, Any]:
    if who.role not in agent().policy.roles:
        raise HTTPException(403, f"unknown role {who.role}")
    return agent().start(who.user_id, who.role, req.message).to_dict()


@app.get("/runs/{run_id}")
def get_run(run_id: str, who: Who) -> dict[str, Any]:
    try:
        run = agent().store.load_run(run_id)
    except KeyError as e:
        raise HTTPException(404, "run not found") from e
    if run["user_id"] != who.user_id and not agent().policy.can_approve(who.role):
        raise HTTPException(403, "not your run")
    state = run["state"]
    return {
        "id": run_id,
        "status": run["status"],
        "user_id": run["user_id"],
        "tool_calls": state["tool_calls"],
        "usage": state["usage"],
        "trace": state["trace"],
    }


@app.get("/approvals")
def approvals(who: Who) -> list[dict[str, Any]]:
    if not agent().policy.can_approve(who.role):
        raise HTTPException(403, "only approvers can list approvals")
    return agent().store.approvals("pending")


@app.post("/approvals/{approval_id}")
def decide(approval_id: str, d: Decision, who: Who) -> dict[str, Any]:
    try:
        return agent().decide(approval_id, who.user_id, who.role, d.approve, d.note).to_dict()
    except KeyError as e:
        raise HTTPException(404, "approval not found") from e
    except PermissionError as e:
        raise HTTPException(403, str(e)) from e
    except ValueError as e:
        raise HTTPException(409, str(e)) from e


@app.get("/audit")
def audit(
    who: Who, run_id: str | None = None, limit: int = Query(200, ge=1, le=5000)
) -> dict[str, Any]:
    if not agent().policy.can_approve(who.role):
        raise HTTPException(403, "audit log is restricted")
    return {"verify": agent().store.verify(), "events": agent().store.audit_log(run_id, limit)}


@app.get("/", response_class=HTMLResponse)
def console() -> str:
    return UI.read_text()
