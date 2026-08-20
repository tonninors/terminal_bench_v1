"""Assertions about the fixture itself.  Run with:  pytest build/fixture_test.py

These re-derive, from the solver-facing bytes alone, every property the v5
task claims: that all three heaps really are PostgreSQL 16 pages, that the
excluded tuple cases (MultiXact, combo CID, frozen rewrites, out-of-line
TOAST) are genuinely absent, that the surviving commit log is authentically
STALE for a controlled set of recent transactions, that the hint-bit evidence
is real but deliberately incomplete, that the constraint system pins exactly
one global outcome assignment and exactly one output, and that every naive
strategy the prompt must defeat lands measurably off the reference.
"""
from __future__ import annotations

import csv
import json
import struct
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(TASK / "solution"))
import golden_recover as G          # noqa: E402

ART = TASK / "artifacts"
SCHEMA = ART / "table_schema.json"
PG_XACT = ART / "pg_xact"
PG_SUBTRANS = ART / "pg_subtrans"
GOLDEN = TASK / "build" / "internal" / "golden.csv"
INFER = TASK / "build" / "internal" / "inference_report.json"
SCORES = TASK / "build" / "internal" / "negative_scores.json"

SOLVER_FILES = {"heap_pages.bin", "ledger_entries_heap.bin",
                "account_tags_heap.bin", "table_schema.json"}
SOLVER_DIRS = {"pg_xact", "pg_subtrans"}


@pytest.fixture(scope="module")
def fx():
    js = json.loads(SCHEMA.read_text(encoding="utf-8"))
    relations = [G.Relation(meta, ART) for meta in js["relations"]]
    relations.sort(key=lambda r: 0 if r.name == js["output_relation"] else 1)
    log = G.TransactionLog(PG_XACT, PG_SUBTRANS)
    snap = G.Snapshot(int(js["snapshot_xmin"]), int(js["snapshot_xmax"]),
                      [int(x) for x in js["snapshot_xip"]])
    ev = G.Evidence(relations, log, snap)
    engine = G.Engine(relations, log, snap, ev)
    survivors = engine.solve()
    tuples = [t for r in relations for t in r.tuples]
    return dict(js=js, relations=relations, log=log, snap=snap, ev=ev,
                engine=engine, survivors=survivors, tuples=tuples)


# ------------------------------------------------------------------ the bytes
def test_three_heaps_are_whole_8192_byte_blocks(fx):
    names = {r.name for r in fx["relations"]}
    assert names == {"accounts", "ledger_entries", "account_tags"}
    for r in fx["relations"]:
        assert r.heap_bytes % 8192 == 0
        assert r.tuples, f"{r.name} decoded no tuples"
    main = next(r for r in fx["relations"] if r.name == "accounts")
    assert main.heap_bytes // 8192 >= 2, "accounts must span several blocks"


def test_every_block_declares_the_postgresql_page_layout(fx):
    for meta in fx["js"]["relations"]:
        heap = (ART / meta["file"]).read_bytes()
        for blk in range(len(heap) // 8192):
            page = heap[blk * 8192:(blk + 1) * 8192]
            lower, upper, special, pv = struct.unpack_from("<HHHH", page, 12)
            if lower == 0 and upper == 0:
                continue
            assert pv & 0xFF00 == 8192, f"{meta['file']} block {blk}: size"
            assert pv & 0x00FF == 4, (
                f"{meta['file']} block {blk}: layout {pv & 0xFF}, "
                "PostgreSQL 8.3-16 uses 4")
            assert 24 <= lower <= upper <= special <= 8192


def test_schema_declares_postgresql_16(fx):
    assert fx["js"]["postgres_version"].startswith("16.")
    assert "PostgreSQL 16." in fx["js"]["postgres_version_full"]


def test_main_relation_keeps_its_v1_filename(fx):
    main = next(m for m in fx["js"]["relations"] if m["name"] == "accounts")
    assert main["file"] == "heap_pages.bin"
    assert fx["js"]["output_relation"] == "accounts"


# ------------------------------------------ deliberately excluded tuple cases
def test_no_multixact_xmax(fx):
    bad = [(t.rel, t.block, t.lp) for t in fx["tuples"]
           if t.infomask & G.HEAP_XMAX_IS_MULTI]
    assert not bad, f"MultiXact xmax present on {bad[:5]}"


def test_no_combo_command_ids(fx):
    bad = [(t.rel, t.block, t.lp) for t in fx["tuples"]
           if t.infomask & G.HEAP_COMBOCID]
    assert not bad


def test_no_frozen_tuples(fx):
    bad = [(t.rel, t.block, t.lp) for t in fx["tuples"]
           if (t.infomask & G.HEAP_XMIN_FROZEN) == G.HEAP_XMIN_FROZEN
           or t.xmin in (1, 2)]
    assert not bad, f"frozen tuple present on {bad[:5]}"


def test_no_out_of_line_toast_datum(fx):
    bad = [(t.rel, t.block, t.lp) for t in fx["tuples"]
           if t.infomask & G.HEAP_HASEXTERNAL]
    assert not bad


# ------------------------------------------------------------ page mechanics
def test_hot_pruning_left_marks_on_the_main_heap(fx):
    main = next(r for r in fx["relations"] if r.name == "accounts")
    assert main.census["LP_REDIRECT"] > 0, "no LP_REDIRECT slot"
    assert main.census["LP_DEAD"] > 0, "no pruned line pointer"


def test_heap_only_tuples_are_present(fx):
    hot = [t for t in fx["tuples"] if t.infomask2 & G.HEAP_ONLY_TUPLE]
    assert len(hot) >= 10, f"only {len(hot)} heap-only tuple(s)"


def test_lock_only_xmax_tuples_exist(fx):
    lo = [t for t in fx["tuples"]
          if t.xmax and G.xmax_is_locked_only(t.infomask)]
    assert len(lo) >= 3, f"only {len(lo)} lock-only tuple(s)"


def test_multiple_physical_versions_exist_for_keys(fx):
    main = next(r for r in fx["relations"] if r.name == "accounts")
    counts = {}
    for t in main.tuples:
        counts[main.key_of(t)] = counts.get(main.key_of(t), 0) + 1
    multi = [k for k, n in counts.items() if n > 1]
    # HOT pruning legitimately reclaims superseded versions, so only the
    # unprunable chains survive on disk
    assert len(multi) >= 5, f"only {len(multi)} key(s) have several versions"


# --------------------------------------------------- the stale commit log
def test_slru_directories_look_like_real_segments():
    for d, label in ((PG_XACT, "pg_xact"), (PG_SUBTRANS, "pg_subtrans")):
        assert d.is_dir(), f"{label}/ is missing"
        segs = sorted(p for p in d.iterdir() if p.is_file())
        assert segs, f"{label}/ holds no segment files"
        for f in segs:
            assert len(f.name) == 4 and all(c in "0123456789ABCDEFabcdef"
                                            for c in f.name), f.name
            size = f.stat().st_size
            assert size and size % 8192 == 0, (f.name, size)


def test_commit_log_is_a_bitmap_not_a_table():
    """A committed-heavy SLRU page is mostly 0x55 bytes (binary 01010101),
    which happens to be printable ASCII - so test for table structure, not
    printability: a decoded listing would be full of newlines and keywords."""
    for f in PG_XACT.iterdir():
        blob = f.read_bytes()
        assert blob.count(b"\n") < 16, f"{f.name} is line-structured text"
        for needle in (b"xid", b"commit", b"abort", b","):
            assert blob.count(needle) < 8, f"{f.name} contains {needle!r}"


def test_the_commit_log_tail_is_genuinely_stale(fx):
    """The load-bearing v5 property: a batch of transactions completed before
    the snapshot, yet the surviving pg_xact holds zero bits for every one of
    them - their clog page never reached disk before the crash."""
    ev = fx["ev"]
    assert 12 <= len(ev.unknown) <= 22, (
        f"{len(ev.unknown)} unresolved xid(s); the design floor is 12")
    for x in ev.unknown:
        assert fx["log"].raw_status(x) in (None, G.XACT_IN_PROGRESS), x
        assert fx["snap"].completed(x, fx["log"]), (
            f"xid {x} counted unresolved but the snapshot does not prove "
            "it completed")


def test_the_commit_log_head_still_resolves_old_transactions(fx):
    ev = fx["ev"]
    assert len(ev.recorded) >= 5, (
        f"only {len(ev.recorded)} xid(s) resolve from the surviving clog")
    assert set(ev.recorded.values()) == {"committed", "aborted"}, (
        "the surviving clog must show both outcomes")


def test_unresolved_transactions_are_recent_and_recorded_are_old(fx):
    """The staleness must look like a lost TAIL, not scattered corruption."""
    ev = fx["ev"]
    assert min(ev.unknown) > max(ev.recorded), (
        "unresolved xids do not form a contiguous recent tail above the "
        "recorded ones")


def test_in_progress_writers_touch_the_heap(fx):
    ev, snap = fx["ev"], fx["snap"]
    assert snap.xip, "snapshot_xip is empty"
    touched = {t.xmin for t in fx["tuples"]} | {t.xmax for t in fx["tuples"]
                                                if t.xmax}
    assert set(snap.xip) & touched, "no in-progress writer reached the pages"
    for x in snap.xip:
        assert x in ev.irrelevant or x in ev.recorded, (
            f"xip xid {x} was mis-classified as unresolved")


def test_subtransactions_appear_in_pg_subtrans(fx):
    parents = {x: fx["log"].parent(x) for x in
               range(fx["snap"].xmin - 200, fx["snap"].xmax)}
    linked = [x for x, p in parents.items() if p]
    assert linked, "pg_subtrans links no xid in the recent window"


# ------------------------------------------------------------- the evidence
def test_hint_bits_are_present_but_incomplete(fx):
    ev = fx["ev"]
    rep = json.loads(INFER.read_text(encoding="utf-8"))
    direct = set(rep["direct_hint_xids"])
    indirect = set(rep["indirect_xids"])
    assert set(ev.hints) == direct
    assert direct | indirect == ev.unknown and not (direct & indirect)
    assert len(indirect) >= max(4, int(0.30 * len(ev.unknown))), (
        f"only {len(indirect)} of {len(ev.unknown)} unresolved xid(s) lack a "
        "direct hint")


def test_hints_cover_both_outcomes(fx):
    outcomes = set(fx["ev"].hints.values())
    assert outcomes == {"committed", "aborted"}, (
        f"hint outcomes seen: {sorted(outcomes)}")


def test_hints_live_on_more_than_one_relation(fx):
    rels = {src.split("(")[0]
            for srcs in fx["ev"].hint_sources.values() for src in srcs}
    assert len(rels) >= 2, (
        f"all hints sit on {rels}; cross-relation atomicity would be idle")


def test_unresolved_transactions_span_relations(fx):
    """Atomicity must be load-bearing: unresolved xids touch several heaps."""
    span = {}
    for t in fx["tuples"]:
        for x in (t.xmin, t.xmax):
            if x in fx["ev"].unknown:
                span.setdefault(x, set()).add(t.rel)
    multi = [x for x, rels in span.items() if len(rels) >= 2]
    assert len(multi) >= 4, (
        f"only {len(multi)} unresolved xid(s) touch more than one relation")


def test_hint_bits_never_contradict_recorded_outcomes(fx):
    # Evidence() raises on contradiction; reaching here proves consistency.
    assert fx["ev"].hints or fx["ev"].recorded


# ------------------------------------------------- the machine-proven answer
def test_exactly_one_assignment_survives(fx):
    funnel = fx["engine"].funnel
    assert funnel["after_all_constraints"] == 1, funnel
    assert funnel["distinct_outputs"] == 1, funnel


def test_hints_alone_do_not_pin_the_answer(fx):
    funnel = fx["engine"].funnel
    assert funnel["after_hint_filter"] > 1, (
        "the hint bits alone already pin the assignment; the constraint "
        "system would be decorative")


def test_keys_alone_do_not_pin_the_answer(fx):
    funnel = fx["engine"].funnel
    assert funnel["after_key_uniqueness"] > funnel["after_all_constraints"], (
        "PK/UNIQUE alone already pins the assignment; the foreign keys "
        "would be decorative")


def test_funnel_matches_the_recorded_inference_report(fx):
    rep = json.loads(INFER.read_text(encoding="utf-8"))
    assert fx["engine"].funnel == rep["funnel"]
    want = {int(x): o for x, o in rep["final_assignment"].items()}
    assert fx["survivors"] and fx["survivors"][0] == want


def test_the_unique_survivor_reproduces_the_postgresql_reference(fx):
    with tempfile.TemporaryDirectory() as d:
        out = Path(d) / "recovered.csv"
        r = subprocess.run([sys.executable,
                            str(TASK / "solution" / "golden_recover.py"),
                            "--dir", str(ART), "--out", str(out)],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        got = [row for row in csv.reader(out.read_text(encoding="utf-8")
                                         .splitlines()) if row]
    want = [row for row in csv.reader(GOLDEN.read_text(encoding="utf-8")
                                      .splitlines()) if row]
    assert got[0] == want[0]
    assert sorted(got[1:]) == sorted(want[1:]), (
        "the inference answer differs from the state PostgreSQL itself "
        "reported under this snapshot")


def test_the_answer_depends_on_the_unresolved_outcomes(fx):
    """Flipping any single indirect xid must change visibility somewhere -
    otherwise that xid would be dead weight."""
    rep = json.loads(INFER.read_text(encoding="utf-8"))
    truth = {int(x): o for x, o in rep["final_assignment"].items()}
    fn = G.outcome_fn(fx["ev"], truth)
    base = {r.name: {(t.block, t.lp) for t in r.tuples
                     if G.tuple_visible(t, fx["snap"], fx["log"], fn)}
            for r in fx["relations"]}
    for x in map(int, rep["indirect_xids"]):
        flipped = dict(truth)
        flipped[x] = ("aborted" if truth[x] == "committed" else "committed")
        fn2 = G.outcome_fn(fx["ev"], flipped)
        changed = any(
            {(t.block, t.lp) for t in r.tuples
             if G.tuple_visible(t, fx["snap"], fx["log"], fn2)} != base[r.name]
            for r in fx["relations"])
        assert changed, f"flipping xid {x} changes nothing"


# ----------------------------------------------------------- no leakage
def test_artifacts_hold_exactly_the_solver_files():
    files = {p.name for p in ART.iterdir() if p.is_file()}
    dirs = {p.name for p in ART.iterdir() if p.is_dir()}
    assert files == SOLVER_FILES, files
    assert dirs == SOLVER_DIRS, dirs


def test_solver_files_carry_no_decoded_answer():
    golden = GOLDEN.read_bytes()
    expected = (TASK / "tests" / "expected_state.json").read_bytes()
    digest = json.loads(expected.decode("utf-8"))["sha256"].encode()
    files = [ART / n for n in SOLVER_FILES]
    for d in SOLVER_DIRS:
        files += [f for f in (ART / d).rglob("*") if f.is_file()]
    for f in files:
        blob = f.read_bytes()
        assert golden not in blob, f"{f.name} embeds the reference answer"
        assert expected not in blob, f"{f.name} embeds the verifier fixture"
        assert digest not in blob, f"{f.name} embeds the expected digest"


def test_schema_carries_no_answer_keys():
    meta = json.loads(SCHEMA.read_text(encoding="utf-8"))
    for key in ("rows", "visible", "answer", "sha256", "expected", "status",
                "tx_status", "outcomes", "assignment", "hints"):
        assert key not in meta, f"table_schema.json carries a {key!r} key"
    blob = SCHEMA.read_text(encoding="utf-8")
    for needle in ("committed", "aborted", "hint", "unresolved"):
        assert needle not in blob, (
            f"table_schema.json mentions {needle!r}; outcome vocabulary must "
            "not leak into the solver metadata")


def test_no_stale_artifacts_from_earlier_versions():
    assert not (ART / "tx_status.csv").exists()
    assert not (ART / "pg_multixact").exists()


# ------------------------------------------------- the naive strategies fail
def test_every_negative_strategy_misses_the_reference():
    scores = json.loads(SCORES.read_text(encoding="utf-8"))
    assert len(scores["strategies"]) == 12
    for label, s in scores["strategies"].items():
        assert s["keys_wrong_in_total"] >= 1, f"{label} matches the reference"


def test_most_negative_failures_are_systemic():
    scores = json.loads(SCORES.read_text(encoding="utf-8"))
    systemic = [label for label, s in scores["strategies"].items()
                if s["keys_wrong_in_total"] >= 10]
    assert len(systemic) >= 8, (
        f"only {len(systemic)} strategies lose 10+ keys: {systemic}")


def test_zero_fill_produces_a_plausible_looking_wrong_csv():
    """The trap answer must survive shape checks: valid header, no duplicate
    keys, plausible row count - and still be far from the reference."""
    scores = json.loads(SCORES.read_text(encoding="utf-8"))
    z = scores["strategies"]["1. zero-fill: missing clog = aborted"]
    assert z["duplicate_key_records"] == 0
    assert z["rows"] >= 100
    assert z["keys_wrong_in_total"] >= 50
