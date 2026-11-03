"""Agent-facing tools (SPEC section 5).

Every tool takes a hidden `db` argument (the per-rollout SQLite path, injected by the environment
via `args_to_skip=["db"]`). Tools return JSON or a one-line result, show amounts as decimal
strings, and return "Error: ..." strings instead of raising. Tools enforce legal status
transitions only; they never apply the approval policy (that is the agent's job).
"""

import functools
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from . import ledger, money, schema

# Only invoices in status 'received' can be processed; every other status is final in v1.
PROCESSABLE_STATUS = "received"
MAX_LIST_LIMIT = 200
MAX_SEARCH_RESULTS = 20
MAX_COMMENT_LENGTH = 500


class ToolError(Exception):
    """An expected, user-facing error; its message is returned to the agent."""


def _tool(func):
    """Turn any exception into an 'Error: ...' string so the agent never sees a traceback."""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        try:
            return func(*args, **kwargs)
        except ToolError as e:
            return f"Error: {e}"
        except FileNotFoundError:
            return "Error: no database is available for this session."
        except sqlite3.Error as e:
            return f"Error: database error ({e})."
        except Exception as e:  # noqa: BLE001 - tools must never raise
            return f"Error: unexpected {type(e).__name__}: {e}"

    return wrapper


@contextmanager
def _read(db: str):
    conn = schema.connect(db)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write(db: str):
    """One atomic transaction: everything inside commits together or not at all."""
    conn = schema.connect(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")
    finally:
        conn.close()


def _dumps(obj) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _norm_id(value: str) -> str:
    return (value or "").strip().upper()


def _invoice_row(conn: sqlite3.Connection, invoice_id: str) -> sqlite3.Row:
    cols = ", ".join(f"i.{c}" for c in schema.PUBLIC_INVOICE_COLUMNS)
    row = conn.execute(
        f"SELECT {cols}, s.name AS supplier_name FROM invoices i "
        "JOIN suppliers s ON s.id = i.supplier_id WHERE i.id = ?",
        (invoice_id,),
    ).fetchone()
    if row is None:
        raise ToolError(f"invoice {invoice_id!r} not found. Invoice IDs look like ER-2026-00417.")
    return row


def _invoice_view(row: sqlite3.Row) -> dict:
    return {
        "id": row["id"],
        "supplier_id": row["supplier_id"],
        "supplier_name": row["supplier_name"],
        "supplier_invoice_number": row["supplier_invoice_number"],
        "invoice_date": row["invoice_date"],
        "received_date": row["received_date"],
        "due_date": row["due_date"],
        "po_number": row["po_number"],
        "cost_center": row["cost_center"],
        "category": row["category"],
        "net_amount": money.fmt_cents(row["net_cents"]),
        "vat_rate_pct": row["vat_rate_pct"],
        "vat_amount": money.fmt_cents(row["vat_cents"]),
        "gross_amount": money.fmt_cents(row["gross_cents"]),
        "currency": row["currency"],
        "status": row["status"],
        "reason_code": row["reason_code"],
        "assigned_to": row["assigned_to"],
        "comment": row["comment"],
    }


def _limit_view(limit_cents: int | None) -> str:
    return "unlimited" if limit_cents is None else money.fmt_cents(limit_cents)


def _approver_view(row: sqlite3.Row) -> dict:
    return {
        "approver_id": row["id"],
        "name": row["name"],
        "role": row["role"],
        "approval_limit_gross": _limit_view(row["approval_limit_gross_cents"]),
    }


def _actor(conn: sqlite3.Connection) -> str:
    return schema.get_acting_approver_id(conn) or "system"


def _audit(conn: sqlite3.Connection, action: str, invoice_id: str, details: dict) -> None:
    conn.execute(
        "INSERT INTO audit_log (timestamp, actor, action, invoice_id, details) VALUES (?, ?, ?, ?, ?)",
        (
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
            _actor(conn),
            action,
            invoice_id,
            _dumps(details),
        ),
    )


def _clean_comment(comment: str) -> str | None:
    comment = (comment or "").strip()
    if len(comment) > MAX_COMMENT_LENGTH:
        raise ToolError(f"comment is too long ({len(comment)} characters, max {MAX_COMMENT_LENGTH}).")
    return comment or None


def _require_processable(conn: sqlite3.Connection, invoice_id: str, verb: str) -> sqlite3.Row:
    row = _invoice_row(conn, invoice_id)
    if row["status"] != PROCESSABLE_STATUS:
        raise ToolError(
            f"cannot {verb} {invoice_id}: its status is '{row['status']}'. "
            f"Only invoices in status '{PROCESSABLE_STATUS}' can be processed."
        )
    return row


def _set_status(
    conn: sqlite3.Connection,
    invoice_id: str,
    status: str,
    reason_code: str | None,
    assigned_to: str | None,
    comment: str | None,
) -> None:
    cur = conn.execute(
        "UPDATE invoices SET status = ?, reason_code = ?, assigned_to = ?, comment = ? "
        "WHERE id = ? AND status = ?",
        (status, reason_code, assigned_to, comment, invoice_id, PROCESSABLE_STATUS),
    )
    if cur.rowcount != 1:  # guarded update; can only fail if the status changed underneath us
        raise ToolError(f"{invoice_id} is no longer in status '{PROCESSABLE_STATUS}'.")


# ------------------------------------------------------------------ read tools


@_tool
def list_invoices(status: str, supplier_id: str = "", limit: int = 50, db: str = "") -> str:
    """List invoices with a given status, oldest first (sorted by received_date, then invoice ID).

    Args:
        status: One of received, approved, rejected, on_hold, escalated.
        supplier_id: Optional supplier ID (e.g. K-10023) to only list that supplier's invoices.
        limit: Maximum number of invoices to return (1-200, default 50).
    """
    status = (status or "").strip().lower()
    if status not in schema.INVOICE_STATUSES:
        raise ToolError(f"unknown status {status!r}. Use one of: {', '.join(schema.INVOICE_STATUSES)}.")
    if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= MAX_LIST_LIMIT:
        raise ToolError(f"limit must be a whole number between 1 and {MAX_LIST_LIMIT}.")
    where, params = "i.status = ?", [status]
    supplier_id = _norm_id(supplier_id)
    with _read(db) as conn:
        if supplier_id:
            if conn.execute("SELECT 1 FROM suppliers WHERE id = ?", (supplier_id,)).fetchone() is None:
                raise ToolError(f"supplier {supplier_id!r} not found.")
            where += " AND i.supplier_id = ?"
            params.append(supplier_id)
        total = conn.execute(f"SELECT COUNT(*) FROM invoices i WHERE {where}", params).fetchone()[0]
        rows = conn.execute(
            "SELECT i.id, i.supplier_id, s.name AS supplier_name, i.supplier_invoice_number, "
            "i.received_date, i.po_number, i.gross_cents, i.status FROM invoices i "
            f"JOIN suppliers s ON s.id = i.supplier_id WHERE {where} "
            "ORDER BY i.received_date, i.id LIMIT ?",
            [*params, limit],
        ).fetchall()
    invoices = [
        {
            "id": r["id"],
            "supplier_id": r["supplier_id"],
            "supplier_name": r["supplier_name"],
            "supplier_invoice_number": r["supplier_invoice_number"],
            "received_date": r["received_date"],
            "po_number": r["po_number"],
            "gross_amount": money.fmt_cents(r["gross_cents"]),
            "status": r["status"],
        }
        for r in rows
    ]
    return _dumps({"total_matching": total, "returned": len(invoices), "invoices": invoices})


@_tool
def get_invoice(invoice_id: str, db: str = "") -> str:
    """Show all details of one invoice.

    Args:
        invoice_id: Internal invoice ID, e.g. ER-2026-00417.
    """
    with _read(db) as conn:
        return _dumps(_invoice_view(_invoice_row(conn, _norm_id(invoice_id))))


@_tool
def search_suppliers(query: str, db: str = "") -> str:
    """Search suppliers by name, city, supplier ID or USt-IdNr (case-insensitive, partial match).

    Args:
        query: Text to search for, e.g. "Huber" or "München" or "DE1234".
    """
    needle = (query or "").strip().casefold()
    if not needle:
        raise ToolError("query must not be empty.")
    with _read(db) as conn:
        rows = conn.execute("SELECT id, name, zip, city, country, vat_id FROM suppliers ORDER BY id").fetchall()
    hits = [
        dict(r)
        for r in rows
        if any(needle in (r[k] or "").casefold() for k in ("id", "name", "city", "vat_id"))
    ]
    if not hits:
        return f"No suppliers match {query.strip()!r}."
    return _dumps({"total_matching": len(hits), "suppliers": hits[:MAX_SEARCH_RESULTS]})


@_tool
def get_supplier(supplier_id: str, db: str = "") -> str:
    """Show supplier master data (address, USt-IdNr, IBAN, payment terms, block status).

    Args:
        supplier_id: Supplier ID, e.g. K-10023.
    """
    supplier_id = _norm_id(supplier_id)
    with _read(db) as conn:
        row = conn.execute("SELECT * FROM suppliers WHERE id = ?", (supplier_id,)).fetchone()
    if row is None:
        raise ToolError(f"supplier {supplier_id!r} not found. Supplier IDs look like K-10023.")
    data = dict(row)
    data["is_blocked"] = bool(data["is_blocked"])
    return _dumps(data)


@_tool
def get_purchase_order(po_number: str, db: str = "") -> str:
    """Show a purchase order (Bestellung): supplier, order date, cost center, ordered net amount, net amount already invoiced, status.

    Args:
        po_number: 10-digit PO number, e.g. 4500012345.
    """
    po_number = (po_number or "").strip()
    with _read(db) as conn:
        row = conn.execute(
            "SELECT p.*, s.name AS supplier_name FROM purchase_orders p "
            "JOIN suppliers s ON s.id = p.supplier_id WHERE p.po_number = ?",
            (po_number,),
        ).fetchone()
    if row is None:
        raise ToolError(f"purchase order {po_number!r} not found.")
    return _dumps(
        {
            "po_number": row["po_number"],
            "supplier_id": row["supplier_id"],
            "supplier_name": row["supplier_name"],
            "order_date": row["order_date"],
            "cost_center": row["cost_center"],
            "description": row["description"],
            "net_amount": money.fmt_cents(row["net_cents"]),
            "net_invoiced_amount": money.fmt_cents(row["net_invoiced_cents"]),
            "currency": "EUR",
            "status": row["status"],
        }
    )


@_tool
def list_approvers(db: str = "") -> str:
    """List all approvers with their role and gross approval limit."""
    with _read(db) as conn:
        rows = conn.execute("SELECT * FROM approvers ORDER BY id").fetchall()
    return _dumps([_approver_view(r) for r in rows])


@_tool
def whoami(db: str = "") -> str:
    """Show which approver you are acting as, including your own gross approval limit."""
    with _read(db) as conn:
        approver_id = schema.get_acting_approver_id(conn)
        row = conn.execute("SELECT * FROM approvers WHERE id = ?", (approver_id,)).fetchone()
    if row is None:
        raise ToolError("no acting approver is configured for this session.")
    return _dumps(_approver_view(row))


# ------------------------------------------------------------------ write tools


@_tool
def approve_invoice(invoice_id: str, db: str = "") -> str:
    """Approve an invoice for payment. This also adds its net amount to the PO's invoiced total and posts the ledger entries.

    Args:
        invoice_id: Internal invoice ID, e.g. ER-2026-00417.
    """
    invoice_id = _norm_id(invoice_id)
    with _write(db) as conn:
        inv = _require_processable(conn, invoice_id, "approve")
        _set_status(conn, invoice_id, "approved", None, None, None)

        po_note, po_details = "", None
        if inv["po_number"]:
            cur = conn.execute(
                "UPDATE purchase_orders SET net_invoiced_cents = net_invoiced_cents + ? WHERE po_number = ?",
                (inv["net_cents"], inv["po_number"]),
            )
            if cur.rowcount:
                po = conn.execute(
                    "SELECT net_cents, net_invoiced_cents FROM purchase_orders WHERE po_number = ?",
                    (inv["po_number"],),
                ).fetchone()
                po_note = (
                    f" PO {inv['po_number']} invoiced net now {money.fmt_cents(po['net_invoiced_cents'])}"
                    f" of {money.fmt_cents(po['net_cents'])}."
                )
                po_details = {"po_number": inv["po_number"], "added_net_cents": inv["net_cents"]}

        text = f"{inv['supplier_name']} RE {inv['supplier_invoice_number']}"
        lines = ledger.ledger_lines(
            inv["category"], inv["vat_rate_pct"], inv["net_cents"], inv["vat_cents"], inv["gross_cents"], text
        )
        conn.executemany(
            "INSERT INTO ledger_entries (invoice_id, account, debit_cents, credit_cents, text) VALUES (?, ?, ?, ?, ?)",
            [(invoice_id, *line) for line in lines],
        )
        _audit(
            conn,
            "approve_invoice",
            invoice_id,
            {"from_status": inv["status"], "to_status": "approved", "po": po_details,
             "ledger": [[a, d, c] for a, d, c, _ in lines]},
        )

    posted = ", ".join(
        f"{a} {'credit' if c else 'debit'} {money.fmt_cents(c or d)}" for a, d, c, _ in lines
    )
    return f"Invoice {invoice_id} approved. Posted 3 ledger lines: {posted}.{po_note}"


def _reject_or_hold(db, invoice_id, reason_code, comment, *, status, codes, action, verb, done):
    invoice_id = _norm_id(invoice_id)
    reason_code = (reason_code or "").strip().upper()
    if reason_code not in codes:
        raise ToolError(f"invalid reason_code {reason_code!r} for {action}. Use one of: {', '.join(codes)}.")
    comment = _clean_comment(comment)
    with _write(db) as conn:
        inv = _require_processable(conn, invoice_id, verb)
        _set_status(conn, invoice_id, status, reason_code, None, comment)
        _audit(
            conn,
            action,
            invoice_id,
            {"from_status": inv["status"], "to_status": status, "reason_code": reason_code, "comment": comment},
        )
    return f"Invoice {invoice_id} {done} ({reason_code})."


@_tool
def reject_invoice(invoice_id: str, reason_code: str, comment: str = "", db: str = "") -> str:
    """Reject an invoice (it goes back to the supplier).

    Args:
        invoice_id: Internal invoice ID, e.g. ER-2026-00417.
        reason_code: One of DUPLICATE, MISSING_PO, INVALID_PO, VAT_ERROR.
        comment: Optional short note for the supplier or colleagues.
    """
    return _reject_or_hold(
        db, invoice_id, reason_code, comment, status="rejected", codes=schema.REJECT_CODES,
        action="reject_invoice", verb="reject", done="rejected",
    )


@_tool
def hold_invoice(invoice_id: str, reason_code: str, comment: str = "", db: str = "") -> str:
    """Put an invoice on hold (parked for clarification, neither approved nor rejected).

    Args:
        invoice_id: Internal invoice ID, e.g. ER-2026-00417.
        reason_code: One of PO_EXCEEDED, SUPPLIER_BLOCKED.
        comment: Optional short note for colleagues.
    """
    return _reject_or_hold(
        db, invoice_id, reason_code, comment, status="on_hold", codes=schema.HOLD_CODES,
        action="hold_invoice", verb="put on hold", done="put on hold",
    )


@_tool
def escalate_invoice(invoice_id: str, approver_id: str, comment: str = "", db: str = "") -> str:
    """Escalate an invoice to another approver because it exceeds your approval limit.

    Args:
        invoice_id: Internal invoice ID, e.g. ER-2026-00417.
        approver_id: ID of the approver to escalate to (see list_approvers).
        comment: Optional short note for the approver.
    """
    invoice_id = _norm_id(invoice_id)
    approver_id = _norm_id(approver_id)
    comment = _clean_comment(comment)
    with _write(db) as conn:
        approver = conn.execute("SELECT * FROM approvers WHERE id = ?", (approver_id,)).fetchone()
        if approver is None:
            raise ToolError(f"approver {approver_id!r} not found. Use list_approvers to see valid IDs.")
        inv = _require_processable(conn, invoice_id, "escalate")
        _set_status(conn, invoice_id, "escalated", schema.ESCALATION_CODE, approver_id, comment)
        _audit(
            conn,
            "escalate_invoice",
            invoice_id,
            {"from_status": inv["status"], "to_status": "escalated", "reason_code": schema.ESCALATION_CODE,
             "assigned_to": approver_id, "comment": comment},
        )
    return f"Invoice {invoice_id} escalated to {approver_id} {approver['name']} ({approver['role']})."


READ_TOOLS = [list_invoices, get_invoice, search_suppliers, get_supplier, get_purchase_order, list_approvers, whoami]
WRITE_TOOLS = [approve_invoice, reject_invoice, hold_invoice, escalate_invoice]
TOOLS = READ_TOOLS + WRITE_TOOLS
