"""The boundary and edge cases listed in SPEC sections 4, 6 and 8, on hand-built data."""

import pytest
from conftest import AL, BL
from reference_policy import Decision, decide, process

from ap import money

APPROVE = Decision("approved")
DUPLICATE = Decision("rejected", "DUPLICATE")


@pytest.mark.parametrize("variant", ["mb-2026-0412", "MB-2026-0412 ", "MB2026-0412", " m b 2026 - 0412"])
def test_duplicate_numbers_differing_by_case_spaces_hyphens(mini, variant):
    s = mini.supplier()
    mini.invoice(s, 10_000, number="MB-2026-0412", received="2026-10-01")
    assert decide(mini.path, mini.invoice(s, 10_000, number=variant, received="2026-10-02")) == DUPLICATE


def test_similar_but_different_number_is_not_duplicate(mini):
    s = mini.supplier()
    mini.invoice(s, 10_000, number="MB-2026-0412", received="2026-10-01")
    assert decide(mini.path, mini.invoice(s, 10_000, number="MB-2026-0413")) == APPROVE


def test_resubmission_after_vat_error_rejection_is_not_duplicate(mini):
    s = mini.supplier()
    po = mini.po(s, 100_000)
    bad = mini.invoice(s, 41_200, po=po, rate=7, number="RE-77", received="2026-10-01")
    fixed = mini.invoice(s, 41_200, po=po, number="RE-77", received="2026-10-03")
    # While the faulty original is still open, the resubmission looks like a duplicate ...
    assert decide(mini.path, fixed) == DUPLICATE
    # ... processed in order, the original is rejected first and the resubmission is approved.
    assert process(mini.path) == {bad: Decision("rejected", "VAT_ERROR"), fixed: APPROVE}


def test_duplicate_with_different_amount_is_still_duplicate(mini):
    s = mini.supplier()
    mini.invoice(s, 10_000, number="RE-5", received="2026-10-01")
    assert decide(mini.path, mini.invoice(s, 19_999, number="RE-5", received="2026-10-02")) == DUPLICATE


def test_duplicate_same_received_date_lower_id_is_original(mini):
    s = mini.supplier()
    lo = mini.invoice(s, 10_000, number="RE-5", invoice_id="ER-2026-00010")
    hi = mini.invoice(s, 10_000, number="RE-5", invoice_id="ER-2026-00011")
    assert decide(mini.path, lo) == APPROVE
    assert decide(mini.path, hi) == DUPLICATE


def test_kleinbetrag_with_valid_po_counts_toward_po(mini):
    s = mini.supplier()
    po = mini.po(s, 100_000)
    small = mini.invoice(s, 15_000, po=po, received="2026-10-01")
    later = mini.invoice(s, 90_000, po=po, received="2026-10-02")
    assert decide(mini.path, later) == APPROVE  # alone it would fit
    assert process(mini.path) == {small: APPROVE, later: Decision("on_hold", "PO_EXCEEDED")}
    assert mini.po_row(po)["net_invoiced_cents"] == 15_000


def test_kleinbetrag_with_other_suppliers_po_is_invalid(mini):
    s = mini.supplier()
    inv = mini.invoice(s, 10_000, po=mini.po(mini.supplier(), 100_000))
    assert decide(mini.path, inv) == Decision("rejected", "INVALID_PO")


@pytest.mark.parametrize("category,net,vat", [("goods", 150, 29), ("goods", 41_250, 7_838), ("books", 50, 4),
                                              ("food", 1_050, 74)])
def test_vat_on_half_cent_boundary(mini, category, net, vat):
    s = mini.supplier()
    rate = money.expected_vat_rate_pct(category)
    assert (net * rate) % 100 == 50  # really a half cent
    po = mini.po(s, 100_000)
    assert decide(mini.path, mini.invoice(s, net, category=category, vat=vat, po=po)) == APPROVE
    assert decide(mini.path, mini.invoice(s, net, category=category, vat=vat - 1, po=po)) == Decision(
        "rejected", "VAT_ERROR")


@pytest.mark.parametrize("order", ["a_first", "b_first"])
def test_two_invoices_on_one_po_exceed_only_together(mini, order):
    s = mini.supplier()
    po = mini.po(s, 400_000)
    da, db = ("2026-10-01", "2026-10-02") if order == "a_first" else ("2026-10-02", "2026-10-01")
    a = mini.invoice(s, 250_000, po=po, received=da)
    b = mini.invoice(s, 200_000, po=po, received=db)
    assert decide(mini.path, a) == APPROVE and decide(mini.path, b) == APPROVE
    decisions = process(mini.path)
    first, second = (a, b) if order == "a_first" else (b, a)
    assert decisions == {first: APPROVE, second: Decision("on_hold", "PO_EXCEEDED")}


def test_po_exactly_at_tolerance_passes_one_cent_above_holds(mini):
    s = mini.supplier()
    exact = mini.invoice(s, 206_000, po=mini.po(s, 300_000, invoiced=100_000))
    over = mini.invoice(s, 206_001, po=mini.po(s, 300_000, invoiced=100_000))
    assert decide(mini.path, exact) == APPROVE
    assert decide(mini.path, over) == Decision("on_hold", "PO_EXCEEDED")


def test_kleinbetrag_limit_250_00_vs_250_01(mini):
    s = mini.supplier()
    at = mini.invoice(s, 21_008)  # 210.08 + 39.92 = 250.00
    above = mini.invoice(s, 21_009)  # 210.09 + 39.92 = 250.01
    assert mini.inv(at)["gross_cents"] == 25_000 and mini.inv(above)["gross_cents"] == 25_001
    assert decide(mini.path, at) == APPROVE
    assert decide(mini.path, above) == Decision("rejected", "MISSING_PO")


@pytest.mark.parametrize("net,gross,expected", [
    (420_168, 500_000, APPROVE),
    (420_169, 500_001, Decision("escalated", "APPROVAL_LIMIT", AL)),
    (2_100_840, 2_500_000, Decision("escalated", "APPROVAL_LIMIT", AL)),
    (2_100_841, 2_500_001, Decision("escalated", "APPROVAL_LIMIT", BL)),
])
def test_approval_limit_boundaries(mini, net, gross, expected):
    s = mini.supplier()
    inv = mini.invoice(s, net, po=mini.po(s, 10_000_000))
    assert mini.inv(inv)["gross_cents"] == gross
    assert decide(mini.path, inv) == expected
