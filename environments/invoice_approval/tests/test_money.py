from ap import money


def test_vat_half_cent_rounds_up_not_to_even():
    # Python's round() would give 28 and 47 here (banker's rounding); the spec requires half-up.
    assert money.expected_vat_cents(150, 19) == 29  # 28.5
    assert money.expected_vat_cents(250, 19) == 48  # 47.5
    assert money.expected_vat_cents(50, 7) == 4  # 3.5
    assert money.expected_vat_cents(1_050, 7) == 74  # 73.5


def test_vat_ordinary_amounts():
    assert money.expected_vat_cents(41_200, 19) == 7_828
    assert money.expected_vat_cents(149, 19) == 28  # 28.31
    assert money.expected_vat_cents(151, 19) == 29  # 28.69
    assert money.expected_vat_cents(0, 19) == 0
    assert isinstance(money.expected_vat_cents(12_345, 7), int)


def test_vat_large_amounts_stay_exact():
    assert money.expected_vat_cents(8_403_361, 19) == 1_596_639  # 1596638.59
    assert money.expected_vat_cents(10**12 + 50, 19) == 190_000_000_010  # ...009.5 -> up


def test_expected_rate_by_category():
    assert money.expected_vat_rate_pct("books") == 7
    assert money.expected_vat_rate_pct("food") == 7
    assert money.expected_vat_rate_pct("goods") == 19
    assert money.expected_vat_rate_pct("services") == 19


def test_gross_is_exact_sum():
    assert money.expected_gross_cents(41_200, 7_828) == 49_028


def test_kleinbetrag_boundary():
    assert money.is_kleinbetrag(25_000)
    assert not money.is_kleinbetrag(25_001)


def test_po_tolerance_exact_and_one_cent_above():
    assert money.within_po_tolerance(300_000, 100_000, 206_000)  # exactly 102 %
    assert not money.within_po_tolerance(300_000, 100_000, 206_001)
    assert money.within_po_tolerance(100, 0, 102)
    assert not money.within_po_tolerance(100, 0, 103)
    assert money.within_po_tolerance(0, 0, 0)


def test_fmt_cents():
    assert money.fmt_cents(49_028) == "490.28"
    assert money.fmt_cents(5) == "0.05"
    assert money.fmt_cents(0) == "0.00"
    assert money.fmt_cents(-150) == "-1.50"
    assert money.fmt_cents(100_000_000) == "1000000.00"
