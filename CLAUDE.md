# CLAUDE.md — standing rules for this repo

## How to report
- Don't propose follow-ups one at a time. Before finishing any task, do a full review pass, then report suggestions once, at the end, as a single numbered list titled **"Open suggestions"**. For each item: what, why, and whether it changes the seed fingerprint, a policy rule, or nothing.
- Only report suggestions that block the current task's goal, or that would change the seed fingerprint or a policy rule. Write everything else directly into BACKLOG.md without asking, and mention only the count at the end.

## Conventions
- verifiers is pinned `==0.3.1`; import it as `import verifiers.legacy as vf`.
- Use the installed source in `environments/invoice_approval/.venv/lib/python3.13/site-packages/verifiers/legacy/` as the API reference — not GitHub main or online docs (those describe 0.4/v1).
- Run evals with `uv run --no-sync`. Command pattern:
  `uv run --no-sync vf-eval invoice-approval -m <model> -b https://api.openai.com/v1 -k OPENAI_API_KEY -n <n> -r <r> -s -C stop_condition -d`
- For gpt-5.6-sol add `--api-client-type openai_responses`: its chat-completions route rejects function tools with reasoning (every rollout fails with `has_error` before the first turn).
- Keep concurrency low to stay under OpenAI's tokens-per-minute limit: `-c 2` for gpt-5.6-sol, `-c 4` for gpt-4.1-mini.
- A rollout that ends in `has_error` is an infrastructure failure, never a model failure: re-run those tasks with `-a '{"task_ids": [...]}'` until none are left, and merge the results before reading pass rates.
- SPEC.md is the source of truth. If code and SPEC disagree, ask before changing either.

## Git
- Commit after each finished task, on main, with a clear message. Never push.

## Day 5 TODO
- **Strip hidden columns per rollout:** `setup_state` copies the seed DB for each rollout; drop the `hidden_*` columns from that copy so no future tool (e.g. a raw query) can leak intended outcomes. SPEC 8a never compares them.
- **Set the acting approver per task:** `setup_state` must call `ap.schema.set_acting_approver(db, task_approver)`; otherwise `whoami` and the audit actor default to the Teamleiter even when the task names someone else.
- **Replace the wiring skeleton:** `invoice_approval.py` still uses the 3-invoice placeholder schema and tools; switch it to `ap/` (seed, tools with `args_to_skip=["db"]`, `ap.load_policy()` as system prompt, solver-generated gold actions, SPEC 8a grader).
