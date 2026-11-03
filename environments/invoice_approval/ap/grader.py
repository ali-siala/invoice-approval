"""Grading by replay (SPEC sections 2 and 8a).

The gold actions are replayed on a fresh seed copy; the result is compared with the agent's final
database on exactly the 8a fields. Reward is 1.0 on an exact match, else 0.0; partial-credit
numbers are returned separately for logging only.
"""

import sqlite3
import tempfile
from collections import Counter
from pathlib import Path

from . import rollout, solver


def snapshot(db: str) -> dict:
    """The SPEC 8a state, JSON-serializable: invoices and POs by key, ledger as a sorted multiset.

    Excluded on purpose: audit_log, invoice comment, ledger text and all autoincrement IDs.
    """
    conn = sqlite3.connect(db)
    try:
        return {
            "invoices": {r[0]: list(r[1:]) for r in conn.execute(
                "SELECT id, status, reason_code, assigned_to FROM invoices")},
            "purchase_orders": {r[0]: list(r[1:]) for r in conn.execute(
                "SELECT po_number, net_invoiced_cents, status FROM purchase_orders")},
            "ledger": sorted(list(r) for r in conn.execute(
                "SELECT invoice_id, account, debit_cents, credit_cents FROM ledger_entries")),
        }
    finally:
        conn.close()


def replay(actions: list[dict], approver_id: str) -> dict:
    """Apply actions to a fresh rollout copy of the seed and return its snapshot.

    Actions that the tools refuse (e.g. an illegal transition in a wrong action list) simply leave
    the state unchanged, exactly as they would for an agent.
    """
    with tempfile.TemporaryDirectory() as tmp:
        db = rollout.prepare_db(Path(tmp) / "replay.sqlite", approver_id)
        for action in actions:
            solver.apply_action(db, action)
        return snapshot(db)


def score(final: dict | None, gold: dict, scope: list[str]) -> tuple[float, dict]:
    """Return (reward, metrics). Reward is all-or-nothing; metrics are for diagnosis only."""
    if not final:
        return 0.0, {"scope_correct": 0.0, "collateral_changes": 0.0, "po_mismatches": 0.0,
                     "ledger_mismatches": 0.0}
    in_scope = set(scope)
    scope_ok = sum(final["invoices"].get(i) == gold["invoices"][i] for i in scope)
    collateral = sum(final["invoices"].get(i) != v for i, v in gold["invoices"].items() if i not in in_scope)
    po_bad = sum(final["purchase_orders"].get(p) != v for p, v in gold["purchase_orders"].items())
    ledger_bad = _multiset_difference(final["ledger"], gold["ledger"])
    metrics = {
        "scope_correct": scope_ok / len(scope) if scope else 1.0,
        "collateral_changes": float(collateral),
        "po_mismatches": float(po_bad),
        "ledger_mismatches": float(ledger_bad),
    }
    return (1.0 if final == gold else 0.0), metrics


def _multiset_difference(a: list, b: list) -> int:
    """Size of the symmetric difference of two multisets of ledger lines."""
    ca, cb = Counter(map(tuple, a)), Counter(map(tuple, b))
    return sum(((ca - cb) + (cb - ca)).values())
