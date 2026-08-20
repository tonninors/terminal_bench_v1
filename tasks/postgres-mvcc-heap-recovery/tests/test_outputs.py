"""Deterministic verifier for the `postgres-mvcc-heap-recovery` task.

The ONLY thing inspected is the final artifact `/app/recovered.csv`.  Nothing
here looks at the solver's scripts, shell history, logs, intermediate files,
chosen algorithm or methodology, and there is no timing threshold, no network
access, no fuzzy matching and no model-based judging.  Every check is a pure
function of the bytes of that one file, so repeated runs always agree.

The comparison is outcome-based: rows are canonicalised per column type, keyed
by primary key, and reduced to a single SHA-256 digest.  Any method that arrives
at the correct MVCC-visible table state passes.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
from pathlib import Path

import pytest

RECOVERED = Path(os.environ.get("TB_RECOVERED_CSV", "/app/recovered.csv"))
EXPECTED_PATH = Path(__file__).parent / "expected_state.json"


class _Expected(dict):
    """The verifier fixture, loaded on first use.

    Deferred so that build/make_expected_state.py can import this module's
    canonicaliser while it is still generating expected_state.json."""

    def _ensure(self):
        if not getattr(self, "_loaded", False):
            self.update(json.loads(EXPECTED_PATH.read_text(encoding="utf-8")))
            self._loaded = True

    def __getitem__(self, key):
        self._ensure()
        return dict.__getitem__(self, key)

    def get(self, key, default=None):
        self._ensure()
        return dict.get(self, key, default)


EXPECTED = _Expected()

NULL_TOKEN = "\\N"
DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
INT_RE = re.compile(r"^-?\d+$")
TRUE_TOKENS = {"t", "true"}
FALSE_TOKENS = {"f", "false"}


# --------------------------------------------------------------------------
# canonicalisation - one accepted spelling per logical value
# --------------------------------------------------------------------------
def canon_value(raw: str, coltype: str, column: str, rowid: str) -> str:
    """Map an accepted textual spelling onto its canonical form.

    Only spellings the prompt explicitly allows are accepted; everything else
    raises, which fails the test that called it."""
    if raw == NULL_TOKEN:
        return "\x00NULL"
    kind = coltype.split("(")[0].strip().lower()
    if kind in ("integer", "smallint", "bigint"):
        if not INT_RE.match(raw):
            raise ValueError(f"row {rowid}: column {column!r} value {raw!r} is not "
                             f"a decimal integer")
        return str(int(raw))
    if kind == "boolean":
        low = raw.strip().lower()
        if low in TRUE_TOKENS:
            return "t"
        if low in FALSE_TOKENS:
            return "f"
        raise ValueError(f"row {rowid}: column {column!r} value {raw!r} is not a "
                         f"boolean written as t/f or true/false")
    if kind == "date":
        if not DATE_RE.match(raw):
            raise ValueError(f"row {rowid}: column {column!r} value {raw!r} is not "
                             f"a YYYY-MM-DD date")
        return raw
    return raw          # text / character varying: compared literally


def canon_row(values, columns, coltypes, rowid) -> tuple:
    return tuple(canon_value(v, coltypes[i], columns[i], rowid)
                 for i, v in enumerate(values))


def row_digest(canon_rows_by_key: dict) -> str:
    h = hashlib.sha256()
    for key in sorted(canon_rows_by_key):
        h.update(b"\x01")
        h.update(("\x1f".join(key)).encode("utf-8"))
        h.update(b"\x02")
        for field in canon_rows_by_key[key]:
            h.update(field.encode("utf-8"))
            h.update(b"\x1e")
    return h.hexdigest()


# --------------------------------------------------------------------------
# fixtures
# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def raw_bytes() -> bytes:
    assert RECOVERED.exists(), f"required output {RECOVERED} does not exist"
    assert RECOVERED.is_file(), f"{RECOVERED} is not a regular file"
    data = RECOVERED.read_bytes()
    assert data, f"{RECOVERED} is empty"
    return data


@pytest.fixture(scope="session")
def text(raw_bytes) -> str:
    try:
        return raw_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        pytest.fail(f"{RECOVERED} is not valid UTF-8: {exc}")


@pytest.fixture(scope="session")
def parsed(text):
    """(header, rows) parsed as RFC 4180 CSV."""
    try:
        rows = list(csv.reader(io.StringIO(text, newline="")))
    except csv.Error as exc:
        pytest.fail(f"{RECOVERED} is not parsable as CSV: {exc}")
    rows = [r for r in rows if r != []]
    assert rows, f"{RECOVERED} contains no CSV records"
    return rows[0], rows[1:]


@pytest.fixture(scope="session")
def keyed(parsed):
    """{primary key tuple: canonical row}, plus the raw key list for dup checks."""
    header, rows = parsed
    columns = EXPECTED["columns"]
    coltypes = EXPECTED["column_types"]
    pk_pos = [columns.index(c) for c in EXPECTED["primary_key"]]
    out, keys = {}, []
    for n, row in enumerate(rows, start=2):
        if len(row) != len(columns):
            pytest.fail(f"line {n}: {len(row)} field(s), expected {len(columns)}")
        rowid = "/".join(row[i] for i in pk_pos)
        try:
            canon = canon_row(row, columns, coltypes, rowid)
        except ValueError as exc:
            pytest.fail(str(exc))
        key = tuple(canon[i] for i in pk_pos)
        keys.append(key)
        out[key] = canon
    return out, keys


# --------------------------------------------------------------------------
# A. file, encoding, shape
# --------------------------------------------------------------------------
def test_output_exists_and_is_utf8(raw_bytes):
    raw_bytes.decode("utf-8")


def test_output_is_not_a_binary_blob(raw_bytes):
    assert b"\x00" not in raw_bytes, (
        f"{RECOVERED} contains NUL bytes; it is not a text CSV file")


def test_header_matches_schema_columns_in_order(parsed):
    header, _rows = parsed
    assert header == EXPECTED["columns"], (
        "the header must list the schema columns in their defined order;\n"
        f"  expected: {EXPECTED['columns']}\n  actual:   {header}")


def test_every_record_has_the_right_field_count(parsed):
    _header, rows = parsed
    width = len(EXPECTED["columns"])
    bad = [(n, len(r)) for n, r in enumerate(rows, start=2) if len(r) != width]
    assert not bad, f"records with the wrong number of fields: {bad[:5]}"


# --------------------------------------------------------------------------
# B. primary keys
# --------------------------------------------------------------------------
def test_no_duplicate_primary_keys(keyed):
    _rows, keys = keyed
    seen, dupes = set(), []
    for k in keys:
        if k in seen:
            dupes.append(k)
        seen.add(k)
    assert not dupes, (
        f"{len(dupes)} primary key(s) appear more than once, e.g. {dupes[:5]}; "
        "exactly one row per visible primary key is required")


def test_row_count(keyed):
    rows, _keys = keyed
    assert len(rows) == EXPECTED["row_count"], (
        f"{len(rows)} row(s), expected {EXPECTED['row_count']}")


def test_no_missing_primary_key(keyed):
    rows, _keys = keyed
    missing = sorted(set(map(tuple, EXPECTED["primary_keys"])) - set(rows))
    assert not missing, (
        f"{len(missing)} visible primary key(s) are missing, e.g. {missing[:8]}")


def test_no_unexpected_primary_key(keyed):
    rows, _keys = keyed
    extra = sorted(set(rows) - set(map(tuple, EXPECTED["primary_keys"])))
    assert not extra, (
        f"{len(extra)} primary key(s) are not visible under the supplied "
        f"snapshot, e.g. {extra[:8]}")


# --------------------------------------------------------------------------
# C. NULL contract
# --------------------------------------------------------------------------
def test_null_representation(parsed):
    """SQL NULL is `\\N`; an empty field is the empty string, not NULL."""
    _header, rows = parsed
    columns = EXPECTED["columns"]
    nullable = set(EXPECTED["nullable_columns"])
    offenders = []
    for n, row in enumerate(rows, start=2):
        for i, v in enumerate(row):
            if i < len(columns) and v == "" and columns[i] in nullable:
                offenders.append((n, columns[i]))
    assert not offenders, (
        "empty fields found in nullable columns; SQL NULL must be written as "
        f"the two characters \\N, not as an empty field: {offenders[:5]}")


def test_null_cells_are_exactly_where_expected(keyed):
    rows, _keys = keyed
    columns = EXPECTED["columns"]
    got = sorted(
        [list(k) + [columns[i]] for k, r in rows.items()
         for i, v in enumerate(r) if v == "\x00NULL"])
    want = sorted(EXPECTED["null_cells"])
    assert got == want, (
        f"NULL cells differ; {len(got)} present, {len(want)} expected. "
        f"unexpected={[c for c in got if c not in want][:5]} "
        f"missing={[c for c in want if c not in got][:5]}")


# --------------------------------------------------------------------------
# D. values
# --------------------------------------------------------------------------
def test_row_values_match_the_visible_state(keyed):
    rows, _keys = keyed
    mismatches = []
    expected_rows = {tuple(r["key"]): tuple(r["row"]) for r in EXPECTED["rows"]}
    for key, canon in sorted(rows.items()):
        exp = expected_rows.get(key)
        if exp is None:
            continue                     # reported by test_no_unexpected_primary_key
        if tuple(canon) != exp:
            mismatches.append((key, exp, tuple(canon)))
    assert not mismatches, (
        f"{len(mismatches)} row(s) do not match the visible state. "
        f"First: key={mismatches[0][0]}\n  expected: {mismatches[0][1]}\n"
        f"  actual:   {mismatches[0][2]}")


def test_content_digest(keyed):
    rows, _keys = keyed
    assert row_digest(rows) == EXPECTED["sha256"], (
        "the canonical SHA-256 digest of the recovered rows does not match the "
        "expected MVCC-visible state")


# --------------------------------------------------------------------------
# E. row order must not matter, and the verdict must be stable
# --------------------------------------------------------------------------
def test_row_order_is_not_significant(text):
    """Shuffling the data records must not change the verdict."""
    rows = list(csv.reader(io.StringIO(text, newline="")))
    rows = [r for r in rows if r != []]
    header, data = rows[0], rows[1:]
    columns, coltypes = EXPECTED["columns"], EXPECTED["column_types"]
    pk_pos = [columns.index(c) for c in EXPECTED["primary_key"]]

    def digest(records):
        keyed = {}
        for row in records:
            canon = canon_row(row, columns, coltypes, "?")
            keyed[tuple(canon[i] for i in pk_pos)] = canon
        return row_digest(keyed)

    assert digest(data) == digest(list(reversed(data))) == digest(
        sorted(data, key=lambda r: r[-1])), "row order changed the verdict"


def test_repeated_evaluation_is_stable():
    digests = []
    for _ in range(3):
        data = RECOVERED.read_bytes().decode("utf-8")
        rows = [r for r in csv.reader(io.StringIO(data, newline="")) if r != []]
        columns, coltypes = EXPECTED["columns"], EXPECTED["column_types"]
        pk_pos = [columns.index(c) for c in EXPECTED["primary_key"]]
        keyed = {}
        for row in rows[1:]:
            canon = canon_row(row, columns, coltypes, "?")
            keyed[tuple(canon[i] for i in pk_pos)] = canon
        digests.append(row_digest(keyed))
    assert digests[0] == digests[1] == digests[2], "verification is not reproducible"
