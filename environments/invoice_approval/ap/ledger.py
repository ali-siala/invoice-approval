"""SKR03-style accounts and the three ledger lines posted when an invoice is approved."""

EXPENSE_ACCOUNTS = {
    "goods": "3400",  # Wareneingang 19 % Vorsteuer
    "services": "4900",  # Sonstige betriebliche Aufwendungen
    "books": "4940",  # Zeitschriften, Bücher
    "food": "3300",  # Wareneingang 7 % Vorsteuer
}
VAT_ACCOUNTS = {
    19: "1576",  # Abziehbare Vorsteuer 19 %
    7: "1571",  # Abziehbare Vorsteuer 7 %
}
VAT_ACCOUNT_OTHER = "1570"  # Abziehbare Vorsteuer (any other rate)
PAYABLES_ACCOUNT = "1600"  # Verbindlichkeiten aus Lieferungen und Leistungen


def ledger_lines(
    category: str, vat_rate_pct: int, net_cents: int, vat_cents: int, gross_cents: int, text: str
) -> list[tuple[str, int, int, str]]:
    """Return (account, debit_cents, credit_cents, text) for expense, input VAT and payable."""
    return [
        (EXPENSE_ACCOUNTS[category], net_cents, 0, text),
        (VAT_ACCOUNTS.get(vat_rate_pct, VAT_ACCOUNT_OTHER), vat_cents, 0, text),
        (PAYABLES_ACCOUNT, 0, gross_cents, text),
    ]
