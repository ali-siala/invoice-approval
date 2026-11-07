# invoice-approval

A multi-turn tool-use environment for training and evaluating LLM agents on German accounts payable
(*Kreditorenrechnungsprüfung*). The agent is a clerk at a fictional Mittelstand machine builder. It works through
supplier invoices with 11 tools on a SQLite database, under a one-page approval policy covering duplicates, purchase
orders and their budgets, VAT, blocked suppliers and approval limits. Each task is graded by comparing the final
database state, exactly, with a replay of the reference solver's gold actions. There is no LLM judge, and every
rollout gets a fresh copy of the frozen seed. It is built for lab engineers who want a deterministic, stateful,
long-horizon tool-use task with an objective reward. It runs on [verifiers](https://github.com/PrimeIntellect-ai/verifiers)
0.3.1.

**Details:** [environment README](environments/invoice_approval/README.md) (policy, tools, grading, tasks,
limitations, roadmap) · **Prime Hub:**
[ali-siala/invoice-approval](https://app.primeintellect.ai/dashboard/environments/ali-siala/invoice-approval)

## Results

60 tasks in 4 tiers; binary reward per task. `has_error` rollouts (infrastructure failures) were re-run and are not
counted.

| Tier | gpt-5.6-sol (3 attempts per task) | gpt-4.1-mini (1 attempt per task, smoke run) |
|---|---|---|
| single (20) | 60/60 (100 %) | 14/20 (70 %) |
| batch (20) | 56/60 (93 %) | 2/20 (10 %) |
| long-horizon (10) | 28/30 (93 %) | 0/10 (0 %) |
| hard (10) | 30/30 (100 %) | not run |

The gpt-4.1-mini numbers come from a single smoke run and are indicative only.

## Quick start

```bash
prime env install ali-siala/invoice-approval
vf-eval invoice-approval -m gpt-4.1-mini -b https://api.openai.com/v1 -k OPENAI_API_KEY \
    -n 10 -r 3 -c 4 -s -C stop_condition
```

For reasoning models such as gpt-5.6-sol, add `--api-client-type openai_responses`.

## Repository layout

```
environments/invoice_approval/ap/       the simulated company: seed, tools, policy, reference solver, grader
environments/invoice_approval/tasks/    tasks.jsonl, 60 work orders with gold and wrong action lists
environments/invoice_approval/scripts/  task generator (make_tasks.py) and an invoice viewer
environments/invoice_approval/tests/    pytest suite (513 tests): money, tools, policy, seed, tasks
SPEC.md                                 design spec, the source of truth
DOCKER.md                               clean-container check: install the wheel, run tests and a short eval
```

## License and author

Apache-2.0 (see [LICENSE](LICENSE)). Author: Ali Siala, GitHub [ali-siala](https://github.com/ali-siala).
