# Manifest

Task root: `tasks/postgres-mvcc-heap-recovery/`

## Solver-facing (everything the solver may see)

| path | role |
| --- | --- |
| `artifacts/heap_pages.bin` | 8 raw 8192-byte PostgreSQL 16 heap blocks, block 0 first — task input |
| `artifacts/pg_xact/0000` | the cluster's commit log, copied verbatim as a raw SLRU segment — task input |
| `artifacts/pg_subtrans/0000` | the cluster's subtransaction parent map, same form — task input |
| `artifacts/table_schema.json` | columns, types, nullability, primary key, PostgreSQL version, block size and the target snapshot — task input |
| `dist/postgres_mvcc_heap_inputs_v3.zip` | upload bundle; contains **only** those four files, with `pg_xact/` and `pg_subtrans/` keeping their real directory names |
| `FINAL_PROMPT.txt` | the prompt shown to the solver |
| `FILE_DESCRIPTION.txt` | the Outlier "File Description" text for the bundle |
| `task.yaml` (`instruction:` field) | the same prompt, in the Terminal Bench task definition |
| `Dockerfile`, `docker-compose.yaml` | build the task container; copy the three inputs to `/app/` |

Inside the container the solver sees exactly `/app/heap_pages.bin`,
`/app/table_schema.json`, `/app/pg_xact/` and `/app/pg_subtrans/` - what the
prompt names, with no duplicate copies and nothing else from this repository.
There is no decoded transaction table: v2 shipped `tx_status.csv`, and v3
replaces it with the cluster's own SLRU segments. The tests, the oracle, the generator and the
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
| `build/negatives/*.py` | twelve intentionally wrong answers, plus format/key-damage variants and two alternate correct constructions |
| `build/measure_negatives.py` | scores every wrong strategy against the correct answer |
| `build/internal/negative_scores.json` | the measured divergence of each wrong strategy |
| `VERSION_HISTORY.md` | what changed between fixture generations v1, v2 and v3, and why |
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
dist/postgres_mvcc_heap_inputs_v3.zip
  sha256 678f0f9561082f332b6e28dc48b68fb84c1fe0af18a333362d84c14b8e59e912
  4 member(s), no directory entries:
         65536  heap_pages.bin         sha256 91a3205b8b5562542e67fa903ff3ab0739a73e011a05cc4fe408b628ca93413a
          8192  pg_subtrans/0000       sha256 e9a3e47b122548e8f4c27d75cf05c9d202a7a33c6bf6bf72879970e6d860057c
          8192  pg_xact/0000           sha256 60ec438e8d52508f46daa3db9655fdd3364ccf93a8dfc01a5eadda40d45bbfeb
          1139  table_schema.json      sha256 ccd502b031201ecec2a4a2e0cbad924d2563bcd41cdf196ccd6a48cfb98ce3dc
```

`make_zip.py` asserts on every build that the member list is exactly those four
names, that the only nested paths are `pg_xact/` and `pg_subtrans/`, that no
explicit directory entry exists, and that no member name matches the
internal-artefact patterns (`golden`, `expected`, `solution`, `oracle`, `answer`,
`internal`, `negative`, `test`, `readme`, `hint`).
`build/run_all_validation.sh` step 11 additionally asserts that neither
`golden.csv` nor `expected_state.json` appears anywhere inside the archive's
bytes, and that `table_schema.json` carries no answer-shaped key.

## The three inputs, in full

`table_schema.json` (1139 bytes) describes `public.account_ledger`:

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
`["account_id"]`, and the target snapshot `snapshot_xmin` 789, `snapshot_xmax`
811, `snapshot_xip` `[789, 791, 793, 795, 796, 802, 804, 805, 806, 807, 808]`.

`pg_xact/0000` (8192 bytes) is one SLRU segment of the cluster's commit log: two
bits per transaction id, covering xids 0-32767. `pg_subtrans/0000` (8192 bytes)
is one segment of the subtransaction map: a four-byte parent transaction id per
entry, zero for a top-level transaction, covering xids 0-2047. Between them they
resolve all 61 transactions the pages refer to (43 committed, 12 aborted, 6
still in progress), including 10 subtransaction xids whose ancestry is up to 3
hops deep.

`heap_pages.bin` (65536 bytes) is 8 blocks holding 753 line pointers — 725
`LP_NORMAL`, 15 `LP_REDIRECT`, 13 `LP_DEAD` — over 393 distinct primary keys, of
which 353 are visible under the target snapshot. 53 of the surviving tuples carry
a lock-only `xmax`, and 24 tuple stamps cannot be resolved without
`pg_subtrans`.

## Dependencies

* Solving and verifying: Python 3.11 standard library only (`csv`, `json`,
  `struct`, `hashlib`, `datetime`), plus `pytest` to run the verifier. The task
  container needs no network.
* Regenerating the fixture (author side only): Docker and the `postgres:16`
  image; `build/pg_fixture.py` uses `python3-psycopg2`, installed inside that
  throwaway container. No third-party package is needed anywhere else, and no
  external or downloaded data is used at any stage.
