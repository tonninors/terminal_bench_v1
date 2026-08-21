# Input-files package report — ministore storage engine

Deliverable: `dist/storage-engine-inputs.zip`, which unpacks to a single
directory `storage-engine/`, so extracting it under `/app` yields
`/app/storage-engine` as the final prompt assumes.

Nothing from the previous PostgreSQL MVCC task is reused: this repository,
its on-disk format, its tests and its documentation were all written from
scratch for this task.

## 1. ZIP contents

    dist/storage-engine-inputs.zip
      sha256 85d27f6e8a190bd43fca1c3c01202e7dbf6e72053e9798554feafba936c8be47
      27577 bytes, 19 members:

            29  storage-engine/.gitignore
           603  storage-engine/Makefile
          3746  storage-engine/README.md
          5486  storage-engine/docs/FORMAT.md
          2375  storage-engine/include/kvstore.h
          8054  storage-engine/src/btree.c
          5930  storage-engine/src/internal.h
         10048  storage-engine/src/kvstore.c
          3918  storage-engine/src/node.c
          6228  storage-engine/src/pager.c
          6115  storage-engine/src/recover.c
          1419  storage-engine/src/util.c
          3619  storage-engine/src/wal.c
          2058  storage-engine/tests/lib.sh
           493  storage-engine/tests/run_all.sh
          2268  storage-engine/tests/test_basic.sh
          3425  storage-engine/tests/test_crash_recovery.sh
          4076  storage-engine/tests/test_idempotence.sh
          8835  storage-engine/tools/kvcli.c

The archive is byte reproducible: `build/make_zip.py` writes members in
sorted order with fixed timestamps and fixed modes, and refuses to
package CRLF files or any path whose name suggests private material.

## 2. What the repository is

`ministore` is a single-writer embedded key/value engine, roughly 1,600
lines of C11 across eight translation units:

* `src/pager.c` — 1 KiB pages, 256-frame LRU cache, lazy write-back, and
  the write-ahead rule (a dirty page is never written before the log
  record describing its newest change is durable);
* `src/wal.c` — append-only log; self-describing, CRC-protected records
  so a torn tail is detectable;
* `src/node.c` — leaf and internal page primitives (search, insert,
  delete, split);
* `src/btree.c` — descent, leaf splits, internal splits, root promotion,
  and the records each of those logs;
* `src/recover.c` — two-pass redo: find committed transactions and the
  end of the readable log, then replay against the data file, skipping
  pages whose stored LSN shows the change already landed;
* `src/kvstore.c` — transactions, checkpoints, scans, and `kv_verify`,
  a structural self-check (page types, key ordering, subtree ranges,
  child reachability with cycle detection, leaf chain versus tree key
  counts);
* `tools/kvcli.c` — `fill`, `update`, `get`, `del`, `verify`, `stats`,
  `dump`, `recover`, `waldump`.

Public API (`include/kvstore.h`) and on-disk format (`docs/FORMAT.md`)
are established and documented in the shipped repository, as required.

Fault injection is part of the engine, as it is in real storage engines:
`MINISTORE_CRASH_POINT` (`commit`, `page_write`, `meta_write`,
`checkpoint`) plus `MINISTORE_CRASH_COUNT` make the process `_exit(90)`
at that point without flushing anything — a power cut, deterministically
placed.

### Language and build commands

    language:  C11 (POSIX file API only, no third-party dependencies)
    build:     make                 -> build/libministore.a, build/kvcli
    test:      make test            -> tests/run_all.sh
    clean:     make clean

Verified warning-free with `gcc -std=c11 -D_POSIX_C_SOURCE=200809L -O2 -g
-Wall -Wextra` (Debian gcc 12.2.0).

## 3. Buggy baseline results (the shipped tree)

Extracted from the ZIP as `/app/storage-engine`, built clean (0 warnings,
0 errors):

    ---- test_basic.sh:           26 passed,  0 failed
    ---- test_crash_recovery.sh:   5 passed,  5 failed
    ---- test_idempotence.sh:     12 passed,  5 failed
    2 test file(s) reported failures

Normal inserts, updates, deletes, checkpoints, reopens and clean
shutdowns all work — the whole basic suite is green, including a
1,200-key three-batch reopen whose contents hash identically across a
clean restart.

The crash suite separates the scenarios required by the brief, and the
separation is what localises the defect:

| replayed window contains | shipped tree |
| --- | --- |
| leaf splits only, on a checkpointed base | **passes** |
| internal-node splits, no promotion (height-3 base) | **passes** |
| a root promotion | **fails** |
| two levels of growth | **fails** |
| crash during page write-back | **fails** |
| mixed workload with deletes | passes |
| uncommitted work must not appear | fails |

Failures are reported as concrete structural damage, e.g.
`tree is not well formed: page 3 reachable twice: the tree contains a
cycle`, and `kvcli waldump` exposes the underlying record. The engine
never hangs on a damaged tree: descent is depth-limited and `kv_verify`
detects cycles.

## 4. Reference-fix results (private, not shipped)

`private/reference_fix.patch` — two hunks, both in `src/btree.c`.
Applied to a tree extracted from the ZIP:

    ---- test_basic.sh:           26 passed, 0 failed
    ---- test_crash_recovery.sh:  10 passed, 0 failed
    ---- test_idempotence.sh:     17 passed, 0 failed
    ALL TESTS PASSED

53/53. Grading is behavioural, so other correct repairs also pass; two
are noted in `private/NOTES.md`.

## 5. Negative-fix results (private, not shipped)

Five plausible incomplete repairs, each built from the shipped tree and
run against the full suite:

| incomplete fix | idea a developer would plausibly try | crash fails | idem fails | basic |
| --- | --- | --- | --- | --- |
| `nf1_reorder_bookkeeping` | reorder the bookkeeping around the promotion | 5 | 5 | 26/26 |
| `nf2_recovery_assumes_first_root` | patch recovery, assuming the tree grew out of page 1 | 2 | 5 | 26/26 |
| `nf3_replay_everything` | fix the record, then drop the page-LSN gate "to be safe" | 0 | 2 | 26/26 |
| `nf4_recovery_keeps_old_root` | fix the record but stop republishing the root in redo | 5 | 5 | 26/26 |
| `nf5_only_first_promotion` | special-case the first promotion (patch the first failing test) | 2 | 5 | 26/26 |

Every one keeps normal operation working and still fails the suite, which
is the profile a good trap needs. `nf3` is the reason the idempotence
file contains an interrupted-replay case that starts from a checkpoint:
an earlier, weaker version of that test let `nf3` through, so the test
was strengthened until double application was actually observable.

## 6. Leak audit

* Searched every shipped file for `bug`, `fixme`, `xxx`, `hack`, `wrong`,
  `broken`, `regression`, `root cause`, `todo`, `workaround`,
  `known issue`, `should be`. The only hits are `old_root` (a local
  variable in the correct in-memory promotion path — legitimate engine
  code, and its presence is precisely what makes the defect subtle) and
  a `"wrong value"` string in a test-failure message.
* No comment, filename, fixture or test names the failing mechanism.
  Crash tests are named after the workload shape
  (`crash_after_ckpt_small`, `crash_from_empty_large`, …), never after a
  cause.
* `README.md` says only that crash recovery is under investigation and
  that some crash/idempotence cases do not pass yet — the same thing the
  test output shows.
* The archive was checked programmatically for any member matching
  `patch|fixed|reference|negative|private|solution|answer|oracle|golden`:
  0 hits, and every member is rooted at `storage-engine/`.
* The private reference fix, the fixed tree and all negatives live in
  `private/`, which `make_zip.py` cannot package.

## 7. Network independence

* No source file references `http`, `curl`, `wget`, sockets or any
  package manager; the sole grep hit is the README sentence stating that
  nothing is downloaded.
* Verified empirically: the archive was extracted, built and the full
  suite run inside a container started with `--network none` (only the
  `lo` interface present). Build succeeded and the suite produced exactly
  the same verdict as the networked run.

## 8. Verification performed

1. built the buggy repository from scratch — clean compile, no warnings;
2. normal tests pass (26/26);
3. deterministic crash tests fail for the intended recovery defect, with
   scenario separation confirming it is confined to root promotion /
   multi-level growth;
4. created the private reference fix;
5. reference fix passes all crash and repeated-recovery/idempotence tests
   (53/53), including recovery interrupted part way and then resumed;
6. five plausible incomplete fixes built and confirmed still failing;
7. leak audit of the solver-facing tree (above);
8. no hidden network dependency (`--network none` run);
9. ZIP confirmed to contain no private solution, patch, or verifier
   answers, and to unpack exactly as `storage-engine/`.

`build/run_validation.sh` performs steps 1–9 unattended in throwaway
containers and asserts each expected verdict.

## 9. Licensing statement

All solver-facing source code, headers, tests, tooling, documentation and
data in `dist/storage-engine-inputs.zip` were written from scratch for
this benchmark. No third-party code, no external dataset, no downloaded
material and no content from any other task is included. The engine
depends only on the C standard library and the POSIX file API, and the
task requires no network access at any point.
