"""Invoice approval (Kreditorenrechnungsprüfung) — verifiers 0.3.1, legacy API.

Each rollout gets its own copy of the seed-42 database (hidden columns stripped, the task's acting
approver set); the agent only sees business arguments, and the DB path is injected behind the
scenes. After the rollout, the final state is snapshotted (SPEC 8a fields) and the file deleted.
The grader replays the task's gold actions on a fresh copy and compares: 1.0 on exact match, else 0.0.
"""

import json
import os
import tempfile
from pathlib import Path

import verifiers.legacy as vf
from datasets import Dataset

from ap import grader, load_policy, rollout, seed, tools

TASKS_PATH = Path(__file__).with_name("tasks") / "tasks.jsonl"

SYSTEM_PROMPT = (
    "You are a clerk in accounts payable (Kreditorenbuchhaltung) at Alpenwerk Maschinenbau GmbH, München. "
    "Carry out the work order using the tools, following the policy below exactly. "
    "When you are finished, reply with a short summary and no tool call.\n\n" + load_policy()
)


def load_tasks(path: Path = TASKS_PATH) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


# ---------- environment ----------
class InvoiceApprovalEnv(vf.StatefulToolEnv):
    def __init__(self, **kwargs):
        super().__init__(tools=[], **kwargs)
        for tool in tools.TOOLS:
            self.add_tool(tool, args_to_skip=["db"])

    async def setup_state(self, state: vf.State) -> vf.State:
        fd, path = tempfile.mkstemp(suffix=".sqlite")
        os.close(fd)
        rollout.prepare_db(path, state["info"]["approver_id"])
        state["db_path"] = path
        return state

    def update_tool_args(self, tool_name, tool_args, messages, state, **kwargs) -> dict:
        tool_args["db"] = state["db_path"]  # inject the hidden argument
        return tool_args

    @vf.cleanup
    async def snapshot_db(self, state: vf.State) -> None:
        """Runs after the rollout, before scoring: freeze the 8a state, delete the file."""
        path = state.get("db_path")
        if not path or not os.path.exists(path):
            return
        state["final_db"] = grader.snapshot(path)
        os.remove(path)


# ---------- grader: replay gold actions, compare SPEC 8a state ----------
def make_rubric(tasks: list[dict]) -> vf.Rubric:
    gold_cache: dict[str, dict] = {}

    def result(state, info) -> tuple[float, dict]:
        task_id = info["task_id"]
        if task_id not in gold_cache:
            gold_cache[task_id] = grader.replay(info["gold_actions"], info["approver_id"])
        return grader.score(state.get("final_db"), gold_cache[task_id], info["scope"])

    def state_matches(state, info) -> float:
        """1.0 if the final database equals the replayed gold database on every SPEC 8a field."""
        return result(state, info)[0]

    def metric(name):
        def fn(state, info) -> float:
            return result(state, info)[1][name]

        fn.__name__ = name
        return fn

    rubric = vf.Rubric(funcs=[state_matches], weights=[1.0])
    for name in ("scope_correct", "collateral_changes", "po_mismatches", "ledger_mismatches"):
        rubric.add_metric(metric(name))  # weight 0: logged for diagnosis, never part of the reward
    return rubric


def gold_path_turns(task: dict) -> int:
    """Turns a careful agent needs for the gold path, one tool call per turn: whoami and list_approvers
    once; per invoice get_invoice, get_supplier, get_purchase_order, a list_invoices duplicate check and
    the decision; plus the final summary message."""
    return 2 + 5 * len(task["scope"]) + 1


def load_environment(max_turns: int = 200, task_ids: list[str] | None = None, **kwargs) -> vf.Environment:
    """max_turns defaults to about 1.7x the longest task's gold path (118 turns for the 23-invoice task).
    task_ids optionally restricts the dataset to those tasks (e.g. to re-run failed rollouts)."""
    tasks = load_tasks()
    if task_ids is not None:
        unknown = set(task_ids) - {t["task_id"] for t in tasks}
        if unknown:
            raise ValueError(f"unknown task_ids: {sorted(unknown)}")
        tasks = [t for t in tasks if t["task_id"] in set(task_ids)]
    actual = seed.fingerprint(rollout.seed_template())
    too_short = [t["task_id"] for t in tasks if gold_path_turns(t) > max_turns]
    if too_short:
        raise ValueError(f"max_turns={max_turns} is shorter than the gold path of tasks {too_short}.")
    stale = [t["task_id"] for t in tasks if t["seed_fingerprint"] != actual]
    if stale:
        raise RuntimeError(f"Tasks {stale} were generated from a different seed; run scripts/make_tasks.py.")
    dataset = Dataset.from_list([
        {
            "question": t["work_order"],
            "answer": "",
            "info": json.dumps({k: t[k] for k in ("task_id", "approver_id", "scope", "gold_actions")}),
        }
        for t in tasks
    ])
    return InvoiceApprovalEnv(
        dataset=dataset, system_prompt=SYSTEM_PROMPT, rubric=make_rubric(tasks), max_turns=max_turns, **kwargs
    )
