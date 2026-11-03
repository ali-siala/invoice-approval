"""Money arithmetic (SPEC section 3). All amounts are integer cents; VAT rates are whole percents.

This is the only place where VAT, gross, Kleinbetrag and PO tolerance are computed.
"""

from decimal import ROUND_HALF_UP, Decimal

STANDARD_VAT_PCT = 19
REDUCED_VAT_PCT = 7
REDUCED_VAT_CATEGORIES = frozenset({"books", "food"})

KLEINBETRAG_LIMIT_CENTS = 25_000  # EUR 250.00 gross
PO_TOLERANCE_PCT = 2


def expected_vat_rate_pct(category: str) -> int:
    """7% for books and food, 19% for everything else."""
    return REDUCED_VAT_PCT if category in REDUCED_VAT_CATEGORIES else STANDARD_VAT_PCT


def expected_vat_cents(net_cents: int, rate_pct: int) -> int:
    """net * rate / 100, rounded half-up to whole cents."""
    vat = Decimal(net_cents) * rate_pct / 100
    return int(vat.quantize(Decimal(1), rounding=ROUND_HALF_UP))


def expected_gross_cents(net_cents: int, vat_cents: int) -> int:
    return net_cents + vat_cents


def is_kleinbetrag(gross_cents: int) -> bool:
    return gross_cents <= KLEINBETRAG_LIMIT_CENTS


def within_po_tolerance(po_net_cents: int, net_invoiced_cents: int, net_cents: int) -> bool:
    """True if already-invoiced net plus this net stays within PO net + 2% (pure integer comparison)."""
    return (net_invoiced_cents + net_cents) * 100 <= po_net_cents * (100 + PO_TOLERANCE_PCT)


def fmt_cents(cents: int) -> str:
    """12345 -> "123.45". Amounts are shown to the model as strings, never floats."""
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}{cents // 100}.{cents % 100:02d}"
