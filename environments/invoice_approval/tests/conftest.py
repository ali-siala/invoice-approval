import shutil
import sqlite3
from itertools import count

import pytest

from ap import money, schema, seed

# Same roles and limits as the spec, written out here so tests don't depend on the generator.
TL, AL, BL, CFO = "AP-203", "AP-117", "AP-052", "AP-008"
TEST_APPROVERS = [
    (TL, "Jonas Weber", "Teamleiter", 500_000),
    (AL, "Sabine Krüger", "Abteilungsleiter", 2_500_000),
    (BL, "Dr. Thomas Lindner", "Bereichsleiter", 10_000_000),
    (CFO, "Claudia Hofmann", "CFO", None),
]


class MiniDB:
    """A tiny hand-built database for precise tool and policy tests."""

    def __init__(self, path):
        self.path = str(path)
        self._ids = count(1)
        conn = sqlite3.connect(self.path)
        schema.create_schema(conn)
        with conn:
            conn.executemany("INSERT INTO approvers VALUES (?, ?, ?, ?)", TEST_APPROVERS)
            conn.execute("INSERT INTO app_context VALUES ('acting_approver_id', ?)", (TL,))
        conn.close()

    def _exec(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        with conn:
            conn.execute(sql, params)
        conn.close()

    def query(self, sql, params=()):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        rows = [dict(r) for r in conn.execute(sql, params)]
        conn.close()
        return rows

    def supplier(self, *, blocked=False, name=None, city="München") -> str:
        sid = f"K-{20000 + next(self._ids)}"
        self._exec(
            "INSERT INTO suppliers VALUES (?, ?, 'Teststraße 1', '80331', ?, 'Bayern', 'DE', 'DE123456788', "
            "'DE89370400440532013000', 30, ?, ?)",
            (sid, name or f"Testlieferant {sid} GmbH", city, int(blocked), "Insolvenz" if blocked else None),
        )
        return sid

    def po(self, supplier_id, net, *, invoiced=0, status="open") -> str:
        number = f"45000{90000 + next(self._ids)}"
        self._exec(
            "INSERT INTO purchase_orders VALUES (?, ?, '2026-09-01', '3100', 'Testbestellung', ?, ?, ?)",
            (number, supplier_id, net, invoiced, status),
        )
        return number

    def invoice(self, supplier_id, net, *, category="goods", rate=None, vat=None, gross=None, po=None,
                number=None, received="2026-10-05", status="received", invoice_id=None) -> str:
        n = next(self._ids)
        invoice_id = invoice_id or f"ER-2026-{n:05d}"
        rate = money.expected_vat_rate_pct(category) if rate is None else rate
        vat = money.expected_vat_cents(net, rate) if vat is None else vat
        gross = net + vat if gross is None else gross
        self._exec(
            "INSERT INTO invoices (id, supplier_id, supplier_invoice_number, invoice_date, received_date, due_date, "
            "po_number, cost_center, category, net_cents, vat_rate_pct, vat_cents, gross_cents, status) "
            "VALUES (?, ?, ?, '2026-10-01', ?, '2026-10-31', ?, '3100', ?, ?, ?, ?, ?, ?)",
            (invoice_id, supplier_id, number or f"RE-{n:06d}", received, po, category, net, rate, vat, gross, status),
        )
        return invoice_id

    def inv(self, invoice_id) -> dict:
        return self.query("SELECT * FROM invoices WHERE id = ?", (invoice_id,))[0]

    def po_row(self, po_number) -> dict:
        return self.query("SELECT * FROM purchase_orders WHERE po_number = ?", (po_number,))[0]

    def count(self, table) -> int:
        return self.query(f"SELECT COUNT(*) AS n FROM {table}")[0]["n"]


@pytest.fixture
def mini(tmp_path) -> MiniDB:
    return MiniDB(tmp_path / "mini.sqlite")


@pytest.fixture(scope="session")
def seed_template(tmp_path_factory) -> str:
    return seed.build_db(tmp_path_factory.mktemp("seed") / "seed.sqlite")


@pytest.fixture
def seeded_db(seed_template, tmp_path) -> str:
    """A fresh copy of the seed=42 database for one test."""
    path = tmp_path / "seeded.sqlite"
    shutil.copy(seed_template, path)
    return str(path)
