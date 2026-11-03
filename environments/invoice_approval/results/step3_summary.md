# Step 3 results — gpt-5.6-sol vs gpt-4.1-mini on the 50 tasks

Seed fingerprint `67c8d94a…` · tasks from `scripts/make_tasks.py` (commit 9377ffd) · run 28 Oct 2026.

- **gpt-5.6-sol:** `-r 3` per task, `-c 2`, `--api-client-type openai_responses`, `-s -C stop_condition`, one run per tier.
- **gpt-4.1-mini:** the Step 2 smoke test (`-r 1`); the full run at `-c 10` plus a re-run of the 6 tasks that hit the
  OpenAI tokens-per-minute limit (`-c 2`, `task_ids`). No new gpt-4.1-mini runs.
- `has_error` rollouts are infrastructure failures and were re-run, never counted as model failures.

## Pass rate per tier

| Tier | gpt-5.6-sol (`-r 3`) | gpt-4.1-mini (`-r 1`) |
|---|---|---|
| single (20 tasks) | 60/60 (100%) | 14/20 (70%) |
| batch (20 tasks) | 56/60 (93%) | 2/20 (10%) |
| long-horizon (10 tasks) | 28/30 (93%) | 0/10 (0%) |
| **all 50** | 144/150 (96%) | 16/50 (32%) |
| hard (10 tasks, added later) | 30/30 (100%) | not run |

## Stop reasons (merged results)

| Model | no_tools_called | max_turns_reached | has_error (re-run, not counted) |
|---|---|---|---|
| gpt-5.6-sol | 180 (150 + 30 hard) | 0 | 0 |
| gpt-4.1-mini | 50 | 0 | 6 (rate limit; plus 7 in an earlier, superseded full run) |

## gpt-5.6-sol per task (passes out of 3)

No task scored 0/3. Tasks below 3/3:

- `batch-supplier-K-10009`: 2/3
- `batch-supplier-K-10030`: 2/3
- `batch-received-2026-10-01`: 1/3
- `long-7`: 2/3
- `long-9`: 2/3

## Every failed gpt-5.6-sol rollout (all are model mistakes)

| Task | What went wrong | Model or environment? |
|---|---|---|
| `batch-supplier-K-10009` (failed 1 of 3) | Rejected the corrected resubmission ER-2026-00436 as DUPLICATE right after rejecting its VAT-error original; policy rule 1 says rejected invoices don't count. | Model |
| `batch-supplier-K-10030` (failed 1 of 3) | Approved ER-2026-00343, whose gross (908.20) is not net + VAT (758.25 + 144.07 = 902.32). | Model |
| `batch-received-2026-10-01` (failed 2 of 3) | The same invoice ER-2026-00343 approved despite the gross mismatch, both times. | Model |
| `long-7` (failed 1 of 3) | Approved ER-2026-00475 with VAT 125.50 instead of 125.94 (19 % of 662.84). | Model |
| `long-9` (failed 1 of 3) | Held ER-2026-00372 as PO_EXCEEDED; it repeats the number of ER-2026-00316, approved in September (visible via `list_invoices(status="approved")`), so DUPLICATE comes first. | Model |

All failures processed the invoices in the correct order and touched nothing outside the scope; each is one
wrong decision on one invoice. The data and the policy text were checked for each case.

## gpt-4.1-mini (smoke test, `-r 1`)

Single 14/20, batch 2/20, long-horizon 0/10. Its failed rollouts were not analysed in Step 3 (rewards were not the
purpose of the smoke test).


## Hard tier (one bounded round, 29 Oct 2026)

10 extra tasks from `make_tasks.py` (the 50 above unchanged), 12–15 invoices each, listed in a misleading order,
acting as Abteilungsleiter, Bereichsleiter or CFO. Each contains at least one gross ≠ net + VAT invoice, one wrong VAT
amount, one half-cent VAT case and one duplicate of an already approved invoice — the mistakes gpt-5.6-sol made above —
plus a PO chain, resubmission pair, PO pair or Kleinbetrag pair. No seed change.

gpt-5.6-sol (`-r 3`, `-c 2`, Responses API): **30/30**, all `no_tools_called`, no `has_error`, no failures.
Concentrating these traps did not make them harder: the Step 3 mistakes look like occasional lapses in larger, mixed
batches rather than a systematic blind spot the current rule set can target.
