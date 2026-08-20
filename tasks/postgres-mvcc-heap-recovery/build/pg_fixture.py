#!/usr/bin/env python3
"""Build the PostgreSQL 16 MVCC heap fixture.

This script runs INSIDE a postgres:16 container, as the `postgres` OS user, and
drives a real server through a deterministic history of committed, aborted and
still-running transactions.  It then captures the relation's main fork verbatim
and records, independently, the logical rows PostgreSQL itself reports for the
target snapshot.

Everything written here is synthetic and self-created; nothing is downloaded or
derived from external material.

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
HEAP_COMBOCID = 0x0020
HEAP_XMAX_LOCK_ONLY = 0x0080
HEAP_XMIN_COMMITTED = 0x0100
HEAP_XMIN_INVALID = 0x0200
HEAP_XMIN_FROZEN = HEAP_XMIN_COMMITTED | HEAP_XMIN_INVALID
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
) WITH (fillfactor = 70, autovacuum_enabled = false,
        toast.autovacuum_enabled = false);
CREATE INDEX account_ledger_region_idx ON %(t)s (region_code);
""" % {"t": QUALIFIED}

# ---------------------------------------------------------------- synthetic data
REGIONS = ["EU-WEST-1", "US-EAST-2", "AP-SOUTH-1", "SA-EAST-1",
           "US-WEST-1", "EU-NORTH-1", "ME-CENTRAL"]
NOTES = [None, "verified", "re-audit pendiente", "priority, escalated",
         "ueberprueft 2024", "manual review", None, "dormant account",
         "kyc refresh", None, "watchlist", "tier upgrade"]

BASE_IDS = list(range(1, 66))

# id groups - every physical scenario the fixture must contain
G_HOT = list(range(1, 7))            # HOT churn + page pruning, pre-snapshot
G_UPD_COMMIT = list(range(7, 13))    # committed non-HOT update, pre-snapshot
G_DEL_COMMIT = list(range(13, 18))   # committed delete, pre-snapshot
G_UPD_ABORT = list(range(18, 24))    # update rolled back, pre-snapshot
G_DEL_ABORT = list(range(24, 29))    # delete rolled back, pre-snapshot
G_MULTIVER = list(range(29, 35))     # several committed versions, pre-snapshot
G_LOCKED = list(range(35, 38))       # SELECT FOR UPDATE in a committed txn
G_IP_UPD = list(range(38, 42))       # updated by a still-running transaction
G_IP_DEL = list(range(42, 45))       # deleted by a still-running transaction
G_IP_UPD2 = list(range(45, 47))      # updated by the second running transaction
G_POST_UPD = list(range(47, 53))     # updated by a transaction that commits AFTER
G_POST_DEL = list(range(53, 56))     # deleted by a transaction that commits AFTER
G_POST_ABORT = list(range(56, 59))   # updated then rolled back, after the snapshot
G_POST_DEL_ABORT = list(range(59, 61))  # deleted then rolled back, after
G_XIP_UPD = list(range(61, 64))      # updated by a txn listed in snapshot_xip
G_XIP_DEL = list(range(64, 66))      # deleted by a txn listed in snapshot_xip

# Filler accounts.  They carry the same scenarios onto the later blocks of the
# relation, so the file cannot be solved by looking at block 0 alone.
FILLER_IDS = list(range(1001, 1161))
F_POST_UPD = list(range(1001, 1041))    # committed after the snapshot
F_UPD_ABORT = list(range(1041, 1071))   # update rolled back
F_IP_UPD = list(range(1071, 1091))      # updated by a still-running transaction
F_UPD_COMMIT = list(range(1091, 1121))  # committed before the snapshot
F_DEL_COMMIT = list(range(1121, 1131))  # committed delete before the snapshot
F_XIP_DEL = list(range(1131, 1141))     # deleted by a transaction in snapshot_xip
F_POST_DEL = list(range(1141, 1151))    # deleted after the snapshot, committed

INS_ABORT_IDS = [501, 502, 503, 504]      # inserted by an aborted transaction
INS_IP1_IDS = [601, 602, 603]             # inserted by a running transaction
INS_IP2_IDS = [604, 605]
INS_XIP_IDS = [606, 607]                  # inserted by the snapshot_xip transaction
INS_POST_IDS = [701, 702, 703, 704, 705]  # inserted after the snapshot, committed


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

    # ---- phase 1: base population, committed, before the snapshot -----------
    def _base(s):
        for i in BASE_IDS:
            s.run(ins, base_row(i))
        for i in FILLER_IDS:
            s.run(ins, base_row(i))
    xids["T_base_insert"] = one_shot("base-insert", _base, True)

    # ---- phase 2: HOT churn, then page pruning -----------------------------
    # No long-lived transaction is open here, so the pruning horizon is current
    # and the dead intermediate versions really are reclaimed, leaving
    # LP_REDIRECT roots and LP_DEAD/LP_UNUSED slots behind.
    hot_xids = []
    for rnd in range(24):
        def _hot(s, rnd=rnd):
            for i in G_HOT:
                s.run(upd + "balance_cents = balance_cents + %s, owner_note = %s "
                      "WHERE account_id = %s", (rnd + 1, "hot round " + str(rnd), i))
        hot_xids.append(one_shot("hot-" + str(rnd), _hot, True))
        acur.execute("SELECT count(*) FROM " + QUALIFIED)   # provoke pruning
    xids["T_hot_rounds"] = hot_xids

    def _hot_final(s):
        for i in G_HOT:
            s.run(upd + "balance_cents = %s, owner_note = %s, risk_tier = %s "
                  "WHERE account_id = %s",
                  (250000 + i, None if i % 2 else "settled " + str(i), (i % 4) + 1, i))
    xids["T_hot_final"] = one_shot("hot-final", _hot_final, True)
    acur.execute("SELECT count(*) FROM " + QUALIFIED)

    # ---- phase 3: committed non-HOT update (an indexed column changes) ------
    def _upd(s):
        for i in G_UPD_COMMIT:
            s.run(upd + "region_code = %s, balance_cents = %s, owner_note = %s "
                  "WHERE account_id = %s",
                  ("EU-NORTH-1", 310000 + i, "regional transfer", i))
        for i in F_UPD_COMMIT:
            s.run(upd + "balance_cents = %s, risk_tier = %s, owner_note = %s "
                  "WHERE account_id = %s",
                  (320000 + i, None if i % 5 == 0 else (i % 3) + 1,
                   None if i % 4 == 0 else "settled batch", i))
    xids["T_update_committed"] = one_shot("upd-commit", _upd, True)

    # ---- phase 4: committed delete ----------------------------------------
    def _del(s):
        s.run(dele, (G_DEL_COMMIT + F_DEL_COMMIT,))
    xids["T_delete_committed"] = one_shot("del-commit", _del, True)

    # ---- phase 5: several committed versions of the same key ---------------
    mv = []
    for rnd in range(3):
        def _mv(s, rnd=rnd):
            for i in G_MULTIVER:
                s.run(upd + "region_code = %s, balance_cents = %s, risk_tier = %s, "
                      "owner_note = %s WHERE account_id = %s",
                      (REGIONS[(i + rnd) % len(REGIONS)], 400000 + rnd * 1000 + i,
                       None if rnd == 1 else (rnd + 1),
                       None if (i + rnd) % 3 == 0 else "revision " + str(rnd), i))
        mv.append(one_shot("multiver-" + str(rnd), _mv, True))
    xids["T_multiversion"] = mv

    # ---- phase 6: aborted insert ------------------------------------------
    def _ins_abort(s):
        for i in INS_ABORT_IDS:
            s.run(ins, extra_row(i, "phantom"))
    xids["T_insert_aborted"] = one_shot("ins-abort", _ins_abort, False)

    # ---- phase 7: aborted update ------------------------------------------
    def _upd_abort(s):
        for i in G_UPD_ABORT:
            s.run(upd + "balance_cents = %s, region_code = %s, is_active = %s, "
                  "owner_note = %s WHERE account_id = %s",
                  (-1, "XX-BOGUS-1", False, "rolled back", i))
        s.run(upd + "balance_cents = %s, is_active = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-2, False, "rolled back batch", F_UPD_ABORT))
    xids["T_update_aborted"] = one_shot("upd-abort", _upd_abort, False)

    # ---- phase 8: aborted delete ------------------------------------------
    def _del_abort(s):
        s.run(dele, (G_DEL_ABORT,))
    xids["T_delete_aborted"] = one_shot("del-abort", _del_abort, False)

    # ---- phase 9: committed row lock (xmax present, but lock-only) ----------
    def _lock(s):
        s.run("SELECT account_id FROM " + QUALIFIED +
              " WHERE account_id = ANY(%s) ORDER BY account_id FOR UPDATE",
              (G_LOCKED,))
        s.cur.fetchall()
    xids["T_lock_only"] = one_shot("lock-only", _lock, True)

    # ---- phase 10: two transactions that stay open across the capture ------
    ip1 = Session("in-progress-1")
    ip1.run(upd + "balance_cents = %s, owner_note = %s, is_active = %s "
            "WHERE account_id = ANY(%s)",
            (-999, "uncommitted write", False, G_IP_UPD))
    ip1.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = ANY(%s)",
            (-777, "uncommitted batch", F_IP_UPD))
    for i in INS_IP1_IDS:
        ip1.run(ins, extra_row(i, "uncommitted"))
    xids["T_in_progress_1"] = ip1.xid()

    ip2 = Session("in-progress-2")
    ip2.run(dele, (G_IP_DEL,))
    ip2.run(upd + "region_code = %s, balance_cents = %s WHERE account_id = ANY(%s)",
            ("XX-PENDING", -888, G_IP_UPD2))
    for i in INS_IP2_IDS:
        ip2.run(ins, extra_row(i, "uncommitted"))
    xids["T_in_progress_2"] = ip2.xid()

    # A third writer that is still running when the snapshot is taken, but that
    # COMMITS before the heap is captured.  tx_status.csv therefore reports it
    # as `committed` while snapshot_xip lists it as in progress: its rows are
    # invisible to the target snapshot even though its commit record exists.
    ip3 = Session("in-progress-3")
    ip3.run(upd + "balance_cents = %s, region_code = %s, owner_note = %s "
            "WHERE account_id = ANY(%s)",
            (-1234, "XX-INFLIGHT", "in flight at snapshot time", G_XIP_UPD))
    ip3.run(dele, (G_XIP_DEL + F_XIP_DEL,))
    for i in INS_XIP_IDS:
        ip3.run(ins, extra_row(i, "inflight"))
    xids["T_in_xip_then_committed"] = ip3.xid()

    # ---- phase 10b: a transaction that completes while ip1..ip3 are open ---
    # Without this, the snapshot's xmax would sit below every running xid and
    # snapshot_xip would come out empty (xmax = latestCompletedXid + 1).
    def _mv_late(s):
        for i in G_MULTIVER:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (500000 + i, "late but committed", i))
    xids["T_committed_after_open_writers"] = one_shot("mv-late", _mv_late, True)

    # ---- phase 11: the target snapshot ------------------------------------
    obs = Session("observer", isolation="repeatable read")
    obs.run("SELECT pg_current_snapshot()::text")
    snapshot_text = obs.cur.fetchone()[0]
    obs.run("SELECT pg_current_xact_id_if_assigned()")   # must stay read-only
    if obs.cur.fetchone()[0] is not None:
        raise SystemExit("the observer transaction wrote; it must be read-only")

    # ---- phase 12: work that COMMITS after the snapshot was taken ----------
    def _post_upd(s):
        for i in G_POST_UPD:
            s.run(upd + "balance_cents = %s, region_code = %s, risk_tier = %s, "
                  "owner_note = %s WHERE account_id = %s",
                  (777000 + i, "ZZ-FUTURE-9", 9, "committed after snapshot", i))
        s.run(upd + "balance_cents = %s, region_code = %s, owner_note = %s "
              "WHERE account_id = ANY(%s)",
              (-31337, "ZZ-FUTURE-9", "future batch", F_POST_UPD))
    xids["T_post_update"] = one_shot("post-upd", _post_upd, True)

    def _post_ins(s):
        for i in INS_POST_IDS:
            s.run(ins, extra_row(i, "future"))
    xids["T_post_insert"] = one_shot("post-ins", _post_ins, True)

    def _post_del(s):
        s.run(dele, (G_POST_DEL + F_POST_DEL,))
    xids["T_post_delete"] = one_shot("post-del", _post_del, True)

    def _post_hot(s):
        for i in G_HOT[:3]:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (888000 + i, "future hot version", i))
    xids["T_post_hot_update"] = one_shot("post-hot", _post_hot, True)

    def _post_upd_abort(s):
        for i in G_POST_ABORT:
            s.run(upd + "balance_cents = %s, owner_note = %s WHERE account_id = %s",
                  (-4242, "future rollback", i))
    xids["T_post_update_aborted"] = one_shot("post-upd-abort", _post_upd_abort, False)

    def _post_del_abort(s):
        s.run(dele, (G_POST_DEL_ABORT,))
    xids["T_post_delete_aborted"] = one_shot("post-del-abort", _post_del_abort, False)

    # ---- phase 12b: the xip writer commits, after the snapshot was taken ---
    ip3.commit()
    ip3.close()

    # ---- phase 13: flush and capture the relation file verbatim ------------
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

    # ---- phase 14: inspect exactly those bytes -----------------------------
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

    # ---- phase 15: transaction states -------------------------------------
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

    # ---- phase 16: the reference answer, computed by PostgreSQL ------------
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
    ip1.rollback()
    ip1.close()
    ip2.rollback()
    ip2.close()
    acur.close()
    admin.close()

    # ---- phase 17: write everything out ------------------------------------
    xmin_s, xmax_s, xip_s = snapshot_text.split(":")
    snap_xmin, snap_xmax = int(xmin_s), int(xmax_s)
    snap_xip = sorted(int(v) for v in xip_s.split(",") if v)
    if snap_xmin >= 2 ** 32 or snap_xmax >= 2 ** 32:
        raise SystemExit("snapshot uses a non-zero xid epoch")
    if not snap_xip:
        raise SystemExit("the snapshot has an empty in-progress list")

    committed_before = [x for x, s in status.items()
                        if s == "committed" and x < snap_xmax and x not in snap_xip]
    committed_after = [x for x, s in status.items()
                       if s == "committed" and x >= snap_xmax]
    committed_in_xip = [x for x in snap_xip if status.get(x) == "committed"]
    in_progress = [x for x, s in status.items() if s == "in progress"]
    if not committed_before or not committed_after:
        raise SystemExit("the snapshot does not separate committed transactions")
    if not committed_in_xip:
        raise SystemExit("no committed transaction is listed in snapshot_xip; "
                         "ignoring xip would not be punished")
    if not in_progress:
        raise SystemExit("no transaction is still in progress in tx_status.csv")
    for x in (xids["T_in_progress_1"], xids["T_in_progress_2"],
              xids["T_in_xip_then_committed"]):
        if x not in snap_xip:
            raise SystemExit("expected xid %d in snapshot_xip, got %r"
                             % (x, snap_xip))

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
        "tx_status_counts": {s: sum(1 for v in status.values() if v == s)
                             for s in sorted(set(status.values()))},
        "xids_in_tx_status": len(needed),
        "committed_before_snapshot": len(committed_before),
        "committed_after_snapshot": len(committed_after),
        "committed_but_listed_in_xip": committed_in_xip,
        "still_in_progress": sorted(in_progress),
        "line_pointer_counts": {
            "LP_UNUSED": lp_counts.get(0, 0), "LP_NORMAL": lp_counts.get(1, 0),
            "LP_REDIRECT": lp_counts.get(2, 0), "LP_DEAD": lp_counts.get(3, 0)},
        "physical_tuples": len(normals),
        "tuples_with_nulls": sum(1 for it in normals
                                 if it["t_infomask"] & HEAP_HASNULL),
        "hot_updated_tuples": sum(1 for it in normals
                                  if it["t_infomask2"] & HEAP_HOT_UPDATED),
        "heap_only_tuples": sum(1 for it in normals
                                if it["t_infomask2"] & HEAP_ONLY_TUPLE),
        "lock_only_tuples": sum(1 for it in normals
                                if it["t_infomask"] & HEAP_XMAX_LOCK_ONLY),
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
