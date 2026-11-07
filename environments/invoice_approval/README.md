# invoice-approval

A multi-turn tool-use environment for German accounts payable (*Kreditorenrechnungsprüfung*). The agent is a clerk at
**Alpenwerk Maschinenbau GmbH**, a fictional Mittelstand machine builder in Munich. It works through incoming supplier
invoices against a written approval policy, using 11 tools on a SQLite database. It is graded on the database it
leaves behind, never on what it says.

- **Environment ID:** `invoice-approval` · **Version:** 0.1.1 · **verifiers:** `==0.3.1` (legacy API)
- **Type:** multi-turn, tool use, stateful · **Reward:** binary, exact final-state match
- **Tasks:** 60 work orders in 4 tiers, all on one frozen synthetic company
- **License:** Apache-2.0 · **Author:** Ali Siala (GitHub: [ali-siala](https://github.com/ali-siala))

## Why it is useful for agent training

- **Long, stateful tool use with consequences.** Every write changes the books. An approval uses up purchase-order
  budget, so one wrong or out-of-order decision changes what is correct for later invoices.
- **Precise rule following, not trivia.** The policy fits on one page and is in the system prompt. The difficulty
  lies in applying it exactly: half-cent VAT rounding, a 2 % tolerance that allows exactly the limit but not one cent
  more, duplicate detection after normalizing invoice numbers, and the rule that rejected invoices don't count as
  originals.
- **Objective, cheap grading.** There is no LLM judge. The reward is a deterministic comparison of database state, and
  each task's gold path comes from a reference solver.
- **Enterprise realism.** German VAT (19 % / 7 %), Kleinbetragsrechnungen (§ 33 UStDV), SKR03 ledger accounts,
  approval limits by role, valid IBAN and USt-IdNr check digits, plausible suppliers, POs and cost centers.

## What the agent sees

The system prompt contains the full policy (`ap/policy.md`). The user message is a work order, for example
(tier `hard`, 13 invoices):

> Controlling needs these invoices decided before the month-end close: ER-2026-00332, ER-2026-00493, ER-2026-00466,
> ER-2026-00473, ER-2026-00327, ER-2026-00380, ER-2026-00337, ER-2026-00346, ER-2026-00434, ER-2026-00334,
> ER-2026-00372, ER-2026-00343, ER-2026-00475. Process each of them.
>
> You are acting as Sabine Krüger (Abteilungsleiter, approver ID AP-117).

The list order is deliberately misleading. The policy requires processing by received date, so following the list
order fails the task.

### Policy in short

Rules are checked in this order, and the first rule that fails decides:

1. **Blocked supplier:** hold with `SUPPLIER_BLOCKED`.
2. **Duplicate:** reject with `DUPLICATE`. A duplicate is another invoice from the same supplier with the same
   normalized number that is received, approved, on hold or escalated. Rejected invoices don't count, and the earliest
   one received is the original.
3. **Purchase order:** invoices above EUR 250 gross need a PO. A missing PO is rejected with `MISSING_PO`. A PO that
   doesn't exist, belongs to another supplier or is closed is rejected with `INVALID_PO`. A Kleinbetrag (≤ EUR 250)
   may have no PO, but if it names one, the PO rules apply in full.
4. **PO amount:** the net already invoiced plus this invoice's net may be at most PO net × 1.02. Otherwise hold with
   `PO_EXCEEDED`.
5. **VAT:** the rate is 19 % (7 % for books and food), VAT is net × rate rounded half up to the cent, and gross is
   exactly net + VAT. Otherwise reject with `VAT_ERROR`.
6. **Approval limit:** above the acting approver's limit, escalate to the approver with the lowest sufficient limit.
   The limits are Teamleiter 5,000, Abteilungsleiter 25,000, Bereichsleiter 100,000 and CFO unlimited (EUR, gross).
7. **Otherwise approve.**

Several invoices are processed by received date, then by internal ID. Each invoice gets exactly one final decision,
and invoices outside the work order must not be touched.

### Tools

| Tool | Purpose |
|---|---|
| `list_invoices(status, supplier_id="", limit=50)` | Invoices by status, oldest first (limit 1–200) |
| `get_invoice(invoice_id)` | One invoice with all its fields |
| `search_suppliers(query)` | Search by name, city, supplier ID or USt-IdNr |
| `get_supplier(supplier_id)` | Supplier master data, including `is_blocked` |
| `get_purchase_order(po_number)` | PO net amount, net invoiced so far, status, order date |
| `list_approvers()` | All approvers and their limits |
| `whoami()` | The acting approver and their own limit |
| `approve_invoice(invoice_id)` | Approve: posts 3 ledger lines and adds the net to the PO |
| `reject_invoice(invoice_id, reason_code, comment="")` | Reject with a reject code |
| `hold_invoice(invoice_id, reason_code, comment="")` | Put on hold with a hold code |
| `escalate_invoice(invoice_id, approver_id, comment="")` | Escalate to another approver |

The tools apply no policy, so they let the agent make any wrong decision. They only enforce valid status transitions
(processing is allowed only from `received`). Errors come back as `Error: ...` strings, never as exceptions, and every
write is audited.

## Grading

The grading is state-based, by replay:

1. After the rollout, the agent's database is snapshotted.
2. The task's gold actions, produced by the reference solver in `ap/solver.py`, are replayed on a fresh copy of the
   seed.
3. The two states are compared exactly on these fields:
   - **invoices:** `status`, `reason_code` and `assigned_to` for **every** invoice, not just those in scope, so
     collateral damage fails the task
   - **purchase orders:** `net_invoiced_cents` and `status`
   - **ledger:** the multiset of `(invoice_id, account, debit_cents, credit_cents)`, ignoring row IDs and order

Excluded from the comparison: the audit log, free-text comments, ledger text and autoincrement IDs.

**Reward:** `state_matches` is 1.0 on an exact match and 0.0 otherwise. Zero-weight diagnostic metrics are logged
alongside: `scope_correct` (fraction of in-scope invoices right), `collateral_changes`, `po_mismatches`,
`ledger_mismatches`, plus verifiers' per-tool call counts.

Every task ships 3–6 plausible **wrong action lists**, mutations of the gold path such as approving the duplicate,
the wrong reason code, the wrong escalation target, the list order instead of the received order, or touching an
extra invoice. The test suite checks that the gold path scores 1.0 and every wrong list scores 0.0 (513 tests).

## Tasks

| Tier | Tasks | Invoices per task | What it tests |
|---|---|---|---|
| single | 20 | 1 | Every trap type and boundary case on its own (never a clean invoice) |
| batch | 20 | 3–10 | All received invoices of one supplier or one received date, or short lists; mixed traps |
| long-horizon | 10 | 15–23 | Long lists in a misleading order; PO budget chains where order decides the outcome; escalations and duplicates interacting |
| hard | 10 | 12–15 | Dense in arithmetic traps (gross ≠ net + VAT, wrong VAT, half-cent cases) and duplicates of already approved invoices; non-Teamleiter approvers |

Each record in `tasks/tasks.jsonl` holds the work order, the acting approver, the invoice scope, the gold actions,
the wrong action lists and the seed fingerprint.

## Determinism

- **Frozen seed.** One deterministic company is generated from seed 42: 60 suppliers, 150 POs and 200 invoices, of
  which 182 are received and the rest are history. Its SHA-256 fingerprint is
  `67c8d94ae76bb911d6e3baff0afc48d82cc58995b8d27c495cf60ef439df161b`. `load_environment` refuses to run if the tasks
  were generated from a different seed.
- **Fresh database per rollout.** Each rollout works on its own copy, which is deleted after scoring, so rollouts
  never share state.
- **Hidden columns stripped.** The seed carries the intended outcome of each invoice for testing. These columns are
  dropped from the rollout copy and the file is vacuumed, so no tool can leak them.
- The reward is a pure function of the final state. There is no judge and no sampling in grading.

## Results

Status: 28–29 Oct 2026, tasks and seed as shipped. `has_error` rollouts (API or infrastructure failures) were re-run
and are not counted. Every rollout ended with the model stopping on its own (`no_tools_called`), with none hitting
the turn limit.

| Tier | gpt-5.6-sol (3 attempts per task) | gpt-4.1-mini (1 attempt per task, smoke run) |
|---|---|---|
| single (20) | 60/60 (100 %) | 14/20 (70 %) |
| batch (20) | 56/60 (93 %) | 2/20 (10 %) |
| long-horizon (10) | 28/30 (93 %) | 0/10 (0 %) |
| hard (10) | 30/30 (100 %) | not run |

- **gpt-5.6-sol** ran with `--api-client-type openai_responses`. Its 6 failures are each one wrong decision on one
  invoice:
  - an approved gross mismatch, 3 times on the same invoice
  - one wrong VAT amount approved
  - a corrected resubmission rejected as a duplicate
  - a duplicate of an approved invoice held as PO_EXCEEDED
- **gpt-4.1-mini** numbers come from a single smoke run (`-r 1`). Treat them as indicative only; they are not a
  measured pass rate.

## Install and run

```bash
# from the Prime Environments Hub
prime env install ali-siala/invoice-approval

# or from a checkout of this repository
uv pip install ./environments/invoice_approval
```

Run an eval (OpenAI-compatible endpoint; the key is read from the named environment variable):

```bash
vf-eval invoice-approval -m gpt-4.1-mini -b https://api.openai.com/v1 -k OPENAI_API_KEY \
    -n 10 -r 3 -c 4 -s -C stop_condition
```

- **Reasoning models** (e.g. gpt-5.6-sol): add `--api-client-type openai_responses`. Their chat-completions route
  rejects function tools combined with reasoning, and every rollout fails before the first turn.
- **Concurrency:** keep it low (`-c 2` to `-c 4`) to stay under tokens-per-minute limits. A batch of long tasks
  sends large, growing contexts.
- **Environment arguments** (`-a '{...}'`):
  - `task_ids` (list of strings) restricts the dataset to those tasks, e.g. `-a '{"task_ids": ["long-1", "hard-3"]}'`.
  - `max_turns` (default 200) must be at least each selected task's gold path length (118 turns for the longest
    task); `load_environment` checks this.

Run the tests from `environments/invoice_approval/` with `uv run pytest`. The repository's `DOCKER.md` describes a
clean-container check: install from the built wheel, run the tests, then run a short eval.

## Limitations

- **Synthetic data, one company.** All 60 tasks share a single seeded database, and many tasks overlap in invoices.
  There is no separate train/eval split; if you train on some tasks and evaluate on others, split by task and expect
  overlap.
- **Near the ceiling for strong reasoning models.** gpt-5.6-sol passes 96–100 % per tier, so this version mainly
  separates mid-size models and checks reliability on long runs.
- **Sparse reward.** The reward is all-or-nothing per task. The partial-credit metrics are logged but not part of the
  reward.
- **Structured invoices only.** Invoices arrive as database fields, so there is no document reading or extraction.
- **Postal codes not checked against an official dataset.** IBAN and VAT ID check digits are valid, but ZIP-to-city
  pairs come from a hand-written list.
- **Small result samples.** The gpt-4.1-mini results come from one attempt per task.

## Roadmap (v2)

- **Reverse charge** (§ 13b UStG) for the Austrian and Dutch suppliers: no German VAT on the invoice, and the agent
  books input VAT and the tax liability itself.
- **Credit notes (Gutschriften):** negative invoices that reduce a PO's invoiced amount and must be matched to the
  original.
- **Goods receipts / three-way match:** approve only what was ordered *and* delivered. Partial deliveries become new
  hold reasons.
- **Invoices as PDF or free text:** the agent first extracts amounts, numbers and the PO from noisy documents.
- **Pagination in `list_invoices`:** large inboxes need several calls for scopes and duplicate checks.

Each of these, except pagination, changes the seed and the policy, and would be released as a new version.
