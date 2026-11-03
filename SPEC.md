# Environment #1 — German Invoice Approval (Kreditorenrechnungsprüfung) · Spec v0.3

Status: P0 Day 4 built (ap/ package, 98 tests passing) · Oct 2026 · verifiers 0.3.1 (legacy API, `import verifiers.legacy as vf`)

**Changes in v0.3 (Day 4 decisions):** a PO number that doesn't exist → `INVALID_PO` · only `received` invoices can be processed; all decisions are final in v1 · escalation sets `reason_code = APPROVAL_LIMIT` automatically · gold actions come from a reference solver per task, not from the seed's hidden column · AT/NL suppliers have POs but no invoices (reverse charge out of scope) · grader comparison rules defined (section 8a): audit_log, comments and row IDs excluded; ledger compared as a multiset.

**Changes in v0.2 (26 Oct 2026, from Ali's Day-3 review):** approval updates `net_invoiced` (only approval counts) · a PO given on a Kleinbetrag is validated · duplicate numbers are normalized; rejected originals don't count · all money in integer cents with Decimal half-up VAT · fixed processing order (received_date, then id) · escalation tie-breaker · Section 9: explicit `import verifiers.legacy as vf`, exact pin `verifiers==0.3.1` (including for `vf-init`), Windows vs WSL/Linux vf-eval note.

## 1. What it is
The agent plays an accounts-payable clerk (Sachbearbeiter Kreditorenbuchhaltung) at a fictional German mid-size company, **Alpenwerk Maschinenbau GmbH, München**. It works through incoming supplier invoices using tools (an API, like an ERP backend), following a written policy (Kreditorenrichtlinie). Each task gives it a work order; the grader checks the **database state after the rollout**, never the model's text.

## 2. Key design decisions
- **Tool-based, not browser-based (for env #1).** The "app" is a Python backend over SQLite exposed as tools. This is the dominant format for enterprise-workflow environments (tau-bench style), it grades exactly, and it's buildable in days. A browser UI over the same backend is a later upsell, not a v1 requirement.
- **One fresh database per rollout.** `setup_state` creates a temp SQLite file from a deterministic seed; the DB path is injected into tools as a hidden argument (`args_to_skip=["db"]`); a `@vf.cleanup` handler snapshots the final state and deletes the file. (Verified working in the wiring skeleton.)
- **Grading by replay.** Each task stores a list of *gold actions*. The grader replays them on a fresh copy of the same seed and compares the decision-relevant state with the agent's final DB (rules in 8a). Reward = 1.0 on exact match, else 0.0. Partial-credit numbers are logged as metrics only (for diagnosis), not as reward.
- **Gold actions come from a reference solver.** `solve(db, invoice_ids, approver_id)` applies the policy in the required order to exactly the invoices in a task's scope, as the task's acting approver, and returns the gold action list. Pilot tasks are additionally checked by hand.
- **Collateral damage counts.** Touching an invoice the task didn't ask about changes the final state → reward 0. This is realistic (an agent that "helpfully" processes extra invoices is a liability) and catches sloppy agents.
- **Exactly one correct end state per task.** Every rule below is written so that two correct agents always end in the same database state (hence the fixed processing order and tie-breakers).
- **Scope cut for v1:** no PDF reading, no browser, no line items, no reverse-charge or Skonto logic, no releasing held/escalated invoices. Those are difficulty tiers for v2.

## 3. Data model (SQLite)
**All money is stored as integer cents** (`*_cents` columns). **VAT rates are stored as whole percents** (19, 7). No floats anywhere in storage or logic.

**suppliers** — id (K-10001…), name, street, zip, city, region (Bundesland for DE/AT, province for NL), country (DE/AT/NL…), vat_id (USt-IdNr, e.g. DE123456789), iban, payment_terms_days, is_blocked (0/1), blocked_reason. AT/NL suppliers have POs but no invoices in v1 (their invoices would need reverse charge).
**purchase_orders** — po_number (SAP-style 45000xxxxx), supplier_id, order_date (always before the invoice date of every invoice citing the PO), cost_center, description, net_cents, net_invoiced_cents (running total of *approved* invoices), status (open/closed — set by the seed only; no automatic closing in v1)
**invoices** — id (ER-2026-xxxxx, internal), supplier_id, supplier_invoice_number, invoice_date, received_date, due_date, po_number (nullable), cost_center, category (goods / services / books / food), net_cents, vat_rate_pct, vat_cents, gross_cents, currency (EUR), status (received / approved / rejected / on_hold / escalated), reason_code, assigned_to, comment
**approvers** — id, name, role (Teamleiter / Abteilungsleiter / Bereichsleiter / CFO), approval_limit_gross_cents (500000 / 2500000 / 10000000 / NULL = unlimited); one approver per role in the seed
**ledger_entries** — id, invoice_id, account, debit_cents, credit_cents, text  (written automatically on approval)
**audit_log** — every write tool call: timestamp (real time), actor, action, invoice_id, details

**Status transitions:** only invoices in status `received` can be approved, rejected, held or escalated. Every decision is final in v1 — an on-hold or escalated invoice cannot be released or re-processed. Tools refuse any other transition with an error string.

**Effects of approval** (automatic, inside `approve_invoice`, atomic):
1. invoice status → approved
2. if the invoice has a PO: `purchase_orders.net_invoiced_cents += invoice.net_cents`
3. three SKR03-style ledger lines: expense account by category (net, debit) · Vorsteuer 1576 (19%) or 1571 (7%) (VAT, debit) · Verbindlichkeiten 1600 (gross, credit)

**Effects of escalation** (automatic, inside `escalate_invoice`): status → escalated · `assigned_to` = target approver · `reason_code = APPROVAL_LIMIT` (set by the tool, not chosen by the agent).

Rejected, on-hold and escalated invoices never change `net_invoiced_cents`.

**Money arithmetic** (one helper module, used by seed, tools, policy checks and tests):
- expected VAT: `Decimal(net_cents) * rate_pct / 100`, quantized to whole cents with `ROUND_HALF_UP`
- expected gross: `net_cents + vat_cents` (exact integer)
- Kleinbetrag: `gross_cents <= 25000`
- PO tolerance: `(net_invoiced_cents + net_cents) * 100 <= po_net_cents * 102` (pure integer comparison)
- tools present amounts to the model as strings like `"490.28"`, never as JSON floats

## 4. Policy (Kreditorenrichtlinie) — given to the agent in the system prompt
0. **Processing order:** when a work order covers several invoices, process them in order of `received_date`, then internal invoice ID (ascending). Order matters, because approvals change PO budgets. Only invoices in status `received` are processed; decisions are final.
1. **Duplicate:** an invoice is a duplicate if the same supplier has another invoice with the same *normalized* supplier invoice number (uppercase, all characters except letters and digits removed — so `MB-2026-0412`, `mb-2026-0412 ` and `MB2026-0412` match) in status received, approved, on_hold or escalated. The amount is irrelevant (invoice numbers must be unique, §14 UStG). The original is the one with the earliest `received_date`, then the lower internal ID; every later one → reject `DUPLICATE`. **Rejected invoices don't count:** a corrected resubmission of a rejected invoice under the same number is not a duplicate.
2. **Purchase order:** invoices above €250.00 gross need a valid PO. Missing → reject `MISSING_PO`. A PO number that doesn't exist, belongs to another supplier, or is closed → reject `INVALID_PO`.
3. **Kleinbetragsrechnung:** invoices ≤ €250.00 gross may have no PO (mirrors §33 UStDV). **If a PO is given anyway, rules 2 and 4 apply in full** and approval counts toward the PO.
4. **PO amount:** the PO's already-invoiced net plus this invoice's net may exceed the PO net by at most 2%. More → put on hold `PO_EXCEEDED`.
5. **VAT:** standard 19%; 7% only for categories books and food. `vat_cents` must equal the expected VAT (half-up to the cent) and `gross_cents` must equal net + VAT exactly. Wrong rate or arithmetic → reject `VAT_ERROR`.
6. **Blocked supplier:** → put on hold `SUPPLIER_BLOCKED` (never approve, never reject).
7. **Approval limit:** the agent acts as a named approver (given in the task). If gross > own limit → escalate to the approver with the lowest limit that is ≥ the gross amount (tie → lowest approver ID); the reason code `APPROVAL_LIMIT` is set automatically. Never approve above own limit.
8. **Check order** (first failing rule decides): blocked supplier → duplicate → PO (rules 2–3) → PO amount → VAT → approval limit → approve.

## 5. Agent tools
Read: `list_invoices(status, supplier_id?, limit?)` · `get_invoice(invoice_id)` · `search_suppliers(query)` · `get_supplier(supplier_id)` · `get_purchase_order(po_number)` · `list_approvers()` · `whoami()`
Write: `approve_invoice(invoice_id)` · `reject_invoice(invoice_id, reason_code, comment?)` · `hold_invoice(invoice_id, reason_code, comment?)` · `escalate_invoice(invoice_id, approver_id, comment?)`
Rules for tools: return JSON or clear one-line results; amounts as decimal strings; return a helpful error string (not an exception) for invalid input; refuse illegal state transitions (anything not from `received`); every write goes to audit_log. `list_invoices` returns results sorted by received_date, then id.

## 6. Seed data
Deterministic generator (`seed=42`): ~60 suppliers (realistic German Mittelstand names and cities, valid-format USt-IdNr and IBANs), ~150 POs, 200 invoices. Trap rates, approximately: 8% duplicates, 8% missing PO, 5% invalid PO, 6% PO exceeded, 8% VAT errors, 4% blocked suppliers, 15% above Teamleiter limit, 10% Kleinbetrag without PO (valid).

**Hidden intended outcome:** each invoice records the outcome it would get if the *whole inbox* were processed in the required order by the **Teamleiter**. It is used only by seed tests (and to cross-check the reference solver on that full scenario), never by tools, and never as gold for individual tasks — some outcomes (e.g. the second invoice of a PO pair) change when the scope or the acting approver changes.

Realism conventions: internal IDs follow scanning order, not received_date (so sorting by ID is not the required processing order) · a resent duplicate keeps the original's invoice and due dates (its due date can lie before its received date) · a blocked supplier's insolvency court is the Amtsgericht of the supplier's own city · larger cities use several real ZIP codes; ZIP, city and region always agree, and a German IBAN's bank code matches the region · a PO's order date lies 3–30 workdays before its earliest invoice; invoice, received and order dates are workdays · cost centers and PO descriptions follow the supplier's trade, and an invoice with a PO carries that PO's cost center · small invoices without a PO come mostly from office supplies, safety equipment, books and food; amounts above €25,000 only from trades that plausibly bill that much · supplier names come from a surname pool large enough that no surname repeats by accident (repeats only where deliberate, e.g. a search-ambiguity trap).

Required edge cases (at least one each): duplicate numbers differing only by case / spaces / hyphens · a resubmission after a VAT_ERROR rejection (not a duplicate) · a duplicate with a different amount (still a duplicate) · a Kleinbetrag with a valid PO (counts toward the PO) · a Kleinbetrag with another supplier's PO (`INVALID_PO`) · a PO number that doesn't exist (`INVALID_PO`) · VAT amounts exactly on a half-cent boundary · two invoices on one PO that only exceed it together (order decides) · an invoice exactly at the 2% tolerance (passes) and one cent above (holds) · PO budget chains of 4 invoices on one PO: one where a later invoice still fits after an earlier one was held (with IDs inverted so that processing by ID holds a different invoice), one where an escalated invoice consumes no budget so the acting approver changes which invoices fit, and one containing a duplicate whose original has the higher ID · a corrected resubmission whose copy has a lower ID than its rejected original.

## 7. Task tiers (60 tasks: 50 by Day 9, plus the hard tier)
- **Single (20):** "Process invoice ER-2026-00417."
- **Batch (20):** "Process all received invoices from supplier X" / "…received on 14 Oct" (3–10 invoices, mixed traps).
- **Long-horizon (10):** whole inbox for a cost center, or interacting rules (duplicate of a Kleinbetrag; escalation chains; PO exceeded only after an earlier invoice in the same batch is approved — order matters).
- **Hard (10):** dense lists of 12–15 invoices in a misleading order, acting as a non-Teamleiter approver (Abteilungsleiter, Bereichsleiter or CFO). Each mixes the arithmetic traps (gross ≠ net + VAT, wrong VAT amount, half-cent VAT) with a duplicate of an already-approved invoice and an order-dependent group (PO chain, resubmission, PO pair or Kleinbetrag pair). Generated after the first 50, which stay byte-identical; part of the default dataset.
Each task record: work order text, acting approver, invoice scope, gold actions (generated by the reference solver), and wrong action lists.
Difficulty target: gpt-4.1-mini should fail some batch and most long-horizon tasks. A task every model always passes teaches nothing.

## 8. Quality bar (every task)
- gold action list → replay → reward 1.0
- at least one plausible *wrong* action list (e.g. approves the duplicate, uses the wrong reason code, escalates to the wrong approver, processes in the wrong order, touches an extra invoice) → reward 0.0
- both live in pytest and run on every change
- money helper has its own tests, including half-cent boundaries and the exact-tolerance cases from section 6
- reference solver cross-check: solving the full inbox as Teamleiter reproduces every hidden intended outcome

## 8a. Grader comparison rules
Compared (agent's final DB vs. replayed gold DB):
- **invoices:** status, reason_code, assigned_to — for every invoice, not only those in scope (catches collateral damage)
- **purchase_orders:** net_invoiced_cents, status
- **ledger_entries:** as a **multiset** of (invoice_id, account, debit_cents, credit_cents) — row IDs and order ignored, since independent approvals in a different order produce different IDs but the same books

Excluded: audit_log (real timestamps), invoice `comment` (free text), all autoincrement row IDs, ledger `text`.

## 9. Packaging gotchas (verifiers 0.3.1 template)
- `requires-python = ">=3.11"` (template says 3.10)
- import as `import verifiers.legacy as vf` (explicit; don't rely on the top-level `verifiers` alias)
- pin exactly `verifiers==0.3.1` in pyproject.toml — 0.4.0 is out and restructures the API
- the pin is needed **even for `vf-init`**: run `uvx --from verifiers==0.3.1 vf-init <name>`. An unpinned `uvx --from verifiers` pulls 0.4 and breaks
- the template's `[tool.hatch.build] include` only lists the single env file; add the `ap/` package and `tasks/` folder or they won't ship to the Hub
- On Windows use `--disable-env-server`. Under WSL/Linux run vf-eval normally.
