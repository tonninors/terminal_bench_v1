# Manifest

Task root: `tasks/postgres-mvcc-heap-recovery/`

## Solver-facing (everything the solver may see)

| path | role |
| --- | --- |
| `artifacts/heap_pages.bin` | 4 raw 8192-byte PostgreSQL 16 heap blocks, block 0 first — task input |
| `artifacts/tx_status.csv` | `xid,status` for every transaction id on those pages — task input |
| `artifacts/table_schema.json` | columns, types, nullability, primary key, PostgreSQL version, block size and the target snapshot — task input |
| `dist/postgres_mvcc_heap_inputs.zip` | upload bundle; contains **only** those three files, at the archive root, no directory nesting |
| `FINAL_PROMPT.txt` | the prompt shown to the solver |
| `FILE_DESCRIPTION.txt` | the Outlier "File Description" text for the bundle |
| `task.yaml` (`instruction:` field) | the same prompt, in the Terminal Bench task definition |
| `Dockerfile`, `docker-compose.yaml` | build the task container; copy the three inputs to `/app/` and read-only copies to `/app/evidence/` |

Inside the container the solver sees `/app/heap_pages.bin`,
`/app/tx_status.csv`, `/app/table_schema.json`, `/app/evidence/` (read-only,
byte-identical copies, so a partial write cannot destroy the evidence) and
nothing else from this repository. The tests, the oracle, the generator and the
PostgreSQL reference answer are copied in only after the agent has finished, per
the Terminal Bench execution model.

## Internal-only — must NOT reach the solver or the ZIP

| path | role |
| --- | --- |
| `build/pg_fixture.py` | drives a live PostgreSQL 16 server through the transaction history and captures the relation file |
| `build/generate_case.py` | host-side driver: starts `postgres:16`, runs the fixture builder, copies the results back, rebuilds everything downstream |
| `build/make_expected_state.py` | derives the verifier fixture from the PostgreSQL reference answer |
| `build/make_solution_sh.py` | regenerates the self-contained `solution.sh` from the oracle |
| `build/make_zip.py` | deterministic builder + leak check for the solver ZIP |
| `build/fixture_test.py` | asserts the fixture's properties (exclusions, HOT artefacts, snapshot boundary, trap strength) |
| `build/harness_test.py` | PASS/FAIL matrix over every candidate answer |
| `build/run_all_validation.sh` | the whole local validation suite |
| `build/negatives/*.py` | intentionally wrong answers, plus two alternate correct constructions |
| `build/internal/golden.csv` | **the reference answer, produced by PostgreSQL itself**; used to build and validate the fixture, never by the oracle |
| `build/internal/generation_report.json` | machine-readable record of the generated case, including every transaction id |
| `build/internal/page_items.json` | `pageinspect` dump of the captured bytes, used to cross-check the parser |
| `build/_work/` | scratch directory, recreated on every generator run |
| `solution/golden_recover.py` | oracle recovery |
| `solution.sh` | oracle entry point (generated) |
| `tests/test_outputs.py` | verifier |
| `tests/expected_state.json` | verifier fixture (expected keys, rows, NULL cells, digest) |
| `run-tests.sh` | verifier entry point |
| `INTERNAL_NOTES.md` | how the case is built, and which tuple cases are deliberately excluded |
| `GOLDEN_SOLUTION.md`, `VERIFIER_SPEC.md`, `DIFFICULTY_EXPLANATION.md`, `MANIFEST.md` | review documentation |

Nothing in the internal set is referenced by `FINAL_PROMPT.txt`, and nothing in
it is reachable from the ZIP.

## ZIP contents (verified programmatically)

Built by `build/make_zip.py` with fixed timestamps, so the archive itself is
byte-reproducible.

```
$ python3 build/make_zip.py
dist/postgres_mvcc_heap_inputs.zip
  sha256 149c6e7b21dabb8a7bf89e8e5f4f4b411edcb9261b5da228799cf60d60c8a1c6
  3 member(s), no directory entries:
         32768  heap_pages.bin       sha256 469c05309cd49962e8b8fc17ef32a359eb7e740a75c76df843d5c10d8298955e
          1067  table_schema.json    sha256 ada6f1dbbf9d16dd3213196f357ae9b38c00b5338e3a9862f191cc2847c77c07
           271  tx_status.csv        sha256 0ac8f5605debbe68ff432ab2be4e7d7a408225f14d15c783f4c0e046b798e65d
```

`make_zip.py` asserts on every build that the member list is exactly those three
names, that no member is nested in a folder, and that no member name matches the
internal-artefact patterns (`golden`, `expected`, `solution`, `oracle`, `answer`,
`internal`, `negative`, `test`, `readme`, `hint`).
`build/run_all_validation.sh` step 11 additionally asserts that neither
`golden.csv` nor `expected_state.json` appears anywhere inside the archive's
bytes, and that `table_schema.json` carries no answer-shaped key.

## The three inputs, in full

`table_schema.json` (1067 bytes) describes `public.account_ledger`:

| # | column | type | nullable |
| --- | --- | --- | --- |
| 1 | `account_id` | `integer` | no (primary key) |
| 2 | `region_code` | `character varying(12)` | no |
| 3 | `balance_cents` | `integer` | no |
| 4 | `is_active` | `boolean` | no |
| 5 | `risk_tier` | `smallint` | yes |
| 6 | `opened_on` | `date` | no |
| 7 | `owner_note` | `text` | yes |

plus `postgres_version` `16.15`, `block_size` 8192, `primary_key`
`["account_id"]`, and the target snapshot `snapshot_xmin` 770, `snapshot_xmax`
774, `snapshot_xip` `[770, 771, 772]`.

`tx_status.csv` (271 bytes) holds 19 transactions: 12 `committed`, 5 `aborted`,
2 `in_progress`.

`heap_pages.bin` (32768 bytes) is 4 blocks holding 394 line pointers — 323
`LP_NORMAL`, 37 `LP_REDIRECT`, 34 `LP_DEAD` — over 236 distinct primary keys, of
which 210 are visible under the target snapshot.

## Dependencies

* Solving and verifying: Python 3.11 standard library only (`csv`, `json`,
  `struct`, `hashlib`, `datetime`), plus `pytest` to run the verifier. The task
  container needs no network.
* Regenerating the fixture (author side only): Docker and the `postgres:16`
  image; `build/pg_fixture.py` uses `python3-psycopg2`, installed inside that
  throwaway container. No third-party package is needed anywhere else, and no
  external or downloaded data is used at any stage.
