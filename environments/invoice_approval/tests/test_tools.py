import json
import sqlite3

import pytest
from conftest import AL, CFO, TL
from verifiers.legacy.envs.stateful_tool_env import filter_signature
from verifiers.legacy.utils.tool_utils import convert_func_to_tool_def

from ap import ledger, schema, tools


def _floats(obj):
    """All float values anywhere in a decoded JSON structure."""
    if isinstance(obj, float):
        return [obj]
    if isinstance(obj, dict):
        return [f for v in obj.values() for f in _floats(v)]
    if isinstance(obj, list):
        return [f for v in obj for f in _floats(v)]
    return []


# ------------------------------------------------------------------ one test per tool


def test_list_invoices(mini):
    a, b = mini.supplier(), mini.supplier()
    late = mini.invoice(a, 10_000, received="2026-10-09", invoice_id="ER-2026-00001")
    early_hi = mini.invoice(b, 20_000, received="2026-10-02", invoice_id="ER-2026-00009")
    early_lo = mini.invoice(a, 30_000, received="2026-10-02", invoice_id="ER-2026-00005")
    mini.invoice(a, 40_000, status="approved")

    out = json.loads(tools.list_invoices("received", db=mini.path))
    assert [i["id"] for i in out["invoices"]] == [early_lo, early_hi, late]  # received_date, then id
    assert out["total_matching"] == 3 and out["returned"] == 3
    assert out["invoices"][0]["gross_amount"] == "357.00"
    assert _floats(out) == []

    only_a = json.loads(tools.list_invoices("received", supplier_id=a.lower(), db=mini.path))
    assert [i["id"] for i in only_a["invoices"]] == [early_lo, late]

    limited = json.loads(tools.list_invoices("received", limit=1, db=mini.path))
    assert limited["returned"] == 1 and limited["total_matching"] == 3

    assert json.loads(tools.list_invoices("approved", db=mini.path))["total_matching"] == 1
    assert tools.list_invoices("paid", db=mini.path).startswith("Error: unknown status")
    assert tools.list_invoices("received", supplier_id="K-99999", db=mini.path).startswith("Error: supplier")
    assert tools.list_invoices("received", limit=0, db=mini.path).startswith("Error: limit")
    assert tools.list_invoices("received", limit="ten", db=mini.path).startswith("Error: limit")


def test_get_invoice(mini):
    s = mini.supplier(name="Huber Präzisionstechnik GmbH")
    po = mini.po(s, 100_000)
    inv = mini.invoice(s, 41_200, po=po, number="MB-2026-0412")
    out = json.loads(tools.get_invoice(inv.lower(), db=mini.path))
    assert out["id"] == inv
    assert out["supplier_name"] == "Huber Präzisionstechnik GmbH"
    assert out["supplier_invoice_number"] == "MB-2026-0412"
    assert (out["net_amount"], out["vat_amount"], out["gross_amount"]) == ("412.00", "78.28", "490.28")
    assert out["vat_rate_pct"] == 19 and out["po_number"] == po and out["status"] == "received"
    assert not any(k.startswith("hidden") for k in out)
    assert _floats(out) == []
    assert tools.get_invoice("ER-2026-99999", db=mini.path).startswith("Error: invoice 'ER-2026-99999' not found")


def test_search_suppliers(mini):
    huber = mini.supplier(name="Huber Präzisionstechnik GmbH", city="München")
    mini.supplier(name="Schäfer Logistik GmbH", city="Köln")
    out = json.loads(tools.search_suppliers("huber", db=mini.path))
    assert [s["id"] for s in out["suppliers"]] == [huber]
    assert json.loads(tools.search_suppliers("MÜNCHEN", db=mini.path))["suppliers"][0]["id"] == huber
    assert json.loads(tools.search_suppliers("präzision", db=mini.path))["total_matching"] == 1
    assert json.loads(tools.search_suppliers("GmbH", db=mini.path))["total_matching"] == 2
    assert tools.search_suppliers("Nonexistent AG", db=mini.path) == "No suppliers match 'Nonexistent AG'."
    assert tools.search_suppliers("  ", db=mini.path).startswith("Error: query")


def test_get_supplier(mini):
    s = mini.supplier(blocked=True)
    out = json.loads(tools.get_supplier(s, db=mini.path))
    assert out["id"] == s and out["is_blocked"] is True and out["blocked_reason"] == "Insolvenz"
    assert out["iban"] and out["vat_id"] and out["payment_terms_days"] == 30
    assert tools.get_supplier("K-1", db=mini.path).startswith("Error: supplier 'K-1' not found")


def test_get_purchase_order(mini):
    s = mini.supplier()
    po = mini.po(s, 1_234_567, invoiced=50_005, status="closed")
    out = json.loads(tools.get_purchase_order(f" {po} ", db=mini.path))
    assert out["supplier_id"] == s and out["status"] == "closed" and out["order_date"] == "2026-09-01"
    assert (out["net_amount"], out["net_invoiced_amount"]) == ("12345.67", "500.05")
    assert tools.get_purchase_order("4500000000", db=mini.path) == "Error: purchase order '4500000000' not found."


def test_list_approvers(mini):
    out = json.loads(tools.list_approvers(db=mini.path))
    assert [a["approver_id"] for a in out] == sorted([TL, AL, CFO, "AP-052"])
    limits = {a["role"]: a["approval_limit_gross"] for a in out}
    assert limits == {"Teamleiter": "5000.00", "Abteilungsleiter": "25000.00",
                      "Bereichsleiter": "100000.00", "CFO": "unlimited"}


def test_whoami(mini):
    assert json.loads(tools.whoami(db=mini.path)) == {
        "approver_id": TL, "name": "Jonas Weber", "role": "Teamleiter", "approval_limit_gross": "5000.00"}
    schema.set_acting_approver(mini.path, CFO)
    assert json.loads(tools.whoami(db=mini.path))["role"] == "CFO"


def test_approve_invoice(mini):
    s = mini.supplier()
    po = mini.po(s, 100_000, invoiced=10_000)
    inv = mini.invoice(s, 41_200, po=po)
    result = tools.approve_invoice(inv, db=mini.path)
    assert result.startswith(f"Invoice {inv} approved. Posted 3 ledger lines: 3400 debit 412.00, "
                             "1576 debit 78.28, 1600 credit 490.28.")
    assert mini.inv(inv)["status"] == "approved"
    assert mini.po_row(po)["net_invoiced_cents"] == 51_200
    lines = mini.query("SELECT account, debit_cents, credit_cents FROM ledger_entries WHERE invoice_id = ? "
                       "ORDER BY id", (inv,))
    assert [(r["account"], r["debit_cents"], r["credit_cents"]) for r in lines] == [
        ("3400", 41_200, 0), ("1576", 7_828, 0), ("1600", 0, 49_028)]
    audit = mini.query("SELECT * FROM audit_log")
    assert len(audit) == 1 and audit[0]["action"] == "approve_invoice" and audit[0]["actor"] == TL
    assert json.loads(audit[0]["details"])["to_status"] == "approved"


@pytest.mark.parametrize("category,expense,vat_account", [("books", "4940", "1571"), ("food", "3300", "1571"),
                                                          ("services", "4900", "1576")])
def test_approve_invoice_ledger_accounts_by_category(mini, category, expense, vat_account):
    s = mini.supplier()
    inv = mini.invoice(s, 10_000, category=category)  # Kleinbetrag without PO
    tools.approve_invoice(inv, db=mini.path)
    accounts = [r["account"] for r in mini.query("SELECT account FROM ledger_entries ORDER BY id")]
    assert accounts == [expense, vat_account, ledger.PAYABLES_ACCOUNT]


def test_approve_is_atomic(mini, monkeypatch):
    s = mini.supplier()
    po = mini.po(s, 100_000)
    inv = mini.invoice(s, 41_200, po=po)

    def boom(*args, **kwargs):
        raise RuntimeError("ledger down")

    monkeypatch.setattr(tools.ledger, "ledger_lines", boom)
    assert tools.approve_invoice(inv, db=mini.path).startswith("Error: unexpected RuntimeError")
    assert mini.inv(inv)["status"] == "received"
    assert mini.po_row(po)["net_invoiced_cents"] == 0
    assert mini.count("ledger_entries") == 0 and mini.count("audit_log") == 0


def test_reject_invoice(mini):
    inv = mini.invoice(mini.supplier(), 41_200)
    assert tools.reject_invoice(inv, "missing_po", comment="Bitte Bestellnummer angeben", db=mini.path) == (
        f"Invoice {inv} rejected (MISSING_PO).")
    row = mini.inv(inv)
    assert (row["status"], row["reason_code"], row["comment"]) == ("rejected", "MISSING_PO", "Bitte Bestellnummer angeben")
    assert mini.query("SELECT action FROM audit_log") == [{"action": "reject_invoice"}]
    assert mini.count("ledger_entries") == 0

    other = mini.invoice(mini.supplier(), 41_200)
    assert tools.reject_invoice(other, "PO_EXCEEDED", db=mini.path).startswith("Error: invalid reason_code")
    assert mini.inv(other)["status"] == "received" and mini.count("audit_log") == 1


def test_hold_invoice(mini):
    inv = mini.invoice(mini.supplier(), 41_200)
    assert tools.hold_invoice(inv, "PO_EXCEEDED", db=mini.path) == f"Invoice {inv} put on hold (PO_EXCEEDED)."
    row = mini.inv(inv)
    assert (row["status"], row["reason_code"], row["comment"]) == ("on_hold", "PO_EXCEEDED", None)
    assert mini.query("SELECT action FROM audit_log") == [{"action": "hold_invoice"}]

    other = mini.invoice(mini.supplier(), 41_200)
    assert tools.hold_invoice(other, "DUPLICATE", db=mini.path).startswith("Error: invalid reason_code")
    assert mini.inv(other)["status"] == "received"


def test_escalate_invoice(mini):
    inv = mini.invoice(mini.supplier(), 1_000_000)
    assert tools.escalate_invoice(inv, AL.lower(), comment="über Limit", db=mini.path) == (
        f"Invoice {inv} escalated to {AL} Sabine Krüger (Abteilungsleiter).")
    row = mini.inv(inv)
    assert (row["status"], row["reason_code"], row["assigned_to"]) == ("escalated", "APPROVAL_LIMIT", AL)
    assert mini.query("SELECT action FROM audit_log") == [{"action": "escalate_invoice"}]

    other = mini.invoice(mini.supplier(), 1_000_000)
    assert tools.escalate_invoice(other, "AP-999", db=mini.path).startswith("Error: approver 'AP-999' not found")
    assert mini.inv(other)["status"] == "received" and mini.count("audit_log") == 1


# ------------------------------------------------------------------ cross-cutting tool rules

WRITE_CALLS = {
    "approve": lambda i, db: tools.approve_invoice(i, db=db),
    "reject": lambda i, db: tools.reject_invoice(i, "DUPLICATE", db=db),
    "hold": lambda i, db: tools.hold_invoice(i, "PO_EXCEEDED", db=db),
    "escalate": lambda i, db: tools.escalate_invoice(i, AL, db=db),
}


@pytest.mark.parametrize("from_status", ["approved", "rejected", "on_hold", "escalated"])
@pytest.mark.parametrize("call", list(WRITE_CALLS))
def test_illegal_transitions_are_refused(mini, from_status, call):
    s = mini.supplier()
    po = mini.po(s, 100_000)
    inv = mini.invoice(s, 41_200, po=po, status=from_status)
    result = WRITE_CALLS[call](inv, mini.path)
    assert result.startswith("Error: cannot") and f"status is '{from_status}'" in result
    assert mini.inv(inv)["status"] == from_status
    assert mini.po_row(po)["net_invoiced_cents"] == 0
    assert mini.count("audit_log") == 0 and mini.count("ledger_entries") == 0


def test_second_decision_on_same_invoice_is_refused(mini):
    inv = mini.invoice(mini.supplier(), 10_000)
    assert "approved" in tools.approve_invoice(inv, db=mini.path)
    assert tools.reject_invoice(inv, "DUPLICATE", db=mini.path).startswith("Error: cannot reject")
    assert mini.inv(inv)["status"] == "approved" and mini.count("audit_log") == 1


def test_every_write_is_audited(mini):
    s = mini.supplier()
    ids = [mini.invoice(s, 10_000) for _ in range(4)]
    for call, inv in zip(WRITE_CALLS.values(), ids):
        assert not call(inv, mini.path).startswith("Error")
    call(ids[0], mini.path)  # refused: not audited
    audit = mini.query("SELECT actor, action, invoice_id FROM audit_log ORDER BY id")
    assert [(a["action"], a["invoice_id"]) for a in audit] == [
        ("approve_invoice", ids[0]), ("reject_invoice", ids[1]), ("hold_invoice", ids[2]),
        ("escalate_invoice", ids[3])]
    assert {a["actor"] for a in audit} == {TL}


def test_tools_return_errors_instead_of_raising(tmp_path, mini):
    missing = str(tmp_path / "nope.sqlite")
    for tool in tools.READ_TOOLS:
        args = {"list_invoices": ("received",), "list_approvers": (), "whoami": ()}.get(tool.__name__, ("x",))
        assert tool(*args, db=missing) == "Error: no database is available for this session."
        assert tool(*args, db="") == "Error: no database is available for this session."
    assert tools.approve_invoice("ER-1", db=missing).startswith("Error: no database")
    assert not (tmp_path / "nope.sqlite").exists()  # tools never create a database
    assert tools.reject_invoice("ER-2026-00001", "", db=mini.path).startswith("Error: invalid reason_code")
    assert tools.approve_invoice(None, db=mini.path).startswith("Error: invoice")
    assert tools.reject_invoice(mini.invoice(mini.supplier(), 100), "DUPLICATE", comment="x" * 501,
                                db=mini.path).startswith("Error: comment is too long")


def test_tool_schemas_hide_db_argument():
    """The legacy verifiers schema (as built by StatefulToolEnv.add_tool) must not show `db`."""
    expected = {
        "list_invoices": {"status", "supplier_id", "limit"},
        "get_invoice": {"invoice_id"},
        "search_suppliers": {"query"},
        "get_supplier": {"supplier_id"},
        "get_purchase_order": {"po_number"},
        "list_approvers": set(),
        "whoami": set(),
        "approve_invoice": {"invoice_id"},
        "reject_invoice": {"invoice_id", "reason_code", "comment"},
        "hold_invoice": {"invoice_id", "reason_code", "comment"},
        "escalate_invoice": {"invoice_id", "approver_id", "comment"},
    }
    assert {t.__name__ for t in tools.TOOLS} == set(expected)
    for tool in tools.TOOLS:
        tool_def = convert_func_to_tool_def(filter_signature(tool, ["db"]))
        props = tool_def.parameters.get("properties", {})
        assert set(props) == expected[tool.__name__], tool.__name__
        assert tool_def.description
        assert all(p.get("description") for p in props.values()), tool.__name__


def test_read_tools_never_expose_hidden_columns(seeded_db):
    conn = sqlite3.connect(seeded_db)
    invoice_ids = [r[0] for r in conn.execute("SELECT id FROM invoices")]
    supplier_ids = [r[0] for r in conn.execute("SELECT id FROM suppliers")]
    pos = [r[0] for r in conn.execute("SELECT po_number FROM purchase_orders")]
    conn.close()
    outputs = [tools.list_invoices(s, limit=200, db=seeded_db) for s in schema.INVOICE_STATUSES]
    outputs += [tools.get_invoice(i, db=seeded_db) for i in invoice_ids]
    outputs += [tools.get_supplier(s, db=seeded_db) for s in supplier_ids]
    outputs += [tools.get_purchase_order(p, db=seeded_db) for p in pos]
    outputs += [tools.search_suppliers("GmbH", db=seeded_db), tools.list_approvers(db=seeded_db),
                tools.whoami(db=seeded_db)]
    for out in outputs:
        assert not out.startswith("Error"), out
        assert "hidden" not in out and "trap" not in out and "outcome" not in out
        assert _floats(json.loads(out)) == []
