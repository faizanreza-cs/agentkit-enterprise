"""agentkit CLI: run | approvals | approve | deny | audit"""

from __future__ import annotations

import argparse
import json
import sys


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="agentkit")
    p.add_argument("--user", default="cli-user")
    p.add_argument("--role", default="support_agent")
    sub = p.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("run")
    r.add_argument("message")
    sub.add_parser("approvals")
    for name in ("approve", "deny"):
        d = sub.add_parser(name)
        d.add_argument("approval_id")
        d.add_argument("--note")
    sub.add_parser("audit")
    a = p.parse_args(argv)

    from agentkit.app import agent

    ag = agent()
    if a.cmd == "run":
        res = ag.start(a.user, a.role, a.message)
        print(f"[{res.status}] {res.answer}")
        for ap in res.pending_approvals:
            print(f"  awaiting approval {ap['id']}: {ap['tool']} {json.dumps(ap['args'])}")
    elif a.cmd == "approvals":
        for ap in ag.store.approvals("pending"):
            print(f"{ap['id']}  {ap['tool']:<16} by {ap['requested_by']}  {json.dumps(ap['args'])}")
    elif a.cmd in {"approve", "deny"}:
        try:
            res = ag.decide(a.approval_id, a.user, a.role, a.cmd == "approve", a.note)
        except (PermissionError, ValueError, KeyError) as e:
            print(f"error: {e}", file=sys.stderr)
            return 1
        print(f"[{res.status}] {res.answer}")
    else:
        print(json.dumps(ag.store.verify()))
        for ev in ag.store.audit_log()[-20:]:
            print(
                f"{ev['seq']:>4} {ev['ts']} {ev['actor']:<10} {ev['event']:<20} "
                f"{json.dumps(ev['data'])[:80]}"
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
