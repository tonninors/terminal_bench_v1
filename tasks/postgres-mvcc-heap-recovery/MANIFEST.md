# Manifest

Task root: `tasks/postgres-mvcc-heap-recovery/`

## Solver-facing (everything the solver may see)

| path | role |
| --- | --- |
| `artifacts/heap_pages.bin` | 9 raw 8192-byte PostgreSQL 16 heap blocks, block 0 first — task input |
| `artifacts/pg_xact/0000` | the cluster's commit log, copied verbatim as a raw SLRU segment — task input |
| `artifacts/pg_subtrans/0000` | the cluster's subtransaction parent map, same form — task input |
| `artifacts/pg_multixact/offsets/0000` | MultiXactId → first member index, verbatim — task input |
| `artifacts/pg_multixact/members/0000` | MultiXact member xids and status flags, verbatim — task input |
| `artifacts/table_schema.json` | columns, types, nullability, primary key, PostgreSQL version, block size and the target snapshot — task input |
| `dist/postgres_mvcc_heap_inputs_v4.zip` | upload bundle; contains **only** those six files, with every SLRU directory keeping its real name |
| `FINAL_PROMPT.txt` | the prompt shown to the solver |
| `FILE_DESCRIPTION.txt` | the Outlier "File Description" text for the bundle |
| `task.yaml` (`instruction:` field) | the same prompt, in the Terminal Bench task definition |
| `Dockerfile`, `docker-compose.yaml` | build the task container; copy every input to `/app/` |

Inside the container the solver sees exactly `/app/heap_pages.bin`,
`/app/table_schema.json`, `/app/pg_xact/`, `/app/pg_subtrans/` and
`/app/pg_multixact/` - what the prompt names, with no duplicate copies and
nothing else from this repository.
There is no decoded transaction table and no decoded MultiXact member list:
v2's `tx_status.csv` stays gone, and every transaction and multi resolves from
the cluster's own SLRU segments. The tests, the oracle, the generator and the
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
| `build/negatives/*.py` | twenty-two intentionally wrong answers, plus format/key-damage variants and two alternate correct constructions |
| `build/measure_negatives.py` | scores every wrong strategy against the correct answer |
| `build/internal/negative_scores.json` | the measured divergence of each wrong strategy |
| `VERSION_HISTORY.md` | what changed between fixture generations v1 through v4, and why |
| `build/internal/golden.csv` | **the reference answer, produced by PostgreSQL itself**; used to build and validate the fixture, never by the oracle |
| `build/internal/generation_report.json` | machine-readable record of the generated case, including every transaction id |
| `build/internal/page_items.json` | `pageinspect` dump of the captured bytes, used to cross-check the parser |
| `build/_work/` | scratch directory, recreated on every generator run |
| `solution/golden_recover.py` | oracle recovery |
| `solution.sh` | oracle entry point (generated) |
| `tests/test_outputs.py` | verifier |
| `tests/expected_state.json` | verifier fixture (expected keys, rows, NULL cells, digest) |
| `run-tests.sh` | verifier entry point |
| `INTERNAL_NOTES.md` | how the case is built, the MultiXact/frozen mechanics, and which tuple cases remain excluded |
| `GOLDEN_SOLUTION.md`, `VERIFIER_SPEC.md`, `DIFFICULTY_EXPLANATION.md`, `MANIFEST.md` | review documentation |

Nothing in the internal set is referenced by `FINAL_PROMPT.txt`, and nothing in
it is reachable from the ZIP.

## ZIP contents (verified programmatically)

Built by `build/make_zip.py` with fixed timestamps, so the archive itself is
byte-reproducible.

```
$ python3 build/make_zip.py
dist/postgres_mvcc_heap_inputs_v4.zip
  sha256 71b20bbaf086367da5fdb48f4a891da59d4185ef86cd97bbd5ccb5d5645907be
  6 member(s), no directory entries:
         73728  heap_pages.bin             sha256 518ecbff305723dbfea0415696b9add17d9faf88fd6879a1cf693fff8b1083e2
          8192  pg_multixact/members/0000  sha256 d563abd3f0d39abd287b3d016d8764e2cbf400b566f159f7b0a16a5894c83a75
          8192  pg_multixact/offsets/0000  sha256 970b43db633a556cb405ec770fbca20452e1b3f2d1faf43f97914643bc9fa540
          8192  pg_subtrans/0000           sha256 d163e332d1fca4518132a56d9d749a9982962e92f9a91ced39f95eeedd5768bd
          8192  pg_xact/0000               sha256 b715a92f3e812b9c62b447a6a63ddd1966a4f8e8d5e65d4e24ec5af7251e9975
          1175  table_schema.json          sha256 34484b19f5f94821d2f142fd253d7fa61cfe8dca3985a6ec7821f6a91399ea94
```

`make_zip.py` asserts on every build that the member list is exactly those six
names, that the only nested paths are `pg_xact/`, `pg_subtrans/`,
`pg_multixact/offsets/` and `pg_multixact/members/`, that no
explicit directory entry exists, and that no member name matches the
internal-artefact patterns (`golden`, `expected`, `solution`, `oracle`, `answer`,
`internal`, `negative`, `test`, `readme`, `hint`).
`build/run_all_validation.sh` step 11 additionally asserts that neither
`golden.csv` nor `expected_state.json` appears anywhere inside the archive's
bytes, and that `table_schema.json` carries no answer-shaped key.

## The solver inputs, in full

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
`["account_id"]`, and the target snapshot `snapshot_xmin` 804, `snapshot_xmax`
842, `snapshot_xip` `[804, 810, 814, 816, 818, 820, 821, 827, 829, 830, 831, 832, 833, 836, 838]`.

`pg_xact/0000` (8192 bytes) is one SLRU segment of the cluster's commit log: two
bits per transaction id. `pg_subtrans/0000` (8192 bytes) is one segment of the
subtransaction map: a four-byte parent transaction id per entry.
`pg_multixact/offsets/0000` and `pg_multixact/members/0000` (8192 bytes each)
resolve the 12 MultiXactIds the pages reference to their member transactions and
per-member lock/update status. Between them the four areas resolve all 87
transactions the pages refer to (65 committed, 14 aborted, 8 still in
progress), including 13 subtransaction xids with ancestry up to 3 hops deep.

`heap_pages.bin` (73728 bytes) is 9 blocks holding 891 line pointers — 855
`LP_NORMAL`, 23 `LP_REDIRECT`, 13 `LP_DEAD` — of which 429 are visible under the
target snapshot. 67 tuples carry a MultiXact xmax (18 locker-only, 49 with an
update member, 23 with a subtransaction updater, 28 decided by the snapshot
alone); 422 tuples are frozen and 411 of those carry an xmax; 57 tuples carry a
single-xid lock-only xmax.

## Dependencies

* Solving and verifying: Python 3.11 standard library only (`csv`, `json`,
  `struct`, `hashlib`, `datetime`), plus `pytest` to run the verifier. The task
  container needs no network.
* Regenerating the fixture (author side only): Docker and the `postgres:16`
  image; `build/pg_fixture.py` uses `python3-psycopg2`, installed inside that
  throwaway container. No third-party package is needed anywhere else, and no
  external or downloaded data is used at any stage.
