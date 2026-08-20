#!/usr/bin/env python3
"""Build the PostgreSQL 16 MVCC heap fixture (v2).

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

Outputs (into --outdir):
    heap_pages.bin              solver input  - raw 8192-byte blocks, in order
    tx_status.csv               solver input  - xid,status
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

    # HOT churn with a scan between rounds, so pruning really runs and leaves
    # LP_REDIRECT roots and LP_DEAD slots behind.  Nothing holds the horizon
    # back yet, which is exactly why these chains collapse.
    churn = []
    for rnd in range(22):
        def _hot(s, rnd=rnd):
            for i in A_CHURN + A_CHURN_LONG:
                s.run(upd + "balance_cents = balance_cents + %s, owner_note = %s "
                      "WHERE account_id = %s", (rnd + 1, "churn " + str(rnd), i))
        churn.append(one_shot("churn-" + str(rnd), _hot, True))
        acur.execute("SELECT count(*) FROM " + QUALIFIED)   # provoke pruning
    xids["A_churn_rounds"] = churn

    def _churn_final(s):
        for i in A_CHURN + A_CHURN_LONG:
            s.run(upd + "balance_cents = %s, owner_note = %s, risk_tier = %s "
                  "WHERE account_id = %s",
                  (250000 + i, None if i % 2 else "settled " + str(i),
                   (i % 4) + 1, i))
    xids["A_churn_final"] = one_shot("churn-final", _churn_final, True)
    acur.execute("SELECT count(*) FROM " + QUALIFIED)

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

    # ================= phase C: writers interleaved with committed work ======
    writers = {}

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

    # ================= writers finish, after the snapshot ====================
    for name in ("w1_upd_commits_later", "w2_del_commits_later",
                 "w3_ins_commits_later", "w4_lock_commits_later"):
        writers[name].commit()
        writers[name].close()
        del writers[name]
    writers["w5_upd_aborts_later"].rollback()
    writers["w5_upd_aborts_later"].close()
    del writers["w5_upd_aborts_later"]

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
        if m & HEAP_XMAX_IS_MULTI:
            problems.append(("multixact", it))
        if (m & HEAP_XMIN_FROZEN) == HEAP_XMIN_FROZEN:
            problems.append(("frozen", it))
        if it["t_xmin"] == FROZEN_XID:
            problems.append(("frozen-xid", it))
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

    # ================= transaction states ====================================
    needed = sorted({it["t_xmin"] for it in normals if it["t_xmin"]} |
                    {it["t_xmax"] for it in normals if it["t_xmax"]})
    if any(x >= 2 ** 32 for x in needed):
        raise SystemExit("32-bit xid space overflow")
    status = {}
    for x in needed:
        acur.execute("SELECT pg_xact_status(%s::text::xid8)", (x,))
        status[x] = acur.fetchone()[0]
    if set(status.values()) - {"committed", "aborted", "in progress"}:
        raise SystemExit("unexpected transaction states: %r" % (set(status.values()),))

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
        raise SystemExit("no transaction is still in progress in tx_status.csv")
    if not aborted_in_xip:
        raise SystemExit("no aborted transaction is listed in snapshot_xip")
    for name, x in xids.items():
        if name.startswith("C_writer_") and x not in snap_xip:
            raise SystemExit("writer %s (xid %d) is not in snapshot_xip %r"
                             % (name, x, snap_xip))

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

    with (outdir / "tx_status.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, lineterminator="\n")
        w.writerow(["xid", "status"])
        for x in needed:
            w.writerow([x, status[x].replace(" ", "_")])

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
        "tx_status_counts": {s: sum(1 for v in status.values() if v == s)
                             for s in sorted(set(status.values()))},
        "xids_in_tx_status": len(needed),
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
        "excluded_cases_present": {"multixact": 0, "frozen": 0, "combocid": 0,
                                   "toast_external": 0},
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
