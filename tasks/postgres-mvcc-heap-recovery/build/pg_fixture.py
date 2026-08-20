#!/usr/bin/env python3
"""Build the PostgreSQL 16 MVCC heap fixture (v4).

Runs INSIDE a postgres:16 container, as the `postgres` OS user, and drives a
real server through a scripted history of committed, aborted and still-running
transactions.  It then captures the relation's main fork verbatim and records,
independently, the logical rows PostgreSQL itself reports for the target
snapshot.

Everything written here is synthetic and self-created; nothing is downloaded or
derived from external material.

v2 raises the difficulty over v1 without touching the solver-facing contract.
The three ideas that do the work:

  * a read-only REPEATABLE READ "horizon holder" is opened part-way through, so
    from that point on nothing is pruned.  Physical versions therefore survive:
    HOT chains grow to three, four and five live tuples, aborted versions stay
    on the page, and - crucially - a lock-only xmax keeps `HEAP_XMAX_LOCK_ONLY`
    WITHOUT PostgreSQL later stamping `HEAP_XMAX_INVALID` over it during a
    prune.  Reading the infomask becomes mandatory rather than optional;
  * many concurrent writers are opened *interleaved* with transactions that
    commit between them, so `snapshot_xip` is a sparse set inside
    [snapshot_xmin, snapshot_xmax) rather than a contiguous tail.  Neither
    "ignore xip" nor "everything recent is in progress" survives that;
  * most keys exercise several mechanisms at once - a committed base, an
    aborted update, a later committed update, a row lock, a concurrent writer -
    so no single rule reproduces the answer.

v3 removes the decoded `tx_status.csv` entirely.  The solver now receives the
cluster's own commit log and subtransaction map as raw SLRU segments, and has to
decode transaction state itself.  That makes real subtransactions usable as a
difficulty lever, because `pg_current_snapshot()` exports only *top-level* xids
in its xip list: a tuple written by a subtransaction of a still-running
transaction carries a subxid that is marked COMMITTED in pg_xact and is absent
from snapshot_xip, and only pg_subtrans reveals that its topmost parent was in
flight when the snapshot was taken.

v4 adds the two mechanisms the earlier versions conditionally excluded, both
now fully implemented and tested:

  * **MultiXact xmax.**  When several transactions lock one row, or a locker and
    an updater coexist, t_xmax stops holding a transaction id and holds a
    MultiXactId (HEAP_XMAX_IS_MULTI).  The mxid resolves through two more SLRU
    areas - pg_multixact/offsets and pg_multixact/members - whose members carry
    per-member lock/update status; only an updater member can kill the tuple,
    and its fate re-enters pg_xact, pg_subtrans and the snapshot.  In a fresh
    cluster the mxids are tiny integers that collide numerically with committed
    bootstrap xids, so a solver that never checks the IS_MULTI bit silently
    deletes live rows.
  * **Frozen tuples.**  The base population is frozen with VACUUM (FREEZE)
    before any of the interesting history runs, so HEAP_XMIN_FROZEN (both hint
    bits) is set on every original row while the raw xmin is preserved.  Rows
    modified later carry a frozen xmin *and* a live xmax, so neither
    "XMIN_INVALID means aborted" (tested before the frozen mask) nor "frozen
    means visible, stop" survives.

Outputs (into --outdir):
    heap_pages.bin              solver input  - raw 8192-byte blocks, in order
    pg_xact/NNNN                solver input  - the cluster's commit log, verbatim
    pg_subtrans/NNNN            solver input  - the subtransaction parent map
    pg_multixact/offsets/NNNN   solver input  - MultiXact id -> first member index
    pg_multixact/members/NNNN   solver input  - member xids + lock/update flags
    table_schema.json           solver input  - columns, PK, target snapshot
    internal/golden.csv         hidden reference, produced by PostgreSQL
    internal/generation_report.json
    internal/page_items.json    pageinspect dump of the captured bytes
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import hashlib
import json
import sys
from pathlib import Path

import psycopg2
import psycopg2.extensions

DSN = dict(host="/var/run/postgresql", user="postgres", dbname="tb")

SCHEMA = "public"
TABLE = "account_ledger"
QUALIFIED = SCHEMA + "." + TABLE

# t_infomask / t_infomask2 bits (src/include/access/htup_details.h, PG 16)
HEAP_HASNULL = 0x0001
HEAP_HASEXTERNAL = 0x0004
HEAP_XMAX_KEYSHR_LOCK = 0x0010
HEAP_COMBOCID = 0x0020
HEAP_XMAX_EXCL_LOCK = 0x0040
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_LOCK_MASK = HEAP_XMAX_KEYSHR_LOCK | HEAP_XMAX_EXCL_LOCK
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMIN_FROZEN = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID
HEAP_XMAX_COMMITTED = 0x0400
HEAP_XMAX_INVALID = 0x0800
HEAP_XMAX_IS_MULTI = 0x1000
HEAP_HOT_UPDATED = 0x4000
HEAP_ONLY_TUPLE = 0x8000

FROZEN_XID = 2

# SLRU geometry (src/include/access/clog.h, src/backend/access/transam/subtrans.c)
BLCKSZ = 8192
SLRU_PAGES_PER_SEGMENT = 32
CLOG_XACTS_PER_BYTE = 4
CLOG_XACTS_PER_PAGE = BLCKSZ * CLOG_XACTS_PER_BYTE          # 32768
SUBTRANS_XACTS_PER_PAGE = BLCKSZ // 4                       # 2048
XACT_IN_PROGRESS, XACT_COMMITTED, XACT_ABORTED, XACT_SUB_COMMITTED = 0, 1, 2, 3
CLOG_NAMES = {0: "in progress", 1: "committed", 2: "aborted",
              3: "sub committed"}

# MultiXact SLRU geometry (src/backend/access/transam/multixact.c, PG 16)
MULTIXACT_OFFSETS_PER_PAGE = BLCKSZ // 4                    # 2048
MULTIXACT_MEMBERS_PER_GROUP = 4
MULTIXACT_MEMBERGROUP_SIZE = 4 + 4 * 4                      # 4 flag bytes + 4 xids
MULTIXACT_GROUPS_PER_PAGE = BLCKSZ // MULTIXACT_MEMBERGROUP_SIZE   # 409
MULTIXACT_MEMBERS_PER_PAGE = MULTIXACT_GROUPS_PER_PAGE * MULTIXACT_MEMBERS_PER_GROUP
# member status values (MultiXactStatus): 0-3 are lockers, 4-5 are updaters
MXS_NAMES = {0: "keysh", 1: "sh", 2: "fornokeyupd", 3: "forupd",
             4: "nokeyupd", 5: "upd"}
MXS_IS_UPDATE = (4, 5)

COLUMNS = [
    ("account_id", "integer", False),
    ("region_code", "character varying(12)", False),
    ("balance_cents", "integer", False),
    ("is_active", "boolean", False),
    ("risk_tier", "smallint", True),
    ("opened_on", "date", False),
    ("owner_note", "text", True),
]
COLNAMES = [c[0] for c in COLUMNS]

DDL = """
DROP TABLE IF EXISTS %(t)s;
CREATE TABLE %(t)s (
    account_id     integer               NOT NULL,
    region_code    character varying(12) NOT NULL,
    balance_cents  integer               NOT NULL,
    is_active      boolean               NOT NULL,
    risk_tier      smallint,
    opened_on      date                  NOT NULL,
    owner_note     text,
    CONSTRAINT account_ledger_pkey PRIMARY KEY (account_id)
) WITH (fillfactor = 60, autovacuum_enabled = false,
        toast.autovacuum_enabled = false);
CREATE INDEX account_ledger_region_idx ON %(t)s (region_code);
DROP TABLE IF EXISTS public.subxid_probe;
CREATE TABLE public.subxid_probe (marker integer);
""" % {"t": QUALIFIED}

# ---------------------------------------------------------------- synthetic data
REGIONS = ["EU-WEST-1", "US-EAST-2", "AP-SOUTH-1", "SA-EAST-1",
           "US-WEST-1", "EU-NORTH-1", "ME-CENTRAL"]
NOTES = [None, "verified", "re-audit pendiente", "priority, escalated",
         "ueberprueft 2024", "manual review", None, "dormant account",
         "kyc refresh", None, "watchlist", "tier upgrade"]


class Ids:
    """Hands out contiguous account_id ranges in allocation order.

    Base rows are inserted in id order, so allocation order is also physical
    order: the groups allocated last land on the last blocks of the relation.
    """

    def __init__(self, start):
        self.next = start
        self.groups = {}

    def take(self, name, n):
        r = list(range(self.next, self.next + n))
        self.next += n
        self.groups[name] = r
        return r


IDS = Ids(1)

# --- phase A: written before the horizon holder opens, so these pages prune ---
A_CHURN = IDS.take("A_churn", 8)               # HOT churn -> LP_REDIRECT / LP_DEAD
A_CHURN_LONG = IDS.take("A_churn_long", 6)     # churn, then extended after the snapshot
# dead root tuples created and pruned before the horizon is pinned: a non-HOT
# update and a delete leave root line pointers that still have index entries,
# so pruning retires them as LP_DEAD rather than LP_UNUSED
A_NONHOT_PRUNED = IDS.take("A_nonhot_pruned", 8)
A_DEL_PRUNED = IDS.take("A_del_pruned", 6)
# churned pre-horizon like A_CHURN, then hit by a MultiXact whose updater is a
# subtransaction of an xip-listed writer: the cascade keys.  Their visible
# version sits behind an LP_REDIRECT, is heap-only, and carries an IS_MULTI xmax
# whose verdict routes through offsets -> members -> flags -> pg_subtrans ->
# snapshot_xip.
A_CASCADE = IDS.take("A_cascade", 8)

# --- phase B: committed work, still before any writer opens ------------------
B_UPD_COMMIT = IDS.take("B_upd_commit", 8)     # one committed non-HOT update
B_MULTI3 = IDS.take("B_multi3", 8)             # three committed updates
B_DEL_COMMIT = IDS.take("B_del_commit", 6)     # committed delete
B_UPD_ABORT = IDS.take("B_upd_abort", 8)       # update rolled back
B_DEL_ABORT = IDS.take("B_del_abort", 6)       # delete rolled back
B_ABORT_THEN_COMMIT = IDS.take("B_abort_then_commit", 6)
B_COMMIT_ABORT_LOCK = IDS.take("B_commit_abort_lock", 6)
B_PRESNAP_CHAIN = IDS.take("B_presnap_chain", 6)   # extended again after the snapshot

# --- phase C: concurrent writers, interleaved with committed transactions ----
C_XIP_UPD_C = IDS.take("C_xip_upd_c", 6)       # writer commits after the snapshot
C_GAP_UPD_1 = IDS.take("C_gap_upd_1", 6)       # committed between writers -> visible
C_XIP_DEL_C = IDS.take("C_xip_del_c", 6)
C_GAP_DEL = IDS.take("C_gap_del", 6)           # committed delete between writers
C_GAP_UPD_2 = IDS.take("C_gap_upd_2", 6)
C_XIP_LOCK_C = IDS.take("C_xip_lock_c", 5)     # locked by a writer that commits later
C_XIP_UPD_A = IDS.take("C_xip_upd_a", 6)       # writer aborts after the snapshot
C_GAP_UPD_3 = IDS.take("C_gap_upd_3", 6)
C_IP_UPD = IDS.take("C_ip_upd", 6)             # writer never finishes
C_IP_DEL = IDS.take("C_ip_del", 6)
C_IP_LOCK = IDS.take("C_ip_lock", 5)
C_GAP_UPD_4 = IDS.take("C_gap_upd_4", 6)

# --- phase D: committed or aborted after the snapshot was taken -------------
D_POST_UPD = IDS.take("D_post_upd", 8)
D_POST_DEL = IDS.take("D_post_del", 6)
D_HOT_CHAIN = IDS.take("D_hot_chain", 8)       # three post-snapshot HOT rounds
D_POST_UPD_ABORT = IDS.take("D_post_upd_abort", 6)
D_POST_DEL_ABORT = IDS.take("D_post_del_abort", 6)
D_POST_LOCK = IDS.take("D_post_lock", 5)

# --- MultiXact groups.  All locking runs after the horizon holder opens, so no
# --- later prune stamps HEAP_XMAX_INVALID over a resolved multi.
M_LOCKONLY_SH = IDS.take("M_lockonly_share_share", 5)     # two SHARE lockers
M_LOCKONLY_MIX = IDS.take("M_lockonly_keysh_share", 5)    # KEY SHARE + SHARE
M_LOCKONLY_NKU = IDS.take("M_lockonly_keysh_nku", 4)      # KEY SHARE + FOR NO KEY UPDATE
M_LOCKONLY_IP = IDS.take("M_lockonly_in_progress", 4)     # one member never finishes
M_UPD_COMMIT = IDS.take("M_upd_committed", 6)     # updater commits pre-snapshot -> dead
M_UPD_ABORT = IDS.take("M_upd_aborted", 5)        # updater rolls back -> alive
M_UPD_IP = IDS.take("M_upd_in_progress", 5)       # updater never finishes -> alive
M_UPD_SUBC = IDS.take("M_upd_sub_committed", 5)   # updater is a released savepoint
M_UPD_XIP = IDS.take("M_upd_xip", 6)              # updater commits after the snapshot
M_UPD_SUBXIP = IDS.take("M_upd_subxip", 10)       # updater = savepoint of an xip writer
M_POST = IDS.take("M_post_snapshot", 4)           # whole multi after the snapshot
M_SENTINEL = IDS.take("M_sentinel", 1)            # trailing multi; bounds the last range

# --- frozen-then-modified groups (the base population is frozen wholesale, so
# --- every later update already yields frozen-xmin + xmax; these make the
# --- deliberate cases explicit and countable)
F_FRZ_DEL = IDS.take("F_frozen_deleted", 5)       # frozen root, committed delete
F_FRZ_DEL_ABORT = IDS.take("F_frozen_del_aborted", 4)
F_FRZ_LOCK = IDS.take("F_frozen_locked", 4)       # frozen root, single-xid row lock

# --- subtransactions -------------------------------------------------------
# committed parent, one savepoint rolled back and one released.  The rolled-back
# subtransaction is ABORTED in pg_xact while its parent is COMMITTED, so a solver
# that resolves a subxid to its parent and then uses the *parent's* commit status
# gets these wrong in both directions.
# 16 keys: a rolled-back savepoint leaves a dead new version whose xmin is the
# ABORTED subxid.  PostgreSQL does not stamp HEAP_XMIN_INVALID on all of them,
# so a solver that hands the subtransaction its parent's COMMITTED status
# resurrects the discarded update and reports the key twice.
B_SUB_ROLLED = IDS.take("B_sub_rolled", 16)
B_SUB_KEPT = IDS.take("B_sub_kept", 8)
B_SUB_DEL_ROLLED = IDS.take("B_sub_del_rolled", 6)
B_SUB_DEL_KEPT = IDS.take("B_sub_del_kept", 6)
B_SUB_TOP = IDS.take("B_sub_top", 6)

# The decisive group.  A writer holds nested savepoints across the snapshot and
# commits afterwards.  Its subxids read COMMITTED in pg_xact and are NOT in
# snapshot_xip - only pg_subtrans links them back to a top-level xid that is.
C_SUBXIP_TOP = IDS.take("C_subxip_top", 6)
C_SUBXIP_L1 = IDS.take("C_subxip_l1", 6)
C_SUBXIP_L2 = IDS.take("C_subxip_l2", 6)
# a subtransaction of a transaction that committed *between* the writers: its
# topmost parent is NOT in xip, so its work IS visible
C_GAP_SUB_TOP = IDS.take("C_gap_sub_top", 5)
C_GAP_SUB = IDS.take("C_gap_sub", 6)
# a subtransaction of a writer that never finishes
C_SUBIP_TOP = IDS.take("C_subip_top", 5)
C_SUBIP_DEL = IDS.take("C_subip_del", 6)
# a savepoint rolled back inside a transaction that commits after the snapshot
D_SUB_POST_ROLLED = IDS.take("D_sub_post_rolled", 6)
D_SUB_POST_KEPT = IDS.take("D_sub_post_kept", 6)

# --- filler: ordinary rows, so not every key is a trap ----------------------
FILLER = IDS.take("filler", 60)
F_UPD_COMMIT = FILLER[0:20]
F_UPD_ABORT = FILLER[20:35]
F_POST_UPD = FILLER[35:50]
# FILLER[50:60] stay exactly as inserted

# --- the quiet lock zone: allocated last, so it lands on the last blocks -----
# Nothing updates or deletes these rows, so their pages never acquire a
# pd_prune_xid and are never pruned.  That is what keeps HEAP_XMAX_LOCK_ONLY
# visible in the captured bytes instead of being overwritten by
# HEAP_XMAX_INVALID during a later prune.
A_LOCK_UPDATE = IDS.take("A_lock_for_update", 6)
A_LOCK_NOKEY = IDS.take("A_lock_for_no_key_update", 5)
A_LOCK_SHARE = IDS.take("A_lock_for_share", 5)
A_LOCK_KEYSHARE = IDS.take("A_lock_for_key_share", 5)
A_LOCK_ABORTED = IDS.take("A_lock_aborted", 5)
A_UPD_THEN_LOCK = IDS.take("A_upd_then_lock", 6)

BASE_IDS = list(range(1, IDS.next))

# ids that do not exist in the base population
EXTRA = Ids(5001)
INS_ABORT_IDS = EXTRA.take("ins_abort", 6)          # inserted by an aborted txn
INS_XIP_IDS = EXTRA.take("ins_xip_committed", 6)    # inserted by a writer in xip
INS_IP_IDS = EXTRA.take("ins_in_progress", 6)       # inserted by a running writer
INS_POST_IDS = EXTRA.take("ins_post_snapshot", 6)   # inserted after the snapshot
INS_SUBXIP_IDS = EXTRA.take("ins_subxip", 6)        # inserted by a nested subxact


def base_row(i):
    return (
        i,
        REGIONS[(i * 3) % len(REGIONS)],
        100000 + (i * 7919) % 900000,
        (i % 3) != 0,
        None if i % 11 == 0 else (i % 5) + 1,
        dt.date(2019, 1, 1) + dt.timedelta(days=(i * 37) % 2200),
        NOTES[(i * 5) % len(NOTES)],
    )


def extra_row(i, tag):
    return (
        i,
        REGIONS[(i * 5) % len(REGIONS)],
        900000 + (i * 131) % 90000,
        (i % 2) == 0,
        None if i % 7 == 0 else (i % 4) + 1,
        dt.date(2021, 6, 1) + dt.timedelta(days=(i * 11) % 900),
        tag + "-" + str(i),
    )


# ------------------------------------------------- raw SLRU decoding (verification)
def slru_segment(xid, xacts_per_page):
    """(segment file name, page index inside the segment) for an xid."""
    pageno = xid // xacts_per_page
    return "%04X" % (pageno // SLRU_PAGES_PER_SEGMENT), \
        pageno % SLRU_PAGES_PER_SEGMENT


def clog_status(segments, xid):
    name, page = slru_segment(xid, CLOG_XACTS_PER_PAGE)
    data = segments.get(name)
    if data is None:
        raise SystemExit("pg_xact segment %s is missing for xid %d" % (name, xid))
    off = page * BLCKSZ + (xid % CLOG_XACTS_PER_PAGE) // CLOG_XACTS_PER_BYTE
    shift = (xid % CLOG_XACTS_PER_BYTE) * 2
    return (data[off] >> shift) & 0x03


def subtrans_parent(segments, xid):
    name, page = slru_segment(xid, SUBTRANS_XACTS_PER_PAGE)
    data = segments.get(name)
    if data is None:
        return 0
    off = page * BLCKSZ + (xid % SUBTRANS_XACTS_PER_PAGE) * 4
    return int.from_bytes(data[off:off + 4], "little")


def multixact_offset(segments, mxid):
    pageno = mxid // MULTIXACT_OFFSETS_PER_PAGE
    name, page = "%04X" % (pageno // SLRU_PAGES_PER_SEGMENT), \
        pageno % SLRU_PAGES_PER_SEGMENT
    data = segments.get(name)
    if data is None:
        raise SystemExit("pg_multixact/offsets segment %s missing for mxid %d"
                         % (name, mxid))
    off = page * BLCKSZ + (mxid % MULTIXACT_OFFSETS_PER_PAGE) * 4
    return int.from_bytes(data[off:off + 4], "little")


def multixact_members(off_segments, mem_segments, mxid):
    """[(xid, status)] for one multi, from the raw segment bytes.

    Member offset 0 is reserved so that a zero entry can mean 'not written';
    the end of mxid's member list is the next multi's start, which is why the
    generator creates a trailing sentinel multi."""
    start = multixact_offset(off_segments, mxid)
    end = multixact_offset(off_segments, mxid + 1)
    if start == 0:
        raise SystemExit("offsets[%d] is zero; not a created multi" % mxid)
    if end == 0 or end < start:
        raise SystemExit("offsets[%d]=%d cannot bound the members of multi %d; "
                         "the sentinel multi is missing" % (mxid + 1, end, mxid))
    out = []
    for i in range(start, end):
        pageno, within = divmod(i, MULTIXACT_MEMBERS_PER_PAGE)
        name = "%04X" % (pageno // SLRU_PAGES_PER_SEGMENT)
        page = pageno % SLRU_PAGES_PER_SEGMENT
        data = mem_segments.get(name)
        if data is None:
            raise SystemExit("pg_multixact/members segment %s missing" % name)
        group, idx = divmod(within, MULTIXACT_MEMBERS_PER_GROUP)
        base = page * BLCKSZ + group * MULTIXACT_MEMBERGROUP_SIZE
        flag = data[base + idx]
        xid = int.from_bytes(data[base + 4 + idx * 4:base + 8 + idx * 4],
                             "little")
        out.append((xid, flag))
    return out


def subtrans_topmost(segments, xid):
    seen = set()
    while True:
        parent = subtrans_parent(segments, xid)
        if parent == 0 or parent in seen or parent >= xid:
            return xid
        seen.add(xid)
        xid = parent


# ---------------------------------------------------------------- session helper
class Session:
    """One server connection with explicit transaction control."""

    def __init__(self, label, isolation=None):
        self.label = label
        self.conn = psycopg2.connect(**DSN)
        self.conn.autocommit = False
        if isolation == "repeatable read":
            self.conn.set_session(
                isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)
        self.cur = self.conn.cursor()

    def run(self, sql, params=None):
        self.cur.execute(sql, params)
        return self.cur

    def xid(self):
        self.cur.execute("SELECT pg_current_xact_id_if_assigned()")
        v = self.cur.fetchone()[0]
        return None if v is None else int(v)

    def subxid(self):
        """The xid of the subtransaction currently in force.

        pg_current_xact_id() reports the *top-level* xid, so the only way to
        observe a subtransaction's own xid is to look at a tuple it wrote."""
        self.cur.execute("INSERT INTO public.subxid_probe VALUES (1) "
                         "RETURNING xmin::text::bigint")
        return int(self.cur.fetchone()[0])

    def commit(self):
        x = self.xid()
        self.conn.commit()
        return x

    def rollback(self):
        x = self.xid()
        self.conn.rollback()
        return x

    def close(self):
        self.cur.close()
        self.conn.close()


def one_shot(label, body, commit):
    """Run `body(session)` in its own transaction and return its xid."""
    s = Session(label)
    body(s)
    x = s.commit() if commit else s.rollback()
    s.close()
    if x is None:
        raise SystemExit("transaction " + label + " was never assigned an xid")
    return x


# ---------------------------------------------------------------- the history
def build(outdir):
    xids = {}
    # sessions that must stay open across the snapshot / capture; phase B's
    # multi groups and phase C's writers both register here
    writers = {}
    # subxid -> the top-level transaction it belongs to.  Used only to verify the
    # captured pg_subtrans segment; never shipped.  The *immediate* parent is
    # PostgreSQL's business: ROLLBACK TO SAVEPOINT re-enters a fresh
    # subtransaction, so later savepoints nest inside it and the chains get
    # deeper than the SQL text suggests.
    sub_tops = {}
    internal = outdir / "internal"
    internal.mkdir(parents=True, exist_ok=True)

    admin = psycopg2.connect(**DSN)
    admin.autocommit = True
    acur = admin.cursor()
    acur.execute("CREATE EXTENSION IF NOT EXISTS pageinspect")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            acur.execute(stmt)

    ins = ("INSERT INTO " + QUALIFIED + " (" + ", ".join(COLNAMES) +
           ") VALUES (%s,%s,%s,%s,%s,%s,%s)")
    upd = "UPDATE " + QUALIFIED + " SET "
    dele = "DELETE FROM " + QUALIFIED + " WHERE account_id = ANY(%s)"
    lock = "SELECT account_id FROM " + QUALIFIED + " WHERE account_id = ANY(%s) ORDER BY account_id "

    def do_lock(s, ids, mode):
        s.run(lock + mode, (ids,))
        s.cur.fetchall()

    # ================= phase A: before the horizon holder opens ==============
    def _base(s):
        for i in BASE_IDS:
            s.run(ins, base_row(i))
    xids["A_base_insert"] = one_shot("base-insert", _base, True)

    # Freeze the whole base population.  VACUUM (FREEZE) sets HEAP_XMIN_FROZEN
    # (both hint bits) on every tuple while PRESERVING the raw xmin, so from
    # here on every original row is a frozen tuple, and every row a later phase
    # updates or deletes becomes a frozen-xmin-plus-live-xmax case.
    acur.execute("VACUUM (FREEZE) " + QUALIFIED)

    # HOT churn with a scan between rounds, so pruning really runs and leaves
    # LP_REDIRECT roots and LP_DEAD slots behind.  Nothing holds the horizon
    # back yet, which is exactly why these chains collapse.
    churn = []
    for rnd in range(22):
        def _hot(s, rnd=rnd):
            for i in A_CHURN + A_CHURN_LONG + A_CASCADE:
                s.run(upd + "balance_cents = balance_cents + %s, owner_note = %s "
                      "WHERE account_id = %s", (rnd + 1, "churn " + str(rnd), i))
        churn.append(one_shot("churn-" + str(rnd), _hot, True))
        acur.execute("SELECT count(*) FROM " + QUALIFIED)   # provoke pruning
    xids["A_churn_rounds"] = churn

    def _churn_final(s):
        for i in A_CHURN + A_CHURN_LONG + A_CASCADE:
            s.run(upd + "balance_cents = %s, owner_note = %s, risk_tier = %s "
                  "WHERE account_id = %s",
                  (250000 + i, None if i % 2 else "settled " + str(i),
                   (i % 4) + 1, i))
    xids["A_churn_final"] = one_shot("churn-final", _churn_final, True)
    acur.execute("SELECT count(*) FROM " + QUALIFIED)

    # A plain VACUUM while the horizon is still current: the dead intermediate
    # churn versions and their LP_DEAD slots are reclaimed to LP_UNUSED, while
    # the live chains keep their LP_REDIRECT roots.  The A_NONHOT_PRUNED /
    # A_DEL_PRUNED work below then recreates LP_DEAD, so all four line-pointer
    # states appear in the captured file.
    acur.execute("VACUUM " + QUALIFIED)

    # A non-HOT update (it changes the indexed column) and a delete, both
    # committed while the horizon is still current.  Their old versions are root
    # line pointers with index entries pointing at them, so the prune that
    # follows retires them as LP_DEAD instead of freeing the slot outright.
    def _nonhot_pruned(s):
        for i in A_NONHOT_PRUNED:
            s.run(upd + "region_code = %s, balance_cents = %s, owner_note = %s "
                  "WHERE account_id = %s",
                  ("ME-CENTRAL", 210000 + i, "reindexed", i))
    xids["A_nonhot_update_pruned"] = one_shot("a-nonhot", _nonhot_pruned, True)

    def _del_pruned(s):
        s.run(dele, (A_DEL_PRUNED,))
    xids["A_delete_pruned"] = one_shot("a-del-pruned", _del_pruned, True)

    for _ in range(4):
        acur.execute("SELECT count(*) FROM " + QUALIFIED)
        acur.execute("SELECT sum(balance_cents) FROM " + QUALIFIED)

    # ================= the horizon holder ====================================
    # A read-only REPEATABLE READ session.  It is never assigned an xid, so it
    # appears in no snapshot and in no commit log - but it pins the vacuum
    # horizon, so from here on PostgreSQL prunes nothing.  Every physical
    # version created below therefore survives into the captured file.
    horizon = Session("horizon-holder", isolation="repeatable read")
    horizon.run("SELECT 1")
    if horizon.xid() is not None:
        raise SystemExit("the horizon holder must stay read-only")

    # ================= phase B: committed work, still before any writer ======
    # Row locks in the quiet zone.  Each is a single locker, so no MultiXact is
    # created, and every one of these transactions completes before any writer
    # opens - so their xids end up below snapshot_xmin and are plainly visible.
    # A solver that reads "xmax names a committed transaction" as "the row was
    # deleted" therefore loses all of these rows.
    #
    # They run *after* the horizon holder deliberately.  Once the horizon is
    # pinned PostgreSQL stops pruning, and it is pruning - through
    # HeapTupleSatisfiesVacuum - that would otherwise stamp HEAP_XMAX_INVALID
    # over a completed locker's xmax and hand the answer to a solver that never
    # looks at HEAP_XMAX_LOCK_ONLY at all.
    def _lock_upd(s):
        do_lock(s, A_LOCK_UPDATE, "FOR UPDATE")
    xids["B_lock_for_update"] = one_shot("lock-upd", _lock_upd, True)

    def _lock_nokey(s):
        do_lock(s, A_LOCK_NOKEY, "FOR NO KEY UPDATE")
    xids["B_lock_for_no_key_update"] = one_shot("lock-nokey", _lock_nokey, True)

    def _lock_share(s):
        do_lock(s, A_LOCK_SHARE, "FOR SHARE")
    xids["B_lock_for_share"] = one_shot("lock-share", _lock_share, True)

    def _lock_keyshare(s):
        do_lock(s, A_LOCK_KEYSHARE, "FOR KEY SHARE")
    xids["B_lock_for_key_share"] = one_shot("lock-keyshare", _lock_keyshare, True)

    def _lock_abort(s):
        do_lock(s, A_LOCK_ABORTED, "FOR UPDATE")
    xids["B_lock_aborted"] = one_shot("lock-abort", _lock_abort, False)

    # a committed update, then a committed lock of the *new* version: the
    # surviving tuple carries one transaction in xmin and a different one in
    # xmax, and is still live
    def _upd_then_lock_a(s):
        for i in A_UPD_THEN_LOCK:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (330000 + i, "relocated", i))
    xids["B_upd_before_lock"] = one_shot("upd-before-lock", _upd_then_lock_a, True)

    def _upd_then_lock_b(s):
        do_lock(s, A_UPD_THEN_LOCK, "FOR UPDATE")
    xids["B_lock_after_update"] = one_shot("lock-after-upd", _upd_then_lock_b, True)

    def _upd_commit(s):
        for i in B_UPD_COMMIT:
            s.run(upd + "region_code = %s, balance_cents = %s, owner_note = %s "
                  "WHERE account_id = %s",
                  ("EU-NORTH-1", 310000 + i, "regional transfer", i))
        for i in F_UPD_COMMIT:
            s.run(upd + "balance_cents = %s, risk_tier = %s WHERE account_id = %s",
                  (320000 + i, None if i % 5 == 0 else (i % 3) + 1, i))
    xids["B_update_committed"] = one_shot("b-upd-commit", _upd_commit, True)

    multi = []
    for rnd in range(3):
        def _multi(s, rnd=rnd):
            for i in B_MULTI3 + B_PRESNAP_CHAIN:
                s.run(upd + "balance_cents = %s, risk_tier = %s, owner_note = %s "
                      "WHERE account_id = %s",
                      (400000 + rnd * 1000 + i, None if rnd == 1 else (rnd + 1),
                       None if (i + rnd) % 3 == 0 else "revision " + str(rnd), i))
        multi.append(one_shot("b-multi-" + str(rnd), _multi, True))
    xids["B_multi_version"] = multi

    def _del_commit(s):
        s.run(dele, (B_DEL_COMMIT,))
    xids["B_delete_committed"] = one_shot("b-del-commit", _del_commit, True)

    def _ins_abort(s):
        for i in INS_ABORT_IDS:
            s.run(ins, extra_row(i, "phantom"))
    xids["B_insert_aborted"] = one_shot("b-ins-abort", _ins_abort, False)

    def _upd_abort(s):
        for i in B_UPD_ABORT:
            s.run(upd + "balance_cents = %s, region_code = %s, is_active = %s, "
                  "owner_note = %s WHERE account_id = %s",
                  (-1, "XX-BOGUS-1", False, "rolled back", i))
        s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
              (-2, "rolled back batch", F_UPD_ABORT))
    xids["B_update_aborted"] = one_shot("b-upd-abort", _upd_abort, False)

    def _del_abort(s):
        s.run(dele, (B_DEL_ABORT,))
    xids["B_delete_aborted"] = one_shot("b-del-abort", _del_abort, False)

    # abort an update, then commit a different one: the dead version keeps the
    # greatest xmin of the three, and the live version is the newest committed
    def _mix_abort(s):
        for i in B_ABORT_THEN_COMMIT:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (-77, "discarded attempt", i))
    xids["B_mix_update_aborted"] = one_shot("b-mix-abort", _mix_abort, False)

    def _mix_commit(s):
        for i in B_ABORT_THEN_COMMIT:
            s.run(upd + "balance_cents = %s, region_code = %s, owner_note = %s "
                  "WHERE account_id = %s",
                  (505000 + i, "AP-SOUTH-1", "retried and kept", i))
    xids["B_mix_update_committed"] = one_shot("b-mix-commit", _mix_commit, True)

    # commit an update, abort another, then lock the survivor: the live tuple
    # has a committed xmin, a *committed lock* in xmax, and a dead successor
    # carrying the greatest xmin on the key
    def _cal_commit(s):
        for i in B_COMMIT_ABORT_LOCK:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (515000 + i, "settled then locked", i))
    xids["B_cal_update_committed"] = one_shot("b-cal-commit", _cal_commit, True)

    def _cal_abort(s):
        for i in B_COMMIT_ABORT_LOCK:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (-88, "abandoned", i))
    xids["B_cal_update_aborted"] = one_shot("b-cal-abort", _cal_abort, False)

    def _cal_lock(s):
        do_lock(s, B_COMMIT_ABORT_LOCK, "FOR UPDATE")
    xids["B_cal_lock"] = one_shot("b-cal-lock", _cal_lock, True)

    # --- subtransactions inside a committed transaction ---------------------
    # One savepoint is rolled back and one is released.  After COMMIT the parent
    # is COMMITTED in pg_xact while the rolled-back child is ABORTED, so the
    # commit log has to be read per xid: inheriting the parent's status would
    # resurrect the discarded work and drop the rows whose deletion was undone.
    b_sub = {}

    def _sub_mixed(s):
        s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
              (540000, "subxact parent", B_SUB_TOP))
        top = s.subxid()
        b_sub["top"] = top

        s.run("SAVEPOINT sp_upd_rolled")
        s.run(upd + "balance_cents = %s, region_code = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-501, "XX-SUBROLL", "savepoint discarded", B_SUB_ROLLED))
        x = s.subxid()
        sub_tops[x] = top
        b_sub["upd_rolled"] = x
        s.run("ROLLBACK TO SAVEPOINT sp_upd_rolled")

        s.run("SAVEPOINT sp_upd_kept")
        s.run(upd + "balance_cents = %s, risk_tier = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (551000, 6, "savepoint kept", B_SUB_KEPT))
        x = s.subxid()
        sub_tops[x] = top
        b_sub["upd_kept"] = x
        s.run("RELEASE SAVEPOINT sp_upd_kept")

        s.run("SAVEPOINT sp_del_rolled")
        s.run(dele, (B_SUB_DEL_ROLLED,))
        x = s.subxid()
        sub_tops[x] = top
        b_sub["del_rolled"] = x
        s.run("ROLLBACK TO SAVEPOINT sp_del_rolled")

        s.run("SAVEPOINT sp_del_kept")
        s.run(dele, (B_SUB_DEL_KEPT,))
        x = s.subxid()
        sub_tops[x] = top
        b_sub["del_kept"] = x
        s.run("RELEASE SAVEPOINT sp_del_kept")

    xids["B_subxact_parent"] = one_shot("b-subxact", _sub_mixed, True)
    xids["B_subxact_children"] = b_sub

    # ---- frozen roots explicitly deleted / almost-deleted / locked ----------
    def _frz_del(s):
        s.run(dele, (F_FRZ_DEL,))
    xids["B_frozen_delete"] = one_shot("b-frz-del", _frz_del, True)

    def _frz_del_abort(s):
        s.run(dele, (F_FRZ_DEL_ABORT,))
    xids["B_frozen_delete_aborted"] = one_shot("b-frz-del-abort", _frz_del_abort,
                                               False)

    def _frz_lock(s):
        do_lock(s, F_FRZ_LOCK, "FOR UPDATE")
    xids["B_frozen_lock"] = one_shot("b-frz-lock", _frz_lock, True)

    # ================= MultiXact phase (pre-snapshot groups) =================
    # Burn the first two MultiXactIds on the (uncaptured) probe table, so every
    # multi the captured pages reference is numerically >= 3.  Misread as plain
    # transaction ids those decode as committed bootstrap xids in the shipped
    # commit log - which is what arms the IS_MULTI trap.  Xids 1 and 2
    # (Bootstrap/Frozen) are special-cased by PostgreSQL and read as
    # "in progress" from the raw bits, so multis 1 and 2 must not reach the
    # heap pages.
    def _probe_multi():
        ba, bb = Session("mx-burn-a"), Session("mx-burn-b")
        for b in (ba, bb):
            b.run("SELECT marker FROM public.subxid_probe "
                  "WHERE marker = 999 FOR SHARE")
            b.cur.fetchall()
        ba.commit(); ba.close()
        bb.commit(); bb.close()

    def _probe_row(s):
        s.run("INSERT INTO public.subxid_probe VALUES (999)")
    one_shot("mx-burn-row", _probe_row, True)
    _probe_multi()
    _probe_multi()

    # Every case uses compatible lock modes, so the strictly sequenced sessions
    # never block and generation stays deterministic.  A multi is created the
    # moment a second transaction locks (or a locker's row is updated by) a row
    # whose xmax already names a live locker.
    def multi_lockers(ids, mode_a, mode_b, commit_a=True, commit_b=True):
        """Two lockers on the same rows -> a locker-only multi."""
        la, lb = Session("mx-lock-a"), Session("mx-lock-b")
        do_lock(la, ids, mode_a)
        do_lock(lb, ids, mode_b)
        xa, xb = la.xid(), lb.xid()
        if commit_a:
            la.commit(); la.close()
        if commit_b:
            lb.commit(); lb.close()
        return (xa, la if not commit_a else None), (xb, lb if not commit_b else None)

    (x, _), (y, _) = multi_lockers(M_LOCKONLY_SH, "FOR SHARE", "FOR SHARE")
    xids["M_lockonly_share_share"] = [x, y]

    (x, _), (y, _) = multi_lockers(M_LOCKONLY_MIX, "FOR KEY SHARE", "FOR SHARE")
    xids["M_lockonly_keysh_share"] = [x, y]

    (x, _), (y, _) = multi_lockers(M_LOCKONLY_NKU, "FOR KEY SHARE",
                                   "FOR NO KEY UPDATE")
    xids["M_lockonly_keysh_nku"] = [x, y]

    # one member never finishes: its xid stays IN_PROGRESS in pg_xact
    (x, _), (y, open_locker) = multi_lockers(M_LOCKONLY_IP, "FOR SHARE",
                                             "FOR SHARE", commit_b=False)
    xids["M_lockonly_in_progress"] = [x, y]
    writers["mx_locker_never_finishes"] = open_locker
    xids["C_writer_mx_locker_never_finishes"] = y

    def multi_with_updater(ids, values, fate):
        """KEY SHARE locker + a non-key updater -> multi {keysh, nokeyupd}.

        fate: 'commit' | 'abort' | 'hold' (leave the updater session open).
        Returns (locker_xid, updater_xid, updater_session_or_None)."""
        lk, up = Session("mx-locker"), Session("mx-updater")
        do_lock(lk, ids, "FOR KEY SHARE")
        up.run(upd + "balance_cents = %s, owner_note = %s "
               "WHERE account_id = ANY(%s)", (values[0], values[1], ids))
        ux = up.xid()
        if fate == "commit":
            up.commit(); up.close(); up = None
        elif fate == "abort":
            up.rollback(); up.close(); up = None
        lx = lk.commit()
        lk.close()
        return lx, ux, up

    lx, ux, _ = multi_with_updater(M_UPD_COMMIT, (700100, "multi updater kept"),
                                   "commit")
    xids["M_upd_committed"] = {"locker": lx, "updater": ux}

    lx, ux, _ = multi_with_updater(M_UPD_ABORT, (-604, "multi updater dropped"),
                                   "abort")
    xids["M_upd_aborted"] = {"locker": lx, "updater": ux}

    lx, ux, up = multi_with_updater(M_UPD_IP, (-605, "multi updater in flight"),
                                    "hold")
    xids["M_upd_in_progress"] = {"locker": lx, "updater": ux}
    writers["mx_updater_never_finishes"] = up
    xids["C_writer_mx_updater_never_finishes"] = ux

    # updater inside a released savepoint that commits pre-snapshot: the update
    # member is a subxid whose own clog entry is COMMITTED
    lk = Session("mx-locker-subc")
    do_lock(lk, M_UPD_SUBC, "FOR KEY SHARE")
    up = Session("mx-updater-subc")
    up.run("SAVEPOINT m1")
    up.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
           (700200, "savepoint updater kept", M_UPD_SUBC))
    subc_upd = up.subxid()
    sub_tops[subc_upd] = up.xid()
    up.run("RELEASE SAVEPOINT m1")
    up_top = up.commit()
    up.close()
    lk.commit(); lk.close()
    xids["M_upd_sub_committed"] = {"updater_subxid": subc_upd, "parent": up_top}

    # ================= phase C: writers interleaved with committed work ======

    def open_writer(name, body):
        s = Session(name)
        body(s)
        x = s.xid()
        if x is None:
            raise SystemExit("writer " + name + " was never assigned an xid")
        writers[name] = s
        xids["C_writer_" + name] = x
        return x

    def committed_between(name, body):
        xids["C_gap_" + name] = one_shot("gap-" + name, body, True)

    open_writer("w1_upd_commits_later", lambda s: s.run(
        upd + "balance_cents = %s, region_code = %s, owner_note = %s "
        "WHERE account_id = ANY(%s)",
        (-901, "XX-INFLIGHT", "in flight at snapshot", C_XIP_UPD_C)))

    committed_between("upd_1", lambda s: s.run(
        upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
        (610000, "committed between writers", C_GAP_UPD_1)))

    open_writer("w2_del_commits_later",
                lambda s: s.run(dele, (C_XIP_DEL_C,)))

    committed_between("del", lambda s: s.run(dele, (C_GAP_DEL,)))

    def _w3(s):
        for i in INS_XIP_IDS:
            s.run(ins, extra_row(i, "inflight"))
    open_writer("w3_ins_commits_later", _w3)

    committed_between("upd_2", lambda s: s.run(
        upd + "risk_tier = %s, owner_note = %s WHERE account_id = ANY(%s)",
        (7, "second gap commit", C_GAP_UPD_2)))

    open_writer("w4_lock_commits_later",
                lambda s: do_lock(s, C_XIP_LOCK_C, "FOR UPDATE"))

    # --- the decisive writer -------------------------------------------------
    # Three *nested* savepoints, so pg_subtrans holds a chain rather than a flat
    # map and resolving a subxid to its top-level parent takes several hops.
    # This transaction is still running when the snapshot is taken and commits
    # afterwards: every one of its subxids ends up COMMITTED in pg_xact while
    # being absent from snapshot_xip.
    w10 = {}

    def _w10(s):
        s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
              (-950, "inflight top level", C_SUBXIP_TOP))
        top = s.subxid()
        w10["top"] = top

        s.run("SAVEPOINT l1")
        s.run(upd + "balance_cents = %s, region_code = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-951, "XX-SUBFLY", "written by savepoint l1", C_SUBXIP_L1))
        s1 = s.subxid()
        sub_tops[s1] = top
        w10["l1"] = s1

        s.run("SAVEPOINT l2")
        s.run(dele, (C_SUBXIP_L2,))
        s2 = s.subxid()
        sub_tops[s2] = top
        w10["l2"] = s2

        s.run("SAVEPOINT l3")
        for i in INS_SUBXIP_IDS:
            s.run(ins, extra_row(i, "subinflight"))
        s3 = s.subxid()
        sub_tops[s3] = top
        w10["l3"] = s3

    open_writer("w10_nested_subxacts_commit_later", _w10)
    xids["C_w10_subxids"] = w10

    # A transaction that uses a savepoint and COMMITS between the writers.  Its
    # topmost parent is not in xip either, so its work is visible - proving that
    # "this xid has a pg_subtrans parent" does not by itself mean invisible.
    gap_sub = {}

    def _gap_sub(s):
        s.run(upd + "owner_note = %s WHERE account_id = ANY(%s)",
              ("gap parent", C_GAP_SUB_TOP))
        top = s.subxid()
        gap_sub["top"] = top
        s.run("SAVEPOINT g1")
        s.run(upd + "balance_cents = %s, risk_tier = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (660000, 3, "gap savepoint committed", C_GAP_SUB))
        g1 = s.subxid()
        sub_tops[g1] = top
        gap_sub["g1"] = g1
        s.run("RELEASE SAVEPOINT g1")

    committed_between("subxact", _gap_sub)
    xids["C_gap_subxids"] = gap_sub

    open_writer("w5_upd_aborts_later", lambda s: s.run(
        upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
        (-902, "in flight, later abandoned", C_XIP_UPD_A)))

    committed_between("upd_3", lambda s: s.run(
        upd + "balance_cents = %s, is_active = %s WHERE account_id = ANY(%s)",
        (620000, False, C_GAP_UPD_3)))

    open_writer("w6_upd_never_finishes", lambda s: s.run(
        upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
        (-903, "uncommitted write", C_IP_UPD)))

    open_writer("w7_del_never_finishes", lambda s: s.run(dele, (C_IP_DEL,)))

    def _w8(s):
        for i in INS_IP_IDS:
            s.run(ins, extra_row(i, "uncommitted"))
    open_writer("w8_ins_never_finishes", _w8)

    open_writer("w9_lock_never_finishes",
                lambda s: do_lock(s, C_IP_LOCK, "FOR UPDATE"))

    # a writer with a savepoint that never finishes: its subxid stays
    # IN_PROGRESS in pg_xact
    w11 = {}

    def _w11(s):
        s.run(upd + "owner_note = %s WHERE account_id = ANY(%s)",
              ("uncommitted parent", C_SUBIP_TOP))
        top = s.subxid()
        w11["top"] = top
        s.run("SAVEPOINT k1")
        s.run(dele, (C_SUBIP_DEL,))
        k1 = s.subxid()
        sub_tops[k1] = top
        w11["k1"] = k1

    open_writer("w11_subxact_never_finishes", _w11)
    xids["C_w11_subxids"] = w11

    # ---- multi whose updater commits AFTER the snapshot ---------------------
    # The locker commits pre-snapshot; the updater stays open across it and is
    # therefore listed in snapshot_xip.  Its clog entry will read COMMITTED at
    # capture, so only the snapshot keeps these rows alive.
    lk = Session("mx-locker-xip")
    do_lock(lk, M_UPD_XIP, "FOR KEY SHARE")

    def _w12(s):
        s.run(upd + "balance_cents = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)", (-701, "xip multi updater", M_UPD_XIP))
    open_writer("w12_multi_updater_commits_later", _w12)
    lk.commit(); lk.close()

    # ---- multi whose updater is a SAVEPOINT of an xip writer ----------------
    # The deepest chain in the fixture: IS_MULTI -> offsets -> members -> the
    # nokeyupd member is a subxid -> pg_subtrans -> topmost parent -> xip.
    # The same writer also updates the churned cascade keys from a second,
    # nested savepoint, so the cascade rows add HOT/LP_REDIRECT on top.
    lk = Session("mx-locker-subxip")
    do_lock(lk, list(M_UPD_SUBXIP) + list(A_CASCADE), "FOR KEY SHARE")
    w13 = {}

    def _w13(s):
        w13["top"] = s.subxid()          # assigns the top-level xid
        s.run("SAVEPOINT mx1")
        s.run(upd + "balance_cents = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-702, "subxip multi updater", M_UPD_SUBXIP))
        w13["mx1"] = s.subxid()
        sub_tops[w13["mx1"]] = w13["top"]
        s.run("SAVEPOINT mx2")
        s.run(upd + "balance_cents = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-703, "cascade multi updater", A_CASCADE))
        w13["mx2"] = s.subxid()
        sub_tops[w13["mx2"]] = w13["top"]
    open_writer("w13_subxip_multi_updater", _w13)
    xids["C_w13_subxids"] = w13
    lk.commit(); lk.close()

    # the last completed transaction before the snapshot: this is what lifts
    # snapshot_xmax above every running writer, so all of them land in xip
    committed_between("upd_4", lambda s: s.run(
        upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
        (630000, "final gap commit", C_GAP_UPD_4)))

    # ================= the target snapshot ===================================
    obs = Session("observer", isolation="repeatable read")
    obs.run("SELECT pg_current_snapshot()::text")
    snapshot_text = obs.cur.fetchone()[0]
    if obs.xid() is not None:
        raise SystemExit("the observer transaction wrote; it must be read-only")

    # ================= phase D: after the snapshot was taken =================
    def _post_upd(s):
        for i in D_POST_UPD:
            s.run(upd + "balance_cents = %s, region_code = %s, risk_tier = %s, "
                  "owner_note = %s WHERE account_id = %s",
                  (777000 + i, "ZZ-FUTURE-9", 9, "committed after snapshot", i))
        s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
              (-31337, "future batch", F_POST_UPD))
    xids["D_post_update"] = one_shot("d-post-upd", _post_upd, True)

    def _post_ins(s):
        for i in INS_POST_IDS:
            s.run(ins, extra_row(i, "future"))
    xids["D_post_insert"] = one_shot("d-post-ins", _post_ins, True)

    def _post_del(s):
        s.run(dele, (D_POST_DEL,))
    xids["D_post_delete"] = one_shot("d-post-del", _post_del, True)

    # three more committed rounds on chains that already have versions: the
    # visible tuple ends up in the middle of a four- or five-deep chain
    post_chain = []
    for rnd in range(3):
        def _chain(s, rnd=rnd):
            for i in D_HOT_CHAIN + B_PRESNAP_CHAIN + A_CHURN_LONG:
                s.run(upd + "balance_cents = %s, owner_note = %s "
                      "WHERE account_id = %s",
                      (880000 + rnd * 1000 + i, "future version " + str(rnd), i))
        post_chain.append(one_shot("d-chain-" + str(rnd), _chain, True))
    xids["D_post_chain"] = post_chain

    def _post_upd_abort(s):
        for i in D_POST_UPD_ABORT:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (-4242, "future rollback", i))
    xids["D_post_update_aborted"] = one_shot("d-post-upd-abort", _post_upd_abort,
                                             False)

    def _post_del_abort(s):
        s.run(dele, (D_POST_DEL_ABORT,))
    xids["D_post_delete_aborted"] = one_shot("d-post-del-abort", _post_del_abort,
                                             False)

    def _post_lock(s):
        do_lock(s, D_POST_LOCK, "FOR UPDATE")
    xids["D_post_lock"] = one_shot("d-post-lock", _post_lock, True)

    # a whole locker+updater multi created and committed AFTER the snapshot:
    # both members commit, but the updater sits at/above snapshot_xmax, so the
    # old version stays visible
    lk = Session("mx-locker-post")
    do_lock(lk, M_POST, "FOR KEY SHARE")
    up = Session("mx-updater-post")
    up.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
           (990000, "post-snapshot multi updater", M_POST))
    post_upd_xid = up.commit()
    up.close()
    lk.commit(); lk.close()
    xids["M_post_snapshot"] = {"updater": post_upd_xid}

    # a savepoint rolled back inside a transaction that commits after the
    # snapshot: an ABORTED child under a COMMITTED-but-invisible parent
    d_sub = {}

    def _post_sub(s):
        s.run(upd + "owner_note = %s WHERE account_id = ANY(%s)",
              ("future parent", D_SUB_POST_KEPT))
        top = s.subxid()
        d_sub["top"] = top
        s.run("SAVEPOINT f1")
        s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
              (-7777, "future savepoint discarded", D_SUB_POST_ROLLED))
        f1 = s.subxid()
        sub_tops[f1] = top
        d_sub["f1"] = f1
        s.run("ROLLBACK TO SAVEPOINT f1")

    xids["D_post_subxact"] = one_shot("d-post-sub", _post_sub, True)
    xids["D_post_subxids"] = d_sub

    # ================= writers finish, after the snapshot ====================
    for name in ("w1_upd_commits_later", "w2_del_commits_later",
                 "w3_ins_commits_later", "w4_lock_commits_later",
                 "w10_nested_subxacts_commit_later",
                 "w12_multi_updater_commits_later",
                 "w13_subxip_multi_updater"):
        writers[name].commit()
        writers[name].close()
        del writers[name]
    writers["w5_upd_aborts_later"].rollback()
    writers["w5_upd_aborts_later"].close()
    del writers["w5_upd_aborts_later"]

    # ================= the trailing sentinel multi ===========================
    # pg_multixact/offsets bounds multi M's member list with offsets[M+1], and
    # the very last multi's end lives only in pg_control (which is not shipped).
    # One final locker-only multi on the uncaptured probe table guarantees that
    # offsets[M+1] is on disk for every multi the pages reference, without
    # putting the sentinel itself onto the pages.
    _probe_multi()

    # ================= flush and capture the relation file verbatim ==========
    acur.execute("CHECKPOINT")
    acur.execute("SELECT current_setting('data_directory')")
    datadir = acur.fetchone()[0]
    acur.execute("SELECT pg_relation_filepath('" + QUALIFIED + "')")
    relpath = acur.fetchone()[0]
    acur.execute("SELECT pg_relation_size('" + QUALIFIED + "')")
    relsize = int(acur.fetchone()[0])

    heap = Path(datadir, relpath).read_bytes()
    if len(heap) != relsize:
        raise SystemExit("relation file is %d bytes, pg_relation_size reports %d"
                         % (len(heap), relsize))
    if len(heap) % 8192 != 0:
        raise SystemExit("captured %d bytes, not a multiple of 8192" % len(heap))
    nblocks = len(heap) // 8192

    # ================= inspect exactly those bytes ===========================
    items = []
    for blk in range(nblocks):
        page = heap[blk * 8192:(blk + 1) * 8192]
        acur.execute(
            "SELECT lp, lp_off, lp_flags, lp_len, t_xmin, t_xmax, t_field3, "
            "       t_ctid, t_infomask2, t_infomask, t_hoff "
            "FROM heap_page_items(%s::bytea)", (psycopg2.Binary(page),))
        for r in acur.fetchall():
            items.append({
                "block": blk, "lp": r[0], "lp_off": r[1], "lp_flags": r[2],
                "lp_len": r[3],
                "t_xmin": None if r[4] is None else int(r[4]),
                "t_xmax": None if r[5] is None else int(r[5]),
                "t_field3": r[6], "t_ctid": r[7],
                "t_infomask2": r[8], "t_infomask": r[9], "t_hoff": r[10],
            })

    normals = [it for it in items
               if it["lp_flags"] == 1 and it["t_infomask"] is not None]
    problems = []
    for it in normals:
        m, m2 = it["t_infomask"], it["t_infomask2"]
        if it["t_xmin"] == FROZEN_XID:
            problems.append(("frozen-xid-rewritten", it))
        if m & HEAP_COMBOCID:
            problems.append(("combocid", it))
        if m & HEAP_HASEXTERNAL:
            problems.append(("toast-external", it))
        if (m2 & 0x07FF) != len(COLUMNS):
            problems.append(("natts", it))
    if problems:
        raise SystemExit("excluded tuple case present in the fixture: %r"
                         % (problems[:5],))

    lp_counts = {}
    for it in items:
        lp_counts[it["lp_flags"]] = lp_counts.get(it["lp_flags"], 0) + 1
    if lp_counts.get(2, 0) == 0:
        raise SystemExit("no LP_REDIRECT line pointer was produced; "
                         "HOT pruning did not happen")
    if lp_counts.get(3, 0) == 0:
        raise SystemExit("no LP_DEAD line pointer was produced")

    # the TOAST relation must be empty - every value stays inline
    acur.execute("SELECT reltoastrelid::regclass::text, reltoastrelid "
                 "FROM pg_class WHERE oid = '" + QUALIFIED + "'::regclass")
    toastname, toastoid = acur.fetchone()
    toast_rows = 0
    if toastoid:
        acur.execute("SELECT count(*) FROM " + toastname)
        toast_rows = int(acur.fetchone()[0])
    if toast_rows:
        raise SystemExit("%d TOAST chunks exist; values are not inline" % toast_rows)

    # ================= capture the commit log and subtransaction map =========
    # Verbatim copies of the cluster's own SLRU segments.  CHECKPOINT above has
    # already flushed both (CheckPointCLOG / CheckPointSUBTRANS), so what is on
    # disk is what the server would read back.
    slru = {}
    for area in ("pg_xact", "pg_subtrans", "pg_multixact/offsets",
                 "pg_multixact/members"):
        srcdir = Path(datadir, area)
        slru[area] = {}
        for f in sorted(srcdir.iterdir()):
            if f.is_file():
                slru[area][f.name.upper()] = f.read_bytes()
        if not slru[area]:
            raise SystemExit("%s holds no segment files" % area)
        for name, blob in slru[area].items():
            if len(blob) % BLCKSZ:
                raise SystemExit("%s/%s is %d bytes, not a multiple of %d"
                                 % (area, name, len(blob), BLCKSZ))

    # ================= resolve the MultiXacts the pages reference ============
    multi_tuples = [it for it in normals
                    if it["t_xmax"] and it["t_infomask"] & HEAP_XMAX_IS_MULTI]
    multis_used = sorted({it["t_xmax"] for it in multi_tuples})
    if len(multi_tuples) < 40:
        raise SystemExit("only %d tuple(s) carry a MultiXact xmax"
                         % len(multi_tuples))

    multi_members = {}
    for m in multis_used:
        multi_members[m] = multixact_members(slru["pg_multixact/offsets"],
                                             slru["pg_multixact/members"], m)
        ups = [x for x, f in multi_members[m] if f in MXS_IS_UPDATE]
        if len(ups) > 1:
            raise SystemExit("multi %d has %d update members" % (m, len(ups)))

    # the raw decode must agree with the server, member for member
    for m in multis_used:
        acur.execute("SELECT xid::text, mode "
                     "FROM pg_get_multixact_members(%s::text::xid)", (str(m),))
        server = sorted((int(x), mode) for x, mode in acur.fetchall())
        mine = sorted((x, MXS_NAMES[f]) for x, f in multi_members[m])
        if server != mine:
            raise SystemExit("multi %d: raw decode %r != server %r"
                             % (m, mine, server))

    # the trailing sentinel must bound the last referenced multi's member list
    if multixact_offset(slru["pg_multixact/offsets"], max(multis_used) + 1) == 0:
        raise SystemExit("offsets[%d] is zero: the sentinel multi is missing"
                         % (max(multis_used) + 1))

    # the misread trap must be armed: every mxid on the pages, read as a plain
    # transaction id, must decode as COMMITTED in the shipped commit log, so a
    # solver that ignores HEAP_XMAX_IS_MULTI silently deletes live rows instead
    # of crashing
    for m in multis_used:
        if clog_status(slru["pg_xact"], m) != XACT_COMMITTED:
            raise SystemExit("mxid %d misread as an xid would not decode as "
                             "committed; the IS_MULTI trap is unarmed" % m)

    # ================= transaction states ====================================
    member_xids = {x for mm in multi_members.values() for x, _f in mm}
    needed = sorted({it["t_xmin"] for it in normals if it["t_xmin"]} |
                    {it["t_xmax"] for it in normals
                     if it["t_xmax"]
                     and not it["t_infomask"] & HEAP_XMAX_IS_MULTI} |
                    member_xids)
    if any(x >= 2 ** 32 for x in needed):
        raise SystemExit("32-bit xid space overflow")
    status = {}
    for x in needed:
        acur.execute("SELECT pg_xact_status(%s::text::xid8)", (x,))
        status[x] = acur.fetchone()[0]
    if set(status.values()) - {"committed", "aborted", "in progress"}:
        raise SystemExit("unexpected transaction states: %r" % (set(status.values()),))

    # --- the captured pg_xact must agree with the running server -------------
    for x in needed:
        decoded = CLOG_NAMES[clog_status(slru["pg_xact"], x)]
        if decoded == "sub committed":
            decoded = CLOG_NAMES[clog_status(
                slru["pg_xact"], subtrans_topmost(slru["pg_subtrans"], x))]
        if decoded != status[x]:
            raise SystemExit(
                "captured pg_xact disagrees with the server for xid %d: "
                "file says %r, pg_xact_status() says %r"
                % (x, decoded, status[x]))

    # --- the captured pg_subtrans must record the savepoint ancestry --------
    for child, want_top in sorted(sub_tops.items()):
        if subtrans_parent(slru["pg_subtrans"], child) == 0:
            raise SystemExit("pg_subtrans has no parent for subxid %d, but it "
                             "was written inside a savepoint" % child)
        got = subtrans_topmost(slru["pg_subtrans"], child)
        if got != want_top:
            raise SystemExit(
                "captured pg_subtrans resolves subxid %d to top-level %d, "
                "expected %d" % (child, got, want_top))

    subxids_on_pages = sorted(x for x in needed
                              if subtrans_parent(slru["pg_subtrans"], x))
    if len(subxids_on_pages) < 6:
        raise SystemExit("only %d subtransaction xid(s) reach the heap pages"
                         % len(subxids_on_pages))
    max_depth = 0
    for x in subxids_on_pages:
        d, cur = 0, x
        while subtrans_parent(slru["pg_subtrans"], cur):
            cur = subtrans_parent(slru["pg_subtrans"], cur)
            d += 1
        max_depth = max(max_depth, d)
    if max_depth < 3:
        raise SystemExit("deepest subtransaction chain is %d; a single-hop "
                         "lookup would be enough" % max_depth)

    # ================= the reference answer, computed by PostgreSQL ==========
    obs.run("SELECT " + ", ".join(COLNAMES) + " FROM " + QUALIFIED +
            " ORDER BY account_id")
    golden = obs.cur.fetchall()

    acur.execute("SELECT version()")
    version_full = acur.fetchone()[0]
    acur.execute("SHOW server_version")
    server_version = acur.fetchone()[0]
    acur.execute("SHOW block_size")
    block_size = int(acur.fetchone()[0])

    obs.rollback()
    obs.close()
    horizon.rollback()
    horizon.close()
    for s in list(writers.values()):
        s.rollback()
        s.close()
    acur.close()
    admin.close()

    # ================= snapshot bookkeeping and hard assertions ==============
    xmin_s, xmax_s, xip_s = snapshot_text.split(":")
    snap_xmin, snap_xmax = int(xmin_s), int(xmax_s)
    snap_xip = sorted(int(v) for v in xip_s.split(",") if v)
    if snap_xmin >= 2 ** 32 or snap_xmax >= 2 ** 32:
        raise SystemExit("snapshot uses a non-zero xid epoch")

    def in_snapshot(x):
        """XidInMVCCSnapshot: still running as of the target snapshot."""
        if x >= snap_xmax:
            return True
        if x < snap_xmin:
            return False
        return x in snap_xip

    committed_before = [x for x, s in status.items()
                        if s == "committed" and not in_snapshot(x)]
    committed_after = [x for x, s in status.items()
                       if s == "committed" and x >= snap_xmax]
    committed_in_xip = sorted(x for x in snap_xip if status.get(x) == "committed")
    aborted_in_xip = sorted(x for x in snap_xip if status.get(x) == "aborted")
    in_progress = sorted(x for x, s in status.items() if s == "in progress")
    committed_gap = sorted(x for x, s in status.items()
                           if s == "committed" and snap_xmin <= x < snap_xmax
                           and x not in snap_xip)

    if not committed_before or not committed_after:
        raise SystemExit("the snapshot does not separate committed transactions")
    if len(committed_in_xip) < 3:
        raise SystemExit("only %d committed transaction(s) in snapshot_xip; "
                         "ignoring xip would barely be punished"
                         % len(committed_in_xip))
    if len(committed_gap) < 3:
        raise SystemExit("only %d committed transaction(s) sit between "
                         "snapshot_xmin and snapshot_xmax outside xip; treating "
                         "every recent xid as in progress would barely be "
                         "punished" % len(committed_gap))
    if not in_progress:
        raise SystemExit("no transaction is still in progress in the commit log")
    if not aborted_in_xip:
        raise SystemExit("no aborted transaction is listed in snapshot_xip")

    # --- the load-bearing pg_subtrans assertion -----------------------------
    # Tuples stamped by a subtransaction that pg_xact reports as COMMITTED,
    # whose own xid is NOT in snapshot_xip, but whose topmost parent IS.
    # Ignoring pg_subtrans flips every one of these the wrong way.
    decisive = []
    for it in normals:
        for role in ("t_xmin", "t_xmax"):
            x = it[role]
            if not x:
                continue
            if role == "t_xmax" and it["t_infomask"] & HEAP_XMAX_IS_MULTI:
                continue
            top = subtrans_topmost(slru["pg_subtrans"], x)
            if (top != x and status.get(x) == "committed"
                    and x not in snap_xip and in_snapshot(top)):
                decisive.append((it["block"], it["lp"], role, x, top))
    if len(decisive) < 12:
        raise SystemExit(
            "only %d tuple stamp(s) require pg_subtrans to be resolved "
            "correctly; ignoring the subtransaction map would barely be "
            "punished" % len(decisive))

    # a subtransaction whose parent finished before the snapshot: its work IS
    # visible, so "has a parent" must not be read as "invisible"
    benign = [x for x in subxids_on_pages
              if status.get(x) == "committed"
              and not in_snapshot(subtrans_topmost(slru["pg_subtrans"], x))]
    if not benign:
        raise SystemExit("every subtransaction on the pages is invisible; the "
                         "map could be replaced by a blanket rule")

    # an aborted child under a committed parent
    aborted_children = [x for x in sub_tops
                        if clog_status(slru["pg_xact"], x) == XACT_ABORTED
                        and clog_status(slru["pg_xact"],
                                        subtrans_topmost(slru["pg_subtrans"], x))
                        == XACT_COMMITTED]
    if len(aborted_children) < 2:
        raise SystemExit("only %d aborted subtransaction(s) sit under a "
                         "committed parent" % len(aborted_children))
    for name, x in xids.items():
        if name.startswith("C_writer_") and x not in snap_xip:
            raise SystemExit("writer %s (xid %d) is not in snapshot_xip %r"
                             % (name, x, snap_xip))

    # --- frozen-tuple trap strength -----------------------------------------
    frozen_tuples = [it for it in normals
                     if (it["t_infomask"] & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN]
    frozen_with_xmax = [it for it in frozen_tuples if it["t_xmax"]]
    if len(frozen_tuples) < 30:
        raise SystemExit("only %d frozen tuple(s)" % len(frozen_tuples))
    if len(frozen_with_xmax) < 10:
        raise SystemExit("only %d frozen tuple(s) carry an xmax; the "
                         "'frozen means visible, stop' trap is unarmed"
                         % len(frozen_with_xmax))

    # --- MultiXact trap strength --------------------------------------------
    def multi_updater_of(m):
        ups = [x for x, f in multi_members[m] if f in MXS_IS_UPDATE]
        return ups[0] if ups else None

    lockonly_multi_tuples = [it for it in multi_tuples
                             if multi_updater_of(it["t_xmax"]) is None]
    updater_multi_tuples = [it for it in multi_tuples
                            if multi_updater_of(it["t_xmax"]) is not None]
    subxid_updater_tuples = [
        it for it in updater_multi_tuples
        if subtrans_parent(slru["pg_subtrans"], multi_updater_of(it["t_xmax"]))]
    snapdep_updater_tuples = [
        it for it in updater_multi_tuples
        if status.get(multi_updater_of(it["t_xmax"])) == "committed"
        and in_snapshot(subtrans_topmost(slru["pg_subtrans"],
                                         multi_updater_of(it["t_xmax"])))]
    member_flags_seen = sorted({f for mm in multi_members.values()
                                for _x, f in mm})
    member_states_seen = sorted({status.get(x, "?")
                                 for mm in multi_members.values()
                                 for x, _f in mm})
    if len(lockonly_multi_tuples) < 12:
        raise SystemExit("only %d locker-only multi tuple(s)"
                         % len(lockonly_multi_tuples))
    if len(updater_multi_tuples) < 25:
        raise SystemExit("only %d multi tuple(s) with an update member"
                         % len(updater_multi_tuples))
    if len(subxid_updater_tuples) < 15:
        raise SystemExit("only %d multi tuple(s) whose updater is a "
                         "subtransaction" % len(subxid_updater_tuples))
    if len(snapdep_updater_tuples) < 18:
        raise SystemExit("only %d multi tuple(s) whose updater is committed "
                         "yet invisible to the snapshot; ignoring the snapshot "
                         "for update members would barely be punished"
                         % len(snapdep_updater_tuples))
    if len(member_flags_seen) < 3:
        raise SystemExit("only member flags %r appear" % member_flags_seen)
    if not {"committed", "aborted", "in_progress"} <= {
            st.replace(" ", "_") for st in member_states_seen}:
        raise SystemExit("member states %r do not cover committed/aborted/"
                         "in-progress" % member_states_seen)

    # --- the load-bearing lock-only assertion -------------------------------
    # tuples whose xmax names a transaction that both committed AND is visible
    # to the target snapshot, but which are alive because the xmax is a lock.
    lock_only = [it for it in normals
                 if it["t_xmax"] and (
                     it["t_infomask"] & HEAP_XMAX_LOCK_ONLY
                     or (it["t_infomask"] & (HEAP_XMAX_IS_MULTI | HEAP_LOCK_MASK))
                     == HEAP_XMAX_EXCL_LOCK)]
    lock_only_hard = [it for it in lock_only
                      if not (it["t_infomask"] & HEAP_XMAX_INVALID)
                      and status.get(it["t_xmax"]) == "committed"
                      and not in_snapshot(it["t_xmax"])]
    if len(lock_only_hard) < 15:
        why = {"masked_by_xmax_invalid": 0, "xmax_not_committed": 0,
               "xmax_not_visible_to_snapshot": 0}
        for it in lock_only:
            if it["t_infomask"] & HEAP_XMAX_INVALID:
                why["masked_by_xmax_invalid"] += 1
            elif status.get(it["t_xmax"]) != "committed":
                why["xmax_not_committed"] += 1
            elif in_snapshot(it["t_xmax"]):
                why["xmax_not_visible_to_snapshot"] += 1
        raise SystemExit(
            "only %d tuple(s) carry a lock-only xmax that is committed, visible "
            "to the snapshot and NOT masked by HEAP_XMAX_INVALID; the infomask "
            "would not be load-bearing (total lock-only tuples: %d, rejected: %r)"
            % (len(lock_only_hard), len(lock_only), why))

    # --- HOT chain depth ----------------------------------------------------
    heap_only = [it for it in normals if it["t_infomask2"] & HEAP_ONLY_TUPLE]
    if len(heap_only) < 40:
        raise SystemExit("only %d heap-only tuple(s) survive" % len(heap_only))

    (outdir / "heap_pages.bin").write_bytes(heap)

    # the SLRU areas, byte for byte, under their real directory and segment names
    for area, segments in sorted(slru.items()):
        d = outdir / area
        d.mkdir(parents=True, exist_ok=True)
        for name, blob in sorted(segments.items()):
            (d / name).write_bytes(blob)

    schema = {
        "postgres_version": server_version,
        "postgres_version_full": version_full,
        "block_size": block_size,
        "table_name": QUALIFIED,
        "primary_key": ["account_id"],
        "columns": [{"name": n, "type": t, "nullable": nl} for n, t, nl in COLUMNS],
        "snapshot_xmin": snap_xmin,
        "snapshot_xmax": snap_xmax,
        "snapshot_xip": snap_xip,
    }
    (outdir / "table_schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")

    def fmt(v):
        if v is None:
            return "\\N"
        if isinstance(v, bool):
            return "t" if v else "f"
        if isinstance(v, dt.date):
            return v.isoformat()
        return str(v)

    with (internal / "golden.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(COLNAMES)
        for row in golden:
            w.writerow([fmt(v) for v in row])

    (internal / "page_items.json").write_text(json.dumps(items, indent=1) + "\n",
                                              encoding="utf-8")

    report = {
        "fixture_version": 2,
        "postgres_version_full": version_full,
        "block_size": block_size,
        "blocks": nblocks,
        "heap_bytes": len(heap),
        "heap_sha256": hashlib.sha256(heap).hexdigest(),
        "relation_path": relpath,
        "snapshot_text": snapshot_text,
        "snapshot_xmin": snap_xmin,
        "snapshot_xmax": snap_xmax,
        "snapshot_xip": snap_xip,
        "transaction_xids": xids,
        "id_groups": {k: [v[0], v[-1]] for k, v in IDS.groups.items()},
        "transaction_state_counts": {s: sum(1 for v in status.values() if v == s)
                                    for s in sorted(set(status.values()))},
        "xids_referenced_by_the_pages": len(needed),
        "slru_segments": {area: sorted(slru[area]) for area in slru},
        "slru_bytes": {area: sum(len(b) for b in slru[area].values())
                       for area in slru},
        "subtransaction_xids_on_pages": subxids_on_pages,
        "multixact_ids_on_pages": multis_used,
        "multixact_members": {str(m): [[x, MXS_NAMES[f]] for x, f in mm]
                              for m, mm in sorted(multi_members.items())},
        "tuples_with_multixact_xmax": len(multi_tuples),
        "lockonly_multixact_tuples": len(lockonly_multi_tuples),
        "updater_multixact_tuples": len(updater_multi_tuples),
        "subxid_updater_multixact_tuples": len(subxid_updater_tuples),
        "snapshot_dependent_updater_tuples": len(snapdep_updater_tuples),
        "multixact_member_flags_seen": member_flags_seen,
        "frozen_tuples": len(frozen_tuples),
        "frozen_tuples_with_xmax": len(frozen_with_xmax),
        "subtransaction_parent_entries": len(sub_tops),
        "deepest_subtransaction_chain": max_depth,
        "tuple_stamps_requiring_pg_subtrans": len(decisive),
        "visible_subtransactions": sorted(benign),
        "aborted_children_of_committed_parents": sorted(aborted_children),
        "committed_visible_to_snapshot": len(committed_before),
        "committed_after_snapshot": sorted(committed_after),
        "committed_but_listed_in_xip": committed_in_xip,
        "aborted_listed_in_xip": aborted_in_xip,
        "committed_inside_xid_range_but_not_in_xip": committed_gap,
        "still_in_progress": in_progress,
        "line_pointer_counts": {
            "LP_UNUSED": lp_counts.get(0, 0), "LP_NORMAL": lp_counts.get(1, 0),
            "LP_REDIRECT": lp_counts.get(2, 0), "LP_DEAD": lp_counts.get(3, 0)},
        "physical_tuples": len(normals),
        "tuples_with_nulls": sum(1 for it in normals
                                 if it["t_infomask"] & HEAP_HASNULL),
        "hot_updated_tuples": sum(1 for it in normals
                                  if it["t_infomask2"] & HEAP_HOT_UPDATED),
        "heap_only_tuples": len(heap_only),
        "lock_only_tuples": len(lock_only),
        "lock_only_tuples_requiring_infomask": len(lock_only_hard),
        "tuples_without_xmin_committed_hint": sum(
            1 for it in normals if not it["t_infomask"] & HEAP_XMIN_COMMITTED),
        "tuples_with_aborted_xmin": sum(
            1 for it in normals if status.get(it["t_xmin"]) == "aborted"),
        "tuples_with_aborted_xmin_and_invalid_hint": sum(
            1 for it in normals if status.get(it["t_xmin"]) == "aborted"
            and it["t_infomask"] & HEAP_XMIN_INVALID),
        "toast_chunks": toast_rows,
        "golden_rows": len(golden),
        "excluded_cases_present": {"combocid": 0, "toast_external": 0,
                                   "frozen_xid_rewritten": 0},
    }
    (internal / "generation_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    return report


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--outdir", default="/tmp/fixture")
    args = ap.parse_args()
    out = Path(args.outdir)
    out.mkdir(parents=True, exist_ok=True)
    report = build(out)
    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
