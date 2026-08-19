#!/usr/bin/env python3
"""Deterministic generator for the `sqlite-wal-crash-recovery` Terminal Bench task.

Produces, from nothing but this source file:

  artifacts/ledger.db          solver-facing, damaged main database
  artifacts/ledger.db-wal      solver-facing, damaged-header WAL with a crash tail
  build/internal/golden.db     hidden ground truth (state at the last real commit)
  tests/expected_state.json    verifier fixture derived from golden.db
  build/internal/generation_report.json

Run:  python3 build/generate_case.py
"""
from __future__ import annotations

import json
import os
import random
import shutil
import sqlite3
import struct
import sys
import hashlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
TASK = HERE.parent
sys.path.insert(0, str(HERE))

import walkit as W                     # noqa: E402
import ledger_schema as L              # noqa: E402

SEED = 20260819
WORK = HERE / "_work"
ARTIFACTS = TASK / "artifacts"
INTERNAL = HERE / "internal"
TESTS = TASK / "tests"

# Fixed values stamped onto the artifact WAL so the output is byte-deterministic
# even though SQLite chooses its salts randomly.
ART_SALT1 = 0x5C3A19E7
ART_SALT2 = 0xA1447BD2
ART_CKPT_SEQ = 1
ART_MAGIC = W.WAL_MAGIC_BE_CKSUM       # big-endian checksum words
STALE_SALT1 = 0x1D77F204
STALE_SALT2 = 0x93B0C5AE
STALE_SEED = (0x2F81A6C4, 0xD4530E19)

N_ACCOUNTS = 64
N_BASE_ENTRIES = 320


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def connect(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path), isolation_level=None)
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA synchronous=FULL")
    return conn


def copy_pair(src_db: Path, dst_db: Path) -> None:
    dst_db.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(src_db, dst_db)
    shutil.copyfile(Path(str(src_db) + "-wal"), Path(str(dst_db) + "-wal"))


def sha256_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


# ---------------------------------------------------------------------------
# phase 1: base database + checkpointed base state
# ---------------------------------------------------------------------------

def build_base(db: Path) -> None:
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute(f"PRAGMA page_size={L.PAGE_SIZE}")
    conn.execute("PRAGMA auto_vacuum=NONE")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.executescript(L.SCHEMA_SQL)

    rng = random.Random(SEED)
    conn.execute("BEGIN")
    conn.executemany(
        "INSERT INTO accounts(account_id,account_no,owner_name,currency,status,"
        "opened_on,balance_minor) VALUES (?,?,?,?,?,?,?)",
        L.make_accounts(rng, N_ACCOUNTS),
    )
    for eid in range(1, N_BASE_ENTRIES + 1):
        conn.execute(
            "INSERT INTO journal_entries(entry_id,entry_uuid,posted_on,state,"
            "fx_rate,narrative,memo,payload) VALUES (?,?,?,?,?,?,?,?)",
            L.make_entry(rng, eid, 100),
        )
        conn.executemany(
            "INSERT INTO postings(entry_id,leg_no,account_id,amount_minor,leg_note)"
            " VALUES (?,?,?,?,?)",
            L.make_legs(rng, eid, N_ACCOUNTS),
        )
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    conn.close()
    wal = Path(str(db) + "-wal")
    assert (not wal.exists()) or wal.stat().st_size == 0, "base WAL not truncated"


# ---------------------------------------------------------------------------
# phase 2: the three committed transactions kept in the WAL
# ---------------------------------------------------------------------------

def txn_t1(conn, rng, marks):
    """New batch of settlement entries (grows the database)."""
    ids = list(range(N_BASE_ENTRIES + 1, N_BASE_ENTRIES + 27))
    conn.execute("BEGIN")
    for eid in ids:
        conn.execute(
            "INSERT INTO journal_entries(entry_id,entry_uuid,posted_on,state,"
            "fx_rate,narrative,memo,payload) VALUES (?,?,?,?,?,?,?,?)",
            L.make_entry(rng, eid, 220),
        )
        conn.executemany(
            "INSERT INTO postings(entry_id,leg_no,account_id,amount_minor,leg_note)"
            " VALUES (?,?,?,?,?)",
            L.make_legs(rng, eid, N_ACCOUNTS),
        )
    conn.execute("COMMIT")
    marks["t1_entry_ids"] = ids


def txn_t2(conn, rng, marks):
    """Amend narratives/memos of existing entries and re-key some accounts."""
    targets = sorted(rng.sample(range(1, N_BASE_ENTRIES + 1), 34))
    conn.execute("BEGIN")
    for eid in targets:
        conn.execute(
            "UPDATE journal_entries SET narrative=?, memo=?, fx_rate=? WHERE entry_id=?",
            (L._sentence(rng, 7), L._sentence(rng, 400)[:3200],
             round(0.4 + rng.random() * 3.0, 6), eid),
        )
    acct_targets = sorted(rng.sample(range(1, N_ACCOUNTS + 1), 6))
    for aid in acct_targets:
        conn.execute("UPDATE accounts SET status='open' WHERE account_id=? AND status<>'open'",
                     (aid,))
    conn.execute("COMMIT")
    marks["t2_entry_ids"] = targets


def txn_t3(conn, rng, marks):
    """The LAST fully committed transaction: post a batch and settle pending work."""
    ids = list(range(N_BASE_ENTRIES + 27, N_BASE_ENTRIES + 41))
    conn.execute("BEGIN")
    for eid in ids:
        conn.execute(
            "INSERT INTO journal_entries(entry_id,entry_uuid,posted_on,state,"
            "fx_rate,narrative,memo,payload) VALUES (?,?,?,?,?,?,?,?)",
            L.make_entry(rng, eid, 260),
        )
        conn.executemany(
            "INSERT INTO postings(entry_id,leg_no,account_id,amount_minor,leg_note)"
            " VALUES (?,?,?,?,?)",
            L.make_legs(rng, eid, N_ACCOUNTS),
        )
    settled = [r[0] for r in conn.execute(
        "SELECT entry_id FROM journal_entries WHERE state='pending' "
        "AND entry_id <= 120 ORDER BY entry_id LIMIT 18")]
    for eid in settled:
        conn.execute("UPDATE journal_entries SET state='posted' WHERE entry_id=?", (eid,))
    conn.execute("UPDATE accounts SET status='frozen' WHERE account_id IN (11,29)")
    conn.execute("COMMIT")
    marks["t3_entry_ids"] = ids
    marks["t3_settled_ids"] = settled


def txn_t4(conn, rng, marks):
    """The crash tail.  Committed by SQLite here only so that we can harvest real
    page images; in the published artifact its commit frame is never written."""
    ids = list(range(N_BASE_ENTRIES + 41, N_BASE_ENTRIES + 50))
    conn.execute("BEGIN")
    for eid in ids:
        conn.execute(
            "INSERT INTO journal_entries(entry_id,entry_uuid,posted_on,state,"
            "fx_rate,narrative,memo,payload) VALUES (?,?,?,?,?,?,?,?)",
            L.make_entry(rng, eid, 300),
        )
        conn.executemany(
            "INSERT INTO postings(entry_id,leg_no,account_id,amount_minor,leg_note)"
            " VALUES (?,?,?,?,?)",
            L.make_legs(rng, eid, N_ACCOUNTS),
        )
    voided = list(range(200, 206))
    for eid in voided:
        conn.execute("UPDATE journal_entries SET state='void' WHERE entry_id=?", (eid,))
    dropped = [17, 41, 58, 93]
    for eid in dropped:
        conn.execute("DELETE FROM journal_entries WHERE entry_id=?", (eid,))
    frozen = [3, 22, 47]
    conn.execute(
        "UPDATE accounts SET status='frozen' WHERE account_id IN (%s)"
        % ",".join(str(a) for a in frozen))
    conn.execute("COMMIT")
    marks["t4_entry_ids"] = ids
    marks["t4_voided_ids"] = voided
    marks["t4_deleted_ids"] = dropped
    marks["t4_frozen_accounts"] = frozen


# ---------------------------------------------------------------------------
# phase 3: assemble the damaged artifacts
# ---------------------------------------------------------------------------

def classify_pages(db_copy: Path) -> dict[int, tuple[str, str]]:
    conn = sqlite3.connect(str(db_copy))
    out = {}
    for name, pagetype, pageno in conn.execute(
            "SELECT name, pagetype, pageno FROM dbstat"):
        out[pageno] = (pagetype, name)
    conn.close()
    for side in ("-wal", "-shm"):
        p = Path(str(db_copy) + side)
        if p.exists():
            p.unlink()
    return out


def build_stale_generation(dirpath: Path) -> list[W.Frame]:
    """A short, self-consistent WAL from an *earlier* generation of the same file:
    same page size, different salts.  Left behind in the tail of the WAL because
    SQLite does not zero the file when a WAL is restarted."""
    dirpath.mkdir(parents=True, exist_ok=True)
    db = dirpath / "old.db"
    conn = sqlite3.connect(str(db), isolation_level=None)
    conn.execute(f"PRAGMA page_size={L.PAGE_SIZE}")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE stale(k INTEGER PRIMARY KEY, v TEXT, b BLOB)")
    rng = random.Random(SEED ^ 0x5151)
    conn.execute("BEGIN")
    for i in range(1, 200):
        conn.execute("INSERT INTO stale VALUES (?,?,?)",
                     (i, L._sentence(rng, 40), bytes(rng.randrange(256) for _ in range(300))))
    conn.execute("COMMIT")
    conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    conn.execute("BEGIN")
    for i in range(1, 60):
        conn.execute("UPDATE stale SET v=? WHERE k=?", (L._sentence(rng, 30), i))
    conn.execute("COMMIT")
    conn.execute("BEGIN")
    for i in range(60, 100):
        conn.execute("UPDATE stale SET v=? WHERE k=?", (L._sentence(rng, 25), i))
    conn.execute("COMMIT")
    blob = (dirpath / "old.db-wal").read_bytes()
    conn.close()
    hdr, frames = W.parse_wal(blob)
    assert hdr.page_size == L.PAGE_SIZE
    assert W.verify_chain(hdr, frames) == len(frames), "stale generation is not self-consistent"
    return frames[:14]


def main() -> int:
    if WORK.exists():
        shutil.rmtree(WORK)
    WORK.mkdir(parents=True)
    ARTIFACTS.mkdir(exist_ok=True)
    INTERNAL.mkdir(exist_ok=True)
    TESTS.mkdir(exist_ok=True)

    marks: dict = {}
    stage = WORK / "stage" / "ledger.db"
    stage.parent.mkdir(parents=True)
    build_base(stage)

    conn = connect(stage)
    rng = random.Random(SEED ^ 0xA5A5)
    txn_t1(conn, rng, marks)
    txn_t2(conn, rng, marks)
    txn_t3(conn, rng, marks)

    # crash snapshot: exactly what is on disk at the instant T3 has committed
    snap = WORK / "snapshot" / "ledger.db"
    copy_pair(stage, snap)
    conn.close()                                     # stage is now irrelevant

    # ---- harvest the crash-tail page images -------------------------------
    t4dir = WORK / "t4" / "ledger.db"
    copy_pair(snap, t4dir)
    c4 = connect(t4dir)
    rng4 = random.Random(SEED ^ 0x3C3C)
    txn_t4(c4, rng4, marks)
    t4_wal_blob = Path(str(t4dir) + "-wal").read_bytes()
    c4.close()

    snap_wal_blob = Path(str(snap) + "-wal").read_bytes()
    snap_db_blob = Path(snap).read_bytes()

    hdr_s, frames_s = W.parse_wal(snap_wal_blob)
    assert W.verify_chain(hdr_s, frames_s) == len(frames_s), "snapshot WAL chain invalid"
    native_be = hdr_s.big_endian_cksum
    hdr_t, frames_t = W.parse_wal(t4_wal_blob)
    assert W.verify_chain(hdr_t, frames_t) == len(frames_t), "T4 WAL chain invalid"
    assert len(frames_t) > len(frames_s)
    for a, b in zip(frames_s, frames_t):
        assert (a.pgno, a.db_size, a.data) == (b.pgno, b.db_size, b.data), "WAL prefix diverged"

    commit_idx = [i for i, f in enumerate(frames_s) if f.is_commit]
    assert len(commit_idx) == 3, f"expected 3 committed transactions, got {len(commit_idx)}"
    assert commit_idx[-1] == len(frames_s) - 1, "snapshot WAL does not end on a commit"

    tail = frames_t[len(frames_s):]
    tail_commits = [i for i, f in enumerate(tail) if f.is_commit]
    assert tail_commits == [len(tail) - 1], "crash tail must contain exactly one commit frame, last"
    tail_commit_db_size = tail[-1].db_size
    # the crash: the commit frame's payload reached the log, its commit marker did not
    tail[-1].db_size = 0

    # ---- golden state (uncorrupted snapshot, checkpointed by SQLite itself) --
    gold = WORK / "gold" / "ledger.db"
    copy_pair(snap, gold)
    gconn = sqlite3.connect(str(gold), isolation_level=None)
    gconn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    gconn.execute("PRAGMA journal_mode=DELETE")
    assert gconn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
    assert gconn.execute("PRAGMA foreign_key_check").fetchall() == []
    gconn.close()
    for side in ("-wal", "-shm"):
        p = Path(str(gold) + side)
        if p.exists():
            p.unlink()
    shutil.copyfile(gold, INTERNAL / "golden.db")

    # ---- replay-everything state (used only to prove the trap discriminates) --
    replay = WORK / "replay" / "ledger.db"
    replay.parent.mkdir(parents=True)
    shutil.copyfile(snap, replay)
    apply_frames(replay, frames_s + tail, L.PAGE_SIZE, db_size=tail_commit_db_size)
    shutil.copyfile(replay, INTERNAL / "replay_all.db")

    # ---- assemble the damaged WAL -----------------------------------------
    art_hdr = W.WalHeader(ART_MAGIC, W.WAL_FORMAT_VERSION, L.PAGE_SIZE,
                          ART_CKPT_SEQ, ART_SALT1, ART_SALT2, 0, 0)
    good_wal = W.build_wal(art_hdr, frames_s + tail)
    live_frames = len(frames_s) + len(tail)
    frame_size = W.WAL_FRAME_HDR_SIZE + L.PAGE_SIZE

    def slot(i: int) -> bytes:
        o = W.WAL_HDR_SIZE + i * frame_size
        return good_wal[o:o + frame_size]

    # Slots beyond the crash tail still hold frames that this same generation
    # wrote earlier and later superseded (a transaction was rolled back and the
    # slots were being rewritten when the machine died).  They carry the current
    # salts and one of them carries a commit marker, so only the rolling
    # checksum -- which is position dependent -- can rule them out.
    sup_lo = commit_idx[1] - 3
    sup_hi = commit_idx[1] + 3
    superseded = b"".join(slot(i) for i in range(sup_lo, sup_hi))
    assert any(frames_s[i].is_commit for i in range(sup_lo, sup_hi)), \
        "the superseded region must contain a commit marker"

    # Further out, frames from an *older* generation of this WAL survive: the
    # file was never truncated when the log was restarted, so they still carry
    # that generation's salts.
    stale_frames = build_stale_generation(WORK / "stale")
    stale_bytes = W.chain_foreign_frames(stale_frames, STALE_SALT1, STALE_SALT2,
                                         STALE_SEED[0], STALE_SEED[1], big_endian=False)
    wal_out = bytearray(good_wal + superseded + stale_bytes)

    # sanity: as assembled (before header damage) the artifact WAL is genuinely valid
    h2, f2 = W.parse_wal(bytes(wal_out))
    assert W.verify_chain(h2, f2) == live_frames, \
        "artifact WAL chain must validate exactly up to the end of the crash tail"
    assert f2[live_frames].salt1 == ART_SALT1 and f2[live_frames].salt2 == ART_SALT2, \
        "the superseded region must share the live generation's salts"
    assert any(f.is_commit for f in f2[live_frames:live_frames + (sup_hi - sup_lo)]), \
        "the superseded region must expose a commit marker to a salt-only scan"

    # destroy the WAL header's descriptive fields, keep its checksum words
    hrng = random.Random(SEED ^ 0x7E7E)
    for i in range(0, 24):
        wal_out[i] = hrng.randrange(256)
    assert bytes(wal_out[24:32]) == good_wal[24:32]

    # ---- corrupt main-database pages --------------------------------------
    probe = WORK / "probe" / "ledger.db"
    probe.parent.mkdir(parents=True)
    shutil.copyfile(snap, probe)
    pagetypes = classify_pages(probe)
    base_pages = len(snap_db_blob) // L.PAGE_SIZE

    last_frame_of_page: dict[int, int] = {}
    for i, f in enumerate(frames_s):
        last_frame_of_page[f.pgno] = i
    txn_of_frame = {}
    lo = 0
    for t, c in enumerate(commit_idx):
        for i in range(lo, c + 1):
            txn_of_frame[i] = t
        lo = c + 1

    def pick(txn: int, prefer: tuple[str, ...]) -> int:
        cands = [p for p, i in sorted(last_frame_of_page.items())
                 if txn_of_frame[i] == txn and 1 < p <= base_pages]
        assert cands, f"no corruption candidate in T{txn + 1}"
        for want in prefer:
            for p in cands:
                if pagetypes.get(p, ("?", "?"))[0] == want:
                    return p
        return cands[0]

    p_a = pick(0, ("overflow", "leaf"))
    p_b = pick(2, ("internal", "leaf", "overflow"))
    p_c = pick(1, ("leaf", "overflow"))

    def pick_overflow(exclude: set[int]) -> int:
        cands = [p for p, i in sorted(last_frame_of_page.items())
                 if 1 < p <= base_pages and p not in exclude
                 and pagetypes.get(p, ("?", "?"))[0] == "overflow"]
        assert cands, "no overflow page available for corruption"
        return cands[len(cands) // 2]

    p_d = pick_overflow({p_a, p_b, p_c})
    assert len({p_a, p_b, p_c, p_d}) == 4, "corruption targets must be distinct"

    db_out = bytearray(snap_db_blob)
    crng = random.Random(SEED ^ 0x1234)

    def wipe(pgno: int) -> None:
        off = (pgno - 1) * L.PAGE_SIZE
        db_out[off:off + L.PAGE_SIZE] = bytes(crng.randrange(256) for _ in range(L.PAGE_SIZE))

    def nick(pgno: int) -> None:
        off = (pgno - 1) * L.PAGE_SIZE + 1400
        db_out[off:off + 64] = bytes(crng.randrange(256) for _ in range(64))

    wipe(p_a)
    wipe(p_b)
    nick(p_c)
    wipe(p_d)

    (ARTIFACTS / "ledger.db").write_bytes(bytes(db_out))
    (ARTIFACTS / "ledger.db-wal").write_bytes(bytes(wal_out))

    report = {
        "seed": SEED,
        "page_size": L.PAGE_SIZE,
        "sqlite_version": sqlite3.sqlite_version,
        "host_wal_checksum_big_endian_native": native_be,
        "artifact_wal_checksum_big_endian": bool(ART_MAGIC & 1),
        "base_pages_in_main_db": base_pages,
        "committed_frames": len(frames_s),
        "crash_tail_frames": len(tail),
        "superseded_same_salt_frames": sup_hi - sup_lo,
        "superseded_slot_range_0based": [sup_lo, sup_hi],
        "stale_generation_frames": len(stale_frames),
        "commit_frame_indexes_0based": commit_idx,
        "committed_db_size_pages": frames_s[commit_idx[-1]].db_size,
        "corrupted_pages": {
            "wiped_recoverable_from_T1": {"pgno": p_a, "type": pagetypes.get(p_a)},
            "wiped_recoverable_from_T3": {"pgno": p_b, "type": pagetypes.get(p_b)},
            "subtly_damaged_recoverable_from_T2": {"pgno": p_c, "type": pagetypes.get(p_c)},
            "wiped_overflow_page": {"pgno": p_d, "type": pagetypes.get(p_d),
                                    "last_written_by_txn": txn_of_frame[last_frame_of_page[p_d]] + 1},
        },
        "artifact_salts": {"salt1": ART_SALT1, "salt2": ART_SALT2,
                           "ckpt_seq": ART_CKPT_SEQ, "magic": ART_MAGIC},
        "stale_salts": {"salt1": STALE_SALT1, "salt2": STALE_SALT2},
        "marks": marks,
        "sha256": {
            "ledger.db": sha256_file(ARTIFACTS / "ledger.db"),
            "ledger.db-wal": sha256_file(ARTIFACTS / "ledger.db-wal"),
            "golden.db": sha256_file(INTERNAL / "golden.db"),
        },
    }

    verify_traps(report)
    write_expected_state(INTERNAL / "golden.db", INTERNAL / "replay_all.db", marks)
    (INTERNAL / "generation_report.json").write_text(json.dumps(report, indent=2) + "\n")
    print(json.dumps({k: report[k] for k in
                      ("committed_frames", "crash_tail_frames", "stale_generation_frames",
                       "committed_db_size_pages", "corrupted_pages", "sha256")}, indent=2))
    return 0


def apply_frames(db_path: Path, frames, page_size: int, db_size: int) -> None:
    """Overlay page images onto a database file and truncate to `db_size` pages."""
    blob = bytearray(db_path.read_bytes())
    need = db_size * page_size
    if len(blob) < need:
        blob.extend(b"\x00" * (need - len(blob)))
    for f in frames:
        off = (f.pgno - 1) * page_size
        if off + page_size > len(blob):
            blob.extend(b"\x00" * (off + page_size - len(blob)))
        blob[off:off + page_size] = f.data
    del blob[need:]
    # a standalone database file: mark the header as rollback-journal mode so the
    # result needs no -wal sidecar
    blob[18] = 1
    blob[19] = 1
    db_path.write_bytes(bytes(blob))


# ---------------------------------------------------------------------------
# phase 4: prove the artifact actually traps the naive answers
# ---------------------------------------------------------------------------

def open_ro(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"file:{path}?mode=ro", uri=True)


def logical_fingerprint(path: Path) -> dict:
    conn = sqlite3.connect(str(path))
    try:
        out = {t: W.table_digest(conn, t) for t in W.user_tables(conn)}
    finally:
        conn.close()
    for side in ("-wal", "-shm"):
        p = Path(str(path) + side)
        if p.exists():
            p.unlink()
    return out


def verify_traps(report: dict) -> None:
    gold = INTERNAL / "golden.db"
    gold_fp = logical_fingerprint(gold)

    # (a) the supplied main database on its own is NOT the answer
    tmp = WORK / "trap_main" / "ledger.db"
    tmp.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ARTIFACTS / "ledger.db", tmp)
    conn = sqlite3.connect(str(tmp))
    try:
        ic = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        ic = [(f"error: {exc}",)]
    conn.close()
    for side in ("-wal", "-shm"):
        p = Path(str(tmp) + side)
        if p.exists():
            p.unlink()
    assert ic != [("ok",)], "the damaged main database must NOT pass integrity_check"
    report["trap_main_db_integrity_check"] = [r[0] for r in ic][:3]

    # (b) handing SQLite the pair does not silently recover anything
    pair = WORK / "trap_pair" / "ledger.db"
    pair.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(ARTIFACTS / "ledger.db", pair)
    shutil.copyfile(ARTIFACTS / "ledger.db-wal", Path(str(pair) + "-wal"))
    conn = sqlite3.connect(str(pair))
    cp = None
    try:
        cp = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        ic2 = conn.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.DatabaseError as exc:
        ic2 = [(f"error: {exc}",)]
    finally:
        conn.close()
    assert ic2 != [("ok",)], "SQLite's own WAL recovery must not fix the database"
    report["trap_auto_recovery"] = {"wal_checkpoint": list(cp) if cp else None,
                                    "integrity_check": [r[0] for r in ic2][:3]}

    # (c) replaying every frame yields a structurally fine but logically wrong db
    replay_fp = logical_fingerprint(INTERNAL / "replay_all.db")
    conn = sqlite3.connect(str(INTERNAL / "replay_all.db"))
    ric = conn.execute("PRAGMA integrity_check").fetchall()
    rfk = conn.execute("PRAGMA foreign_key_check").fetchall()
    conn.close()
    for side in ("-wal", "-shm"):
        p = Path(str(INTERNAL / "replay_all.db") + side)
        if p.exists():
            p.unlink()
    assert ric == [("ok",)], "replay-all must produce a structurally valid database"
    assert rfk == [], "replay-all must pass foreign_key_check"
    differing = sorted(t for t in gold_fp if gold_fp[t] != replay_fp.get(t))
    assert len(differing) >= 4, f"replay-all differs in too few tables: {differing}"
    report["trap_replay_all"] = {"integrity_check": "ok", "differing_tables": differing}


# ---------------------------------------------------------------------------
# phase 5: verifier fixture
# ---------------------------------------------------------------------------

def write_expected_state(gold: Path, replay: Path, marks: dict) -> None:
    conn = sqlite3.connect(f"file:{gold}?mode=ro", uri=True)
    rconn = sqlite3.connect(f"file:{replay}?mode=ro", uri=True)
    try:
        tables = W.user_tables(conn)
        expected = {
            "schema": W.schema_snapshot(conn),
            "table_info": {t: [list(r) for r in conn.execute(f'PRAGMA table_info("{t}")')]
                           for t in tables},
            "index_list": {t: sorted([r[1], int(r[2])] for r in
                                     conn.execute(f'PRAGMA index_list("{t}")'))
                           for t in tables},
            "tables": {},
            "assertions": [],
        }
        for t in tables:
            n, h = W.table_digest(conn, t)
            expected["tables"][t] = {"row_count": n, "sha256": h}

        def q(sql, params=()):
            return conn.execute(sql, params).fetchone()[0]

        def rq(sql, params=()):
            return rconn.execute(sql, params).fetchone()[0]

        def add(label, sql, params=()):
            want = q(sql, params)
            got_replay = rq(sql, params)
            expected["assertions"].append({
                "label": label, "sql": sql, "params": list(params), "expected": want,
                "discriminates_replay_all": want != got_replay,
            })

        t3_uuids = [L.entry_uuid(e) for e in marks["t3_entry_ids"]]
        t4_uuids = [L.entry_uuid(e) for e in marks["t4_entry_ids"]]
        ph3 = ",".join("?" * len(t3_uuids))
        ph4 = ",".join("?" * len(t4_uuids))
        add("last-commit rows present",
            f"SELECT COUNT(*) FROM journal_entries WHERE entry_uuid IN ({ph3})", t3_uuids)
        add("last-commit postings present",
            "SELECT COUNT(*) FROM postings WHERE entry_id BETWEEN ? AND ?",
            (min(marks["t3_entry_ids"]), max(marks["t3_entry_ids"])))
        add("last-commit state changes applied",
            "SELECT COUNT(*) FROM journal_entries WHERE state='posted' AND entry_id IN (%s)"
            % ",".join(str(e) for e in marks["t3_settled_ids"]), ())
        add("last-commit account freeze applied",
            "SELECT COUNT(*) FROM accounts WHERE account_id IN (11,29) AND status='frozen'")
        add("crash-tail rows absent",
            f"SELECT COUNT(*) FROM journal_entries WHERE entry_uuid IN ({ph4})", t4_uuids)
        add("crash-tail postings absent",
            "SELECT COUNT(*) FROM postings WHERE entry_id >= ?",
            (min(marks["t4_entry_ids"]),))
        add("crash-tail voids not applied",
            "SELECT COUNT(*) FROM journal_entries WHERE state='void' AND entry_id IN (%s)"
            % ",".join(str(e) for e in marks["t4_voided_ids"]), ())
        add("crash-tail deletes not applied",
            "SELECT COUNT(*) FROM journal_entries WHERE entry_id IN (%s)"
            % ",".join(str(e) for e in marks["t4_deleted_ids"]), ())
        add("crash-tail account freezes not applied",
            "SELECT COUNT(*) FROM accounts WHERE account_id IN (%s) AND status='frozen'"
            % ",".join(str(a) for a in marks["t4_frozen_accounts"]), ())
        add("trigger-maintained balances match the last commit",
            "SELECT COUNT(*) FROM account_balances WHERE stored_minor <> derived_minor")
        add("audit trail row count at the last commit",
            "SELECT COUNT(*) FROM audit_log")
        add("total posting weight at the last commit",
            "SELECT COALESCE(SUM(amount_minor),0) FROM postings")

        n_disc = sum(1 for a in expected["assertions"] if a["discriminates_replay_all"])
        assert n_disc >= 6, f"only {n_disc} assertions discriminate the replay-all trap"
    finally:
        conn.close()
        rconn.close()
    (TESTS / "expected_state.json").write_text(json.dumps(expected, indent=2, sort_keys=True) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
