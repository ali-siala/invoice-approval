"""Reference solver (ap/solver.py): cross-checks against the seed's hidden outcomes and against the
independent test reference, scope and approver handling, and SPEC 8a comparability of its results."""

import json
import random
import shutil
import sqlite3
from collections import Counter

import pytest
from conftest import AL, BL, CFO, TL
from reference_policy import process

from ap import solver

ACTION_TO_OUTCOME = {
    "approve_invoice": "approved", "reject_invoice": "rejected", "hold_invoice": "on_hold",
    "escalate_invoice": "escalated",
}


def outcome(action) -> dict:
    """What a gold action does to the invoice's (status, reason_code, assigned_to)."""
    args = action["args"]
    escalated = action["tool"] == "escalate_invoice"
    return {
        "status": ACTION_TO_OUTCOME[action["tool"]],
        "reason_code": "APPROVAL_LIMIT" if escalated else args.get("reason_code"),
        "assigned_to": args.get("approver_id"),
    }


def comparable_state(db) -> dict:
    """The state SPEC 8a compares (what the Day-5 grader will do)."""
    conn = sqlite3.connect(db)
    state = {
        "invoices": {r[0]: r[1:] for r in conn.execute("SELECT id, status, reason_code, assigned_to FROM invoices")},
        "purchase_orders": {r[0]: r[1:] for r in conn.execute(
            "SELECT po_number, net_invoiced_cents, status FROM purchase_orders")},
        "ledger": Counter(conn.execute("SELECT invoice_id, account, debit_cents, credit_cents FROM ledger_entries")),
    }
    conn.close()
    return state


def _received(db) -> list[str]:
    conn = sqlite3.connect(db)
    ids = [r[0] for r in conn.execute("SELECT id FROM invoices WHERE status = 'received' ORDER BY id")]
    conn.close()
    return ids


def test_full_inbox_as_teamleiter_reproduces_hidden_outcomes(seeded_db):
    before = comparable_state(seeded_db)
    actions = solver.solve(seeded_db, _received(seeded_db), TL)
    assert comparable_state(seeded_db) == before  # the solver never touches the task database

    conn = sqlite3.connect(seeded_db)
    hidden = {r[0]: (json.loads(r[1]), r[2]) for r in conn.execute(
        "SELECT id, hidden_outcome, hidden_trap FROM invoices WHERE status = 'received'")}
    conn.close()
    assert [a["args"]["invoice_id"] for a in actions] != sorted(hidden)  # really reordered by date
    assert {a["args"]["invoice_id"] for a in actions} == set(hidden)
    mismatches = [(a["args"]["invoice_id"], hidden[a["args"]["invoice_id"]][1], outcome(a))
                  for a in actions if outcome(a) != hidden[a["args"]["invoice_id"]][0]]
    assert not mismatches, "\n".join(map(str, mismatches))


def test_solver_agrees_with_independent_reference_on_random_tasks(seed_template, tmp_path):
    """Random scopes and approvers: two independent policy implementations must give the same actions."""
    rng = random.Random(4)
    inbox = _received(seed_template)
    for n in range(40):
        scope = rng.sample(inbox, rng.choice([1, 1, 2, 3, 5, 8, 15, 40]))
        approver = rng.choice([TL, TL, AL, BL, CFO])
        gold = solver.solve(seed_template, scope, approver)

        copy = tmp_path / f"ref{n}.sqlite"
        shutil.copy(seed_template, copy)
        decisions = process(str(copy), scope, approver)
        assert [a["args"]["invoice_id"] for a in gold] == list(decisions), (scope, approver)
        assert [outcome(a) for a in gold] == [d.as_dict() for d in decisions.values()], (scope, approver)


def test_replaying_gold_actions_reaches_the_solved_state(seeded_db, seed_template, tmp_path):
    scope = _received(seeded_db)[:30]
    gold = solver.solve(seeded_db, scope, TL)
    for action in gold:
        assert not solver.apply_action(seeded_db, action).startswith("Error")
    other = tmp_path / "ref.sqlite"
    shutil.copy(seed_template, other)
    process(str(other), scope, TL)
    assert comparable_state(seeded_db) == comparable_state(str(other))


def test_scope_is_exact_and_input_order_irrelevant(mini):
    s = mini.supplier()
    po = mini.po(s, 400_000)
    first = mini.invoice(s, 250_000, po=po, received="2026-10-02")
    second = mini.invoice(s, 200_000, po=po, received="2026-10-07")
    # Alone, the second invoice fits the PO; after the first is approved, it doesn't.
    assert solver.solve(mini.path, [second], TL) == [
        {"tool": "approve_invoice", "args": {"invoice_id": second}}]
    both = solver.solve(mini.path, [second, first], TL)
    assert both == solver.solve(mini.path, [first, second, first], TL)
    assert both == [
        {"tool": "approve_invoice", "args": {"invoice_id": first}},
        {"tool": "hold_invoice", "args": {"invoice_id": second, "reason_code": "PO_EXCEEDED"}},
    ]
    assert mini.inv(first)["status"] == "received"  # the task database is untouched


def test_acting_approver_changes_gold(mini):
    s = mini.supplier()
    inv = mini.invoice(s, 1_000_000, po=mini.po(s, 2_000_000))  # 11900.00 gross
    assert solver.solve(mini.path, [inv], TL) == [
        {"tool": "escalate_invoice", "args": {"invoice_id": inv, "approver_id": AL}}]
    assert solver.solve(mini.path, [inv], AL) == [{"tool": "approve_invoice", "args": {"invoice_id": inv}}]
    big = mini.invoice(s, 5_000_000, po=mini.po(s, 6_000_000))  # 59500.00 gross
    assert solver.solve(mini.path, [big], AL) == [
        {"tool": "escalate_invoice", "args": {"invoice_id": big, "approver_id": BL}}]
    assert solver.solve(mini.path, [big], CFO) == [{"tool": "approve_invoice", "args": {"invoice_id": big}}]


def test_invalid_scope_or_approver(mini):
    inv = mini.invoice(mini.supplier(), 10_000)
    done = mini.invoice(mini.supplier(), 10_000, status="approved")
    with pytest.raises(ValueError, match="unknown invoice"):
        solver.solve(mini.path, ["ER-2026-99999"], TL)
    with pytest.raises(ValueError, match="not in status 'received'"):
        solver.solve(mini.path, [inv, done], TL)
    with pytest.raises(ValueError, match="unknown approver"):
        solver.solve(mini.path, [inv], "AP-999")
    assert solver.solve(mini.path, [], TL) == []


def test_spec_8a_state_ignores_order_ids_comments_and_audit(seed_template, tmp_path):
    """Independent approvals in a different order, with comments, give the same comparable state."""
    inbox = _received(seed_template)
    gold = solver.solve(seed_template, inbox, TL)
    independent, used_pos = [], set()  # approvals that don't share a PO, so their order can't matter
    conn = sqlite3.connect(seed_template)
    for a in (a for a in gold if a["tool"] == "approve_invoice"):
        po = conn.execute("SELECT po_number FROM invoices WHERE id = ?", (a["args"]["invoice_id"],)).fetchone()[0]
        if po is None or po not in used_pos:
            used_pos.add(po)
            independent.append(a)
        if len(independent) == 10:
            break
    conn.close()

    a_db, b_db = tmp_path / "a.sqlite", tmp_path / "b.sqlite"
    shutil.copy(seed_template, a_db)
    shutil.copy(seed_template, b_db)
    for action in independent:
        solver.apply_action(str(a_db), action)
    for action in reversed(independent):
        solver.apply_action(str(b_db), action)
    reject = next(a for a in gold if a["tool"] == "reject_invoice")
    solver.apply_action(str(a_db), reject)
    solver.apply_action(str(b_db), {**reject, "args": {**reject["args"], "comment": "Bitte prüfen"}})

    assert comparable_state(str(a_db)) == comparable_state(str(b_db))
    ids = "SELECT invoice_id FROM ledger_entries ORDER BY id"
    a_conn, b_conn = sqlite3.connect(a_db), sqlite3.connect(b_db)
    assert a_conn.execute(ids).fetchall() != b_conn.execute(ids).fetchall()  # row IDs really differ
    a_conn.close()
    b_conn.close()

    # Collateral damage does show up: one extra decision changes the state.
    extra = next(i for i in inbox if i not in {a["args"]["invoice_id"] for a in independent + [reject]})
    solver.apply_action(str(b_db), {"tool": "hold_invoice", "args": {"invoice_id": extra, "reason_code": "PO_EXCEEDED"}})
    assert comparable_state(str(a_db)) != comparable_state(str(b_db))
