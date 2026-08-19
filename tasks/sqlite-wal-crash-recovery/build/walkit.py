"""Low-level SQLite WAL helpers shared by the generator, negatives and test harness.

This module is INTERNAL tooling.  The solver never sees it, and the oracle in
solution/golden_recover.py deliberately re-implements the parts it needs so that
the oracle is a standalone, self-contained recovery program.
"""
from __future__ import annotations

import hashlib
import sqlite3
import struct
from dataclasses import dataclass

WAL_HDR_SIZE = 32
WAL_FRAME_HDR_SIZE = 24
WAL_MAGIC_LE_CKSUM = 0x377F0682  # checksum words read little-endian
WAL_MAGIC_BE_CKSUM = 0x377F0683  # checksum words read big-endian
WAL_FORMAT_VERSION = 3007000


def wal_checksum(data: bytes, s0: int, s1: int, big_endian: bool) -> tuple[int, int]:
    """SQLite's rolling WAL checksum over `data` (length must be a multiple of 8)."""
    if len(data) % 8:
        raise ValueError("WAL checksum input must be a multiple of 8 bytes")
    fmt = ">II" if big_endian else "<II"
    unpack_from = struct.unpack_from
    for off in range(0, len(data), 8):
        x0, x1 = unpack_from(fmt, data, off)
        s0 = (s0 + x0 + s1) & 0xFFFFFFFF
        s1 = (s1 + x1 + s0) & 0xFFFFFFFF
    return s0, s1


@dataclass
class Frame:
    pgno: int
    db_size: int          # non-zero only on a commit frame
    salt1: int
    salt2: int
    cksum1: int
    cksum2: int
    data: bytes

    @property
    def is_commit(self) -> bool:
        return self.db_size != 0


@dataclass
class WalHeader:
    magic: int
    version: int
    page_size: int
    ckpt_seq: int
    salt1: int
    salt2: int
    cksum1: int
    cksum2: int

    @property
    def big_endian_cksum(self) -> bool:
        return bool(self.magic & 1)

    def pack(self) -> bytes:
        head = struct.pack(
            ">IIIIII",
            self.magic, self.version, self.page_size,
            self.ckpt_seq, self.salt1, self.salt2,
        )
        return head + struct.pack(">II", self.cksum1, self.cksum2)

    def recompute_checksum(self) -> None:
        head = struct.pack(
            ">IIIIII",
            self.magic, self.version, self.page_size,
            self.ckpt_seq, self.salt1, self.salt2,
        )
        self.cksum1, self.cksum2 = wal_checksum(head, 0, 0, self.big_endian_cksum)


def parse_wal_header(blob: bytes) -> WalHeader:
    magic, version, page_size, ckpt_seq, salt1, salt2, c1, c2 = struct.unpack_from(
        ">IIIIIIII", blob, 0
    )
    return WalHeader(magic, version, page_size, ckpt_seq, salt1, salt2, c1, c2)


def parse_wal(blob: bytes) -> tuple[WalHeader, list[Frame]]:
    """Parse a *well formed* WAL produced by SQLite (used on pristine artifacts only)."""
    hdr = parse_wal_header(blob)
    frame_size = WAL_FRAME_HDR_SIZE + hdr.page_size
    frames: list[Frame] = []
    off = WAL_HDR_SIZE
    while off + frame_size <= len(blob):
        pgno, db_size, s1, s2, c1, c2 = struct.unpack_from(">IIIIII", blob, off)
        data = blob[off + WAL_FRAME_HDR_SIZE: off + frame_size]
        frames.append(Frame(pgno, db_size, s1, s2, c1, c2, data))
        off += frame_size
    return hdr, frames


def verify_chain(hdr: WalHeader, frames: list[Frame]) -> int:
    """Return the number of leading frames that form a valid salt+checksum chain."""
    s0, s1 = hdr.cksum1, hdr.cksum2
    be = hdr.big_endian_cksum
    good = 0
    for fr in frames:
        if fr.salt1 != hdr.salt1 or fr.salt2 != hdr.salt2:
            break
        head8 = struct.pack(">II", fr.pgno, fr.db_size)
        n0, n1 = wal_checksum(head8, s0, s1, be)
        n0, n1 = wal_checksum(fr.data, n0, n1, be)
        if (n0, n1) != (fr.cksum1, fr.cksum2):
            break
        s0, s1 = n0, n1
        good += 1
    return good


def build_wal(hdr: WalHeader, frames: list[Frame]) -> bytes:
    """Serialise a WAL, stamping `hdr`'s salts onto every frame and re-chaining
    every checksum from the header seed.  This is what makes the generated
    artifacts byte-for-byte deterministic despite SQLite's random salts."""
    hdr.recompute_checksum()
    out = bytearray(hdr.pack())
    s0, s1 = hdr.cksum1, hdr.cksum2
    be = hdr.big_endian_cksum
    for fr in frames:
        fr.salt1, fr.salt2 = hdr.salt1, hdr.salt2
        head8 = struct.pack(">II", fr.pgno, fr.db_size)
        s0, s1 = wal_checksum(head8, s0, s1, be)
        s0, s1 = wal_checksum(fr.data, s0, s1, be)
        fr.cksum1, fr.cksum2 = s0, s1
        out += struct.pack(">IIIIII", fr.pgno, fr.db_size, fr.salt1, fr.salt2, s0, s1)
        out += fr.data
    return bytes(out)


def chain_foreign_frames(frames: list[Frame], salt1: int, salt2: int,
                         seed0: int, seed2: int, big_endian: bool) -> bytes:
    """Serialise frames belonging to a *different* WAL generation (stale tail)."""
    out = bytearray()
    s0, s1 = seed0, seed2
    for fr in frames:
        head8 = struct.pack(">II", fr.pgno, fr.db_size)
        s0, s1 = wal_checksum(head8, s0, s1, big_endian)
        s0, s1 = wal_checksum(fr.data, s0, s1, big_endian)
        out += struct.pack(">IIIIII", fr.pgno, fr.db_size, salt1, salt2, s0, s1)
        out += fr.data
    return bytes(out)


# --------------------------------------------------------------------------
# Canonical, type-preserving serialisation of SQLite table content.
# --------------------------------------------------------------------------

def encode_value(v) -> bytes:
    if v is None:
        return b"N:"
    if isinstance(v, bool):                       # pragma: no cover - defensive
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


def user_tables(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        "SELECT name FROM sqlite_schema WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def table_columns(conn: sqlite3.Connection, table: str) -> list[str]:
    rows = conn.execute(f'PRAGMA table_info("{table}")').fetchall()
    return [r[1] for r in sorted(rows, key=lambda r: r[0])]


def table_digest(conn: sqlite3.Connection, table: str) -> tuple[int, str]:
    """(row_count, sha256) over a canonical, order-independent encoding."""
    cols = table_columns(conn, table)
    sel = ", ".join(f'"{c}"' for c in cols)
    blobs = [encode_row(r) for r in conn.execute(f'SELECT {sel} FROM "{table}"')]
    blobs.sort()
    h = hashlib.sha256()
    h.update(("|".join(cols)).encode("utf-8"))
    h.update(b"\x00")
    for b in blobs:
        h.update(struct.pack(">I", len(b)))
        h.update(b)
    return len(blobs), h.hexdigest()


def normalize_sql(sql: str) -> str:
    """Collapse insignificant whitespace in a schema statement."""
    return " ".join(sql.split())


def schema_snapshot(conn: sqlite3.Connection) -> dict:
    out: dict[str, dict[str, str]] = {"table": {}, "index": {}, "trigger": {}, "view": {}}
    rows = conn.execute(
        "SELECT type, name, sql FROM sqlite_schema "
        "WHERE name NOT LIKE 'sqlite_%' ORDER BY type, name"
    ).fetchall()
    for typ, name, sql in rows:
        if sql is None:
            # auto-index created by a UNIQUE/PK constraint; represented by its table
            continue
        out.setdefault(typ, {})[name] = normalize_sql(sql)
    return out
