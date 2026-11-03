"""One test per policy rule (SPEC section 4), checked against the reference implementation."""

import sqlite3

from conftest import AL, BL, CFO, TL
from reference_policy import Decision, decide, process

APPROVE = Decision("approved")


def test_rule0_processing_order(mini):
    s = mini.supplier()
    po = mini.po(s, 400_000)
    # IDs deliberately disagree with dates: the later ID was received first.
    first = mini.invoice(s, 250_000, po=po, received="2026-10-02", invoice_id="ER-2026-00009")
    second = mini.invoice(s, 200_000, po=po, received="2026-10-07", invoice_id="ER-2026-00001")
    decisions = process(mini.path)
    assert list(decisions) == [first, second]
    assert decisions[first] == APPROVE
    assert decisions[second] == Decision("on_hold", "PO_EXCEEDED")


def test_rule0_same_date_ordered_by_id(mini):
    s = mini.supplier()
    po = mini.po(s, 400_000)
    hi = mini.invoice(s, 250_000, po=po, invoice_id="ER-2026-00020")
    lo = mini.invoice(s, 200_000, po=po, invoice_id="ER-2026-00010")
    decisions = process(mini.path)
    assert list(decisions) == [lo, hi]
    assert decisions[hi] == Decision("on_hold", "PO_EXCEEDED")


def test_rule1_duplicate(mini):
    s = mini.supplier()
    orig = mini.invoice(s, 10_000, number="MB-2026-0412", received="2026-10-02")
    dup = mini.invoice(s, 10_000, number="mb2026 0412", received="2026-10-05")
    other_supplier = mini.invoice(mini.supplier(), 10_000, number="MB-2026-0412", received="2026-10-06")
    assert decide(mini.path, orig) == APPROVE
    assert decide(mini.path, dup) == Decision("rejected", "DUPLICATE")
    assert decide(mini.path, other_supplier) == APPROVE  # same number, different supplier


def test_rule1_original_counts_in_any_status_except_rejected(mini):
    for status in ("received", "approved", "on_hold", "escalated"):
        s = mini.supplier()
        mini.invoice(s, 10_000, number="X-1", received="2026-10-01", status=status)
        assert decide(mini.path, mini.invoice(s, 10_000, number="X-1")) == Decision("rejected", "DUPLICATE")
    s = mini.supplier()
    mini.invoice(s, 10_000, number="X-1", received="2026-10-01", status="rejected")
    assert decide(mini.path, mini.invoice(s, 10_000, number="X-1")) == APPROVE


def test_rule2_purchase_order_required_above_250(mini):
    s, other = mini.supplier(), mini.supplier()
    assert decide(mini.path, mini.invoice(s, 41_200)) == Decision("rejected", "MISSING_PO")
    assert decide(mini.path, mini.invoice(s, 41_200, po=mini.po(other, 100_000))) == Decision("rejected", "INVALID_PO")
    assert decide(mini.path, mini.invoice(s, 41_200, po=mini.po(s, 100_000, status="closed"))) == Decision(
        "rejected", "INVALID_PO")
    assert decide(mini.path, mini.invoice(s, 41_200, po="4500099999")) == Decision("rejected", "INVALID_PO")
    assert decide(mini.path, mini.invoice(s, 41_200, po=mini.po(s, 100_000))) == APPROVE


def test_rule3_kleinbetrag(mini):
    s, other = mini.supplier(), mini.supplier()
    assert decide(mini.path, mini.invoice(s, 20_000)) == APPROVE  # no PO needed
    assert decide(mini.path, mini.invoice(s, 20_000, po=mini.po(other, 50_000))) == Decision("rejected", "INVALID_PO")
    assert decide(mini.path, mini.invoice(s, 20_000, po=mini.po(s, 50_000, status="closed"))) == Decision(
        "rejected", "INVALID_PO")
    assert decide(mini.path, mini.invoice(s, 20_000, po=mini.po(s, 50_000, invoiced=40_000))) == Decision(
        "on_hold", "PO_EXCEEDED")


def test_rule4_po_amount(mini):
    s = mini.supplier()
    assert decide(mini.path, mini.invoice(s, 30_000, po=mini.po(s, 100_000, invoiced=72_000))) == APPROVE
    assert decide(mini.path, mini.invoice(s, 30_001, po=mini.po(s, 100_000, invoiced=72_000))) == Decision(
        "on_hold", "PO_EXCEEDED")


def test_rule5_vat(mini):
    s = mini.supplier()
    po = lambda: mini.po(s, 1_000_000)  # noqa: E731
    err = Decision("rejected", "VAT_ERROR")
    assert decide(mini.path, mini.invoice(s, 41_200, po=po())) == APPROVE
    assert decide(mini.path, mini.invoice(s, 41_200, po=po(), category="books")) == APPROVE  # 7 %
    assert decide(mini.path, mini.invoice(s, 41_200, po=po(), rate=7)) == err  # goods at 7 %
    assert decide(mini.path, mini.invoice(s, 41_200, po=po(), category="food", rate=19)) == err
    assert decide(mini.path, mini.invoice(s, 41_200, po=po(), vat=7_829)) == err  # VAT amount off
    assert decide(mini.path, mini.invoice(s, 41_200, po=po(), gross=49_027)) == err  # gross != net + VAT


def test_rule6_blocked_supplier(mini):
    s = mini.supplier(blocked=True)
    assert decide(mini.path, mini.invoice(s, 41_200, po=mini.po(s, 100_000))) == Decision("on_hold", "SUPPLIER_BLOCKED")


def test_rule7_approval_limit_and_escalation_target(mini):
    s = mini.supplier()
    po = mini.po(s, 100_000_000)
    small = mini.invoice(s, 400_000, po=po)  # 4760.00 gross
    mid = mini.invoice(s, 1_000_000, po=po)  # 11900.00
    big = mini.invoice(s, 5_000_000, po=po)  # 59500.00
    huge = mini.invoice(s, 20_000_000, po=po)  # 238000.00
    assert decide(mini.path, small) == APPROVE
    assert decide(mini.path, mid) == Decision("escalated", "APPROVAL_LIMIT", AL)
    assert decide(mini.path, big) == Decision("escalated", "APPROVAL_LIMIT", BL)
    assert decide(mini.path, huge) == Decision("escalated", "APPROVAL_LIMIT", CFO)
    # Acting as a higher approver changes the outcome.
    assert decide(mini.path, mid, acting_approver_id=AL) == APPROVE
    assert decide(mini.path, huge, acting_approver_id=CFO) == APPROVE


def test_rule7_escalation_tie_goes_to_lowest_approver_id(mini):
    conn = sqlite3.connect(mini.path)
    with conn:
        conn.execute("INSERT INTO approvers VALUES ('AP-001', 'Zweite Abteilungsleiterin', 'Abteilungsleiter', 2500000)")
    conn.close()
    s = mini.supplier()
    inv = mini.invoice(s, 1_000_000, po=mini.po(s, 2_000_000))
    assert decide(mini.path, inv) == Decision("escalated", "APPROVAL_LIMIT", "AP-001")
    assert decide(mini.path, inv, acting_approver_id=TL) == Decision("escalated", "APPROVAL_LIMIT", "AP-001")


def test_rule8_check_order(mini):
    blocked = mini.supplier(blocked=True)
    s = mini.supplier()
    # blocked beats everything (here: duplicate, missing PO, VAT error, over limit)
    mini.invoice(blocked, 900_000, number="B-1", received="2026-10-01")
    assert decide(mini.path, mini.invoice(blocked, 900_000, number="B-1", rate=7)) == Decision(
        "on_hold", "SUPPLIER_BLOCKED")
    # duplicate beats missing PO and VAT error
    mini.invoice(s, 900_000, number="D-1", received="2026-10-01")
    assert decide(mini.path, mini.invoice(s, 900_000, number="D-1", rate=7)) == Decision("rejected", "DUPLICATE")
    # missing PO beats VAT error and approval limit
    assert decide(mini.path, mini.invoice(s, 900_000, rate=7)) == Decision("rejected", "MISSING_PO")
    # PO exceeded beats VAT error
    assert decide(mini.path, mini.invoice(s, 120_000, po=mini.po(s, 100_000), rate=7)) == Decision(
        "on_hold", "PO_EXCEEDED")
    # VAT error beats approval limit
    assert decide(mini.path, mini.invoice(s, 900_000, po=mini.po(s, 1_000_000), rate=7)) == Decision(
        "rejected", "VAT_ERROR")
    # approval limit last
    assert decide(mini.path, mini.invoice(s, 900_000, po=mini.po(s, 1_000_000))) == Decision(
        "escalated", "APPROVAL_LIMIT", AL)
