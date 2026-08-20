"""PASS/FAIL harness: run the verifier against every candidate answer and assert
the expected outcome.  Run with:  pytest build/harness_test.py

Positive candidates must pass, negative candidates must fail, and the negatives
must fail for the reason they were written to model - so each one also asserts
which verifier checks broke.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

TASK = Path(__file__).resolve().parent.parent
BUILD = TASK / "build"
NEG = BUILD / "negatives"
ART = TASK / "artifacts"
ORACLE = TASK / "solution" / "golden_recover.py"
VERIFIER = TASK / "tests" / "test_outputs.py"

STD = ["--heap", str(ART / "heap_pages.bin"),
       "--tx", str(ART / "tx_status.csv"),
       "--schema", str(ART / "table_schema.json")]


def _env(extra=None):
    e = dict(os.environ)
    e.pop("TB_RECOVERED_CSV", None)
    if extra:
        e.update(extra)
    return e


def run(cmd):
    r = subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                       env=_env())
    assert r.returncode == 0, f"{cmd[1]} failed:\n{r.stdout}\n{r.stderr}"
    return r


def verify(candidate: Path):
    return subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "--no-header", "-rf", str(VERIFIER)],
        capture_output=True, text=True,
        env=_env({"TB_RECOVERED_CSV": str(candidate)}))


def failed_tests(result) -> set:
    out = set()
    for line in result.stdout.splitlines():
        if line.startswith("FAILED "):
            out.add(line.split("::")[-1].split()[0])
    return out


@pytest.fixture(scope="session")
def oracle_csv(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("oracle") / "recovered.csv"
    run([sys.executable, ORACLE, *STD, "--out", out])
    return out


def make(tmp_path, script, *extra) -> Path:
    out = tmp_path / "candidate.csv"
    run([sys.executable, NEG / script, *extra, "--out", out])
    return out


# ------------------------------------------------------------------ positives
def test_oracle_passes(oracle_csv):
    r = verify(oracle_csv)
    assert r.returncode == 0, r.stdout[-4000:]


def test_solution_sh_matches_the_oracle_and_passes(tmp_path):
    """solution.sh embeds solution/golden_recover.py verbatim; it must be
    regenerated whenever the oracle changes and must produce a passing answer."""
    before = (TASK / "solution.sh").read_text(encoding="utf-8")
    run([sys.executable, BUILD / "make_solution_sh.py"])
    assert (TASK / "solution.sh").read_text(encoding="utf-8") == before, (
        "solution.sh is stale - rerun build/make_solution_sh.py")
    out = tmp_path / "via_sh.csv"
    r = subprocess.run(["bash", str(TASK / "solution.sh")], capture_output=True,
                       text=True, env=_env({
                           "HEAP_PAGES": str(ART / "heap_pages.bin"),
                           "TX_STATUS": str(ART / "tx_status.csv"),
                           "TABLE_SCHEMA": str(ART / "table_schema.json"),
                           "RECOVERED_CSV": str(out)}))
    assert r.returncode == 0, r.stdout + r.stderr
    assert verify(out).returncode == 0


def test_independent_parser_passes(tmp_path, oracle_csv):
    """A second recovery written from scratch, emitting CRLF, true/false and
    descending row order, must also pass."""
    out = make(tmp_path, "alt_independent_parser.py", *STD)
    assert out.read_bytes() != oracle_csv.read_bytes(), (
        "the alternative construction produced a byte-identical file; it would "
        "not be proving that the verifier grades the outcome")
    r = verify(out)
    assert r.returncode == 0, r.stdout[-4000:]


def test_postgres_reference_rendering_passes(tmp_path):
    """PostgreSQL's own answer, rotated and with TRUE/FALSE booleans."""
    out = make(tmp_path, "alt_from_postgres.py")
    r = verify(out)
    assert r.returncode == 0, r.stdout[-4000:]


# ------------------------------------------------------------------ negatives
NEGATIVES = [
    ("A-max-xmin", "negative_a_max_xmin.py", STD,
     {"test_content_digest", "test_row_values_match_the_visible_state"}),
    ("B-ignore-xmax", "negative_b_ignore_xmax.py", STD,
     {"test_no_duplicate_primary_keys", "test_content_digest"}),
    ("C-aborted-visible", "negative_c_aborted_visible.py", STD,
     {"test_content_digest"}),
    ("D-ignore-hot", "negative_d_ignore_hot.py", STD,
     {"test_no_duplicate_primary_keys"}),
    ("E-in-progress-committed", "negative_e_in_progress_committed.py", STD,
     {"test_content_digest"}),
    ("F-all-tuples", "negative_f_all_tuples.py", STD,
     {"test_no_duplicate_primary_keys", "test_content_digest"}),
    ("I-hint-bits-only", "negative_i_hint_bits_only.py", STD,
     {"test_content_digest"}),
    ("J-ignore-xip", "negative_j_ignore_xip.py", STD,
     {"test_content_digest"}),
]


@pytest.mark.parametrize("label,script,extra,expect_failed",
                         NEGATIVES, ids=[n[0] for n in NEGATIVES])
def test_negative_fails(tmp_path, label, script, extra, expect_failed):
    out = make(tmp_path, script, *extra)
    r = verify(out)
    assert r.returncode != 0, f"{label} unexpectedly PASSED the verifier"
    got = failed_tests(r)
    assert expect_failed & got, (
        f"{label} failed, but not on the expected checks. "
        f"expected any of {sorted(expect_failed)}, got {sorted(got)}")


@pytest.mark.parametrize("mode,expect", [
    ("drop", "test_no_missing_primary_key"),
    ("duplicate", "test_no_duplicate_primary_keys"),
])
def test_negative_key_damage_fails(tmp_path, oracle_csv, mode, expect):
    out = make(tmp_path, "negative_g_key_damage.py",
               "--src", str(oracle_csv), "--mode", mode)
    r = verify(out)
    assert r.returncode != 0, f"G/{mode} unexpectedly PASSED"
    assert expect in failed_tests(r), (
        f"G/{mode} did not fail on {expect}: {sorted(failed_tests(r))}")


@pytest.mark.parametrize("mode,expect", [
    ("nulls", "test_null_representation"),
    ("order", "test_header_matches_schema_columns_in_order"),
    ("noheader", "test_header_matches_schema_columns_in_order"),
])
def test_negative_format_fails(tmp_path, oracle_csv, mode, expect):
    out = make(tmp_path, "negative_h_format.py",
               "--src", str(oracle_csv), "--mode", mode)
    r = verify(out)
    assert r.returncode != 0, f"H/{mode} unexpectedly PASSED"
    assert expect in failed_tests(r), (
        f"H/{mode} did not fail on {expect}: {sorted(failed_tests(r))}")


@pytest.mark.parametrize("mode", ["json", "binary", "empty"])
def test_negative_malformed_fails(tmp_path, mode):
    out = make(tmp_path, "negative_k_malformed.py", "--mode", mode)
    assert verify(out).returncode != 0, f"K/{mode} unexpectedly PASSED"


def test_missing_output_fails(tmp_path):
    assert verify(tmp_path / "not_created.csv").returncode != 0, (
        "a missing output file unexpectedly PASSED")


def test_directory_instead_of_file_fails(tmp_path):
    d = tmp_path / "recovered.csv"
    d.mkdir()
    assert verify(d).returncode != 0, "a directory unexpectedly PASSED"


# ------------------------------------------------------------------ stability
def test_verifier_is_deterministic(oracle_csv):
    codes = [verify(oracle_csv).returncode for _ in range(3)]
    assert codes == [0, 0, 0], f"verifier verdicts varied across runs: {codes}"


def test_oracle_is_deterministic(tmp_path):
    outs = []
    for i in range(3):
        p = tmp_path / f"run{i}.csv"
        run([sys.executable, ORACLE, *STD, "--out", p])
        outs.append(p.read_bytes())
    assert outs[0] == outs[1] == outs[2], "the oracle is not deterministic"
