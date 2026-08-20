"""Assertions about the fixture itself.  Run with:  pytest build/fixture_test.py

These re-derive, from the solver-facing bytes alone, every property the task
claims: that the pages really are PostgreSQL 16 heap blocks, that the excluded
tuple cases (MultiXact, frozen, combo CID, out-of-line TOAST) are genuinely
absent, that the snapshot separates committed transactions on both sides of the
visibility boundary, that HOT artefacts are present, and that the naive
strategies the prompt must defeat really do give a different answer.
"""
from __future__ import annotations

import csv
import json
import struct
import sys
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TASK / "solution"))
import golden_recover as G          # noqa: E402

ART = TASK / "artifacts"
HEAP = ART / "heap_pages.bin"
TXCSV = ART / "tx_status.csv"
SCHEMA = ART / "table_schema.json"
GOLDEN = TASK / "build" / "internal" / "golden.csv"

HEAP_HASEXTERNAL = 0x0004
HEAP_COMBOCID = 0x0020
HEAP_XMIN_FROZEN = 0x0300
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000


@pytest.fixture(scope="module")
def fixture():
    js, attrs, snap = G.load_schema(SCHEMA)
    status = G.load_tx_status(TXCSV)
    heap = HEAP.read_bytes()
    tuples, census = [], {"LP_UNUSED": 0, "LP_NORMAL": 0,
                          "LP_REDIRECT": 0, "LP_DEAD": 0}
    for blk in range(len(heap) // 8192):
        t, c, _h = G.parse_page(heap[blk * 8192:(blk + 1) * 8192], blk, attrs)
        tuples.extend(t)
        for k in census:
            census[k] += c[k]
    return dict(js=js, attrs=attrs, snap=snap, status=status, heap=heap,
                tuples=tuples, census=census)


# ------------------------------------------------------------------ the bytes
def test_heap_is_a_whole_number_of_8192_byte_blocks(fixture):
    assert len(fixture["heap"]) % 8192 == 0
    assert len(fixture["heap"]) // 8192 >= 2, "the fixture must span several blocks"


def test_declared_block_size_is_8192(fixture):
    assert fixture["js"]["block_size"] == 8192


def test_every_block_declares_the_postgresql_page_layout(fixture):
    heap = fixture["heap"]
    for blk in range(len(heap) // 8192):
        page = heap[blk * 8192:(blk + 1) * 8192]
        lower, upper, special, pagesize_version = struct.unpack_from("<HHHH",
                                                                    page, 12)
        assert pagesize_version & 0xFF00 == 8192, f"block {blk}: page size"
        assert pagesize_version & 0x00FF == 4, (
            f"block {blk}: page layout version {pagesize_version & 0xFF}, "
            "PostgreSQL 8.3-16 uses 4")
        assert 24 <= lower <= upper <= special <= 8192, f"block {blk}: offsets"


def test_schema_declares_postgresql_16(fixture):
    assert fixture["js"]["postgres_version"].startswith("16."), \
        fixture["js"]["postgres_version"]
    assert "PostgreSQL 16." in fixture["js"]["postgres_version_full"]


# ------------------------------------------ deliberately excluded tuple cases
def test_no_multixact_xmax(fixture):
    bad = [(t.block, t.lp) for t in fixture["tuples"]
           if t.infomask & HEAP_XMAX_IS_MULTI]
    assert not bad, f"MultiXact xmax present on {bad[:5]}"


def test_no_frozen_tuples(fixture):
    bad = [(t.block, t.lp) for t in fixture["tuples"]
           if (t.infomask & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN
           or t.xmin in (1, 2)]
    assert not bad, f"frozen tuples present on {bad[:5]}"


def test_no_combo_command_ids(fixture):
    bad = [(t.block, t.lp) for t in fixture["tuples"]
           if t.infomask & HEAP_COMBOCID]
    assert not bad, f"combo CID present on {bad[:5]}"


def test_no_out_of_line_toast_datum(fixture):
    bad = [(t.block, t.lp) for t in fixture["tuples"]
           if t.infomask & HEAP_HASEXTERNAL]
    assert not bad, f"external TOAST pointer on {bad[:5]}"


def test_every_value_decodes_without_toast(fixture):
    """Parsing already ran in the fixture; assert it produced complete rows."""
    ncols = len(fixture["attrs"])
    for t in fixture["tuples"]:
        assert len(t.values) == ncols
    assert fixture["tuples"], "no tuples were decoded"


# ------------------------------------------------------------ HOT is present
def test_hot_pruning_left_redirect_line_pointers(fixture):
    assert fixture["census"]["LP_REDIRECT"] > 0, (
        "no LP_REDIRECT slot: HOT pruning never ran, so a solver that "
        "mis-handles redirects would not be punished")


def test_heap_only_tuples_are_present(fixture):
    hot = [t for t in fixture["tuples"] if t.infomask2 & HEAP_ONLY_TUPLE]
    chained = [t for t in fixture["tuples"] if t.infomask2 & HEAP_HOT_UPDATED]
    assert hot, "no HEAP_ONLY_TUPLE version on the pages"
    assert chained, "no HEAP_HOT_UPDATED version on the pages"


def test_dead_line_pointers_are_present(fixture):
    assert fixture["census"]["LP_DEAD"] > 0, "no pruned line pointer on the pages"


# ------------------------------------------------------ snapshot and statuses
def test_tx_status_covers_every_xid_on_the_pages(fixture):
    needed = {t.xmin for t in fixture["tuples"]} | {
        t.xmax for t in fixture["tuples"] if t.xmax}
    missing = sorted(needed - set(fixture["status"].map))
    assert not missing, f"tx_status.csv is missing xids {missing[:8]}"


def test_tx_status_uses_only_the_documented_states():
    with TXCSV.open(encoding="utf-8", newline="") as fh:
        rows = list(csv.DictReader(fh))
    assert rows and list(rows[0].keys()) == ["xid", "status"]
    states = {r["status"] for r in rows}
    assert states <= {"committed", "aborted", "in_progress"}, states
    assert {"committed", "aborted", "in_progress"} <= states, (
        f"the fixture must exercise all three states, saw {sorted(states)}")


def test_snapshot_has_transactions_on_both_sides_of_the_boundary(fixture):
    snap, status = fixture["snap"], fixture["status"].map
    before = [x for x, s in status.items()
              if s == "committed" and not snap.in_progress(x)]
    after = [x for x, s in status.items()
             if s == "committed" and snap.in_progress(x)]
    assert before, "no committed transaction is visible under the snapshot"
    assert after, ("no committed transaction is invisible under the snapshot; "
                   "snapshot reasoning would be unnecessary")


def test_a_committed_transaction_is_listed_in_snapshot_xip(fixture):
    status = fixture["status"].map
    inxip = [x for x in fixture["snap"].xip if status.get(x) == "committed"]
    assert inxip, ("no xid in snapshot_xip has committed; ignoring "
                   "snapshot_xip would not be punished")


def test_an_in_progress_transaction_wrote_to_the_pages(fixture):
    status = fixture["status"].map
    running = {x for x, s in status.items() if s == "in_progress"}
    touched = {t.xmin for t in fixture["tuples"]} | {
        t.xmax for t in fixture["tuples"] if t.xmax}
    assert running & touched, "no still-running transaction wrote to the heap"


def test_an_aborted_transaction_wrote_to_the_pages(fixture):
    status = fixture["status"].map
    aborted = {x for x, s in status.items() if s == "aborted"}
    inserted = {t.xmin for t in fixture["tuples"]}
    deleted = {t.xmax for t in fixture["tuples"] if t.xmax}
    assert aborted & inserted, "no tuple was inserted by an aborted transaction"
    assert aborted & deleted, "no tuple was deleted by an aborted transaction"


# --------------------------------------------------- the answer is unique...
def test_exactly_one_visible_version_per_primary_key(fixture):
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    pk = [a.name for a in fixture["attrs"]].index(fixture["js"]["primary_key"][0])
    seen = {}
    for t in fixture["tuples"]:
        if G.tuple_visible(t, fixture["snap"], fixture["status"], notes):
            key = t.values[pk]
            assert key not in seen, f"primary key {key} is visible twice"
            seen[key] = t
    assert seen, "the snapshot makes no row visible at all"


def test_the_page_level_answer_equals_the_postgresql_reference():
    """The hidden reference was produced by the server itself, inside the
    repeatable read transaction that owns the target snapshot."""
    import subprocess
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "recovered.csv"
        r = subprocess.run([sys.executable, str(TASK / "solution" /
                                                "golden_recover.py"),
                            "--heap", str(HEAP), "--tx", str(TXCSV),
                            "--schema", str(SCHEMA), "--out", str(out)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        got = [row for row in csv.reader(out.read_text(encoding="utf-8")
                                         .splitlines()) if row]
    want = [row for row in csv.reader(GOLDEN.read_text(encoding="utf-8")
                                      .splitlines()) if row]
    assert got[0] == want[0], "header differs from the PostgreSQL reference"
    assert sorted(got[1:]) == sorted(want[1:]), (
        "the page-level recovery differs from the state PostgreSQL itself "
        "reported under this snapshot")


# ------------------------------------------------- ...and the shortcuts fail
def test_multiple_physical_versions_exist_for_one_key(fixture):
    pk = [a.name for a in fixture["attrs"]].index(fixture["js"]["primary_key"][0])
    counts = {}
    for t in fixture["tuples"]:
        counts[t.values[pk]] = counts.get(t.values[pk], 0) + 1
    multi = [k for k, n in counts.items() if n > 1]
    assert len(multi) >= 20, (
        f"only {len(multi)} key(s) have several physical versions")


def test_greatest_xmin_is_the_wrong_answer(fixture):
    """The naive 'latest version wins' rule must disagree with the snapshot."""
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    pk = [a.name for a in fixture["attrs"]].index(fixture["js"]["primary_key"][0])
    by_key = {}
    for t in fixture["tuples"]:
        by_key.setdefault(t.values[pk], []).append(t)
    wrong = 0
    for key, versions in by_key.items():
        vis = [t for t in versions
               if G.tuple_visible(t, fixture["snap"], fixture["status"], notes)]
        newest = max(versions, key=lambda t: (t.xmin, t.block, t.lp))
        if not vis or vis[0] is not newest:
            wrong += 1
    assert wrong >= 20, (
        f"picking the greatest xmin is wrong for only {wrong} key(s); the trap "
        "is too weak")


def test_ignoring_xmax_is_the_wrong_answer(fixture):
    """Rows the snapshot cannot see because they were deleted must exist."""
    notes = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}
    deleted = 0
    for t in fixture["tuples"]:
        if G.xmin_committed(t, fixture["status"], notes) \
                and not fixture["snap"].in_progress(t.xmin) \
                and not G.tuple_visible(t, fixture["snap"],
                                        fixture["status"], notes):
            deleted += 1
    assert deleted >= 10, (
        f"only {deleted} tuple(s) are hidden by xmax alone; ignoring xmax "
        "would barely change the answer")
