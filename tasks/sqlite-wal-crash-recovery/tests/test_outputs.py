"""Deterministic verifier for the `sqlite-wal-crash-recovery` task.

The ONLY thing inspected is the final artifact `/app/recovered.db`.  Nothing in
this file looks at the solver's scripts, shell history, logs, intermediate
files, chosen algorithm or methodology.  There is no timing threshold, no
network access, no fuzzy matching and no model-based judging: every check is a
pure function of the bytes of that one file, so repeated runs always agree.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import struct
import tempfile
from pathlib import Path

import pytest

RECOVERED = Path(os.environ.get("TB_RECOVERED_DB", "/app/recovered.db"))
EXPECTED = json.loads((Path(__file__).parent / "expected_state.json").read_text())



# --------------------------------------------------------------------------
# canonical, type-preserving encoding (never str() on REAL or BLOB values)
# --------------------------------------------------------------------------
def encode_value(v) -> bytes:
    if v is None:
        return b"N:"
    if isinstance(v, bool):
        return b"I:" + str(int(v)).encode()
    if isinstance(v, int):
        return b"I:" + str(v).encode()
    if isinstance(v, float):
        return b"F:" + struct.pack(">d", v)
    if isinstance(v, str):
        return b"S:" + v.encode("utf-8")
    if isinstance(v, (bytes, bytearray, memoryview)):
        return b"B:" + bytes(v)
    raise TypeError(f"unsupported SQLite value type: {type(v)!r}")


def encode_row(row) -> bytes:
    parts = []
    for v in row:
        e = encode_value(v)
        parts.append(struct.pack(">I", len(e)) + e)
    return b"".join(parts)


def table_digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    cols = [r[1] for r in sorted(conn.execute(f'PRAGMA table_info("{table}")'),
                                 key=lambda r: r[0])]
    sel = ", ".join(f'"{c}"' for c in cols)
    blobs = [encode_row(r) for r in conn.execute(f'SELECT {sel} FROM "{table}"')]
    blobs.sort()                       # deterministic complete ordering of rows
    h = hashlib.sha256()
    h.update(("|".join(cols)).encode("utf-8"))
    h.update(b"\x00")
    for b in blobs:
        h.update(struct.pack(">I", len(b)))
        h.update(b)
    return len(blobs), h.hexdigest()


def normalize_sql(sql: str) -> str:
    """Only insignificant whitespace is normalised.  Identifier spelling, column
    order, constraints, WHERE clauses of partial indexes and trigger bodies all
    still have to match, because any faithful reconstruction reproduces the
    original CREATE statements verbatim."""
    return " ".join(sql.split())


# --------------------------------------------------------------------------
# fixtures: isolate the artifact from every sidecar before touching it
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def isolated_db(tmp_path_factory) -> Path:
    assert RECOVERED.exists(), f"required output {RECOVERED} does not exist"
    assert RECOVERED.is_file(), f"{RECOVERED} is not a regular file"
    assert RECOVERED.stat().st_size > 0, f"{RECOVERED} is empty"
    clean = tmp_path_factory.mktemp("isolated")
    target = clean / "candidate.sqlite3"          # deliberately a different name
    shutil.copyfile(RECOVERED, target)            # no sidecar is ever copied
    leftovers = [p.name for p in clean.iterdir() if p != target]
    assert not leftovers, f"unexpected files in the isolated directory: {leftovers}"
    return target


@pytest.fixture(scope="session")
def conn(isolated_db) -> sqlite3.Connection:
    c = sqlite3.connect(str(isolated_db))
    c.text_factory = bytes if False else str
    yield c
    c.close()


# --------------------------------------------------------------------------
# A. file / independence
# --------------------------------------------------------------------------
def test_output_present_and_looks_like_sqlite():
    assert RECOVERED.exists(), f"{RECOVERED} is missing"
    head = RECOVERED.open("rb").read(16)
    assert head == b"SQLite format 3\x00", (
        f"{RECOVERED} does not start with the SQLite file header")


def test_opens_independently_of_any_sidecar(isolated_db):
    c = sqlite3.connect(str(isolated_db))
    try:
        names = {r[0] for r in c.execute(
            "SELECT name FROM sqlite_schema WHERE type='table' "
            "AND name NOT LIKE 'sqlite_%'")}
    finally:
        c.close()
    assert names == set(EXPECTED["tables"]), (
        "the database does not expose the expected tables when opened without "
        f"any -wal/-shm sidecar; found {sorted(names)}")


# --------------------------------------------------------------------------
# B. SQLite structural integrity
# --------------------------------------------------------------------------
def test_integrity_check(conn):
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    assert rows == [("ok",)], f"PRAGMA integrity_check returned {rows[:5]}"


def test_foreign_key_check(conn):
    rows = conn.execute("PRAGMA foreign_key_check").fetchall()
    assert rows == [], f"PRAGMA foreign_key_check reported {len(rows)} violation(s): {rows[:5]}"


# --------------------------------------------------------------------------
# C. schema
# --------------------------------------------------------------------------
def _actual_schema(conn) -> dict:
    out: dict[str, dict[str, str]] = {"table": {}, "index": {}, "trigger": {}, "view": {}}
    for typ, name, sql in conn.execute(
            "SELECT type, name, sql FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%'"):
        if sql is None:            # implicit index behind a UNIQUE/PK constraint
            continue
        out.setdefault(typ, {})[name] = normalize_sql(sql)
    return out


@pytest.mark.parametrize("objtype", ["table", "index", "trigger", "view"])
def test_schema_object_set(conn, objtype):
    want = set(EXPECTED["schema"].get(objtype, {}))
    got = set(_actual_schema(conn).get(objtype, {}))
    assert got == want, (
        f"user-defined {objtype} set differs; missing={sorted(want - got)} "
        f"unexpected={sorted(got - want)}")


@pytest.mark.parametrize("objtype", ["table", "index", "trigger", "view"])
def test_schema_object_definitions(conn, objtype):
    want = EXPECTED["schema"].get(objtype, {})
    got = _actual_schema(conn).get(objtype, {})
    for name in sorted(want):
        assert name in got, f"missing {objtype} {name!r}"
        assert got[name] == want[name], (
            f"definition of {objtype} {name!r} differs\n  expected: {want[name]}\n"
            f"  actual:   {got[name]}")


def test_table_structure(conn):
    for table, want in EXPECTED["table_info"].items():
        got = [list(r) for r in conn.execute(f'PRAGMA table_info("{table}")')]
        assert got == want, f"column layout of {table!r} differs:\n{got}\n!=\n{want}"


def test_index_definitions(conn):
    for table, want in EXPECTED["index_list"].items():
        got = sorted([r[1], int(r[2])] for r in conn.execute(f'PRAGMA index_list("{table}")'))
        assert got == want, f"indexes on {table!r} differ:\n{got}\n!=\n{want}"


# --------------------------------------------------------------------------
# D. data
# --------------------------------------------------------------------------
def test_no_unexpected_user_tables(conn):
    got = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'")}
    assert got == set(EXPECTED["tables"]), (
        f"unexpected={sorted(got - set(EXPECTED['tables']))} "
        f"missing={sorted(set(EXPECTED['tables']) - got)}")


@pytest.mark.parametrize("table", sorted(EXPECTED["tables"]))
def test_row_count(conn, table):
    got = conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    assert got == EXPECTED["tables"][table]["row_count"], (
        f"{table}: {got} rows, expected {EXPECTED['tables'][table]['row_count']}")


@pytest.mark.parametrize("table", sorted(EXPECTED["tables"]))
def test_table_content(conn, table):
    n, digest = table_digest(conn, table)
    assert n == EXPECTED["tables"][table]["row_count"], f"{table}: wrong row count"
    assert digest == EXPECTED["tables"][table]["sha256"], (
        f"{table}: content hash {digest} does not match the expected state")


# --------------------------------------------------------------------------
# E. transaction correctness
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "case",
    EXPECTED["assertions"],
    ids=[a["label"].replace(" ", "-") for a in EXPECTED["assertions"]],
)
def test_transaction_boundary(conn, case):
    got = conn.execute(case["sql"], tuple(case["params"])).fetchone()[0]
    assert got == case["expected"], (
        f"{case['label']}: got {got!r}, expected {case['expected']!r}")


# --------------------------------------------------------------------------
# F. determinism
# --------------------------------------------------------------------------
def test_repeated_evaluation_is_stable(isolated_db):
    runs = []
    for _ in range(3):
        c = sqlite3.connect(str(isolated_db))
        try:
            runs.append([table_digest(c, t) for t in sorted(EXPECTED["tables"])])
        finally:
            c.close()
    assert runs[0] == runs[1] == runs[2], "verification is not reproducible"
