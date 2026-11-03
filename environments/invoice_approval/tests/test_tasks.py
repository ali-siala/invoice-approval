"""Pilot tasks and the environment wiring: gold actions score 1.0, every wrong action list scores 0.0."""

import asyncio
import json
import re
import sqlite3
from collections import Counter

import pytest
import verifiers.legacy as vf

import invoice_approval
from ap import grader, rollout, schema, seed, solver
from scripts import make_tasks

TASKS = invoice_approval.load_tasks()
WRONG = [(t["task_id"], w["mistake"], w["actions"]) for t in TASKS for w in t["wrong_action_lists"]]


@pytest.fixture(scope="module")
def env():
    return invoice_approval.load_environment()


def _rubric_scores(env, task, actions) -> dict:
    """Score `actions` through the environment's own rubric functions, as a rollout would be scored."""
    info = {k: task[k] for k in ("task_id", "approver_id", "scope", "gold_actions")}
    state = {"final_db": grader.replay(actions, task["approver_id"])}
    ours = next(r for r in env.rubric.rubrics if any(f.__name__ == "state_matches" for f in r.funcs))
    return {f.__name__: f(state=state, info=info) for f in ours.funcs}


def test_task_counts_and_records():
    tiers = Counter(t["tier"] for t in TASKS)
    assert tiers == {"single": 20, "batch": 20, "long_horizon": 10, "hard": 10}
    assert len({t["task_id"] for t in TASKS}) == 60
    fingerprint = seed.fingerprint(rollout.seed_template())
    for t in TASKS:
        assert t["work_order"] and t["scope"] and t["gold_actions"], t["task_id"]
        assert t["seed_fingerprint"] == fingerprint, "tasks are stale: run scripts/make_tasks.py"
        assert len(t["wrong_action_lists"]) >= 3, t["task_id"]
        assert t["approver_id"] in t["work_order"]
        size = len(t["scope"])
        assert {"single": size == 1, "batch": 3 <= size <= 10, "long_horizon": 15 <= size <= 25,
                "hard": 10 <= size <= 15}[t["tier"]], t["task_id"]
    assert {t["approver_id"] for t in TASKS} == {"AP-203", "AP-117", "AP-052", "AP-008"}


def test_single_tasks_are_never_clean_invoices():
    conn = sqlite3.connect(rollout.seed_template())
    trap = dict(conn.execute("SELECT id, hidden_trap FROM invoices"))
    conn.close()
    for t in TASKS:
        if t["tier"] == "single":
            assert not trap[t["scope"][0]].startswith("clean"), t["task_id"]


def test_hard_tasks_are_dense_in_what_the_strong_model_got_wrong():
    conn = sqlite3.connect(rollout.seed_template())
    trap = dict(conn.execute("SELECT id, hidden_trap FROM invoices"))
    conn.close()
    kinds = {"gross": {"vat_gross_mismatch"}, "vat_amount": {"vat_amount_wrong", "vat_amount_wrong_kleinbetrag"},
             "half_cent": {"vat_half_cent_rounded_down", "vat_half_cent_correct"}, "dup_of_paid": {"dup_of_paid"}}
    order_groups = ("po_chain", "resubmission", "po_pair", "kleinbetrag_pair")
    for t in (t for t in TASKS if t["tier"] == "hard"):
        labels = [trap[i] for i in t["scope"]]
        assert all(any(label in k for label in labels) for k in kinds.values()), t["task_id"]
        assert any(label.startswith(order_groups) for label in labels), t["task_id"]
        assert t["approver_id"] != "AP-203", t["task_id"]


def test_every_trap_type_and_edge_case_in_at_least_two_tasks():
    counts = make_tasks.coverage(TASKS)
    expected = {f"trap: {k}" for k in ("DUPLICATE", "MISSING_PO", "INVALID_PO", "PO_EXCEEDED", "VAT_ERROR",
                                        "SUPPLIER_BLOCKED", "escalation", "kleinbetrag without PO")}
    expected |= {f"edge: {k}" for k in list(make_tasks.EDGE_CASES) + list(make_tasks.GROUP_EDGES)}
    assert {k: counts.get(k, 0) for k in expected if counts.get(k, 0) < 2} == {}


@pytest.mark.parametrize("task", TASKS, ids=[t["task_id"] for t in TASKS])
def test_gold_actions_are_the_solvers_and_score_1(env, task):
    assert task["gold_actions"] == solver.solve(rollout.seed_template(), task["scope"], task["approver_id"])
    scores = _rubric_scores(env, task, task["gold_actions"])
    assert scores["state_matches"] == 1.0
    assert scores["scope_correct"] == 1.0 and scores["collateral_changes"] == 0.0
    assert scores["po_mismatches"] == 0.0 and scores["ledger_mismatches"] == 0.0


@pytest.mark.parametrize("task_id,mistake,actions", WRONG, ids=[f"{t}: {m}" for t, m, _ in WRONG])
def test_wrong_action_lists_score_0(env, task_id, mistake, actions):
    task = next(t for t in TASKS if t["task_id"] == task_id)
    scores = _rubric_scores(env, task, actions)
    assert scores["state_matches"] == 0.0
    assert scores["scope_correct"] < 1.0 or scores["collateral_changes"] > 0 or scores["po_mismatches"] > 0


def test_listed_tasks_use_a_misleading_order_that_matters():
    conn = sqlite3.connect(rollout.seed_template())
    received = dict(conn.execute("SELECT id, received_date FROM invoices"))
    conn.close()
    for t in TASKS:
        if t["tier"] in ("long_horizon", "hard") or t["task_id"].startswith("batch-list"):
            listed = re.findall(r"ER-2026-\d{5}", t["work_order"])
            assert sorted(listed) == t["scope"], t["task_id"]
            assert listed != sorted(listed, key=lambda i: (received[i], i)), t["task_id"]
            assert any("order the work order lists" in w["mistake"] for w in t["wrong_action_lists"]), t["task_id"]


def test_rollout_copy_strips_hidden_columns_and_sets_approver(tmp_path):
    db = rollout.prepare_db(tmp_path / "r.sqlite", "AP-117")
    conn = sqlite3.connect(db)
    columns = [r[1] for r in conn.execute("PRAGMA table_info(invoices)")]
    acting = schema.get_acting_approver_id(conn)
    conn.close()
    assert not any(c.startswith("hidden") for c in columns)
    assert b"hidden_outcome" not in (tmp_path / "r.sqlite").read_bytes()  # vacuumed: no stale bytes
    assert acting == "AP-117"
    assert solver.solve(db, ["ER-2026-00437"], "AP-117")  # the app works on the stripped copy


def test_env_setup_and_cleanup(env):
    """setup_state creates the per-rollout DB for the task's approver; cleanup snapshots and deletes it."""
    task = next(t for t in TASKS if t["approver_id"] != "AP-203")
    state = vf.State(input={"info": {k: task[k] for k in ("task_id", "approver_id", "scope", "gold_actions")}})
    state["info"] = state["input"]["info"]
    state = asyncio.run(env.setup_state(state))
    path = state["db_path"]
    assert f'"{task["approver_id"]}"' in env.tool_map["whoami"](db=path)
    args = env.update_tool_args("approve_invoice", {"invoice_id": "X"}, [], state)
    assert args["db"] == path
    for action in task["gold_actions"]:
        solver.apply_action(path, action)
    asyncio.run(env.snapshot_db(state))
    assert not __import__("os").path.exists(path)
    assert grader.score(state["final_db"], grader.replay(task["gold_actions"], task["approver_id"]),
                        task["scope"])[0] == 1.0


def test_tool_schemas_do_not_expose_db(env):
    for tool_def in env.tool_defs:
        assert "db" not in tool_def.parameters.get("properties", {}), tool_def.name
    assert {d.name for d in env.tool_defs} == {t.__name__ for t in invoice_approval.tools.TOOLS}


def test_max_turns_leaves_room_for_the_gold_path(env):
    longest = max(invoice_approval.gold_path_turns(t) for t in TASKS)
    assert env.max_turns >= 60 and env.max_turns >= 1.5 * longest
    with pytest.raises(ValueError, match="shorter than the gold path"):
        invoice_approval.load_environment(max_turns=longest - 1)
    assert invoice_approval.load_environment(max_turns=longest).max_turns == longest


def test_task_ids_selects_a_subset():
    env = invoice_approval.load_environment(task_ids=["long-1", "single-dup_case"])
    assert sorted(json.loads(row["info"])["task_id"] for row in env.dataset) == ["long-1", "single-dup_case"]
    with pytest.raises(ValueError, match="unknown task_ids"):
        invoice_approval.load_environment(task_ids=["nope"])
