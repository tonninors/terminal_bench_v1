# terminal_bench_v1

Terminal Bench 3.0 task packages.

## Tasks

| task | domain / subdomain | difficulty |
| --- | --- | --- |
| [`tasks/sqlite-wal-crash-recovery`](tasks/sqlite-wal-crash-recovery) | systems / databases | hard |
| [`tasks/postgres-mvcc-heap-recovery`](tasks/postgres-mvcc-heap-recovery) | systems / databases | hard |

`postgres-mvcc-heap-recovery` is at fixture generation **v2**; see
[`V1_VS_V2_DIFFICULTY.md`](tasks/postgres-mvcc-heap-recovery/V1_VS_V2_DIFFICULTY.md)
for what changed and why. The solver-facing contract is identical in both.

Each task is self-contained; they share no code, no artifacts and no fixtures.

## Layout of a task package

```
tasks/<name>/
  FINAL_PROMPT.txt           prompt shown to the solver
  FILE_DESCRIPTION.txt       description of the solver-facing bundle
  task.yaml                  Terminal Bench task definition
  Dockerfile                 task image (copies the inputs to /app)
  docker-compose.yaml
  run-tests.sh               verifier entry point
  solution.sh                oracle entry point (self-contained)
  artifacts/                 solver-facing input files
  dist/                      solver-facing upload ZIP
  solution/                  oracle implementation
  tests/                     verifier + expected-state fixture
  build/                     generator, negatives, internal ground truth, harness
  GOLDEN_SOLUTION.md VERIFIER_SPEC.md DIFFICULTY_EXPLANATION.md
  MANIFEST.md INTERNAL_NOTES.md
```

`MANIFEST.md` in each task says exactly which files are solver-facing and which
are internal-only.

## Running the local validation

### `postgres-mvcc-heap-recovery`

```
cd tasks/postgres-mvcc-heap-recovery
bash build/run_all_validation.sh          # inputs, oracle, verifier, negatives, ZIP
bash build/run_all_validation.sh --regen  # regenerate from a live PostgreSQL 16 first
bash build/run_container_checks.sh        # oracle / nop against the real task image
pytest -q build/fixture_test.py           # properties of the fixture itself
pytest -q build/harness_test.py           # PASS/FAIL matrix over all candidate answers
python3 build/measure_negatives.py        # how far off each naive strategy lands
python3 build/final_audit.py              # release checklist
```

Solving and verifying need only the Python 3.11 standard library plus `pytest`.
Regenerating the fixture (`--regen`) needs Docker and the `postgres:16` image; it
starts a throwaway container, drives it through the scripted transaction history
and copies the relation file back out.

### `sqlite-wal-crash-recovery`

```
cd tasks/sqlite-wal-crash-recovery
python3 build/generate_case.py      # regenerate every input file from scratch
bash build/run_all_validation.sh    # generation, oracle, verifier, negatives, ZIP
pytest -q build/harness_test.py     # PASS/FAIL matrix over all candidate answers
```

Python 3.11 standard library plus `pytest`. No network access and no external
data are used at any point.
