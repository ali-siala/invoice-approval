"""Every inbox invoice's hidden intended outcome (set by the generator) must match the independent
reference policy, when the whole inbox is processed in policy order as the Teamleiter."""

import json
import sqlite3
from collections import Counter

from reference_policy import process


def _invoices(db):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT * FROM invoices")}
    conn.close()
    return rows


def test_every_trap_outcome_matches_reference_policy(seeded_db):
    before = _invoices(seeded_db)
    decisions = process(seeded_db)  # applies each decision through the tools
    after = _invoices(seeded_db)

    inbox = {i for i, r in before.items() if r["status"] == "received"}
    assert set(decisions) == inbox

    mismatches = []
    for invoice_id in sorted(inbox):
        intended = json.loads(before[invoice_id]["hidden_outcome"])
        reference = decisions[invoice_id].as_dict()
        final = {k: after[invoice_id][k] for k in ("status", "reason_code", "assigned_to")}
        if not intended == reference == final:
            mismatches.append((invoice_id, before[invoice_id]["hidden_trap"], intended, reference, final))
    assert not mismatches, "\n".join(map(str, mismatches))

    # Every trap kind is exercised.
    kinds = Counter((d.status, d.reason_code) for d in decisions.values())
    for kind in [("approved", None), ("escalated", "APPROVAL_LIMIT"), ("rejected", "DUPLICATE"),
                 ("rejected", "MISSING_PO"), ("rejected", "INVALID_PO"), ("rejected", "VAT_ERROR"),
                 ("on_hold", "PO_EXCEEDED"), ("on_hold", "SUPPLIER_BLOCKED")]:
        assert kinds[kind] > 0, kind


def test_full_run_keeps_books_consistent(seeded_db):
    inbox_size = sum(r["status"] == "received" for r in _invoices(seeded_db).values())
    process(seeded_db)
    conn = sqlite3.connect(seeded_db)
    # PO totals equal the sum of approved invoices on each PO.
    mismatched = conn.execute(
        "SELECT p.po_number FROM purchase_orders p LEFT JOIN "
        "(SELECT po_number, SUM(net_cents) AS s FROM invoices WHERE status = 'approved' GROUP BY po_number) a "
        "ON a.po_number = p.po_number WHERE p.net_invoiced_cents != COALESCE(a.s, 0)"
    ).fetchall()
    # Every approved invoice has exactly 3 balanced ledger lines.
    unbalanced = conn.execute(
        "SELECT invoice_id FROM ledger_entries GROUP BY invoice_id "
        "HAVING COUNT(*) != 3 OR SUM(debit_cents) != SUM(credit_cents)"
    ).fetchall()
    approved = conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'approved'").fetchone()[0]
    posted = conn.execute("SELECT COUNT(DISTINCT invoice_id) FROM ledger_entries").fetchone()[0]
    writes = conn.execute("SELECT COUNT(*) FROM audit_log").fetchone()[0]
    received_left = conn.execute("SELECT COUNT(*) FROM invoices WHERE status = 'received'").fetchone()[0]
    conn.close()
    assert mismatched == [] and unbalanced == []
    assert approved == posted
    assert received_left == 0 and writes == inbox_size
