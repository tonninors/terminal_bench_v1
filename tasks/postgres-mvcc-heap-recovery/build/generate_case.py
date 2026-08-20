#!/usr/bin/env python3
"""Regenerate every input file from a real PostgreSQL 16 server.

Starts a throwaway `postgres:16` container, runs build/pg_fixture.py inside it
against the live server, copies the resulting files back into the task, and
rebuilds the verifier fixture, solution.sh and the solver ZIP.

    python3 build/generate_case.py [--keep] [--image postgres:16]

Requires Docker.  Regeneration is not byte-for-byte reproducible - PostgreSQL
assigns transaction ids and page LSNs itself, and forcing those would mean
editing genuine page structures - but the procedure, the logical expected state
and every downstream check are deterministic.  Use build/check_fixture.py to
re-assert the properties the task depends on.
"""
from __future__ import annotations

import argparse
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

SOLVER_FILES = ["heap_pages.bin", "table_schema.json"]
SOLVER_DIRS = ["pg_xact", "pg_subtrans"]
INTERNAL_FILES = ["golden.csv", "generation_report.json", "page_items.json"]


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
        r = subprocess.run(["docker", "exec", name, "pg_isready", "-U", "postgres"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            return
        time.sleep(1)
    raise SystemExit("PostgreSQL did not become ready within %ds" % timeout)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="postgres:16")
    ap.add_argument("--keep", action="store_true",
                    help="leave the container running for inspection")
    args = ap.parse_args()

    ART.mkdir(exist_ok=True)
    INTERNAL.mkdir(parents=True, exist_ok=True)

    docker("rm", "-f", CONTAINER, check=False)
    print("starting", args.image)
    docker("run", "-d", "--name", CONTAINER,
           "-e", "POSTGRES_PASSWORD=pw", "-e", "POSTGRES_DB=tb",
           "-e", "LANG=C.UTF-8", args.image,
           "-c", "autovacuum=off", "-c", "fsync=off", "-c", "full_page_writes=off")
    try:
        wait_ready(CONTAINER)
        print("installing the psycopg2 driver in the container")
        sh(["docker", "exec", CONTAINER, "bash", "-lc",
            "apt-get update -qq && apt-get install -y -qq python3-psycopg2 "
            "&& mkdir -p /tmp/fixture && chown -R postgres /tmp/fixture"])
        docker("cp", str(BUILD / "pg_fixture.py"), f"{CONTAINER}:/tmp/pg_fixture.py")

        print("driving the transaction history")
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

    heap = (ART / "heap_pages.bin").read_bytes()
    if len(heap) % 8192:
        raise SystemExit("heap_pages.bin is not a whole number of 8192-byte blocks")
    stale = ART / "tx_status.csv"
    if stale.exists():
        stale.unlink()
        print("removed the v2 tx_status.csv; the commit log replaces it")
    for name in SOLVER_DIRS:
        segs = sorted((ART / name).iterdir())
        if not segs:
            raise SystemExit(f"{name}/ came back empty")
        print(f"  {name}/: " + ", ".join(f"{p.name} ({p.stat().st_size} bytes)"
                                         for p in segs))

    print("rebuilding the verifier fixture, solution.sh and the ZIP")
    sh([sys.executable, str(BUILD / "make_expected_state.py")])
    sh([sys.executable, str(BUILD / "make_solution_sh.py")])
    sh([sys.executable, str(BUILD / "make_zip.py")])

    work = BUILD / "_work"
    if work.exists():
        shutil.rmtree(work)
    work.mkdir()

    print(json.dumps(report, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
