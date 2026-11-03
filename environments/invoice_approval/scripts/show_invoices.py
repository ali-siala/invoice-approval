"""Print random inbox invoices in a readable form, to check seed changes by eye.

Builds the seed database in a temporary file (or reads --db), samples invoices with at least
--min-traps trap invoices, and prints each with its supplier, PO, same-number invoices and the
hidden intended outcome.

    python scripts/show_invoices.py                      # 10 invoices from seed 42
    python scripts/show_invoices.py -n 20 --sample-seed 7
    python scripts/show_invoices.py --db /path/to/existing.sqlite
"""

import argparse
import json
import random
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ap import money, seed  # noqa: E402


def normalize(number: str) -> str:
    return "".join(ch for ch in number.upper() if ch.isalnum())


def show(conn: sqlite3.Connection, inv: sqlite3.Row) -> None:
    eur = money.fmt_cents
    s = conn.execute("SELECT * FROM suppliers WHERE id = ?", (inv["supplier_id"],)).fetchone()
    blocked = f"  ⚠ BLOCKED: {s['blocked_reason']}" if s["is_blocked"] else ""
    print(f"── {inv['id']}  {s['name']} ({s['id']}, {s['zip']} {s['city']}, {s['region']}){blocked}")
    print(f"   Supplier invoice no. {inv['supplier_invoice_number']!r} · invoice {inv['invoice_date']}"
          f" · received {inv['received_date']} · due {inv['due_date']} · cost center {inv['cost_center']}")
    rate = money.expected_vat_rate_pct(inv["category"])
    expected = money.expected_vat_cents(inv["net_cents"], rate)
    print(f"   {inv['category']:<8} net {eur(inv['net_cents']):>10}  VAT {inv['vat_rate_pct']:>2}% "
          f"{eur(inv['vat_cents']):>9}  gross {eur(inv['gross_cents']):>10}   "
          f"(expected VAT at {rate}%: {eur(expected)}, gross {eur(inv['net_cents'] + expected)})")
    if inv["po_number"]:
        po = conn.execute("SELECT * FROM purchase_orders WHERE po_number = ?", (inv["po_number"],)).fetchone()
        if po is None:
            print(f"   PO {inv['po_number']}: does not exist")
        else:
            owner = "same supplier" if po["supplier_id"] == inv["supplier_id"] else f"belongs to {po['supplier_id']}!"
            print(f"   PO {po['po_number']} '{po['description']}' ordered {po['order_date']}, cost center "
                  f"{po['cost_center']}: {po['status']}, {owner}, net {eur(po['net_cents'])},"
                  f" already invoiced {eur(po['net_invoiced_cents'])}, limit +2% {eur(po['net_cents'] * 102 // 100)}")
    else:
        print("   No PO" + ("  (Kleinbetrag ≤ 250.00)" if money.is_kleinbetrag(inv["gross_cents"]) else ""))
    for other in conn.execute("SELECT * FROM invoices WHERE supplier_id = ? AND id != ? ORDER BY received_date, id",
                              (inv["supplier_id"], inv["id"])):
        if normalize(other["supplier_invoice_number"]) == normalize(inv["supplier_invoice_number"]):
            print(f"   Same number: {other['id']} {other['supplier_invoice_number']!r} received "
                  f"{other['received_date']}, status {other['status']}, gross {eur(other['gross_cents'])}")
    outcome = json.loads(inv["hidden_outcome"])
    target = f" → {outcome['assigned_to']}" if outcome["assigned_to"] else ""
    print(f"   ► intended: {outcome['status']} {outcome['reason_code'] or ''}{target}   [trap: {inv['hidden_trap']}]\n")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("-n", type=int, default=10, help="number of invoices (default 10)")
    parser.add_argument("--min-traps", type=int, default=3, help="minimum trap invoices in the sample (default 3)")
    parser.add_argument("--seed", type=int, default=seed.SEED, help="seed for the generator (default 42)")
    parser.add_argument("--sample-seed", type=int, default=2026, help="seed for picking invoices (default 2026)")
    parser.add_argument("--db", help="read an existing database instead of building one")
    args = parser.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        db = args.db or seed.build_db(Path(tmp) / "seed.sqlite", seed=args.seed)
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        inbox = conn.execute("SELECT * FROM invoices WHERE status = 'received'").fetchall()
        traps = sum(not r["hidden_trap"].startswith("clean") for r in inbox)
        if args.n > len(inbox) or args.min_traps > min(args.n, traps):
            sys.exit(f"can't pick {args.n} invoices with {args.min_traps} traps from {len(inbox)} ({traps} traps)")
        rng = random.Random(args.sample_seed)
        while True:
            pick = rng.sample(inbox, args.n)
            if sum(not r["hidden_trap"].startswith("clean") for r in pick) >= args.min_traps:
                break
        for inv in sorted(pick, key=lambda r: (r["received_date"], r["id"])):
            show(conn, inv)
        conn.close()


if __name__ == "__main__":
    main()
