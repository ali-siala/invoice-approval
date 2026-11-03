"""Generate tasks/tasks.jsonl (SPEC section 7): 20 single, 20 batch and 10 long-horizon tasks, plus a
"hard" tier of 10 lists (10-15 invoices) dense in what gpt-5.6-sol got wrong in Step 3.

Each task records its work order, acting approver, invoice scope, gold actions from the reference
solver, the seed fingerprint and at least 3 wrong action lists. Wrong lists are mutations of the gold
list (wrong reason code, wrong escalation target, approving a trap, processing in the wrong order,
one extra out-of-scope invoice, one in-scope invoice skipped); only mutations that change the
SPEC 8a end state are kept.

Rerun after any intentional seed change (see test_seed_is_frozen):  python scripts/make_tasks.py
"""

import json
import random
import sqlite3
import sys
import tempfile
from collections import defaultdict
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ap import grader, rollout, schema, seed, solver  # noqa: E402

OUT = Path(__file__).resolve().parent.parent / "tasks" / "tasks.jsonl"
TL, AL, BL, CFO = "AP-203", "AP-117", "AP-052", "AP-008"
NEXT_APPROVER = {AL: BL, BL: CFO, CFO: BL}  # a plausible wrong escalation target for each correct one
ABOVE = {TL: AL, AL: BL, BL: CFO}  # escalating although the amount is within one's own limit
# The tempting wrong decision for a valid invoice, by the trap it was built as.
TEMPTING_REJECTION = {
    "resubmission_after_reject": ("reject_invoice", "DUPLICATE"), "resubmission_in_batch": ("reject_invoice", "DUPLICATE"),
    "resubmission_lower_id": ("reject_invoice", "DUPLICATE"), "po_exact_tolerance": ("hold_invoice", "PO_EXCEEDED"),
    "po_chain_fits_after_hold": ("hold_invoice", "PO_EXCEEDED"), "po_chain_after_escalation": ("hold_invoice", "PO_EXCEEDED"),
    "po_chain_after_duplicate": ("hold_invoice", "PO_EXCEEDED"), "kleinbetrag_pair_first": ("hold_invoice", "PO_EXCEEDED"),
    "vat_half_cent_correct": ("reject_invoice", "VAT_ERROR"), "kleinbetrag_exact_limit": ("reject_invoice", "MISSING_PO"),
    "kleinbetrag_no_po": ("reject_invoice", "MISSING_PO"), "kleinbetrag_with_po": ("reject_invoice", "MISSING_PO"),
    "dup_original": ("reject_invoice", "DUPLICATE"), "po_chain_dup_original": ("reject_invoice", "DUPLICATE"),
}
MIN_WRONG, MAX_WRONG = 3, 6

LIST_FRAMES = [
    "The Montage team asks us to clear these invoices from their clarification list today: {ids}. "
    "Process all of them.",
    "Controlling needs these invoices decided before the month-end close: {ids}. Process each of them.",
    "Please work through this list from the AP inbox review: {ids}.",
    "Purchasing forwarded these invoices because the suppliers are asking about payment: {ids}. Process them.",
    "Audit picked these invoices for a second look; process them now: {ids}.",
]


# ---------------------------------------------------------------- seed access


class Seed:
    def __init__(self, db: str):
        self.db = db
        conn = sqlite3.connect(db)
        conn.row_factory = sqlite3.Row
        self.inv = {r["id"]: dict(r) for r in conn.execute(
            "SELECT i.*, s.name AS supplier_name FROM invoices i JOIN suppliers s ON s.id = i.supplier_id "
            "WHERE i.status = 'received'")}
        self.approver_names = {r["id"]: (r["name"], r["role"]) for r in conn.execute("SELECT * FROM approvers")}
        conn.close()

    def trap(self, label: str) -> list[str]:
        return sorted(i for i, r in self.inv.items() if r["hidden_trap"] == label)

    def by_received(self, ids) -> list[str]:
        return sorted(ids, key=lambda i: (self.inv[i]["received_date"], i))

    def on_po_of(self, invoice_id: str) -> list[str]:
        po = self.inv[invoice_id]["po_number"]
        return self.by_received(i for i, r in self.inv.items() if r["po_number"] == po)

    def same_number(self, invoice_id: str) -> list[str]:
        r0 = self.inv[invoice_id]
        key = (r0["supplier_id"], _normalize(r0["supplier_invoice_number"]))
        return self.by_received(i for i, r in self.inv.items()
                                if (r["supplier_id"], _normalize(r["supplier_invoice_number"])) == key)


def _normalize(number: str) -> str:
    return "".join(ch for ch in number.upper() if ch.isalnum())


def interacting_groups(s: Seed) -> dict[str, list[str]]:
    """Invoices whose outcomes depend on each other (same PO budget or same invoice number)."""
    groups = {
        "chain_A": s.on_po_of(s.trap("po_chain_held")[0]),
        "chain_B": s.on_po_of(s.trap("po_chain_escalated")[0]),
        "chain_C": s.on_po_of(s.trap("po_chain_dup_original")[0]),
        "resub_batch": s.same_number(s.trap("resubmission_in_batch")[0]),
        "resub_lower_id": s.same_number(s.trap("resubmission_lower_id")[0]),
        "po_pair": s.on_po_of(s.trap("po_pair_second")[0]),
        "klein_pair": s.on_po_of(s.trap("kleinbetrag_pair_second")[0]),
    }
    for label in ("dup_case", "dup_space", "dup_hyphen", "dup_different_amount", "dup_same_day", "dup_kleinbetrag",
                  "dup_with_vat_error", "dup_triple"):
        groups[label] = s.same_number(s.trap(label)[0])
    for n, dup in enumerate(s.trap("dup_exact"), 1):
        groups[f"dup_exact_{n}"] = s.same_number(dup)
    return groups


# ---------------------------------------------------------------- wrong action lists


def process_in_order(db: str, ids: list[str], approver_id: str) -> list[dict]:
    """The actions an agent produces if it processes `ids` in exactly this order (not rule 0)."""
    with tempfile.TemporaryDirectory() as tmp:
        work = rollout.prepare_db(Path(tmp) / "work.sqlite", approver_id, template=db)
        actions = []
        for invoice_id in ids:
            action = solver.solve(work, [invoice_id], approver_id)[0]
            solver.apply_action(work, action)
            actions.append(action)
    return actions


def _act(tool: str, invoice_id: str, **args) -> dict:
    return {"tool": tool, "args": {"invoice_id": invoice_id, **args}}


def _swap(gold: list[dict], index: int, action: dict) -> list[dict]:
    return gold[:index] + [action] + gold[index + 1:]


def candidate_mutations(s: Seed, task: dict, listed: list[str] | None, rng: random.Random) -> list[tuple[str, list]]:
    gold, scope, approver = task["gold_actions"], task["scope"], task["approver_id"]
    out = []
    for i, a in enumerate(gold):
        iid, tool, code = a["args"]["invoice_id"], a["tool"], a["args"].get("reason_code")
        if tool == "reject_invoice" and not any("reason code" in m for m, _ in out):
            other = {"DUPLICATE": "INVALID_PO", "MISSING_PO": "INVALID_PO", "INVALID_PO": "MISSING_PO",
                     "VAT_ERROR": "INVALID_PO"}[code]
            out.append((f"rejects {iid} with reason code {other} instead of {code}",
                        _swap(gold, i, _act(tool, iid, reason_code=other))))
        if tool == "hold_invoice" and not any("hold reason" in m for m, _ in out):
            other = "SUPPLIER_BLOCKED" if code == "PO_EXCEEDED" else "PO_EXCEEDED"
            out.append((f"holds {iid} with hold reason {other} instead of {code}",
                        _swap(gold, i, _act(tool, iid, reason_code=other))))
        if tool == "escalate_invoice" and not any("escalates" in m for m, _ in out):
            wrong = NEXT_APPROVER[a["args"]["approver_id"]]
            out.append((f"escalates {iid} to {wrong} instead of {a['args']['approver_id']}",
                        _swap(gold, i, _act(tool, iid, approver_id=wrong))))
        if tool == "approve_invoice" and not any("instead of approving" in m for m, _ in out):
            wrong_tool, wrong_code = TEMPTING_REJECTION.get(s.inv[iid]["hidden_trap"], (None, None))
            if wrong_tool:
                out.append((f"{wrong_tool.split('_')[0]}s {iid} as {wrong_code} instead of approving it",
                            _swap(gold, i, _act(wrong_tool, iid, reason_code=wrong_code))))
        if tool == "approve_invoice" and approver in ABOVE and not any("own limit" in m for m, _ in out):
            out.append((f"escalates {iid} to {ABOVE[approver]} although it is within its own limit",
                        _swap(gold, i, _act("escalate_invoice", iid, approver_id=ABOVE[approver]))))
        if tool != "approve_invoice" and not any("approves the trap" in m for m, _ in out):
            out.append((f"approves the trap invoice {iid} instead of {tool.split('_')[0]}ing it",
                        _swap(gold, i, _act("approve_invoice", iid))))
    if listed and listed != s.by_received(scope):
        out.append(("processes the invoices in the order the work order lists them, not by received date",
                    process_in_order(s.db, listed, approver)))
    if len(scope) > 1 and sorted(scope) != s.by_received(scope):
        out.append(("processes the invoices by internal ID instead of by received date",
                    process_in_order(s.db, sorted(scope), approver)))
    supplier = s.inv[scope[0]]["supplier_id"]
    outside = [i for i, r in s.inv.items() if i not in scope and r["supplier_id"] == supplier] or \
              [i for i in s.inv if i not in scope]
    extra = sorted(outside)[rng.randrange(len(outside))]
    out.append((f"also processes {extra}, which is not in the work order",
                gold + solver.solve(s.db, [extra], approver)))
    skip = gold[len(gold) // 2]["args"]["invoice_id"]
    out.append((f"skips {skip}", [a for a in gold if a["args"]["invoice_id"] != skip]))
    return out


def wrong_action_lists(s: Seed, task: dict, listed: list[str] | None, rng: random.Random) -> list[dict]:
    """Keep mutations whose end state differs from gold and from each other."""
    seen = [json.dumps(grader.replay(task["gold_actions"], task["approver_id"]), sort_keys=True)]
    kept = []
    for mistake, actions in candidate_mutations(s, task, listed, rng):
        state = json.dumps(grader.replay(actions, task["approver_id"]), sort_keys=True)
        if state not in seen:
            seen.append(state)
            kept.append({"mistake": mistake, "actions": actions})
    # Order mistakes are the most informative for multi-invoice tasks; keep them first, cap the rest.
    kept.sort(key=lambda w: "order" not in w["mistake"] and "internal ID" not in w["mistake"])
    if len(kept) < MIN_WRONG:
        raise RuntimeError(f"{task['task_id']}: only {len(kept)} distinct wrong action lists")
    return kept[:MAX_WRONG]


# ---------------------------------------------------------------- task builders


def build_tasks() -> list[dict]:
    db = rollout.seed_template()
    s = Seed(db)
    rng = random.Random(2026)
    fingerprint = seed.fingerprint(db)
    tasks = []

    def add(task_id, tier, work_order, approver_id, scope, listed=None):
        name, role = s.approver_names[approver_id]
        task = {
            "task_id": task_id,
            "tier": tier,
            "work_order": f"{work_order}\n\nYou are acting as {name} ({role}, approver ID {approver_id}).",
            "approver_id": approver_id,
            "scope": sorted(scope),
            "gold_actions": solver.solve(db, scope, approver_id),
            "seed_fingerprint": fingerprint,
        }
        task["wrong_action_lists"] = wrong_action_lists(s, task, listed, rng)
        tasks.append(task)

    # ---- 20 single tasks: every one a trap or a boundary case; approvers vary.
    gross = {i: r["gross_cents"] for i, r in s.inv.items()}
    boundary = {gross[i]: i for i in s.trap("above_limit_boundary")}
    singles = [
        ("dup_case", s.trap("dup_case")[0], TL), ("dup_space", s.trap("dup_space")[0], AL),
        ("dup_hyphen", s.trap("dup_hyphen")[0], BL), ("dup_different_amount", s.trap("dup_different_amount")[0], CFO),
        ("dup_of_paid", s.trap("dup_of_paid")[0], AL),
        ("resubmission_after_reject", s.trap("resubmission_after_reject")[0], TL),
        ("kleinbetrag_with_po", s.trap("kleinbetrag_with_po")[0], BL),
        ("kleinbetrag_other_supplier_po", s.trap("kleinbetrag_other_supplier_po")[0], TL),
        ("po_does_not_exist", s.trap("po_does_not_exist")[0], AL), ("po_closed", s.trap("po_closed")[0], CFO),
        ("vat_half_cent_rounded_down", s.trap("vat_half_cent_rounded_down")[0], TL),
        ("vat_half_cent_correct", s.trap("vat_half_cent_correct")[0], AL),
        ("po_exact_tolerance", s.trap("po_exact_tolerance")[0], BL),
        ("po_one_cent_over", s.trap("po_one_cent_over")[0], TL),
        ("blocked_with_vat_error", s.trap("supplier_blocked_and_vat_error")[0], CFO),
        ("missing_po_250_01", s.trap("missing_po_one_cent_over_kleinbetrag")[0], AL),
        ("kleinbetrag_exact_250_00", s.trap("kleinbetrag_exact_limit")[0], TL),
        ("gross_5000_01_as_teamleiter", boundary[500_001], TL),
        ("gross_25000_00_as_abteilungsleiter", boundary[2_500_000], AL),
        ("gross_25000_01_as_abteilungsleiter", boundary[2_500_001], AL),
    ]
    for name, invoice_id, approver in singles:
        add(f"single-{name}", "single", f"Process invoice {invoice_id}.", approver, [invoice_id])

    groups = interacting_groups(s)
    grouped = {i for ids in groups.values() for i in ids}
    singletons = sorted(i for i, r in s.inv.items() if i not in grouped)
    traps = [i for i in singletons if not s.inv[i]["hidden_trap"].startswith("clean")]
    cleans = [i for i in singletons if s.inv[i]["hidden_trap"].startswith("clean")]

    def misleading(ids: list[str], style: str) -> list[str]:
        """List order for the work order: never rule 0, and later-received invoices tend to come first."""
        if style == "received_desc":
            return s.by_received(ids)[::-1]
        if style == "id_desc":
            return sorted(ids, reverse=True)
        if style == "gross_desc":
            return sorted(ids, key=lambda i: (-gross[i], i))
        if style == "supplier":  # grouped by supplier name, newest first within each supplier
            return sorted(ids, key=lambda i: (s.inv[i]["supplier_name"], [-ord(c) for c in s.inv[i]["received_date"]], i))
        shuffled = ids[:]
        rng.shuffle(shuffled)
        return shuffled

    def list_task(task_id, tier, group_names, approver, size, style, frame, must=()):
        scope = [i for g in group_names for i in groups[g]] + list(must)
        pool = [i for i in traps if i not in scope]
        fill = rng.sample(pool, max(0, size - len(scope) - 1)) + rng.sample(cleans, 1 if size > len(scope) else 0)
        scope = scope + fill
        for candidate in [style, "received_desc", "id_desc", "gross_desc", "supplier", "shuffle"]:
            listed = misleading(scope, candidate)
            if listed != s.by_received(scope) and grader.replay(process_in_order(db, listed, approver), approver) != \
                    grader.replay(solver.solve(db, scope, approver), approver):
                break  # processing in the listed order really ends in a different state
        else:
            raise RuntimeError(f"{task_id}: no misleading order changes the outcome")
        add(task_id, tier, LIST_FRAMES[frame % len(LIST_FRAMES)].format(ids=", ".join(listed)), approver, scope,
            listed=listed)

    # ---- 20 batch tasks: 12 by supplier, 3 by received date, 5 short lists around interacting groups.
    by_supplier = defaultdict(list)
    for i, r in s.inv.items():
        by_supplier[r["supplier_id"]].append(i)

    def interest(supplier_id):
        ids = by_supplier[supplier_id]
        outcomes = {(a["tool"], a["args"].get("reason_code")) for a in solver.solve(db, ids, TL)}
        return (sum(bool(set(ids) & set(g)) for g in groups.values()), len(outcomes), supplier_id)

    candidates = [sid for sid, ids in by_supplier.items() if 3 <= len(ids) <= 10]
    chain_b_supplier = s.inv[groups["chain_B"][0]]["supplier_id"]
    chosen = sorted(candidates, key=interest, reverse=True)[:12]
    if chain_b_supplier not in chosen:
        chosen[-1] = chain_b_supplier
    rotation = [TL, AL, TL, BL, CFO, TL, AL, BL, TL, AL, CFO, TL]
    for n, sid in enumerate(sorted(chosen)):
        approver = AL if sid == chain_b_supplier else rotation[n]
        name = s.inv[by_supplier[sid][0]]["supplier_name"]
        add(f"batch-supplier-{sid}", "batch", f"Process all received invoices from {name} ({sid}).", approver,
            by_supplier[sid])

    by_day = defaultdict(list)
    for i, r in s.inv.items():
        by_day[r["received_date"]].append(i)
    days = sorted(d for d, ids in by_day.items() if 4 <= len(ids) <= 10)[:3]
    for d, approver in zip(days, [BL, TL, AL]):
        nice = date.fromisoformat(d).strftime("%-d %B %Y")
        add(f"batch-received-{d}", "batch",
            f"Process all invoices received on {nice} that are still in status received.", approver, by_day[d])

    small_lists = [
        (["chain_A"], TL, 6, "id_desc"), (["chain_B"], AL, 6, "received_desc"),
        (["chain_C", "resub_lower_id"], TL, 8, "gross_desc"), (["resub_batch", "po_pair"], BL, 7, "received_desc"),
        (["klein_pair", "dup_same_day"], CFO, 6, "id_desc"),
    ]
    for n, (names, approver, size, style) in enumerate(small_lists):
        list_task(f"batch-list-{n + 1}", "batch", names, approver, size, style, n)

    # ---- 10 long-horizon tasks: 15-25 invoices listed in a misleading order; every group appears >= 2 times.
    long_plan = [
        (["chain_A", "resub_lower_id", "dup_case", "dup_kleinbetrag"], TL),
        (["chain_B", "po_pair", "dup_space", "dup_triple"], AL),
        (["chain_C", "resub_batch", "klein_pair", "dup_hyphen"], TL),
        (["chain_A", "dup_different_amount", "dup_same_day", "resub_lower_id"], BL),
        (["chain_B", "chain_C", "dup_with_vat_error", "dup_exact_1"], TL),
        (["chain_A", "klein_pair", "dup_exact_2", "dup_case"], CFO),
        (["chain_C", "resub_batch", "po_pair", "dup_hyphen"], AL),
        (["chain_B", "dup_triple", "dup_space", "dup_kleinbetrag"], TL),
        (["resub_lower_id", "resub_batch", "dup_different_amount", "dup_with_vat_error"], BL),
        (["chain_A", "chain_B", "dup_same_day", "dup_exact_1", "dup_exact_2"], AL),
    ]
    styles = ["received_desc", "id_desc", "gross_desc", "supplier", "shuffle"]
    for n, (names, approver) in enumerate(long_plan):
        list_task(f"long-{n + 1}", "long_horizon", names, approver, rng.randint(15, 25), styles[n % len(styles)], n)

    # ---- 10 hard tasks (added after Step 3; generated last so the 50 above stay unchanged): 10-15 invoices,
    #      dense in gross != net + VAT, wrong VAT amounts (incl. half-cent cases) and duplicates of invoices that
    #      were already approved, mixed with an order-dependent group and a non-Teamleiter approver.
    weak = {
        "gross": s.trap("vat_gross_mismatch"),
        "vat_amount": s.trap("vat_amount_wrong") + s.trap("vat_amount_wrong_kleinbetrag"),
        "half_cent": s.trap("vat_half_cent_rounded_down") + s.trap("vat_half_cent_correct"),
        "dup_of_paid": s.trap("dup_of_paid"),
    }
    weak_all = sorted(i for ids in weak.values() for i in ids)
    hard_plan = [
        (["chain_A", "resub_lower_id"], AL), (["chain_B", "resub_batch"], BL), (["chain_C", "resub_lower_id"], CFO),
        (["chain_B", "po_pair"], AL), (["chain_A", "klein_pair"], BL), (["resub_batch", "resub_lower_id"], AL),
        (["chain_A", "resub_batch"], CFO), (["chain_C", "po_pair"], BL), (["chain_B", "resub_lower_id"], AL),
        (["chain_B", "klein_pair"], CFO),
    ]
    for n, (names, approver) in enumerate(hard_plan):
        must = [rng.choice(ids) for ids in weak.values()]  # one of each kind
        must += rng.sample([i for i in weak_all if i not in must], 2)
        size = rng.randint(10, 15)
        list_task(f"hard-{n + 1}", "hard", names, approver, size, styles[(n + 2) % len(styles)], n + 1, must=must)
    return tasks


def coverage(tasks: list[dict]) -> dict[str, int]:
    """Number of tasks covering each trap type (by gold outcome) and each SPEC 6 edge case (label + outcome)."""
    s = Seed(rollout.seed_template())
    counts = defaultdict(int)
    for t in tasks:
        gold = {a["args"]["invoice_id"]: a for a in t["gold_actions"]}
        hit = set()
        for iid, a in gold.items():
            r, code = s.inv[iid], a["args"].get("reason_code")
            label = r["hidden_trap"]
            if a["tool"] == "escalate_invoice":
                hit.add("trap: escalation")
            elif code:
                hit.add(f"trap: {code}")
            elif not r["po_number"] and r["gross_cents"] <= 25_000:
                hit.add("trap: kleinbetrag without PO")
            for edge, (labels, tool) in EDGE_CASES.items():
                if label in labels and a["tool"] == tool:
                    hit.add(f"edge: {edge}")
        for edge, (members, needs_hold) in GROUP_EDGES.items():
            group = members(s)
            if set(group) <= set(gold) and (not needs_hold or any(gold[i]["tool"] == "hold_invoice" for i in group)):
                hit.add(f"edge: {edge}")
        for h in hit:
            counts[h] += 1
    return dict(sorted(counts.items()))


EDGE_CASES = {  # edge case -> (trap labels, gold tool that shows it was handled)
    "duplicate differing by case": ({"dup_case"}, "reject_invoice"),
    "duplicate differing by spaces": ({"dup_space"}, "reject_invoice"),
    "duplicate differing by hyphens": ({"dup_hyphen"}, "reject_invoice"),
    "duplicate with different amount": ({"dup_different_amount"}, "reject_invoice"),
    "resubmission after VAT_ERROR": ({"resubmission_after_reject", "resubmission_in_batch", "resubmission_lower_id"},
                                     "approve_invoice"),
    "Kleinbetrag with valid PO": ({"kleinbetrag_with_po", "kleinbetrag_pair_first"}, "approve_invoice"),
    "Kleinbetrag with other supplier's PO": ({"kleinbetrag_other_supplier_po"}, "reject_invoice"),
    "PO number does not exist": ({"po_does_not_exist"}, "reject_invoice"),
    "VAT half-cent rounded up correctly": ({"vat_half_cent_correct"}, "approve_invoice"),
    "VAT half-cent rounded down": ({"vat_half_cent_rounded_down"}, "reject_invoice"),
    "exactly at 2% tolerance": ({"po_exact_tolerance"}, "approve_invoice"),
    "one cent above tolerance": ({"po_one_cent_over"}, "hold_invoice"),
}
GROUP_EDGES = {  # edge cases that need the whole group in scope -> (members, a hold must be part of the gold)
    "two invoices exceed a PO only together": (lambda s: s.on_po_of(s.trap("po_pair_second")[0]), True),
    "Kleinbetrag pair exceeds a PO together": (lambda s: s.on_po_of(s.trap("kleinbetrag_pair_second")[0]), True),
    "chain: later invoice fits after a hold": (lambda s: s.on_po_of(s.trap("po_chain_held")[0]), True),
    "chain with escalation freeing budget": (lambda s: s.on_po_of(s.trap("po_chain_escalated")[0]), False),
    "chain with duplicate": (lambda s: s.on_po_of(s.trap("po_chain_dup_original")[0]), False),
    "resubmission with lower ID": (lambda s: s.same_number(s.trap("resubmission_lower_id")[0]), False),
}


def main() -> None:
    tasks = build_tasks()
    OUT.parent.mkdir(exist_ok=True)
    OUT.write_text("".join(json.dumps(t, ensure_ascii=False) + "\n" for t in tasks), encoding="utf-8")
    tiers = defaultdict(list)
    for t in tasks:
        tiers[t["tier"]].append(len(t["scope"]))
    for tier, sizes in tiers.items():
        print(f"{tier}: {len(sizes)} tasks, {min(sizes)}-{max(sizes)} invoices")
    print("approvers:", {a: sum(t["approver_id"] == a for t in tasks) for a in (TL, AL, BL, CFO)})
    print("wrong lists per task:", sorted({len(t["wrong_action_lists"]) for t in tasks}))
    for name, n in coverage(tasks).items():
        print(f"  {n:3d}  {name}")


if __name__ == "__main__":
    main()
