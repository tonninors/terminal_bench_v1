# terminal_bench_v1

Terminal Bench 3.0 task packages.

## Tasks

| task | domain / subdomain | difficulty |
| --- | --- | --- |
| [`tasks/sqlite-wal-crash-recovery`](tasks/sqlite-wal-crash-recovery) | systems / databases | hard |

## Layout of a task package

```
tasks/<name>/
  FINAL_PROMPT.txt           prompt shown to the solver
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

```
cd tasks/sqlite-wal-crash-recovery
python3 build/generate_case.py      # regenerate every input file from scratch
bash build/run_all_validation.sh    # generation, oracle, verifier, negatives, ZIP
pytest -q build/harness_test.py     # PASS/FAIL matrix over all candidate answers
```

Python 3.11 standard library plus `pytest`. No network access and no external
data are used at any point.
