"""Demo tools for a customer-support desk, backed by an in-memory SQLite CRM.

Tools take customer ids, not raw contact details: the model only ever sees masked PII, and
tools resolve addresses themselves. That pattern keeps personal data out of prompts.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any

from pydantic import BaseModel, Field

from agentkit.tools import Registry, ToolError

registry = Registry()
_lock = threading.Lock()
OUTBOX: list[dict[str, Any]] = []  # sent emails (simulated)
REFUNDS: list[dict[str, Any]] = []


def _db() -> sqlite3.Connection:
    con = sqlite3.connect(":memory:", check_same_thread=False)
    con.row_factory = sqlite3.Row
    con.executescript("""
        CREATE TABLE customers (id TEXT PRIMARY KEY, name TEXT, email TEXT, phone TEXT,
                                plan TEXT, card TEXT);
        CREATE TABLE orders (id TEXT PRIMARY KEY, customer_id TEXT, amount REAL, status TEXT,
                             item TEXT);
        CREATE TABLE tickets (id INTEGER PRIMARY KEY AUTOINCREMENT, customer_id TEXT,
                              subject TEXT, priority TEXT);
        INSERT INTO customers VALUES
          ('C-1001', 'Jane Doe', 'jane.doe@example.com', '+1 415-555-0134', 'Enterprise',
           '4242 4242 4242 4242'),
          ('C-1002', 'Ravi Kumar', 'ravi.k@example.org', '+1 212-555-0199', 'Pro',
           '5555 5555 5555 4444'),
          ('C-1003', 'Maria Garcia', 'maria.g@example.net', '+1 305-555-0172', 'Starter',
           '4000 0566 5566 5556');
        INSERT INTO orders VALUES
          ('O-5001', 'C-1001', 1299.00, 'delivered', 'Annual license'),
          ('O-5002', 'C-1002', 89.00, 'delivered', 'Add-on seats'),
          ('O-5003', 'C-1003', 49.00, 'refund_requested', 'Monthly plan');
    """)
    return con


DB = _db()


class SearchCustomers(BaseModel):
    query: str = Field(description="name, email or customer id", min_length=2)


class CustomerId(BaseModel):
    customer_id: str = Field(pattern=r"^C-\d{4}$")


class CreateTicket(BaseModel):
    customer_id: str = Field(pattern=r"^C-\d{4}$")
    subject: str = Field(min_length=3, max_length=200)
    priority: str = Field(default="normal", pattern="^(low|normal|high|urgent)$")


class SendEmail(BaseModel):
    customer_id: str = Field(pattern=r"^C-\d{4}$")
    subject: str = Field(min_length=3, max_length=150)
    body: str = Field(min_length=10, max_length=4000)


class Refund(BaseModel):
    order_id: str = Field(pattern=r"^O-\d{4}$")
    amount: float = Field(gt=0, le=5000)
    reason: str = Field(min_length=3)


@registry.tool(
    description="Find customers by name, email or id. Returns id, name, plan and "
    "masked contact details."
)
def search_customers(args: SearchCustomers) -> dict[str, Any]:
    q = f"%{args.query.lower()}%"
    rows = DB.execute(
        "SELECT id, name, email, phone, plan, card FROM customers WHERE lower(name) LIKE ? "
        "OR lower(email) LIKE ? OR lower(id) LIKE ?",
        (q, q, q),
    ).fetchall()
    return {"customers": [dict(r) for r in rows]}


@registry.tool(description="List a customer's orders with amount and status.")
def get_orders(args: CustomerId) -> dict[str, Any]:
    rows = DB.execute("SELECT * FROM orders WHERE customer_id = ?", (args.customer_id,))
    return {"orders": [dict(r) for r in rows]}


@registry.tool(description="Open a support ticket for a customer.")
def create_ticket(args: CreateTicket) -> dict[str, Any]:
    with _lock:
        cur = DB.execute(
            "INSERT INTO tickets (customer_id, subject, priority) VALUES (?,?,?)",
            (args.customer_id, args.subject, args.priority),
        )
    return {"ticket_id": cur.lastrowid, "status": "open"}


@registry.tool(
    description="Send an email to a customer (address resolved from the CRM).", risk="high"
)
def send_email(args: SendEmail) -> dict[str, Any]:
    row = DB.execute("SELECT email FROM customers WHERE id = ?", (args.customer_id,)).fetchone()
    if row is None:
        raise ToolError(f"no customer {args.customer_id}")
    OUTBOX.append({"to": row["email"], "subject": args.subject, "body": args.body})
    return {"sent": True, "to": row["email"]}


@registry.tool(
    description="Refund all or part of an order to the original payment method.", risk="high"
)
def refund_payment(args: Refund) -> dict[str, Any]:
    row = DB.execute("SELECT amount FROM orders WHERE id = ?", (args.order_id,)).fetchone()
    if row is None:
        raise ToolError(f"no order {args.order_id}")
    if args.amount > row["amount"]:
        raise ToolError(f"refund {args.amount:.2f} exceeds order total {row['amount']:.2f}")
    REFUNDS.append(args.model_dump())
    return {"refunded": args.amount, "order_id": args.order_id}
