#!/usr/bin/env python3
"""Build the PostgreSQL 16 lost-clog-tail MVCC fixture (v5).

Runs INSIDE a postgres:16 container configured with

    shared_buffers=2MB  synchronous_commit=off  wal_writer_delay=10000ms
    checkpoint_timeout=1d  autovacuum=off  fsync=on  full_page_writes=off

and drives a real server through the incident this task recovers from:

  1. an OLTP schema of three relations (accounts, ledger_entries, account_tags)
     runs a normal, fully-resolved history whose transaction outcomes land on
     pg_xact page 0;
  2. ~32k transaction ids are consumed (one subtransaction each), pushing the
     xid counter across the clog page boundary, and a CHECKPOINT flushes
     everything - the last consistent on-disk state;
  3. the interesting recent history then runs.  Its outcomes live on clog
     page 1 BEYOND the range the checkpoint covered - PostgreSQL keeps those
     bits only in the SLRU buffer, because clog writeback is lazy (WAL is what
     makes commits durable);
  4. ordinary buffer-cache eviction (pg_prewarm of a filler relation) forces
     the heap pages of all three relations to the data volume;
  5. the data-volume files are captured at that instant.  This IS the crash
     state: heap effects durable, pg_xact stale for every recent xid, WAL
     (which would close the gap) on the failed volume and therefore absent.

No file is truncated, edited or fabricated afterwards.  The stale clog page is
stale because the server never flushed it - the generator only copies bytes.

Hint-bit evidence is controlled through genuine mechanisms, all probed live in
the authenticity gate:

  * asynchronous commit + a long wal_writer_delay keeps recent commit records
    unflushed, and SetHintBits refuses to stamp XMIN/XMAX_COMMITTED for an
    unflushed commit unless the page LSN already exceeds the commit LSN;
  * a synchronous transaction that performs a real write flushes WAL, after
    which a targeted index probe stamps a hint deliberately;
  * probing a tuple of a rolled-back transaction stamps XMIN/XMAX_INVALID
    unconditionally (abort hints carry no LSN gate).

The recent history is an evidence graph: 15 transaction outcomes are
undecidable from the surviving pg_xact alone; 7 carry a deliberate direct
hint, and 8 are recoverable only by reconciling primary-key / unique /
foreign-key constraints, cross-relation atomicity and the target snapshot over
complete candidate states.  Generation aborts unless exhaustive enumeration
proves exactly one assignment (and exactly one recovered.csv) is consistent
with the captured evidence - that proof runs host-side in generate_case.py,
using the same engine as the oracle.

Outputs (into --outdir):
    heap_pages.bin              solver input - accounts heap, raw blocks
    ledger_entries_heap.bin     solver input - ledger_entries heap
    account_tags_heap.bin       solver input - account_tags heap
    pg_xact/NNNN                solver input - surviving commit log, verbatim
    pg_subtrans/NNNN            solver input - subtransaction map, verbatim
    table_schema.json           solver input - schemas, constraints, snapshot
    internal/golden.csv         hidden reference, produced by PostgreSQL
    internal/generation_report.json
    internal/realism_observations.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import subprocess
import sys
import time
from pathlib import Path

import psycopg2
import psycopg2.extensions

DSN = dict(host="/var/run/postgresql", user="postgres", dbname="tb")

BLCKSZ = 8192
CLOG_XACTS_PER_PAGE = BLCKSZ * 4

DDL = """
DROP TABLE IF EXISTS account_tags, ledger_entries, accounts,
                     burn_seed, buffer_churn CASCADE;
CREATE TABLE accounts (
    account_id     integer               NOT NULL,
    holder_name    text                  NOT NULL,
    region         character varying(12) NOT NULL,
    balance_cents  integer               NOT NULL,
    is_active      boolean               NOT NULL,
    risk_tier      smallint,
    opened_on      date                  NOT NULL,
    note           text,
    CONSTRAINT accounts_pkey PRIMARY KEY (account_id),
    CONSTRAINT accounts_balance_floor CHECK (balance_cents > -10000000)
) WITH (autovacuum_enabled = false, toast.autovacuum_enabled = false);
CREATE TABLE ledger_entries (
    entry_id       integer NOT NULL,
    account_id     integer NOT NULL,
    amount_cents   integer NOT NULL,
    entered_on     date    NOT NULL,
    memo           text,
    CONSTRAINT ledger_entries_pkey PRIMARY KEY (entry_id),
    CONSTRAINT ledger_entries_amount_nonzero CHECK (amount_cents <> 0),
    CONSTRAINT ledger_entries_account_fk FOREIGN KEY (account_id)
        REFERENCES accounts (account_id)
) WITH (autovacuum_enabled = false, toast.autovacuum_enabled = false);
CREATE INDEX ledger_entries_account_idx ON ledger_entries (account_id);
CREATE TABLE account_tags (
    account_id     integer               NOT NULL,
    tag            character varying(20) NOT NULL,
    CONSTRAINT account_tags_pkey PRIMARY KEY (account_id, tag),
    CONSTRAINT account_tags_account_fk FOREIGN KEY (account_id)
        REFERENCES accounts (account_id)
) WITH (autovacuum_enabled = false, toast.autovacuum_enabled = false);
CREATE TABLE burn_seed (i integer);
"""

ACCT_COLS = ["account_id", "holder_name", "region", "balance_cents",
             "is_active", "risk_tier", "opened_on", "note"]
LEDGER_COLS = ["entry_id", "account_id", "amount_cents", "entered_on", "memo"]

REGIONS = ["EU-WEST-1", "US-EAST-2", "AP-SOUTH-1", "SA-EAST-1",
           "US-WEST-1", "EU-NORTH-1", "ME-CENTRAL"]
NAMES = ["Iris Malla", "Ovid Trent", "Kesh Aluri", "Bo Lindqvist", "Ana Reyes",
         "Timo Vaara", "Zara Okoye", "Lev Brandt", "Mai Trinh", "Rui Costa"]
NOTES = [None, "verified", "re-audit pendiente", "priority, escalated",
         "manual review", None, "dormant account", "kyc refresh", None,
         "watchlist"]


def acct_row(i):
    return (i, NAMES[i % len(NAMES)], REGIONS[(i * 3) % len(REGIONS)],
            100000 + (i * 7919) % 900000, (i % 3) != 0,
            None if i % 11 == 0 else (i % 5) + 1,
            dt.date(2019, 1, 1) + dt.timedelta(days=(i * 37) % 2200),
            NOTES[(i * 7) % len(NOTES)])


def ledger_row(e, acct):
    return (e, acct, (1 + (e * 131) % 90000) * (1 if e % 3 else -1),
            dt.date(2022, 3, 1) + dt.timedelta(days=(e * 13) % 700),
            None if e % 4 == 0 else "entry %d" % e)


INS_ACCT = ("INSERT INTO accounts (%s) VALUES (%s)"
            % (", ".join(ACCT_COLS), ", ".join(["%s"] * len(ACCT_COLS))))
INS_LEDGER = ("INSERT INTO ledger_entries (%s) VALUES (%s)"
              % (", ".join(LEDGER_COLS), ", ".join(["%s"] * len(LEDGER_COLS))))
INS_TAG = "INSERT INTO account_tags (account_id, tag) VALUES (%s, %s)"


class Session:
    def __init__(self, label, isolation=None, sync="off"):
        self.label = label
        self.conn = psycopg2.connect(**DSN)
        self.conn.autocommit = False
        if isolation == "repeatable read":
            self.conn.set_session(
                isolation_level=psycopg2.extensions.ISOLATION_LEVEL_REPEATABLE_READ)
        self.cur = self.conn.cursor()
        self.cur.execute("SET synchronous_commit = %s" % sync)
        # Client-side planner preference only: point lookups on these small
        # tables would otherwise be planned as seqscans, which visit every
        # tuple on every page and let PostgreSQL stamp hint bits far beyond
        # the row a statement targets.  The captured bytes stay 100%%
        # server-generated either way; this merely keeps each statement's
        # tuple visits as surgical as its WHERE clause.
        self.cur.execute("SET enable_seqscan = off")
        self.cur.execute("SET enable_bitmapscan = off")

    def run(self, sql, params=None):
        self.cur.execute(sql, params)
        return self.cur

    def xid(self):
        self.cur.execute("SELECT pg_current_xact_id_if_assigned()::text")
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


def one_shot(label, body, commit=True):
    s = Session(label)
    body(s)
    x = s.commit() if commit else s.rollback()
    s.close()
    if x is None:
        raise SystemExit("transaction %s was never assigned an xid" % label)
    return x


def probe(sql, params=None):
    """A read-only index probe from a throwaway session; PostgreSQL's own
    rules decide whether a hint bit is stamped."""
    c = psycopg2.connect(**DSN)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("SET enable_seqscan = off")
    cur.execute("SET enable_bitmapscan = off")
    cur.execute(sql, params)
    cur.fetchall()
    c.close()


def flush_wal():
    """The grant-hint primitive: a synchronous commit that performs a real
    write (a write-free sync commit skips XLogFlush - probed empirically)."""
    s = Session("wal-flush", sync="on")
    s.run("INSERT INTO burn_seed VALUES (-1)")
    s.commit()
    s.close()


# ---------------------------------------------------------------------------
def build(outdir: Path) -> dict:
    internal = outdir / "internal"
    internal.mkdir(parents=True, exist_ok=True)
    xids = {}
    obs_log = {}

    admin = psycopg2.connect(**DSN)
    admin.autocommit = True
    ac = admin.cursor()
    ac.execute("CREATE EXTENSION IF NOT EXISTS pg_prewarm")
    for stmt in DDL.strip().split(";\n"):
        if stmt.strip():
            ac.execute(stmt)
    ac.execute("CREATE TABLE buffer_churn AS "
               "SELECT g, repeat('x', 800) AS pad FROM generate_series(1,9000) g")
    obs_log["settings"] = {}
    for name in ("shared_buffers", "synchronous_commit", "wal_writer_delay",
                 "checkpoint_timeout", "fsync", "full_page_writes"):
        ac.execute("SHOW " + name)
        obs_log["settings"][name] = ac.fetchone()[0]

    def evict():
        for _ in range(2):
            ac.execute("SELECT sum(pg_prewarm(oid)) FROM pg_class "
                       "WHERE relname = 'buffer_churn'")
            ac.fetchall()

    def current_xid():
        ac.execute("SELECT pg_current_xact_id()::text")
        return int(ac.fetchone()[0])

    # ================= P1: fully-resolved history (clog page 0) ==============
    def _base(s):
        for i in range(1, 91):
            s.run(INS_ACCT, acct_row(i))
        # children reference only accounts 1-25, none of which is ever
        # deleted, so P1/P2 deletes of other accounts face no FK children
        for e in range(5040, 5046):
            s.run(INS_LEDGER, ledger_row(e, (e % 25) + 1))
        for e in range(4001, 4061):
            s.run(INS_LEDGER, ledger_row(e, (e % 25) + 1))
        for i in range(1, 30):
            s.run(INS_TAG, (i, "tier-%d" % (i % 4)))
        s.run(INS_TAG, (61, "flag"))
    xids["P1_base_insert"] = one_shot("p1-base", _base)

    churn = []
    for rnd in range(10):
        def _hot(s, rnd=rnd):
            for i in range(1, 7):
                s.run("UPDATE accounts SET balance_cents = balance_cents + %s, "
                      "note = %s WHERE account_id = %s",
                      (rnd + 1, "churn %d" % rnd, i))
        churn.append(one_shot("p1-churn-%d" % rnd, _hot))
        ac.execute("SELECT count(*) FROM accounts")
    xids["P1_churn"] = churn

    def _nonhot(s):
        for i in (70, 71, 72):
            s.run("UPDATE accounts SET region = %s, balance_cents = %s "
                  "WHERE account_id = %s", ("ME-CENTRAL", 210000 + i, i))
    xids["P1_nonhot_update"] = one_shot("p1-nonhot", _nonhot)

    def _p1del(s):
        s.run("DELETE FROM accounts WHERE account_id IN (80, 81)")
    xids["P1_delete"] = one_shot("p1-del", _p1del)
    for _ in range(4):
        ac.execute("SELECT count(*) FROM accounts")
        ac.execute("SELECT sum(balance_cents) FROM accounts")

    def _p1abort(s):
        s.run("UPDATE accounts SET balance_cents = -1, note = 'rolled back' "
              "WHERE account_id IN (73, 74, 75)")
    xids["P1_update_aborted"] = one_shot("p1-abort", _p1abort, commit=False)

    def _p1sub(s):
        s.run("UPDATE accounts SET note = 'sub parent' WHERE account_id = 76")
        s.run("SAVEPOINT sp1")
        s.run("UPDATE accounts SET balance_cents = 313000 "
              "WHERE account_id = 77")
        s.run("RELEASE SAVEPOINT sp1")
        s.run("SAVEPOINT sp2")
        s.run("UPDATE accounts SET balance_cents = -2, note = 'discarded' "
              "WHERE account_id = 78")
        s.run("ROLLBACK TO SAVEPOINT sp2")
    xids["P1_savepoints"] = one_shot("p1-sub", _p1sub)

    # Plug the free space that pruning recorded in the FSM.  Without this,
    # later inserts are routed into the churned pages, and a transaction's own
    # heap insert would raise that page's LSN above an earlier provider's
    # commit LSN - letting its uniqueness probe stamp a hint we need absent.
    def _p1plug(s):
        for i in range(95, 135):
            s.run(INS_ACCT, acct_row(i))
    xids["P1_fsm_plug"] = one_shot("p1-plug", _p1plug)

    def _p1lock(s):
        s.run("SELECT account_id FROM accounts "
              "WHERE account_id IN (85, 86, 87) ORDER BY account_id FOR UPDATE")
        s.cur.fetchall()
    xids["P1_lock_only"] = one_shot("p1-lock", _p1lock)

    # ================= burn across the clog page boundary ====================
    xid0 = current_xid()
    t0 = time.time()
    target = CLOG_XACTS_PER_PAGE + 200
    ac.execute("""DO $$ BEGIN
      FOR i IN 1..%d LOOP
        BEGIN
          INSERT INTO burn_seed VALUES (i);
        EXCEPTION WHEN others THEN NULL;
        END;
      END LOOP;
    END $$""" % (target - xid0))
    xid1 = current_xid()
    obs_log["burn"] = {"from_xid": xid0, "to_xid": xid1,
                       "seconds": round(time.time() - t0, 2)}
    if xid1 <= CLOG_XACTS_PER_PAGE:
        raise SystemExit("burn did not cross the clog page boundary")

    # ================= the last checkpoint ===================================
    ac.execute("CHECKPOINT")
    ac.execute("SELECT current_setting('data_directory')")
    datadir = Path(ac.fetchone()[0])
    clog_path = datadir / "pg_xact" / "0000"
    obs_log["checkpoint"] = {"clog_size_after": clog_path.stat().st_size,
                             "highest_pre_incident_xid": xid1}

    # horizon holder: pins the prune horizon so every physical version created
    # by the unresolved history survives to capture (read-only, no xid)
    horizon = Session("horizon", isolation="repeatable read")
    horizon.run("SELECT 1")
    if horizon.xid() is not None:
        raise SystemExit("horizon holder must stay read-only")

    # ================= P2: the unresolved history ============================
    truth = {}

    def committed(label, body):
        x = one_shot(label, body)
        xids[label] = x
        truth[x] = ("committed", label)
        return x

    def aborted_rollback(label, body):
        x = one_shot(label, body, commit=False)
        xids[label] = x
        truth[x] = ("aborted", label)
        return x

    def aborted_error(label, body):
        """The final statement of `body` raises (unique violation); the txn
        aborts server-side with its earlier tuples physically on the pages."""
        before = current_xid()
        s = Session(label)
        failed = False
        try:
            body(s)
        except psycopg2.Error:
            failed = True
            s.conn.rollback()
        s.close()
        if not failed:
            raise SystemExit("txn %s was expected to fail" % label)
        after = current_xid()
        x = before + 1
        if after != x + 1:
            raise SystemExit("xid accounting broke around %s (%d..%d)"
                             % (label, before, after))
        xids[label] = x
        truth[x] = ("aborted", label)
        return x

    SEAL = 16

    def seal_rows(s, base_id, tag):
        """Fat filler rows that fill the current accounts insertion page, so
        the sealed page holds only this transaction's tuples and its page LSN
        stays below this transaction's commit LSN until it is probed."""
        for j in range(SEAL):
            row = list(acct_row(base_id + j))
            row[7] = ("seal-%s " % tag) + "x" * 480
            s.run(INS_ACCT, row)

    # --- U_A [c]: direct commit hint granted on a CHILD-relation row ---------
    def _ua(s):
        for i in (201, 202, 203):
            s.run(INS_ACCT, acct_row(i))
        s.run(INS_LEDGER, ledger_row(5001, 201))
        s.run(INS_TAG, (201, "vip"))
    committed("U_A", _ua)

    # --- U_B [a]: direct abort hint via a probed aborted DELETE --------------
    def _ub(s):
        s.run("DELETE FROM accounts WHERE account_id = 30")
        s.run(INS_LEDGER, ledger_row(5004, 31))
    aborted_rollback("U_B", _ub)

    # --- CHAIN 1: hint(W2) => W1 committed => W3 aborted ---------------------
    def _w1(s):
        s.run(INS_ACCT, acct_row(220))
        seal_rows(s, 900, "w1a")
        s.run(INS_ACCT, acct_row(221))
        seal_rows(s, 916, "w1b")
    committed("W1", _w1)

    def _w3(s):
        s.run(INS_LEDGER, ledger_row(5005, 22))    # atomicity spread
        s.run(INS_ACCT, acct_row(220))             # unique violation vs W1
    aborted_error("W3", _w3)

    def _w2(s):
        s.run(INS_LEDGER, ledger_row(5010, 220))
        s.run(INS_TAG, (221, "gold"))
    committed("W2", _w2)

    # --- CHAIN 2: hint(W6) => W5 committed (delete of account 40) ------------
    def _w5(s):
        s.run("DELETE FROM accounts WHERE account_id = 40")
    committed("W5", _w5)

    def _w6(s):
        row = list(acct_row(40))
        row[1] = "Reopened Holder"
        row[3] = 40400
        s.run(INS_ACCT, row)
        s.run(INS_LEDGER, ledger_row(5053, 1))     # virgin probe target
    committed("W6", _w6)

    # --- W7 [a, indirect]: abort spread through an error --------------------
    def _w7(s):
        s.run(INS_LEDGER, ledger_row(5021, 43))
        s.run(INS_TAG, (43, "pending"))
        s.run(INS_ACCT, acct_row(44))              # unique violation vs P1
    aborted_error("W7", _w7)

    # --- CHAIN 3: hint(W10) => W8 committed => W15 aborted;
    # --- W9 aborted directly (auto-stamped by W8's HOT-chain walk) -----------
    def _w9(s):
        s.run("UPDATE accounts SET balance_cents = -3, note = 'w9 attempt' "
              "WHERE account_id = 45")
    aborted_rollback("W9", _w9)

    def _w8(s):
        s.run("UPDATE accounts SET balance_cents = 454500, note = 'w8 kept' "
              "WHERE account_id = 45")
        s.run(INS_ACCT, acct_row(230))
        seal_rows(s, 932, "w8a")
        s.run(INS_ACCT, acct_row(231))
        seal_rows(s, 948, "w8b")
    committed("W8", _w8)

    def _w15(s):
        s.run(INS_LEDGER, ledger_row(5022, 46))    # atomicity spread
        s.run(INS_ACCT, acct_row(230))             # unique violation vs W8
    aborted_error("W15", _w15)

    def _w10(s):
        s.run(INS_LEDGER, ledger_row(5030, 230))
        s.run(INS_TAG, (231, "silver"))
    committed("W10", _w10)

    # --- W11 [a, indirect]: composite-PK conflict against known P1 state -----
    def _w11(s):
        s.run(INS_TAG, (60, "audit"))
        s.run(INS_TAG, (61, "flag"))               # composite dup vs P1
    aborted_error("W11", _w11)

    # --- CHAIN 4: hint(W13) => W12 committed (delete of ledger 5040) ---------
    def _w12(s):
        s.run("DELETE FROM ledger_entries WHERE entry_id = 5040")
    committed("W12", _w12)

    def _w13(s):
        s.run(INS_LEDGER, ledger_row(5040, 12))    # re-insert of the deleted PK
        s.run(INS_LEDGER, ledger_row(5052, 12))    # virgin probe target
    committed("W13", _w13)

    # ---- open writers: still running at the snapshot (snapshot_xip) ---------
    writers = []
    wxa = Session("wx-a")
    wxa.run("UPDATE accounts SET balance_cents = -5, note = 'inflight' "
            "WHERE account_id = 50")
    xids["X_open_update"] = wxa.xid()
    writers.append(wxa)

    wxb = Session("wx-b")
    wxb.run(INS_ACCT, acct_row(240))
    wxb.run(INS_LEDGER, ledger_row(5060, 240))
    xids["X_open_insert"] = wxb.xid()
    writers.append(wxb)

    wxc = Session("wx-c")
    wxc.run("DELETE FROM accounts WHERE account_id = 52")
    xids["X_open_delete"] = wxc.xid()
    writers.append(wxc)

    # A monitoring heartbeat COMMITS while the writers are still open.  The
    # snapshot's xmax is latestCompletedXid + 1, so without a completed
    # transaction above the writers their xids would sit at/above xmax and
    # snapshot_xip would come out empty.  The heartbeat writes only to the
    # uncaptured burn_seed relation, so its xid never appears on the shipped
    # pages and adds nothing to the inference space.
    def _heartbeat(s):
        s.run("INSERT INTO burn_seed VALUES (-7)")
    xids["heartbeat"] = one_shot("heartbeat", _heartbeat)

    # ================= the target snapshot ===================================
    obs = Session("observer", isolation="repeatable read")
    obs.run("SELECT pg_current_snapshot()::text")
    snapshot_text = obs.cur.fetchone()[0]
    if obs.xid() is not None:
        raise SystemExit("the observer transaction wrote; it must be read-only")

    # ---- post-snapshot transactions (xid >= snapshot xmax) ------------------
    def _post1(s):
        s.run("UPDATE accounts SET balance_cents = 999000, "
              "note = 'post snapshot' WHERE account_id = 55")
    committed("Y_post_update", _post1)

    def _post2(s):
        s.run("DELETE FROM accounts WHERE account_id = 56")
    committed("Y_post_delete", _post2)

    # ================= grant the deliberate hints ============================
    flush_wal()
    probe("SELECT * FROM ledger_entries WHERE entry_id = 5001")   # U_A
    probe("SELECT * FROM ledger_entries WHERE entry_id = 5010")   # W2
    probe("SELECT * FROM ledger_entries WHERE entry_id = 5053")   # W6
    probe("SELECT * FROM ledger_entries WHERE entry_id = 5030")   # W10
    probe("SELECT * FROM ledger_entries WHERE entry_id = 5052")   # W13
    probe("SELECT * FROM accounts WHERE account_id = 30")         # U_B (xmax)

    # ================= capture: the crash instant ============================
    evict()

    def relfile(name):
        ac.execute("SELECT pg_relation_filepath(%s)", (name,))
        return datadir / ac.fetchone()[0]

    captured = {}
    for rel, fname in (("accounts", "heap_pages.bin"),
                       ("ledger_entries", "ledger_entries_heap.bin"),
                       ("account_tags", "account_tags_heap.bin")):
        data = relfile(rel).read_bytes()
        if len(data) % BLCKSZ:
            raise SystemExit("%s heap not whole pages" % rel)
        (outdir / fname).write_bytes(data)
        captured[rel] = data

    for area in ("pg_xact", "pg_subtrans"):
        d = outdir / area
        d.mkdir(parents=True, exist_ok=True)
        for f in sorted((datadir / area).iterdir()):
            if f.is_file():
                (d / f.name).write_bytes(f.read_bytes())

    ac.execute("SELECT pg_current_wal_insert_lsn()::text, "
               "pg_current_wal_lsn()::text, pg_current_wal_flush_lsn()::text")
    obs_log["capture"] = {
        "clog_file_size": clog_path.stat().st_size,
        "heap_sizes": {r: len(b) for r, b in captured.items()},
        "wal_insert_write_flush_lsn": ac.fetchone(),
    }

    # ================= post-capture validation ===============================
    snap_parts = snapshot_text.split(":")
    snap_xmin, snap_xmax = int(snap_parts[0]), int(snap_parts[1])
    snap_xip = sorted(int(v) for v in snap_parts[2].split(",") if v)

    # WAL recoverability evidence for the realism audit
    wal_lines = []
    for seg in sorted((datadir / "pg_wal").glob("0000*")):
        r = subprocess.run(["/usr/lib/postgresql/16/bin/pg_waldump", str(seg)],
                           capture_output=True, text=True)
        for ln in r.stdout.splitlines():
            up = ln.upper()
            if ("COMMIT" in up or "ABORT" in up) and "tx:" in ln:
                try:
                    tx = int(ln.split("tx:")[1].split(",")[0].strip())
                except ValueError:
                    continue
                if tx in truth:
                    wal_lines.append(ln.strip()[:150])
    obs_log["waldump_outcome_records_found"] = len(wal_lines)
    obs_log["waldump_samples"] = wal_lines[:6]
    if len(wal_lines) < len(truth) - 2:
        raise SystemExit("WAL does not hold the outcome records (%d found for "
                         "%d designed txns)" % (len(wal_lines), len(truth)))

    # server-side truth for every P2 xid must match the design
    server_truth = {}
    for x in sorted(truth):
        ac.execute("SELECT pg_xact_status(%s::text::xid8)", (str(x),))
        server_truth[x] = ac.fetchone()[0]
        if server_truth[x] != truth[x][0]:
            raise SystemExit("designed outcome of xid %d (%s) is %s but the "
                             "server says %s" % (x, truth[x][1], truth[x][0],
                                                 server_truth[x]))

    # the captured clog must be stale (zero bits) for every P2 xid
    clog_bytes = (outdir / "pg_xact" / "0000").read_bytes()
    stale_checked = 0
    for x in list(truth) + [xids["X_open_update"], xids["X_open_insert"],
                            xids["X_open_delete"]]:
        page = x // CLOG_XACTS_PER_PAGE
        off = page * BLCKSZ + (x % CLOG_XACTS_PER_PAGE) // 4
        if off >= len(clog_bytes):
            continue
        bits = (clog_bytes[off] >> ((x % 4) * 2)) & 3
        if bits != 0:
            raise SystemExit("captured clog already records xid %d (bits %d); "
                             "the incident did not occur" % (x, bits))
        stale_checked += 1
    obs_log["stale_clog_xids_verified"] = stale_checked

    # the golden answer, straight from the engine under the target snapshot
    obs.run("SELECT %s FROM accounts ORDER BY account_id"
            % ", ".join(ACCT_COLS))
    golden = obs.cur.fetchall()

    ac.execute("SELECT version()")
    version_full = ac.fetchone()[0]
    ac.execute("SHOW server_version")
    server_version = ac.fetchone()[0]

    obs.rollback(); obs.close()
    horizon.rollback(); horizon.close()
    for w in writers:
        w.rollback(); w.close()
    ac.close(); admin.close()

    # ================= write table_schema.json ===============================
    schema = {
        "postgres_version": server_version,
        "postgres_version_full": version_full,
        "block_size": BLCKSZ,
        "incident": ("The WAL volume of this cluster was lost in a crash. The "
                     "relation heap files and the surviving pg_xact and "
                     "pg_subtrans segments were recovered from the data "
                     "volume. The on-disk pg_xact does not record an outcome "
                     "for every transaction the heap pages reference."),
        "relations": [
            {"name": "accounts", "file": "heap_pages.bin",
             "columns": [
                 {"name": "account_id", "type": "integer", "nullable": False},
                 {"name": "holder_name", "type": "text", "nullable": False},
                 {"name": "region", "type": "character varying(12)",
                  "nullable": False},
                 {"name": "balance_cents", "type": "integer",
                  "nullable": False},
                 {"name": "is_active", "type": "boolean", "nullable": False},
                 {"name": "risk_tier", "type": "smallint", "nullable": True},
                 {"name": "opened_on", "type": "date", "nullable": False},
                 {"name": "note", "type": "text", "nullable": True}],
             "primary_key": ["account_id"],
             "checks": ["balance_cents > -10000000"]},
            {"name": "ledger_entries", "file": "ledger_entries_heap.bin",
             "columns": [
                 {"name": "entry_id", "type": "integer", "nullable": False},
                 {"name": "account_id", "type": "integer", "nullable": False},
                 {"name": "amount_cents", "type": "integer",
                  "nullable": False},
                 {"name": "entered_on", "type": "date", "nullable": False},
                 {"name": "memo", "type": "text", "nullable": True}],
             "primary_key": ["entry_id"],
             "checks": ["amount_cents <> 0"],
             "foreign_keys": [{"columns": ["account_id"],
                               "references": "accounts",
                               "referenced_columns": ["account_id"]}]},
            {"name": "account_tags", "file": "account_tags_heap.bin",
             "columns": [
                 {"name": "account_id", "type": "integer", "nullable": False},
                 {"name": "tag", "type": "character varying(20)",
                  "nullable": False}],
             "primary_key": ["account_id", "tag"],
             "foreign_keys": [{"columns": ["account_id"],
                               "references": "accounts",
                               "referenced_columns": ["account_id"]}]},
        ],
        "output_relation": "accounts",
        "snapshot_xmin": snap_xmin,
        "snapshot_xmax": snap_xmax,
        "snapshot_xip": snap_xip,
    }
    (outdir / "table_schema.json").write_text(
        json.dumps(schema, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8")

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
        w.writerow(ACCT_COLS)
        for row in golden:
            w.writerow([fmt(v) for v in row])

    report = {
        "fixture_version": 5,
        "postgres_version_full": version_full,
        "snapshot_text": snapshot_text,
        "snapshot_xmin": snap_xmin,
        "snapshot_xmax": snap_xmax,
        "snapshot_xip": snap_xip,
        "transaction_xids": {k: v for k, v in xids.items()
                             if isinstance(v, int)},
        "p1_xid_groups": {k: v for k, v in xids.items()
                          if isinstance(v, list)},
        "designed_truth": {str(x): {"outcome": t[0], "label": t[1]}
                           for x, t in sorted(truth.items())},
        "designed_direct_labels": ["U_A", "U_B", "W2", "W6", "W10", "W12",
                                   "W13"],
        "designed_indirect_labels": ["W1", "W3", "W5", "W7", "W8", "W9",
                                     "W11", "W15"],
        "server_truth": {str(x): s for x, s in sorted(server_truth.items())},
        "open_writer_xids": [xids["X_open_update"], xids["X_open_insert"],
                             xids["X_open_delete"]],
        "golden_rows": len(golden),
        "heap_bytes": {r: len(b) for r, b in captured.items()},
        "clog_file_size": obs_log["capture"]["clog_file_size"],
        "burn": obs_log["burn"],
        "realism": obs_log,
    }
    (internal / "generation_report.json").write_text(
        json.dumps(report, indent=2) + "\n", encoding="utf-8")
    (internal / "realism_observations.json").write_text(
        json.dumps(obs_log, indent=2) + "\n", encoding="utf-8")
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
