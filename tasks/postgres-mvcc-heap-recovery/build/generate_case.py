#!/usr/bin/env python3
"""Regenerate every input file from a real PostgreSQL 16 server (fixture v5).

Starts a throwaway `postgres:16` container configured for the lost-clog-tail
incident (async commit, long wal_writer_delay, tiny shared_buffers, fsync on,
no auto checkpoints), runs build/pg_fixture.py inside it against the live
server, copies the captured crash-instant artifacts back into the task, and
rebuilds the verifier fixture, solution.sh and the solver ZIP.

    python3 build/generate_case.py [--keep] [--image postgres:16]

It then re-runs the inference oracle on the captured artifacts and asserts:
  * the exhaustive candidate funnel ends at EXACTLY ONE assignment and one
    distinct output;
  * the oracle's answer equals the golden state PostgreSQL itself reported
    under the target snapshot;
  * the indirect-evidence ratio meets the design floor.
Generation fails otherwise.
"""
from __future__ import annotations

import argparse
import csv
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

TASK = Path(__file__).resolve().parent.parent
BUILD = TASK / "build"
ART = TASK / "artifacts"
INTERNAL = BUILD / "internal"
CONTAINER = "tb-pg-mvcc-fixture"

SOLVER_FILES = ["heap_pages.bin", "ledger_entries_heap.bin",
                "account_tags_heap.bin", "table_schema.json"]
SOLVER_DIRS = ["pg_xact", "pg_subtrans"]
INTERNAL_FILES = ["golden.csv", "generation_report.json",
                  "realism_observations.json"]

PG_FLAGS = ["-c", "shared_buffers=2MB", "-c", "max_connections=25",
            "-c", "synchronous_commit=off", "-c", "wal_writer_delay=10000",
            "-c", "checkpoint_timeout=86400", "-c", "autovacuum=off",
            "-c", "fsync=on", "-c", "full_page_writes=off"]


def sh(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, text=True, **kw)
    if r.returncode != 0:
        raise SystemExit("command failed: %s\n%s\n%s"
                         % (" ".join(map(str, cmd)), r.stdout, r.stderr))
    return r


def docker(*args, check=True):
    return sh(["docker", *map(str, args)]) if check else subprocess.run(
        ["docker", *map(str, args)], capture_output=True, text=True)


def wait_ready(name: str, timeout: int = 90):
    deadline = time.time() + timeout
    while time.time() < deadline:
        r = subprocess.run(["docker", "exec", name, "pg_isready", "-U",
                            "postgres"], capture_output=True, text=True)
        if r.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("PostgreSQL did not become ready within %ds" % timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="postgres:16")
    ap.add_argument("--keep", action="store_true")
    args = ap.parse_args()

    ART.mkdir(exist_ok=True)
    INTERNAL.mkdir(parents=True, exist_ok=True)

    docker("rm", "-f", CONTAINER, check=False)
    print("starting", args.image, "with the incident configuration")
    docker("run", "-d", "--name", CONTAINER,
           "-e", "POSTGRES_PASSWORD=pw", "-e", "POSTGRES_DB=tb",
           "-e", "LANG=C.UTF-8", args.image, *PG_FLAGS)
    try:
        wait_ready(CONTAINER)
        print("installing the psycopg2 driver in the container")
        sh(["docker", "exec", CONTAINER, "bash", "-lc",
            "apt-get update -qq && apt-get install -y -qq python3-psycopg2 "
            "&& mkdir -p /tmp/fixture && chown -R postgres /tmp/fixture"])
        docker("cp", str(BUILD / "pg_fixture.py"),
               f"{CONTAINER}:/tmp/pg_fixture.py")

        print("driving the incident history")
        r = sh(["docker", "exec", "-u", "postgres", CONTAINER,
                "python3", "/tmp/pg_fixture.py", "--outdir", "/tmp/fixture"])
        report = json.loads(r.stdout[r.stdout.index("{"):])

        for name in SOLVER_FILES:
            docker("cp", f"{CONTAINER}:/tmp/fixture/{name}", str(ART / name))
        for name in SOLVER_DIRS:
            target = ART / name
            if target.exists():
                shutil.rmtree(target)
            docker("cp", f"{CONTAINER}:/tmp/fixture/{name}", str(target))
        for name in INTERNAL_FILES:
            docker("cp", f"{CONTAINER}:/tmp/fixture/internal/{name}",
                   str(INTERNAL / name))
    finally:
        if not args.keep:
            docker("rm", "-f", CONTAINER, check=False)

    # retire artifacts of earlier fixture generations
    for stale in ("tx_status.csv",):
        p = ART / stale
        if p.exists():
            p.unlink()
            print("removed stale", stale)
    stale_dir = ART / "pg_multixact"
    if stale_dir.exists():
        shutil.rmtree(stale_dir)
        print("removed stale pg_multixact/ (not part of fixture v5)")

    for name in SOLVER_DIRS:
        segs = sorted(q for q in (ART / name).rglob("*") if q.is_file())
        if not segs:
            raise SystemExit(f"{name}/ came back empty")
        print(f"  {name}/: " + ", ".join(
            f"{q.name} ({q.stat().st_size} bytes)" for q in segs))

    # ---------------- host-side proof: unique assignment, unique output ------
    print("running the inference oracle for the uniqueness proof")
    work = BUILD / "_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()
    r = sh([sys.executable, str(TASK / "solution" / "golden_recover.py"),
            "--dir", str(ART), "--out", str(work / "oracle.csv"), "--report"])
    oracle_report = json.loads(r.stdout[r.stdout.index("{"):])
    funnel = oracle_report["funnel"]
    print("  funnel:", json.dumps(funnel))
    if funnel["after_all_constraints"] != 1:
        raise SystemExit("uniqueness proof FAILED: %d assignments survive"
                         % funnel["after_all_constraints"])
    if funnel["distinct_outputs"] != 1:
        raise SystemExit("output-uniqueness proof FAILED: %d distinct outputs"
                         % funnel["distinct_outputs"])

    # the inference answer must equal PostgreSQL's own answer
    def rows(p):
        with open(p, encoding="utf-8", newline="") as fh:
            rs = [x for x in csv.reader(fh) if x]
        return rs[0], sorted(rs[1:])

    gh, gr = rows(INTERNAL / "golden.csv")
    oh, orr = rows(work / "oracle.csv")
    if gh != oh or gr != orr:
        raise SystemExit("inference oracle differs from the PostgreSQL golden")
    print("  oracle == PostgreSQL golden (%d rows)" % len(gr))

    # the inference answer must match the assignment PostgreSQL really took
    designed = {int(x): d["outcome"]
                for x, d in report["designed_truth"].items()}
    final = {int(x): o for x, o in oracle_report["final_assignment"].items()}
    for x, o in final.items():
        if designed.get(x) != o:
            raise SystemExit("inferred outcome of xid %d (%s) does not match "
                             "the server truth (%s)"
                             % (x, o, designed.get(x)))
    print("  inferred assignment == server truth for all %d unresolved xids"
          % len(final))

    # design floors: indirect evidence share and chain usefulness
    n_unres = len(oracle_report["unresolved_xids"])
    n_direct = len(oracle_report["direct_hint_xids"])
    n_indirect = len(oracle_report["indirect_xids"])
    print("  unresolved=%d direct-hinted=%d indirect=%d"
          % (n_unres, n_direct, n_indirect))
    if n_unres < 12:
        raise SystemExit("only %d unresolved xids; design floor is 12" % n_unres)
    if n_indirect < max(4, int(0.30 * n_unres)):
        raise SystemExit("only %d indirect xids (%d unresolved); the "
                         "inference layer is under-determined by hints"
                         % (n_indirect, n_unres))

    # the designed evidence split must hold EXACTLY: every intended-indirect
    # transaction hint-free, every intended-direct one hinted
    by_label = {d["label"]: int(x) for x, d in report["designed_truth"].items()}
    want_direct = {by_label[l] for l in report["designed_direct_labels"]}
    want_indirect = {by_label[l] for l in report["designed_indirect_labels"]}
    got_direct = set(oracle_report["direct_hint_xids"])
    got_indirect = set(oracle_report["indirect_xids"])
    if got_direct != want_direct or got_indirect != want_indirect:
        raise SystemExit(
            "evidence split drifted from the design: unexpected hints on %r; "
            "hints missing on %r"
            % (sorted(got_direct - want_direct),
               sorted(want_direct - got_direct)))
    print("  evidence split matches the design exactly")

    # persist the oracle-side evidence record for the docs and tests
    (INTERNAL / "inference_report.json").write_text(
        json.dumps(oracle_report, indent=2) + "\n", encoding="utf-8")

    print("rebuilding the verifier fixture, solution.sh and the ZIP")
    sh([sys.executable, str(BUILD / "make_expected_state.py")])
    sh([sys.executable, str(BUILD / "make_solution_sh.py")])
    sh([sys.executable, str(BUILD / "make_zip.py")])

    print(json.dumps({k: report[k] for k in
                      ("fixture_version", "snapshot_text", "golden_rows",
                       "heap_bytes", "clog_file_size", "burn")}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
