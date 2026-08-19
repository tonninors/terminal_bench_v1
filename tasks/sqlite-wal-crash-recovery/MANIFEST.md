# Manifest

Task root: `tasks/sqlite-wal-crash-recovery/`

## Solver-facing (everything the solver may see)

| path | role |
| --- | --- |
| `artifacts/ledger.db` | damaged main database — task input |
| `artifacts/ledger.db-wal` | write-ahead log with a destroyed header and a crash tail — task input |
| `dist/sqlite_wal_recovery_inputs.zip` | upload bundle; contains **only** `ledger.db` and `ledger.db-wal`, no directory nesting |
| `FINAL_PROMPT.txt` | the prompt shown to the solver |
| `task.yaml` (`instruction:` field) | same prompt, in the Terminal Bench task definition |
| `Dockerfile`, `docker-compose.yaml` | build the task container; copy the two artifacts to `/app/` and pristine copies to `/app/evidence/` |

Inside the container the solver sees `/app/ledger.db`, `/app/ledger.db-wal`,
`/app/evidence/` (read-only byte-identical copies, so an accidental open cannot
destroy the evidence) and nothing else from this repository. The tests, the
oracle, the generator and the golden database are copied in only after the agent
has finished, per the Terminal Bench execution model.

## Internal-only — must NOT reach the solver or the ZIP

| path | role |
| --- | --- |
| `build/generate_case.py` | deterministic generator for every input file |
| `build/ledger_schema.py` | synthetic schema and data (seeded, self-created) |
| `build/walkit.py` | WAL structures, checksums, canonical row encoding |
| `build/internal/golden.db` | ground truth; used for validation only, never by the oracle |
| `build/internal/replay_all.db` | the replay-everything state, kept to prove the traps discriminate |
| `build/internal/generation_report.json` | machine-readable record of the generated case |
| `build/negatives/*.py` | intentionally wrong answers, plus three alternate correct constructions |
| `build/harness_test.py` | Phase-7 PASS/FAIL matrix over all candidates |
| `build/make_zip.py` | deterministic builder for the solver ZIP |
| `build/make_solution_sh.py` | regenerates the self-contained `solution.sh` from the oracle |
| `build/run_all_validation.sh` | runs the entire local validation suite |
| `build/_work/` | scratch directory, recreated on every generator run |
| `solution/golden_recover.py` | oracle recovery |
| `solution.sh` | oracle entry point |
| `tests/test_outputs.py` | verifier |
| `tests/expected_state.json` | verifier fixture (expected schema, hashes, assertions) |
| `run-tests.sh` | verifier entry point |
| `INTERNAL_NOTES.md` | how the case is built and why the tail is uncommitted |
| `GOLDEN_SOLUTION.md`, `VERIFIER_SPEC.md`, `DIFFICULTY_EXPLANATION.md`, `MANIFEST.md` | review documentation |

Nothing in the internal set is referenced by `FINAL_PROMPT.txt`, and nothing in
it reveals which WAL frames are committed to anyone who only receives the ZIP.

## ZIP contents (verified programmatically)

Built by `build/make_zip.py` with fixed timestamps, so the archive itself is
byte-reproducible (`sha256 f6feab0a876471ee555e46f689dc5e210380821f2e2c90237ed3620af19e1202`).

```
$ unzip -l dist/sqlite_wal_recovery_inputs.zip
  1323008  ledger.db
  1133032  ledger.db-wal
2 files
```

SHA-256:

```
312afc58cd4a872b4d6aa62d9393c58cdbaa33d8f5620bb459a5edf444562f92  ledger.db
83be7a3da42b7b047e73abfa1641a8f9aa7e0a5d280293ac47e612ed2304ee16  ledger.db-wal
```

## Dependencies

Python 3.11 standard library only (`sqlite3`, `hashlib`, `struct`, `zipfile`,
`random`, `shutil`, `json`), plus `pytest` to run the verifier. No third-party
package, no network access, no downloaded or external data at any stage.
