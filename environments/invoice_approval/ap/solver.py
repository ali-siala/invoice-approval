"""Reference solver (SPEC sections 2 and 7): the gold action list for one task.

solve(db, invoice_ids, approver_id) applies the Kreditorenrichtlinie to exactly the invoices in
scope, in the required order, as the given approver. It works on a private copy of the database
(approvals change PO budgets, so each decision must see the effect of the previous ones) and
leaves `db` untouched. It never reads the seed's hidden columns.

An action is {"tool": <write tool name>, "args": {...}}; apply_action() replays one through the
agent tools exactly as a model's tool call would.
"""

import sqlite3
import tempfile
from pathlib import Path

from . import money, schema, tools

_WRITE_TOOLS = {t.__name__: t for t in tools.WRITE_TOOLS}


def apply_action(db: str, action: dict) -> str:
    """Run one gold action through the agent tools; returns the tool's result line."""
    return _WRITE_TOOLS[action["tool"]](**action["args"], db=db)


def _normalize(number: str) -> str:
    return "".join(ch for ch in number.upper() if ch.isalnum())


def _action(tool: str, invoice_id: str, **args) -> dict:
    return {"tool": tool, "args": {"invoice_id": invoice_id, **args}}


def _decide(conn: sqlite3.Connection, invoice_id: str, approver_id: str) -> dict:
    inv = conn.execute(
        "SELECT i.*, s.is_blocked FROM invoices i JOIN suppliers s ON s.id = i.supplier_id WHERE i.id = ?",
        (invoice_id,),
    ).fetchone()

    # 1. Blocked supplier
    if inv["is_blocked"]:
        return _action("hold_invoice", invoice_id, reason_code="SUPPLIER_BLOCKED")

    # 2. Duplicate: an earlier (received_date, id) non-rejected invoice of the supplier with the same number
    number = _normalize(inv["supplier_invoice_number"])
    earlier = conn.execute(
        "SELECT supplier_invoice_number FROM invoices WHERE supplier_id = ? AND status != 'rejected' "
        "AND (received_date < ? OR (received_date = ? AND id < ?))",
        (inv["supplier_id"], inv["received_date"], inv["received_date"], invoice_id),
    ).fetchall()
    if any(_normalize(r["supplier_invoice_number"]) == number for r in earlier):
        return _action("reject_invoice", invoice_id, reason_code="DUPLICATE")

    # 3. Purchase order (rules 2 and 3) and 4. PO amount
    if inv["po_number"]:
        po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (inv["po_number"],)).fetchone()
        if po is None or po["supplier_id"] != inv["supplier_id"] or po["status"] != "open":
            return _action("reject_invoice", invoice_id, reason_code="INVALID_PO")
        if not money.within_po_tolerance(po["net_cents"], po["net_invoiced_cents"], inv["net_cents"]):
            return _action("hold_invoice", invoice_id, reason_code="PO_EXCEEDED")
    elif not money.is_kleinbetrag(inv["gross_cents"]):
        return _action("reject_invoice", invoice_id, reason_code="MISSING_PO")

    # 5. VAT
    rate = money.expected_vat_rate_pct(inv["category"])
    vat_ok = inv["vat_rate_pct"] == rate and inv["vat_cents"] == money.expected_vat_cents(inv["net_cents"], rate)
    if not vat_ok or inv["gross_cents"] != money.expected_gross_cents(inv["net_cents"], inv["vat_cents"]):
        return _action("reject_invoice", invoice_id, reason_code="VAT_ERROR")

    # 6. Approval limit
    approvers = conn.execute("SELECT id, approval_limit_gross_cents AS lim FROM approvers").fetchall()
    own = next(a["lim"] for a in approvers if a["id"] == approver_id)
    if own is not None and inv["gross_cents"] > own:
        able = [a for a in approvers if a["lim"] is None or a["lim"] >= inv["gross_cents"]]
        target = min(able, key=lambda a: (a["lim"] is None, a["lim"] or 0, a["id"]))
        return _action("escalate_invoice", invoice_id, approver_id=target["id"])

    # 7. Approve
    return _action("approve_invoice", invoice_id)


def _copy(db: str, dest: Path) -> None:
    src = schema.connect(db)
    dst = sqlite3.connect(dest)
    try:
        src.backup(dst)
    finally:
        src.close()
        dst.close()


def solve(db: str, invoice_ids: list[str], approver_id: str) -> list[dict]:
    """Return the gold actions for processing `invoice_ids` as `approver_id`, in policy order.

    Raises ValueError if an ID is unknown or not in status 'received', or the approver is unknown.
    """
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp) / "solve.sqlite"
        _copy(db, work)
        schema.set_acting_approver(str(work), approver_id)
        conn = schema.connect(str(work))
        try:
            rows = {r["id"]: r for r in conn.execute("SELECT id, received_date, status FROM invoices")}
            scope = set(invoice_ids)
            unknown = sorted(scope - rows.keys())
            if unknown:
                raise ValueError(f"unknown invoice IDs: {unknown}")
            decided = sorted(i for i in scope if rows[i]["status"] != "received")
            if decided:
                raise ValueError(f"invoices not in status 'received': {decided}")
            # Rule 0: received_date, then internal ID
            order = sorted(scope, key=lambda i: (rows[i]["received_date"], i))

            actions = []
            for invoice_id in order:
                action = _decide(conn, invoice_id, approver_id)
                result = apply_action(str(work), action)
                if result.startswith("Error"):
                    raise RuntimeError(f"gold action {action} failed: {result}")
                actions.append(action)
        finally:
            conn.close()
    return actions
