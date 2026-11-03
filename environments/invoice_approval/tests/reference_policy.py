"""Reference implementation of the Kreditorenrichtlinie (SPEC section 4), for tests only.

Written independently of ap/seed.py: it never imports the generator and decides every invoice
from the database state alone. It shares only ap.money (the spec's single money helper) and
uses ap.tools to apply its decisions, like an agent would.
"""

import sqlite3
from dataclasses import dataclass

from ap import money, tools


@dataclass(frozen=True)
class Decision:
    status: str
    reason_code: str | None = None
    assigned_to: str | None = None

    def as_dict(self) -> dict:
        return {"status": self.status, "reason_code": self.reason_code, "assigned_to": self.assigned_to}


def normalize_number(number: str) -> str:
    return "".join(ch for ch in number.upper() if ch.isalnum())


def _conn(db: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    return conn


def _escalation_target(conn: sqlite3.Connection, gross: int) -> str:
    candidates = [
        (r["approval_limit_gross_cents"] if r["approval_limit_gross_cents"] is not None else float("inf"), r["id"])
        for r in conn.execute("SELECT id, approval_limit_gross_cents FROM approvers")
        if r["approval_limit_gross_cents"] is None or r["approval_limit_gross_cents"] >= gross
    ]
    return min(candidates)[1]


def decide(db: str, invoice_id: str, acting_approver_id: str | None = None) -> Decision:
    """Decide one invoice from the current database state (check order of rule 8)."""
    conn = _conn(db)
    try:
        inv = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
        supplier = conn.execute("SELECT * FROM suppliers WHERE id = ?", (inv["supplier_id"],)).fetchone()

        # Rule 6: blocked supplier
        if supplier["is_blocked"]:
            return Decision("on_hold", "SUPPLIER_BLOCKED")

        # Rule 1: duplicate (non-rejected invoice of the same supplier with the same normalized number, earlier)
        mine = normalize_number(inv["supplier_invoice_number"])
        for other in conn.execute(
            "SELECT id, received_date, supplier_invoice_number, status FROM invoices WHERE supplier_id = ? AND id != ?",
            (inv["supplier_id"], invoice_id),
        ):
            if (
                other["status"] != "rejected"
                and normalize_number(other["supplier_invoice_number"]) == mine
                and (other["received_date"], other["id"]) < (inv["received_date"], inv["id"])
            ):
                return Decision("rejected", "DUPLICATE")

        # Rules 2-4: purchase order (a Kleinbetrag may omit it; if given, it is checked in full)
        if not inv["po_number"]:
            if not money.is_kleinbetrag(inv["gross_cents"]):
                return Decision("rejected", "MISSING_PO")
        else:
            po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (inv["po_number"],)).fetchone()
            if po is None or po["supplier_id"] != inv["supplier_id"] or po["status"] != "open":
                return Decision("rejected", "INVALID_PO")
            if not money.within_po_tolerance(po["net_cents"], po["net_invoiced_cents"], inv["net_cents"]):
                return Decision("on_hold", "PO_EXCEEDED")

        # Rule 5: VAT
        rate = money.expected_vat_rate_pct(inv["category"])
        if (
            inv["vat_rate_pct"] != rate
            or inv["vat_cents"] != money.expected_vat_cents(inv["net_cents"], rate)
            or inv["gross_cents"] != money.expected_gross_cents(inv["net_cents"], inv["vat_cents"])
        ):
            return Decision("rejected", "VAT_ERROR")

        # Rule 7: approval limit
        if acting_approver_id is None:
            acting_approver_id = conn.execute(
                "SELECT value FROM app_context WHERE key = 'acting_approver_id'"
            ).fetchone()["value"]
        own_limit = conn.execute(
            "SELECT approval_limit_gross_cents FROM approvers WHERE id = ?", (acting_approver_id,)
        ).fetchone()["approval_limit_gross_cents"]
        if own_limit is not None and inv["gross_cents"] > own_limit:
            return Decision("escalated", "APPROVAL_LIMIT", _escalation_target(conn, inv["gross_cents"]))

        return Decision("approved")
    finally:
        conn.close()


def apply(db: str, invoice_id: str, decision: Decision) -> str:
    """Carry out a decision through the agent tools; returns the tool's result line."""
    if decision.status == "approved":
        return tools.approve_invoice(invoice_id, db=db)
    if decision.status == "rejected":
        return tools.reject_invoice(invoice_id, decision.reason_code, db=db)
    if decision.status == "on_hold":
        return tools.hold_invoice(invoice_id, decision.reason_code, db=db)
    return tools.escalate_invoice(invoice_id, decision.assigned_to, db=db)


def processing_order(db: str, invoice_ids: list[str] | None = None) -> list[str]:
    """Rule 0: received_date, then internal ID. Default: every invoice in status 'received'."""
    conn = _conn(db)
    try:
        rows = conn.execute("SELECT id, received_date FROM invoices WHERE status = 'received'").fetchall()
    finally:
        conn.close()
    if invoice_ids is not None:
        rows = [r for r in rows if r["id"] in set(invoice_ids)]
    return [r["id"] for r in sorted(rows, key=lambda r: (r["received_date"], r["id"]))]


def process(db: str, invoice_ids: list[str] | None = None, acting_approver_id: str | None = None) -> dict:
    """Process invoices in policy order, applying each decision before deciding the next."""
    decisions = {}
    for invoice_id in processing_order(db, invoice_ids):
        decision = decide(db, invoice_id, acting_approver_id)
        result = apply(db, invoice_id, decision)
        assert not result.startswith("Error"), f"{invoice_id}: {result}"
        decisions[invoice_id] = decision
    return decisions
