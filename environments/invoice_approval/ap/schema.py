"""SQLite schema (SPEC section 3) and connection helpers.

Columns prefixed `hidden_` hold the generator's intended outcome. They are read by tests only;
tools never select them.
"""

import sqlite3
from pathlib import Path

INVOICE_STATUSES = ("received", "approved", "rejected", "on_hold", "escalated")
PO_STATUSES = ("open", "closed")
CATEGORIES = ("goods", "services", "books", "food")
ROLES = ("Teamleiter", "Abteilungsleiter", "Bereichsleiter", "CFO")

REJECT_CODES = ("DUPLICATE", "MISSING_PO", "INVALID_PO", "VAT_ERROR")
HOLD_CODES = ("PO_EXCEEDED", "SUPPLIER_BLOCKED")
ESCALATION_CODE = "APPROVAL_LIMIT"

SCHEMA = """
CREATE TABLE suppliers (
    id                 TEXT PRIMARY KEY,
    name               TEXT NOT NULL,
    street             TEXT NOT NULL,
    zip                TEXT NOT NULL,
    city               TEXT NOT NULL,
    region             TEXT NOT NULL,  -- Bundesland (DE, AT) or province (NL)
    country            TEXT NOT NULL,
    vat_id             TEXT NOT NULL,
    iban               TEXT NOT NULL,
    payment_terms_days INTEGER NOT NULL,
    is_blocked         INTEGER NOT NULL DEFAULT 0 CHECK (is_blocked IN (0, 1)),
    blocked_reason     TEXT
);

CREATE TABLE purchase_orders (
    po_number          TEXT PRIMARY KEY,
    supplier_id        TEXT NOT NULL REFERENCES suppliers(id),
    order_date         TEXT NOT NULL,
    cost_center        TEXT NOT NULL,
    description        TEXT NOT NULL,
    net_cents          INTEGER NOT NULL CHECK (typeof(net_cents) = 'integer' AND net_cents >= 0),
    net_invoiced_cents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(net_invoiced_cents) = 'integer'),
    status             TEXT NOT NULL CHECK (status IN ('open', 'closed'))
);

CREATE TABLE approvers (
    id                         TEXT PRIMARY KEY,
    name                       TEXT NOT NULL,
    role                       TEXT NOT NULL
        CHECK (role IN ('Teamleiter', 'Abteilungsleiter', 'Bereichsleiter', 'CFO')),
    approval_limit_gross_cents INTEGER
        CHECK (approval_limit_gross_cents IS NULL OR typeof(approval_limit_gross_cents) = 'integer')
);

CREATE TABLE invoices (
    id                      TEXT PRIMARY KEY,
    supplier_id             TEXT NOT NULL REFERENCES suppliers(id),
    supplier_invoice_number TEXT NOT NULL,
    invoice_date            TEXT NOT NULL,
    received_date           TEXT NOT NULL,
    due_date                TEXT NOT NULL,
    po_number               TEXT,  -- deliberately no FK: invoices may cite a PO that doesn't exist
    cost_center             TEXT NOT NULL,
    category                TEXT NOT NULL CHECK (category IN ('goods', 'services', 'books', 'food')),
    net_cents               INTEGER NOT NULL CHECK (typeof(net_cents) = 'integer'),
    vat_rate_pct            INTEGER NOT NULL CHECK (typeof(vat_rate_pct) = 'integer'),
    vat_cents               INTEGER NOT NULL CHECK (typeof(vat_cents) = 'integer'),
    gross_cents             INTEGER NOT NULL CHECK (typeof(gross_cents) = 'integer'),
    currency                TEXT NOT NULL DEFAULT 'EUR',
    status                  TEXT NOT NULL DEFAULT 'received'
        CHECK (status IN ('received', 'approved', 'rejected', 'on_hold', 'escalated')),
    reason_code             TEXT,
    assigned_to             TEXT REFERENCES approvers(id),
    comment                 TEXT,
    hidden_trap             TEXT,  -- generator's scenario label (tests only)
    hidden_outcome          TEXT   -- JSON {"status", "reason_code", "assigned_to"} (tests only)
);
CREATE INDEX idx_invoices_queue ON invoices (status, received_date, id);
CREATE INDEX idx_invoices_supplier ON invoices (supplier_id);

CREATE TABLE ledger_entries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    invoice_id   TEXT NOT NULL REFERENCES invoices(id),
    account      TEXT NOT NULL,
    debit_cents  INTEGER NOT NULL DEFAULT 0 CHECK (typeof(debit_cents) = 'integer'),
    credit_cents INTEGER NOT NULL DEFAULT 0 CHECK (typeof(credit_cents) = 'integer'),
    text         TEXT NOT NULL
);

CREATE TABLE audit_log (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp  TEXT NOT NULL,
    actor      TEXT NOT NULL,
    action     TEXT NOT NULL,
    invoice_id TEXT,
    details    TEXT NOT NULL
);

-- Session settings, e.g. which approver the agent acts as (read by whoami).
CREATE TABLE app_context (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

# Invoice columns tools may show. Never add hidden_* here.
PUBLIC_INVOICE_COLUMNS = (
    "id", "supplier_id", "supplier_invoice_number", "invoice_date", "received_date", "due_date",
    "po_number", "cost_center", "category", "net_cents", "vat_rate_pct", "vat_cents", "gross_cents",
    "currency", "status", "reason_code", "assigned_to", "comment",
)


def create_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(SCHEMA)


def connect(db: str) -> sqlite3.Connection:
    """Open an existing database (never creates one) in autocommit mode; callers BEGIN explicitly."""
    path = Path(db)
    if not db or not path.is_file():
        raise FileNotFoundError(f"database not found: {db!r}")
    conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=rw", uri=True, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def get_acting_approver_id(conn: sqlite3.Connection) -> str | None:
    row = conn.execute("SELECT value FROM app_context WHERE key = 'acting_approver_id'").fetchone()
    return row[0] if row else None


def set_acting_approver(db: str, approver_id: str) -> None:
    """Set which approver the agent acts as (done by the environment per task, not by the agent)."""
    conn = connect(db)
    try:
        if conn.execute("SELECT 1 FROM approvers WHERE id = ?", (approver_id,)).fetchone() is None:
            raise ValueError(f"unknown approver {approver_id!r}")
        conn.execute(
            "INSERT INTO app_context (key, value) VALUES ('acting_approver_id', ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (approver_id,),
        )
    finally:
        conn.close()
