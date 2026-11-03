"""Seed data (SPEC section 6): determinism, realism, consistency, trap rates, required edge cases."""

import json
import re
import sqlite3
from collections import Counter
from datetime import date, timedelta
from pathlib import Path

import pytest
from reference_policy import normalize_number

from ap import money, rollout, seed, solver

TABLES = ["suppliers", "purchase_orders", "approvers", "invoices", "ledger_entries", "audit_log", "app_context"]


def _rows(db, sql, params=()):
    conn = sqlite3.connect(db)
    conn.row_factory = sqlite3.Row
    rows = [dict(r) for r in conn.execute(sql, params)]
    conn.close()
    return rows


def _dump(db):
    return {t: _rows(db, f"SELECT * FROM {t} ORDER BY 1") for t in TABLES}


@pytest.fixture(scope="module")
def db(seed_template):
    return seed_template


@pytest.fixture(scope="module")
def received(db):
    rows = _rows(db, "SELECT * FROM invoices WHERE status = 'received'")
    for r in rows:
        r["outcome"] = json.loads(r["hidden_outcome"])
    return rows


def test_deterministic(db, tmp_path):
    again = seed.build_db(tmp_path / "again.sqlite")
    assert _dump(again) == _dump(db)
    other = seed.build_db(tmp_path / "other.sqlite", seed=7)
    assert _dump(other)["invoices"] != _dump(db)["invoices"]


# The seed is frozen: tasks (scopes, gold actions, wrong action lists) are generated from it.
SEED_42_FINGERPRINT = "67c8d94ae76bb911d6e3baff0afc48d82cc58995b8d27c495cf60ef439df161b"


def test_seed_is_frozen(db):
    actual = seed.fingerprint(db)
    assert actual == SEED_42_FINGERPRINT, (
        f"\n\nThe seed-42 database changed (fingerprint {actual}, expected {SEED_42_FINGERPRINT}).\n"
        "Tasks are generated from this database, so their gold actions may now be wrong.\n"
        "If the change is unintentional: revert it.\n"
        "If it is intentional: REGENERATE ALL TASKS (scopes, gold actions, wrong action lists),\n"
        "re-check them, and only then update SEED_42_FINGERPRINT in tests/test_seed.py.\n"
    )


def test_fingerprint_sees_any_change(db, tmp_path):
    copy = tmp_path / "copy.sqlite"
    copy.write_bytes(Path(db).read_bytes())
    assert seed.fingerprint(copy) == seed.fingerprint(db)
    conn = sqlite3.connect(copy)
    with conn:
        conn.execute("UPDATE suppliers SET payment_terms_days = payment_terms_days + 1 WHERE id = 'K-10001'")
    conn.close()
    assert seed.fingerprint(copy) != seed.fingerprint(db)


def test_refuses_to_overwrite(db):
    with pytest.raises(FileExistsError):
        seed.build_db(db)


def test_sizes(db):
    count = lambda t: _rows(db, f"SELECT COUNT(*) AS n FROM {t}")[0]["n"]  # noqa: E731
    assert count("suppliers") == 60
    assert count("purchase_orders") == 150
    assert count("invoices") == 200
    approvers = _rows(db, "SELECT role, approval_limit_gross_cents AS lim FROM approvers")
    assert sorted((a["role"], a["lim"] or 0) for a in approvers) == [
        ("Abteilungsleiter", 2_500_000), ("Bereichsleiter", 10_000_000), ("CFO", 0), ("Teamleiter", 500_000)]


def _iban_ok(iban: str) -> bool:
    rearranged = iban[4:] + iban[:4]
    return int("".join(str(int(c, 36)) for c in rearranged)) % 97 == 1


def _de_ust_id_ok(vat_id: str) -> bool:
    digits = [int(c) for c in vat_id[2:]]
    product = 10
    for d in digits[:8]:
        total = (d + product) % 10
        if total == 0:
            total = 10
        product = (total * 2) % 11
    check = 11 - product
    return (0 if check == 10 else check) == digits[8]


def _at_uid_ok(vat_id: str) -> bool:
    d = [int(c) for c in vat_id[3:]]
    total = d[0] + d[2] + d[4] + d[6] + sum(x * 2 // 10 + x * 2 % 10 for x in (d[1], d[3], d[5]))
    return (10 - (total + 4) % 10) % 10 == d[7]


def _nl_btw_ok(vat_id: str) -> bool:
    d = [int(c) for c in vat_id[2:11]]
    return (sum(x * w for x, w in zip(d, range(9, 1, -1))) - d[8]) % 11 == 0


def test_supplier_master_data_is_valid(db):
    suppliers = _rows(db, "SELECT * FROM suppliers")
    assert len({s["name"] for s in suppliers}) == len(suppliers)
    assert all(re.fullmatch(r"K-1\d{4}", s["id"]) for s in suppliers)
    assert sum(s["is_blocked"] for s in suppliers) == 3
    assert all(bool(s["blocked_reason"]) == bool(s["is_blocked"]) for s in suppliers)
    formats = {
        "DE": (r"DE\d{9}", r"DE\d{2}\d{18}", r"\d{5}"),
        "AT": (r"ATU\d{8}", r"AT\d{2}\d{16}", r"\d{4}"),
        "NL": (r"NL\d{9}B\d{2}", r"NL\d{2}[A-Z]{4}\d{10}", r"\d{4} [A-Z]{2}"),
    }
    assert Counter(s["country"] for s in suppliers)["DE"] >= 50
    for s in suppliers:
        vat_re, iban_re, zip_re = formats[s["country"]]
        assert re.fullmatch(vat_re, s["vat_id"]), s
        assert re.fullmatch(iban_re, s["iban"]) and _iban_ok(s["iban"]), s
        assert re.fullmatch(zip_re, s["zip"]), s
        assert s["iban"][:2] == s["country"], s  # IBAN country = supplier country
        assert {"DE": _de_ust_id_ok, "AT": _at_uid_ok, "NL": _nl_btw_ok}[s["country"]](s["vat_id"]), s
    # Published valid examples, to check the checkers themselves.
    assert _iban_ok("DE89370400440532013000")
    assert _de_ust_id_ok("DE136695976")
    # AT/NL examples are from python-stdnum's published docs and test data (stdnum/at/uid.py, tests/test_eu_vat.doctest).
    assert _at_uid_ok("ATU13585627") and _at_uid_ok("ATU15159209") and not _at_uid_ok("ATU13585628")
    assert _nl_btw_ok("NL004495445B01") and _nl_btw_ok("NL009122746B01") and not _nl_btw_ok("NL004495446B01")


def test_no_surname_repeats_across_supplier_names(db):
    pools = seed.SURNAMES + seed.AT_SURNAMES + seed.NL_SURNAMES
    assert len(pools) == len(set(pools))
    names = [s["name"] for s in _rows(db, "SELECT name FROM suppliers")]
    for surname in pools:
        used = [n for n in names if re.search(rf"(?<!\w){re.escape(surname)}(?!\w)", n)]
        assert len(used) <= 1, (surname, used)


def test_insolvency_court_is_suppliers_own_city(db):
    blocked = _rows(db, "SELECT city, blocked_reason FROM suppliers WHERE is_blocked = 1")
    courts = [(s["city"], c) for s in blocked for c in re.findall(r"\b(?:Amtsgericht|AG) ([^),]+)", s["blocked_reason"])]
    assert courts  # at least one blocked supplier is in insolvency proceedings
    assert all(city == court for city, court in courts), courts


# ---------- internal consistency (realism conventions, SPEC section 6)

# Postal "Leitzonen": first ZIP digit -> Bundesländer that use it.
ZIP_ZONES = {
    "0": {"Sachsen", "Thüringen", "Sachsen-Anhalt", "Brandenburg"},
    "1": {"Berlin", "Brandenburg", "Mecklenburg-Vorpommern"},
    "2": {"Hamburg", "Schleswig-Holstein", "Niedersachsen", "Bremen", "Mecklenburg-Vorpommern"},
    "3": {"Niedersachsen", "Hessen", "Nordrhein-Westfalen", "Sachsen-Anhalt", "Thüringen"},
    "4": {"Nordrhein-Westfalen", "Niedersachsen"},
    "5": {"Nordrhein-Westfalen", "Rheinland-Pfalz", "Hessen"},
    "6": {"Hessen", "Rheinland-Pfalz", "Saarland", "Baden-Württemberg", "Bayern"},
    "7": {"Baden-Württemberg"},
    "8": {"Bayern", "Baden-Württemberg"},
    "9": {"Bayern", "Thüringen", "Sachsen"},
}
# Bundesbank clearing regions: first BLZ digit -> Bundesländer.
BLZ_ZONES = {
    "1": {"Berlin", "Brandenburg", "Mecklenburg-Vorpommern"},
    "2": {"Hamburg", "Schleswig-Holstein", "Niedersachsen", "Bremen"},
    "3": {"Nordrhein-Westfalen"}, "4": {"Nordrhein-Westfalen"},
    "5": {"Hessen", "Rheinland-Pfalz", "Saarland"},
    "6": {"Baden-Württemberg"}, "7": {"Bayern"},
    "8": {"Sachsen", "Thüringen", "Sachsen-Anhalt"},
}
FOREIGN_REGIONS = {"Linz": "Oberösterreich", "Salzburg": "Salzburg", "Innsbruck": "Tirol",
                   "Eindhoven": "Noord-Brabant", "Rotterdam": "Zuid-Holland"}


def test_zip_city_region_and_bank_region_agree(db):
    suppliers = _rows(db, "SELECT * FROM suppliers")
    region_of_city, city_of_zip = {}, {}
    for s in suppliers:
        region_of_city.setdefault(s["city"], set()).add((s["region"], s["country"]))
        city_of_zip.setdefault(s["zip"], set()).add(s["city"])
        if s["country"] == "DE":
            assert s["region"] in ZIP_ZONES[s["zip"][0]], s
            assert s["region"] in BLZ_ZONES[s["iban"][4]], s  # BLZ follows the bank's region
        else:
            assert FOREIGN_REGIONS[s["city"]] == s["region"], s
        if s["country"] == "NL":
            assert "straße" not in s["street"], s
    assert all(len(v) == 1 for v in region_of_city.values()), region_of_city  # one region per city
    assert all(len(v) == 1 for v in city_of_zip.values()), city_of_zip  # a ZIP belongs to one city
    zips_per_city = Counter(next(iter(cities)) for cities in city_of_zip.values())
    assert max(zips_per_city.values()) > 1  # larger cities really use several ZIPs


def test_po_order_date_before_every_invoice_citing_it(db):
    pos = _pos(db)
    for po in pos.values():
        d = date.fromisoformat(po["order_date"])
        assert d.weekday() < 5 and date(2026, 6, 1) <= d <= date(2026, 10, 23), po
    for r in _rows(db, "SELECT id, po_number, invoice_date FROM invoices WHERE po_number IS NOT NULL"):
        if r["po_number"] in pos:
            assert pos[r["po_number"]]["order_date"] < r["invoice_date"], r


def test_small_no_po_invoices_come_mostly_from_small_ticket_trades():
    gen = seed.generate()
    trade = {s.id: s.trade for s in gen.suppliers}
    small = [i for i in gen.invoices if not i.po_number and money.is_kleinbetrag(i.gross_cents)]
    share = sum(trade[i.supplier_id] in seed.SMALL_TICKET_TRADES for i in small) / len(small)
    assert 0.8 <= share < 1.0, share  # mostly, not exclusively


def _invoices(db):
    rows = _rows(db, "SELECT i.*, s.payment_terms_days AS terms FROM invoices i JOIN suppliers s ON s.id = i.supplier_id")
    for r in rows:
        r["inv"], r["rec"], r["due"] = (date.fromisoformat(r[k]) for k in ("invoice_date", "received_date", "due_date"))
    return rows


def test_dates_are_workdays_and_ordered(db):
    for r in _invoices(db):
        assert r["inv"] <= r["rec"], r["id"]
        assert r["inv"].weekday() < 5 and r["rec"].weekday() < 5, r["id"]


def test_due_date_is_invoice_date_plus_terms_except_resent_copies(db):
    groups = {}
    for r in sorted(_invoices(db), key=lambda r: (r["received_date"], r["id"])):
        groups.setdefault((r["supplier_id"], normalize_number(r["supplier_invoice_number"])), []).append(r)
    for members in groups.values():
        first = members[0]
        for r in members:
            if r["due"] != r["inv"] + timedelta(days=r["terms"]):
                # only a resent copy may deviate, and then it carries the original's dates exactly
                assert r is not first and (r["inv"], r["due"]) == (first["inv"], first["due"]), r["id"]


def test_invoice_numbers_rise_with_invoice_date(db):
    firsts = {}
    for r in sorted(_invoices(db), key=lambda r: (r["received_date"], r["id"])):
        firsts.setdefault((r["supplier_id"], normalize_number(r["supplier_invoice_number"])), r)
    by_supplier = {}
    for (supplier, number), r in firsts.items():
        by_supplier.setdefault(supplier, []).append((r["inv"], int(re.sub(r"\D", "", number))))
    for supplier, entries in by_supplier.items():
        for d1, n1 in entries:
            for d2, n2 in entries:
                if d1 < d2:
                    assert n1 < n2, (supplier, d1, n1, d2, n2)


def test_invoice_with_po_carries_po_cost_center(db):
    pos = _pos(db)
    for r in _rows(db, "SELECT * FROM invoices WHERE po_number IS NOT NULL"):
        if r["po_number"] in pos:
            assert r["cost_center"] == pos[r["po_number"]]["cost_center"], r["id"]


def test_cost_centers_and_descriptions_follow_trade():
    gen = seed.generate()
    suppliers = {s.id: s for s in gen.suppliers}
    pos = {p.po_number: p for p in gen.pos}
    for po in gen.pos:
        s = suppliers[po.supplier_id]
        assert (po.description, po.cost_center) in seed.DESCRIPTIONS[s.trade], (s.name, po.description)
        if s.category in ("goods", "services") and s.country != "NL":
            assert s.trade in s.name, (s.name, s.trade)
    for inv in gen.invoices:
        if inv.po_number not in pos:  # no PO, or one that doesn't exist: cost center from own trade
            assert inv.cost_center in {cc for _, cc in seed.DESCRIPTIONS[suppliers[inv.supplier_id].trade]}, inv
    used = {p.cost_center for p in gen.pos} | {i.cost_center for i in gen.invoices}
    assert used <= set(seed.COST_CENTER_NAMES)
    assert len({s.trade for s in gen.suppliers}) >= 20


def test_large_amounts_only_from_big_ticket_trades():
    gen = seed.generate()
    trade = {s.id: s.trade for s in gen.suppliers}
    for po in gen.pos:
        assert po.net_cents <= seed.AL_LIMIT or trade[po.supplier_id] in seed.BIG_TICKET_TRADES, po
    for inv in gen.invoices:
        assert inv.gross_cents <= seed.AL_LIMIT or trade[inv.supplier_id] in seed.BIG_TICKET_TRADES, inv


def test_blocked_reasons_fit_the_supplier():
    gen = seed.generate()
    for s in gen.suppliers:
        if s.blocked_reason and "48b" in s.blocked_reason:  # construction tax exemption
            assert s.trade in seed.CONSTRUCTION_TRADES, s


def test_money_columns_are_integers(db):
    for table, col in [("invoices", "net_cents"), ("invoices", "vat_cents"), ("invoices", "gross_cents"),
                       ("invoices", "vat_rate_pct"), ("purchase_orders", "net_cents"),
                       ("purchase_orders", "net_invoiced_cents"), ("ledger_entries", "debit_cents"),
                       ("ledger_entries", "credit_cents")]:
        types = _rows(db, f"SELECT DISTINCT typeof({col}) AS t FROM {table}")
        assert [r["t"] for r in types] == ["integer"], (table, col)


def test_ids_and_dates(db):
    invoices = _rows(db, "SELECT * FROM invoices")
    assert all(re.fullmatch(r"ER-2026-\d{5}", i["id"]) for i in invoices)
    assert all(re.fullmatch(r"45000\d{5}", p["po_number"]) for p in _rows(db, "SELECT * FROM purchase_orders"))
    for i in invoices:
        assert i["invoice_date"] <= i["received_date"] <= "2026-10-23"
        assert i["due_date"] > i["invoice_date"]
    received = [i for i in invoices if i["status"] == "received"]
    by_date = sorted(received, key=lambda i: (i["received_date"], i["id"]))
    assert [i["id"] for i in by_date] != sorted(i["id"] for i in received)  # IDs don't simply follow dates


def test_po_invoiced_matches_approved_history(db):
    approved = Counter()
    for i in _rows(db, "SELECT po_number, net_cents FROM invoices WHERE status = 'approved'"):
        approved[i["po_number"]] += i["net_cents"]
    for po in _rows(db, "SELECT * FROM purchase_orders"):
        assert po["net_invoiced_cents"] == approved.get(po["po_number"], 0), po


def test_history_ledger_is_balanced(db):
    approved = _rows(db, "SELECT * FROM invoices WHERE status = 'approved'")
    assert approved
    for inv in approved:
        lines = _rows(db, "SELECT * FROM ledger_entries WHERE invoice_id = ?", (inv["id"],))
        assert len(lines) == 3
        assert sum(l["debit_cents"] for l in lines) == sum(l["credit_cents"] for l in lines) == inv["gross_cents"]
    assert _rows(db, "SELECT COUNT(*) AS n FROM ledger_entries")[0]["n"] == 3 * len(approved)


def test_every_inbox_invoice_has_hidden_outcome(db, received):
    assert len(received) >= 180
    for r in received:
        assert set(r["outcome"]) == {"status", "reason_code", "assigned_to"} and r["hidden_trap"]
    others = _rows(db, "SELECT hidden_outcome FROM invoices WHERE status != 'received'")
    assert all(o["hidden_outcome"] is None for o in others)
    assert _rows(db, "SELECT value FROM app_context WHERE key = 'acting_approver_id'")[0]["value"] == "AP-203"


def test_trap_rates(received):
    n = len(received)
    outcomes = Counter((r["outcome"]["status"], r["outcome"]["reason_code"]) for r in received)
    target = {  # SPEC section 6, "approximately"
        ("rejected", "DUPLICATE"): 0.08, ("rejected", "MISSING_PO"): 0.08, ("rejected", "INVALID_PO"): 0.05,
        ("on_hold", "PO_EXCEEDED"): 0.06, ("rejected", "VAT_ERROR"): 0.08, ("on_hold", "SUPPLIER_BLOCKED"): 0.04,
        ("escalated", "APPROVAL_LIMIT"): 0.15,
    }
    for key, rate in target.items():
        assert abs(outcomes[key] / n - rate) <= 0.015, (key, outcomes[key], n)
    klein_no_po = [r for r in received if not r["po_number"] and money.is_kleinbetrag(r["gross_cents"])
                   and r["outcome"]["status"] == "approved"]
    assert abs(len(klein_no_po) / n - 0.10) <= 0.015


# ---------- required edge cases (SPEC section 6), found structurally, not by trap label


def _pos(db):
    return {p["po_number"]: p for p in _rows(db, "SELECT * FROM purchase_orders")}


def _same_number_groups(db):
    groups = {}
    for i in _rows(db, "SELECT * FROM invoices ORDER BY received_date, id"):
        groups.setdefault((i["supplier_id"], normalize_number(i["supplier_invoice_number"])), []).append(i)
    return [g for g in groups.values() if len(g) > 1]


def test_edge_duplicates_differ_only_by_case_spaces_hyphens(db):
    pairs = [(g[0]["supplier_invoice_number"], i["supplier_invoice_number"])
             for g in _same_number_groups(db) for i in g[1:]]
    assert any(a != b and a.upper() == b.upper() for a, b in pairs)  # case
    assert any(" " in b and a.replace(" ", "") == b.replace(" ", "") for a, b in pairs)  # spaces
    assert any(a.count("-") != b.count("-") and a.replace("-", "") == b.replace("-", "") for a, b in pairs)


def test_edge_resubmission_after_vat_error_rejection(db):
    assert any(g[0]["status"] == "rejected" and g[0]["reason_code"] == "VAT_ERROR"
               and json.loads(g[1]["hidden_outcome"])["status"] == "approved" for g in _same_number_groups(db))


def test_edge_duplicate_with_different_amount(db):
    assert any(i["gross_cents"] != g[0]["gross_cents"] and json.loads(i["hidden_outcome"])["reason_code"] == "DUPLICATE"
               for g in _same_number_groups(db) for i in g[1:])


def test_edge_kleinbetrag_with_valid_and_with_foreign_po(db, received):
    pos = _pos(db)
    klein_po = [r for r in received if r["po_number"] and money.is_kleinbetrag(r["gross_cents"])]
    assert any(r["po_number"] in pos and pos[r["po_number"]]["supplier_id"] == r["supplier_id"]
               and r["outcome"]["status"] == "approved" for r in klein_po)
    assert any(r["po_number"] in pos and pos[r["po_number"]]["supplier_id"] != r["supplier_id"]
               and r["outcome"]["reason_code"] == "INVALID_PO" for r in klein_po)


def test_edge_po_number_that_does_not_exist(db, received):
    pos = _pos(db)
    assert any(r["po_number"] and r["po_number"] not in pos and r["outcome"]["reason_code"] == "INVALID_PO"
               for r in received)


def test_edge_vat_half_cent_boundary(received):
    half = [r for r in received if (r["net_cents"] * r["vat_rate_pct"]) % 100 == 50]
    assert any(r["vat_cents"] == money.expected_vat_cents(r["net_cents"], r["vat_rate_pct"]) for r in half)
    assert any(r["vat_cents"] == money.expected_vat_cents(r["net_cents"], r["vat_rate_pct"]) - 1 for r in half)


def test_edge_two_invoices_exceed_po_only_together(db, received):
    pos = _pos(db)
    by_po = {}
    for r in received:
        by_po.setdefault(r["po_number"], []).append(r)
    found = False
    for po_number, invs in by_po.items():
        po = pos.get(po_number)
        if po is None or len(invs) != 2:
            continue
        a, b = sorted(invs, key=lambda r: (r["received_date"], r["id"]))
        inv0 = po["net_invoiced_cents"]
        alone = all(money.within_po_tolerance(po["net_cents"], inv0, r["net_cents"]) for r in (a, b))
        together = money.within_po_tolerance(po["net_cents"], inv0, a["net_cents"] + b["net_cents"])
        found |= alone and not together
    assert found


def test_edge_exact_tolerance_and_one_cent_above(db, received):
    pos = _pos(db)
    slack = [
        (pos[r["po_number"]]["net_invoiced_cents"] + r["net_cents"]) * 100 - pos[r["po_number"]]["net_cents"] * 102
        for r in received if r["po_number"] in pos
    ]
    assert 0 in slack  # exactly at +2 %
    assert 100 in slack  # one cent net above


def _received_on_po(db, received):
    by_po = {}
    for r in received:
        if r["po_number"]:
            by_po.setdefault(r["po_number"], []).append(r["id"])
    return {po: ids for po, ids in by_po.items() if len(ids) >= 3}


def _outcomes(db, ids, approver):
    return [(a["args"]["invoice_id"], a["tool"]) for a in solver.solve(db, ids, approver)]


def _in_id_order(db, ids, approver, tmp_path):
    """Outcomes when the invoices are processed by ID instead of received date (a common mistake)."""
    work = rollout.prepare_db(tmp_path / "work.sqlite", approver, template=db)
    out = []
    for invoice_id in sorted(ids):
        action = solver.solve(work, [invoice_id], approver)[0]
        solver.apply_action(work, action)
        out.append((invoice_id, action["tool"]))
    return sorted(out)


def test_edge_po_chain_where_a_later_invoice_fits_after_a_hold(db, received, tmp_path):
    found = False
    for ids in _received_on_po(db, received).values():
        in_order = _outcomes(db, ids, "AP-203")
        tools = [t for _, t in in_order]
        if "hold_invoice" in tools and "approve_invoice" in tools[tools.index("hold_invoice"):]:
            found |= _in_id_order(db, ids, "AP-203", tmp_path) != sorted(in_order)  # ID order holds another one
    assert found


def test_edge_po_chain_where_the_approver_changes_what_fits(db, received):
    assert any(
        [t for _, t in _outcomes(db, ids, "AP-203")].count("approve_invoice")
        != [t for _, t in _outcomes(db, ids, "AP-117")].count("approve_invoice")
        and "hold_invoice" in [t for _, t in _outcomes(db, ids, "AP-117")]
        for ids in _received_on_po(db, received).values()
    )


def test_edge_po_chain_with_duplicate_whose_original_has_higher_id(db, received):
    groups = {}
    for r in received:
        groups.setdefault((r["supplier_id"], normalize_number(r["supplier_invoice_number"])), []).append(r)
    chains = _received_on_po(db, received)
    hits = [g for g in groups.values() if len(g) == 2 and g[0]["po_number"] in chains]
    assert any(min(g, key=lambda r: (r["received_date"], r["id"]))["id"] > max(g, key=lambda r: r["received_date"])["id"]
               for g in hits)


def test_edge_resubmission_whose_corrected_copy_has_the_lower_id(received):
    groups = {}
    for r in received:
        groups.setdefault((r["supplier_id"], normalize_number(r["supplier_invoice_number"])), []).append(r)
    assert any(len(g) == 2 and (first := min(g, key=lambda r: r["received_date"]))["outcome"]["reason_code"] == "VAT_ERROR"
               and (second := max(g, key=lambda r: r["received_date"]))["outcome"]["status"] == "approved"
               and second["id"] < first["id"] for g in groups.values())


def test_generator_and_reference_share_no_decision_code():
    tests_dir = Path(__file__).parent
    seed_src = Path(seed.__file__).read_text()  # the imported module, so this also works on an installed wheel
    reference_src = (tests_dir / "reference_policy.py").read_text()
    assert "reference_policy" not in seed_src
    assert not re.search(r"^\s*(from|import)\s+.*\bseed\b", reference_src, re.M)
