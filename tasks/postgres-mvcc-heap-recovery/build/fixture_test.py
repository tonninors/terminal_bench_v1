"""Assertions about the fixture itself.  Run with:  pytest build/fixture_test.py

These re-derive, from the solver-facing bytes alone, every property the task
claims: that the pages really are PostgreSQL 16 heap blocks, that the excluded
tuple cases (MultiXact, frozen, combo CID, out-of-line TOAST) are genuinely
absent, that the snapshot separates committed transactions on both sides of the
visibility boundary, that the HOT and row-lock artefacts the task depends on are
present, and that every naive strategy the prompt must defeat really does give a
materially different answer.
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
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMIN_FROZEN = 0x0300
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000

FRESH_NOTES = {"frozen": 0, "hint_conflicts": 0, "lock_only": 0}


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


def pk_index(fixture):
    return [a.name for a in fixture["attrs"]].index(fixture["js"]["primary_key"][0])


def versions_by_key(fixture):
    idx = pk_index(fixture)
    out = {}
    for t in fixture["tuples"]:
        out.setdefault(t.values[idx], []).append(t)
    return out


def visible_by_key(fixture):
    idx = pk_index(fixture)
    out = {}
    for t in fixture["tuples"]:
        if G.tuple_visible(t, fixture["snap"], fixture["status"], dict(FRESH_NOTES)):
            out[t.values[idx]] = t
    return out


def lock_only_tuples(fixture):
    return [t for t in fixture["tuples"]
            if t.xmax and G.xmax_is_locked_only(t.infomask)]


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


def test_dead_line_pointers_are_present(fixture):
    assert fixture["census"]["LP_DEAD"] > 0, "no pruned line pointer on the pages"


def test_heap_only_tuples_are_present(fixture):
    hot = [t for t in fixture["tuples"] if t.infomask2 & HEAP_ONLY_TUPLE]
    chained = [t for t in fixture["tuples"] if t.infomask2 & HEAP_HOT_UPDATED]
    assert len(hot) >= 40, f"only {len(hot)} HEAP_ONLY_TUPLE version(s)"
    assert len(chained) >= 40, f"only {len(chained)} HEAP_HOT_UPDATED version(s)"


def test_deep_version_chains_exist(fixture):
    """Keys whose physical versions were never pruned away."""
    depths = sorted((len(v) for v in versions_by_key(fixture).values()),
                    reverse=True)
    deep = [d for d in depths if d >= 3]
    assert len(deep) >= 25, (
        f"only {len(deep)} key(s) have 3+ physical versions; the deepest chains "
        f"are {depths[:8]}")
    assert depths[0] >= 4, f"the deepest chain is only {depths[0]} versions"


def test_visible_version_is_often_mid_chain(fixture):
    """The answer must not be readable off either end of a chain."""
    vis = visible_by_key(fixture)
    byk = versions_by_key(fixture)
    mid = 0
    for key, t in vis.items():
        chain = sorted(byk[key], key=lambda x: (x.xmin, x.block, x.lp))
        i = chain.index(t)
        if 0 < i < len(chain) - 1:
            mid += 1
    assert mid >= 10, (
        f"the visible version is strictly inside the chain for only {mid} key(s); "
        "taking the first or the last version would be too close to correct")


def test_skipping_heap_only_tuples_loses_rows(fixture):
    """A solver that dismisses heap-only versions as HOT bookkeeping must fail."""
    lost = [k for k, t in visible_by_key(fixture).items()
            if t.infomask2 & HEAP_ONLY_TUPLE]
    assert len(lost) >= 25, (
        f"only {len(lost)} visible row(s) live in a heap-only tuple")


# --------------------------------------------------------- lock-only xmax
def test_lock_only_xmax_tuples_exist(fixture):
    n = len(lock_only_tuples(fixture))
    assert n >= 30, f"only {n} tuple(s) carry a lock-only xmax"


def test_lock_only_xmax_is_not_masked_by_a_hint_bit(fixture):
    """The decisive case: xmax names a committed transaction that the snapshot
    can see, HEAP_XMAX_INVALID is NOT set, and the row is alive purely because
    the xmax records a row lock.  Reading xmax without consulting the infomask
    deletes every one of these rows."""
    snap, status = fixture["snap"], fixture["status"].map
    hard = [t for t in lock_only_tuples(fixture)
            if not (t.infomask & HEAP_XMAX_INVALID)
            and status.get(t.xmax) == "committed"
            and not snap.in_progress(t.xmax)]
    assert len(hard) >= 15, (
        f"only {len(hard)} lock-only tuple(s) force the infomask to be read; "
        "the rest would already be excused by HEAP_XMAX_INVALID")


def test_several_row_lock_modes_are_represented(fixture):
    modes = {t.infomask & 0x00F0 for t in lock_only_tuples(fixture)}
    assert len(modes) >= 2, (
        f"only one row-lock mode is present: {sorted(hex(m) for m in modes)}")


def test_committed_xmax_rule_would_lose_many_keys(fixture):
    """Strategy 3, 'a committed xmax means deleted', must be badly wrong."""
    status = fixture["status"].map
    lost = {k for k, t in visible_by_key(fixture).items()
            if t.xmax and status.get(t.xmax) == "committed"}
    assert len(lost) >= 30, (
        f"treating every committed xmax as a deletion loses only {len(lost)} "
        "visible row(s)")


def test_nonzero_xmax_does_not_mean_gone(fixture):
    n = sum(1 for t in visible_by_key(fixture).values() if t.xmax)
    assert n >= 40, (
        f"only {n} visible row(s) carry a non-zero xmax; 'xmax set means "
        "deleted' would barely be punished")


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


def test_several_committed_transactions_are_listed_in_snapshot_xip(fixture):
    status = fixture["status"].map
    inxip = [x for x in sorted(fixture["snap"].xip)
             if status.get(x) == "committed"]
    assert len(inxip) >= 3, (
        f"only {len(inxip)} xid(s) in snapshot_xip have committed: {inxip}; "
        "ignoring snapshot_xip would barely be punished")


def test_snapshot_xip_is_interleaved_with_committed_transactions(fixture):
    """xip must be a sparse set inside [xmin, xmax), not a contiguous tail.

    Otherwise "anything at or above snapshot_xmin is still running" would be a
    correct shortcut and the explicit list would carry no information."""
    snap, status = fixture["snap"], fixture["status"].map
    gap = sorted(x for x, s in status.items()
                 if s == "committed" and snap.xmin <= x < snap.xmax
                 and x not in snap.xip)
    assert len(gap) >= 3, (
        f"only {len(gap)} committed xid(s) fall between snapshot_xmin and "
        f"snapshot_xmax while being absent from xip: {gap}")
    assert min(snap.xip) < max(gap) and min(gap) < max(snap.xip), (
        f"xip {sorted(snap.xip)} and the committed gap xids {gap} do not "
        "interleave, so the list would be equivalent to a plain range test")


def test_an_aborted_transaction_is_listed_in_snapshot_xip(fixture):
    status = fixture["status"].map
    aborted = [x for x in fixture["snap"].xip if status.get(x) == "aborted"]
    assert aborted, "no xid in snapshot_xip aborted"


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


def test_hint_bits_alone_are_insufficient(fixture):
    """The hints must be too incomplete to reconstruct the commit log."""
    status = fixture["status"].map
    no_hint = [t for t in fixture["tuples"]
               if not t.infomask & HEAP_XMIN_COMMITTED]
    aborted_unhinted = [t for t in fixture["tuples"]
                        if status.get(t.xmin) == "aborted"
                        and not t.infomask & HEAP_XMIN_INVALID]
    assert len(no_hint) >= 50, (
        f"only {len(no_hint)} tuple(s) lack HEAP_XMIN_COMMITTED")
    assert len(aborted_unhinted) >= 5, (
        f"only {len(aborted_unhinted)} tuple(s) from aborted transactions lack "
        "HEAP_XMIN_INVALID, so the hints would nearly give the commit log away")


# --------------------------------------------------- the answer is unique...
def test_exactly_one_visible_version_per_primary_key(fixture):
    idx = pk_index(fixture)
    seen = {}
    for t in fixture["tuples"]:
        if G.tuple_visible(t, fixture["snap"], fixture["status"],
                           dict(FRESH_NOTES)):
            key = t.values[idx]
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
    counts = {k: len(v) for k, v in versions_by_key(fixture).items()}
    multi = [k for k, n in counts.items() if n > 1]
    assert len(multi) >= 100, (
        f"only {len(multi)} key(s) have several physical versions")


def test_greatest_xmin_is_the_wrong_answer(fixture):
    """The naive "latest version wins" rule must disagree with the snapshot."""
    idx = pk_index(fixture)
    byk = versions_by_key(fixture)
    vis = visible_by_key(fixture)
    wrong = 0
    for key, versions in byk.items():
        newest = max(versions, key=lambda t: (t.xmin, t.block, t.lp))
        if vis.get(key) is not newest:
            wrong += 1
    assert wrong >= 60, (
        f"picking the greatest xmin is wrong for only {wrong} key(s); the trap "
        "is too weak")


def test_greatest_xmin_tuple_is_often_from_an_aborted_transaction(fixture):
    """max-xmin must fail specifically because of aborted work, not only
    because of transactions that committed after the snapshot."""
    status = fixture["status"].map
    n = 0
    for key, versions in versions_by_key(fixture).items():
        newest = max(versions, key=lambda t: (t.xmin, t.block, t.lp))
        if status.get(newest.xmin) == "aborted":
            n += 1
    assert n >= 15, (
        f"the greatest-xmin tuple belongs to an aborted transaction for only "
        f"{n} key(s)")


def test_ignoring_xmax_is_the_wrong_answer(fixture):
    """Rows the snapshot cannot see because they were deleted must exist."""
    notes = dict(FRESH_NOTES)
    deleted = 0
    for t in fixture["tuples"]:
        if G.xmin_committed(t, fixture["status"], notes) \
                and not fixture["snap"].in_progress(t.xmin) \
                and not G.tuple_visible(t, fixture["snap"],
                                        fixture["status"], notes):
            deleted += 1
    assert deleted >= 30, (
        f"only {deleted} tuple(s) are hidden by xmax alone; ignoring xmax "
        "would barely change the answer")
