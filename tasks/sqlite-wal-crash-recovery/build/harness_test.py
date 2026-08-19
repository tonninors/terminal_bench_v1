"""Phase-7 harness: run the verifier against every candidate answer and assert
the expected PASS / FAIL outcome.  Run with:  pytest build/harness_test.py
"""
from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parent.parent
BUILD = TASK / "build"
NEG = BUILD / "negatives"
ART = TASK / "artifacts"
INTERNAL = BUILD / "internal"
ORACLE = TASK / "solution" / "golden_recover.py"
VERIFIER = TASK / "tests" / "test_outputs.py"


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True)


def verify(candidate: Path) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(VERIFIER)],
        capture_output=True, text=True,
        env={**_env(), "TB_RECOVERED_DB": str(candidate)},
    )


def _env() -> dict:
    import os
    e = dict(os.environ)
    e.pop("TB_RECOVERED_DB", None)
    return e


@pytest.fixture(scope="session")
def good(tmp_path_factory) -> Path:
    """A correct recovery produced only from the two solver-visible artifacts."""
    d = tmp_path_factory.mktemp("oracle")
    out = d / "recovered.db"
    r = run([sys.executable, ORACLE, "--db", ART / "ledger.db",
             "--wal", ART / "ledger.db-wal", "--out", out])
    assert r.returncode == 0, r.stderr
    return out


# ---------------------------------------------------------------- positives
def test_solution_sh_is_in_sync_and_passes(tmp_path):
    """solution.sh embeds solution/golden_recover.py; it must be regenerated
    whenever the oracle changes, and it must produce a passing answer."""
    before = (TASK / "solution.sh").read_text()
    r = run([sys.executable, BUILD / "make_solution_sh.py"])
    assert r.returncode == 0, r.stderr
    assert (TASK / "solution.sh").read_text() == before, (
        "solution.sh is stale - rerun build/make_solution_sh.py")
    out = tmp_path / "recovered.db"
    env = {**_env(), "LEDGER_DB": str(ART / "ledger.db"),
           "LEDGER_WAL": str(ART / "ledger.db-wal"), "RECOVERED_DB": str(out)}
    r = subprocess.run(["bash", str(TASK / "solution.sh")], capture_output=True,
                       text=True, env=env)
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode == 0


def test_oracle_passes(good):
    r = verify(good)
    assert r.returncode == 0, r.stdout[-4000:]


def test_alternate_construction_sql_rebuild_passes(good, tmp_path):
    out = tmp_path / "alt.db"
    r = run([sys.executable, NEG / "alt_dump_restore.py", "--src", good, "--out", out])
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() != good.read_bytes(), "alternate build was byte-identical"
    v = verify(out)
    assert v.returncode == 0, v.stdout[-4000:]


def test_alternate_construction_vacuum_into_passes(good, tmp_path):
    out = tmp_path / "alt2.db"
    r = run([sys.executable, NEG / "alt_vacuum_into.py", "--src", good, "--out", out])
    assert r.returncode == 0, r.stderr
    assert out.read_bytes() != good.read_bytes(), "VACUUM INTO was byte-identical"
    v = verify(out)
    assert v.returncode == 0, v.stdout[-4000:]


def test_alternate_construction_repair_header_passes(tmp_path):
    """Repairing the destroyed WAL header and letting SQLite recover is a
    different technique that reaches the same state; it must pass."""
    out = tmp_path / "alt3.db"
    r = run([sys.executable, NEG / "alt_repair_header.py", "--db", ART / "ledger.db",
             "--wal", ART / "ledger.db-wal", "--out", out])
    assert r.returncode == 0, r.stderr
    assert "(0, 201, 201)" in r.stdout, r.stdout
    v = verify(out)
    assert v.returncode == 0, v.stdout[-4000:]


# ---------------------------------------------------------------- negatives
def test_missing_output_fails(tmp_path):
    v = verify(tmp_path / "nothing.db")
    assert v.returncode != 0


def test_empty_output_fails(tmp_path):
    p = tmp_path / "recovered.db"
    p.write_bytes(b"")
    assert verify(p).returncode != 0


def test_copy_of_main_database_fails(tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_copy_main.py",
             "--db", ART / "ledger.db", "--out", out])
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode != 0


def test_ignore_wal_salvage_fails(tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_ignore_wal.py",
             "--db", ART / "ledger.db", "--out", out])
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode != 0


def test_replay_all_frames_fails(tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_replay_all.py",
             "--db", ART / "ledger.db", "--wal", ART / "ledger.db-wal", "--out", out])
    assert r.returncode == 0, r.stderr
    # it must look like a perfectly healthy SQLite database ...
    assert "'ok'" in r.stdout or '"ok"' in r.stdout or "ok" in r.stdout
    import sqlite3
    c = sqlite3.connect(f"file:{out}?mode=ro", uri=True)
    assert [x[0] for x in c.execute("PRAGMA integrity_check")] == ["ok"]
    assert c.execute("PRAGMA foreign_key_check").fetchall() == []
    c.close()
    # ... and still be rejected, because it is one transaction too far
    v = verify(out)
    assert v.returncode != 0
    assert "crash-tail" in v.stdout or "content hash" in v.stdout, v.stdout[-3000:]


def test_salt_only_scan_fails(tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_salt_only_scan.py",
             "--db", ART / "ledger.db", "--wal", ART / "ledger.db-wal", "--out", out])
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode != 0


def test_missing_schema_object_fails(good, tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_drop_schema_object.py",
             "--src", good, "--out", out])
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode != 0


def test_sidecar_dependent_output_fails(good, tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_needs_sidecar.py",
             "--wrong", INTERNAL / "replay_all.db", "--right", good, "--out", out])
    assert r.returncode == 0, r.stderr
    assert Path(str(out) + "-wal").exists(), "fixture did not leave a -wal behind"
    assert verify(out).returncode != 0


def test_malformed_output_fails(tmp_path):
    out = tmp_path / "recovered.db"
    r = run([sys.executable, NEG / "negative_malformed.py", "--out", out])
    assert r.returncode == 0, r.stderr
    assert verify(out).returncode != 0


def test_truncated_output_fails(good, tmp_path):
    out = tmp_path / "recovered.db"
    blob = good.read_bytes()
    out.write_bytes(blob[: len(blob) // 2])
    assert verify(out).returncode != 0


def test_golden_state_with_one_row_edited_fails(good, tmp_path):
    import sqlite3
    out = tmp_path / "recovered.db"
    shutil.copyfile(good, out)
    c = sqlite3.connect(str(out), isolation_level=None)
    c.execute("UPDATE journal_entries SET narrative = narrative || '.' WHERE entry_id=5")
    c.close()
    for side in ("-wal", "-shm"):
        p = Path(str(out) + side)
        if p.exists():
            p.unlink()
    assert verify(out).returncode != 0


# ---------------------------------------------------------------- determinism
def test_verifier_is_deterministic(good):
    outs = [verify(good) for _ in range(3)]
    assert {o.returncode for o in outs} == {0}
    import re
    tails = {re.sub(r" in [0-9.]+s", "", o.stdout.strip().splitlines()[-1]) for o in outs}
    assert len(tails) == 1, tails
