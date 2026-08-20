# Manifest

Task root: `tasks/postgres-mvcc-heap-recovery/`

## Solver-facing (everything the solver may see)

| path | role |
| --- | --- |
| `artifacts/heap_pages.bin` | 6 raw 8192-byte PostgreSQL 16 heap blocks of the `accounts` table, block 0 first — task input |
| `artifacts/ledger_entries_heap.bin` | 1 raw heap block of the `ledger_entries` table — task input |
| `artifacts/account_tags_heap.bin` | 1 raw heap block of the `account_tags` table — task input |
| `artifacts/pg_xact/0000` | the cluster's commit log as it stood on disk after the crash, verbatim; its tail is genuinely stale — task input |
| `artifacts/pg_subtrans/0000` | the cluster's subtransaction parent map, same form — task input |
| `artifacts/table_schema.json` | three relations with columns, types, nullability, PKs, FKs, CHECKs, the output relation, the incident statement, PostgreSQL version, block size and the target snapshot — task input |
| `dist/postgres_mvcc_heap_inputs_v5.zip` | upload bundle; contains **only** those six files, with both SLRU directories keeping their real names |
| `FINAL_PROMPT.txt` | the prompt shown to the solver |
| `FILE_DESCRIPTION.txt` | the Outlier "File Description" text for the bundle |
| `task.yaml` (`instruction:` field) | the same prompt, in the Terminal Bench task definition |
| `Dockerfile`, `docker-compose.yaml` | build the task container; copy every input to `/app/` |

Inside the container the solver sees exactly `/app/heap_pages.bin`,
`/app/ledger_entries_heap.bin`, `/app/account_tags_heap.bin`,
`/app/table_schema.json`, `/app/pg_xact/` and `/app/pg_subtrans/` — what the
prompt names, with no duplicate copies and nothing else from this repository.
There is no decoded transaction table (`tx_status.csv` stays gone since v3) and
no `pg_multixact/` (retired with v5, which removed the MultiXact mechanism).
The tests, the oracle, the generator and the PostgreSQL reference answer are
copied in only after the agent has finished, per the Terminal Bench execution
model.

## Internal-only — must NOT reach the solver or the ZIP

| path | role |
| --- | --- |
| `build/pg_fixture.py` | drives a live PostgreSQL 16 server through the incident history and captures the crash-instant files |
| `build/generate_case.py` | host-side driver: starts `postgres:16` under the incident configuration, runs the fixture builder, copies the results back, re-proves uniqueness, rebuilds everything downstream |
| `build/make_expected_state.py` | derives the verifier fixture from the PostgreSQL reference answer |
| `build/make_solution_sh.py` | regenerates the self-contained `solution.sh` from the oracle |
| `build/make_zip.py` | deterministic builder + leak check for the solver ZIP |
| `build/fixture_test.py` | asserts the fixture's properties (stale clog tail, evidence split, uniqueness funnel, trap strength) |
| `build/harness_test.py` | PASS/FAIL matrix over every candidate answer |
| `build/run_all_validation.sh` | the whole local validation suite |
| `build/run_container_checks.sh` | container-level nop/oracle checks against the real task image |
| `build/final_audit.py` | re-checks every claim the package makes |
| `build/negatives/neg01..neg12*.py` | the twelve intentionally wrong recovery strategies |
| `build/negatives/negative_[ghk]*.py` | key-damage, format and malformed-output candidates |
| `build/negatives/alt_*.py` | two alternate correct constructions (independent implementation; PostgreSQL's own answer reshaped) |
| `build/measure_negatives.py` | scores every wrong strategy against the correct answer |
| `build/internal/negative_scores.json` | the measured divergence of each wrong strategy |
| `build/internal/golden.csv` | **the reference answer, produced by PostgreSQL itself**; used to build and validate the fixture, never by the oracle |
| `build/internal/generation_report.json` | machine-readable record of the generated case, including the designed truth for every transaction |
| `build/internal/inference_report.json` | the oracle's evidence and uniqueness proof over the captured artifacts |
| `build/internal/realism_observations.json` | live-server observations backing REALISM_AUDIT.md |
| `build/_work/` | scratch directory, recreated on every generator run |
| `solution/golden_recover.py` | oracle recovery |
| `solution.sh` | oracle entry point (generated) |
| `tests/test_outputs.py` | verifier |
| `tests/expected_state.json` | verifier fixture (expected keys, rows, NULL cells, digest) |
| `run-tests.sh` | verifier entry point |
| `REALISM_AUDIT.md` | evidence that the stale clog tail is authentic, with live-probe results |
| `INTERNAL_NOTES.md` | how the case is built, the evidence chains, and which tuple cases remain excluded |
| `VERSION_HISTORY.md` | what changed between fixture generations v1 through v5, and why |
| `GOLDEN_SOLUTION.md`, `VERIFIER_SPEC.md`, `DIFFICULTY_EXPLANATION.md`, `MANIFEST.md` | review documentation |

Nothing in the internal set is referenced by `FINAL_PROMPT.txt`, and nothing in
it is reachable from the ZIP.

## ZIP contents (verified programmatically)

Built by `build/make_zip.py` with fixed timestamps, so the archive itself is
byte-reproducible.

```
$ python3 build/make_zip.py
dist/postgres_mvcc_heap_inputs_v5.zip
  sha256 ebda830ea2d3ffa2d9d9881a46c32081bff02f16e9de8e6d7206513353a3e8c2
  6 member(s), no directory entries:
          8192  account_tags_heap.bin  sha256 9c5a66cbef070cdf672f6535aeaf455556a92099ef471b2d42306b1a6a2224b3
         49152  heap_pages.bin         sha256 39ae75a3313bcfa1ce5c54fc5f9675475a11d6f7bdf9cb318ddbf2ca19d23adf
          8192  ledger_entries_heap.bin sha256 86db234659348dede8051c605bd3a99e16d9285c056a850390880b11ac81672e
        139264  pg_subtrans/0000       sha256 822d6cc03760eab371a2985146dc7a2385bcbbcdb4ab0d67d2859888a65483d4
         16384  pg_xact/0000           sha256 cef00fb243144aaeaf0770eae35ec934e5ecd15a612f6629895c89b863da4c9d
          3356  table_schema.json      sha256 4eb70a06edf960e0751329ca48c99d72bc57009f71fca81cc41417ce87911bf1
```

`make_zip.py` asserts on every build that the member list is exactly those six
names, that the only nested paths are `pg_xact/` and `pg_subtrans/`, that no
explicit directory entry exists, and that no member name matches the
internal-artefact patterns (`golden`, `expected`, `solution`, `oracle`,
`answer`, `internal`, `negative`, `test`, `readme`, `hint`, `inference`,
`realism`, `evidence`). `build/run_all_validation.sh` step 11 additionally
asserts that neither `golden.csv` nor `expected_state.json` appears anywhere
inside the archive's bytes, and that `table_schema.json` carries no
answer-shaped key.

## The solver inputs, in full

`table_schema.json` (3356 bytes) describes three relations of the same
database, states the incident, and names `accounts` as the output relation.

`accounts` (`heap_pages.bin`, output):

| # | column | type | nullable |
| --- | --- | --- | --- |
| 1 | `account_id` | `integer` | no (primary key) |
| 2 | `holder_name` | `text` | no |
| 3 | `region` | `character varying(12)` | no |
| 4 | `balance_cents` | `integer` | no, `CHECK (balance_cents > -10000000)` |
| 5 | `is_active` | `boolean` | no |
| 6 | `risk_tier` | `smallint` | yes |
| 7 | `opened_on` | `date` | no |
| 8 | `note` | `text` | yes |

`ledger_entries` (`ledger_entries_heap.bin`): `entry_id` (int, PK),
`account_id` (int, NOT NULL, FK → accounts), `amount_cents` (int,
`CHECK (amount_cents <> 0)`), `entered_on` (date), `memo` (text, nullable).

`account_tags` (`account_tags_heap.bin`): `account_id` (int) + `tag`
(varchar(20)), composite PK, FK → accounts.

The target snapshot is `snapshot_xmin` 32994, `snapshot_xmax` 32998,
`snapshot_xip` `[32994, 32995, 32996]`.

`pg_xact/0000` (16384 bytes, two SLRU pages) is the commit log as it stood on
disk: two bits per transaction id. Its first page resolves the older
transactions the pages reference; its second page is genuinely stale — 15
transactions that the snapshot proves finished have zero (in-progress) bits
there, because their clog page never reached disk before the crash.
`pg_subtrans/0000` (139264 bytes) is the subtransaction map: a four-byte
parent transaction id per entry.

The three heaps hold 321 `LP_NORMAL` tuples (208 accounts + 77 ledger_entries
+ 36 account_tags), 11 `LP_REDIRECT` and 2 `LP_DEAD` line pointers, of which
199 accounts rows are visible under the target snapshot. 38 accounts tuples
carry a lock-only xmax. Genuine hint bits resolve 7 of the 15 unresolved
transactions (five of those seven facts live on `ledger_entries`, not on the
output relation); the remaining 8 are recoverable only through cross-relation
constraint reconciliation, and exhaustive enumeration proves exactly one
outcome assignment — and exactly one output — survives.

## Dependencies

* Solving and verifying: Python 3.11 standard library only (`csv`, `json`,
  `struct`, `hashlib`, `datetime`), plus `pytest` to run the verifier. The task
  container needs no network.
* Regenerating the fixture (author side only): Docker and the `postgres:16`
  image; `build/pg_fixture.py` uses `python3-psycopg2`, installed inside that
  throwaway container. No third-party package is needed anywhere else, and no
  external or downloaded data is used at any stage.
