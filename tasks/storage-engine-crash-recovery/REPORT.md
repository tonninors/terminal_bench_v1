# Status: REDESIGN REJECTED — NOTHING TO UPLOAD

`dist/` is intentionally empty. Neither the original fixture (commit
a5dd20f) nor this redesign may be shipped.

* The original defect was rejected by review as too locally obvious
  (a one-line stale read that `docs/FORMAT.md` effectively pointed at).
* The redesign built in response was rejected by its own clean
  fresh-context attack: an independent agent, given only the task
  statement, the extracted repository and a terminal, produced a
  complete and genuinely correct fix in **~34 minutes**. The brief's stop
  condition is "complete correct fix in < 1 hour → reject the design and
  do not package it."

Everything below records what was built and measured, so the next attempt
starts from evidence rather than from scratch.

---

## 1. What the redesign changed

The goal was a crash-consistency regression with no single line
contradicting the format document, whose root cause spans several
components, and that cannot be closed from `btree.c` alone or
`recover.c` alone.

| area | change |
| --- | --- |
| `src/btree.c` | split records became **physiological** — they name `page`, `aux` and the split position, and no longer carry the new sibling as a page image |
| `src/internal.h` | log payload capped at `KV_MAX_VALUE_LEN`, so a record cannot carry a page |
| `src/recover.c` | split redo reconstructs the new sibling from the page it was split from, gating each half on its own page LSN |
| `src/pager.c` | buffer pool reduced to 32 frames, so any tree larger than the pool continuously evicts and writes frames back in eviction order |
| `src/kvstore.c` | checkpoints **recycle the log** (truncate after the pages and the meta page are durable), so replaying from the beginning of history is not available |
| `tools/kvcli.c` | `--spread` insertion order and an `expect <db> <total> <committed>` check that asserts the exact committed key set |
| `docs/FORMAT.md`, `README.md` | updated to describe the physiological split records, the log recycling and the small pool — the documentation matches the implementation exactly |

Intended defect: when a crash leaves the page that was split already
written back post-split while its new sibling is still dirty, redo
rebuilds the sibling out of an already-truncated page. The moved keys
are gone, and because the log was recycled at the last checkpoint they
cannot be replayed from history.

While building it I also found and fixed a genuine engine bug that
predated the redesign: after recovery the WAL object's `next_lsn` was
never advanced past the log it had just replayed, so a later run reissued
sequence numbers that pages already carried and redo silently skipped
records. That is now `wal_set_next_lsn()`, called from `recover_redo()`.

## 2. Measured behaviour of the redesigned fixture

| build | basic | crash | idempotence |
| --- | --- | --- | --- |
| shipped tree (defective) | 28 pass / 0 fail | 2 pass / **6 fail** | 10 pass / **4 fail** |
| private reference fix | 28 / 0 | 8 / 0 | 14 / 0 |

Scenario separation worked as designed: databases small enough to sit in
the buffer pool recovered correctly (in-order and scattered), while
larger multi-level databases failed, with concrete structural damage
(`key N on leaf page M is outside its subtree range`, `leaf chain holds
X keys but the tree holds Y`).

Reference fix: raise the payload cap (`internal.h`), log the new sibling
as a formatted image (`btree.c`), restore it in redo (`recover.c`) —
three files, which is what made single-file repairs insufficient.

Five plausible incomplete repairs were built and measured; every one kept
the basic suite green and still failed the suite:

| incomplete fix | crash fails | idempotence fails |
| --- | --- | --- |
| `nf_btree_relog` (btree.c only: re-log the moved pairs) | 6 | 4 |
| `nf_recover_atomic_gate` (recover.c only: replay a split only when both halves are behind) | 6 | 4 |
| `nf_recover_force_replay` (recover.c only: distrust page LSNs) | 6 | 4 |
| `nf_recover_protect_left` (recover.c only: never mutate the split page) | 6 | 4 |
| `nf_payload_without_redo` (internal.h + btree.c, redo not taught) | 6 | 4 |

## 3. The fresh-context attack — why this is rejected

Setup: a container holding only the extracted `/app/storage-engine`. The
agent received the task statement, the repository and terminal access.
It did not see the reference patch, the negatives, the generators, this
report, or any design note.

| measurement | result |
| --- | --- |
| wall clock | **~34 minutes** (2,019,597 ms) |
| tool calls | 42 |
| files inspected | all 10 sources, both docs, all test files, Makefile |
| hypotheses considered | 6 (five wrong, then the right one, found by instrumenting one redo branch) |
| edit/build/test iterations | 4, plus 3 full suite runs and 2 stress sweeps |
| final result | **ALL TESTS PASSED** — 28/0, 8/0, 14/0 |

Independently re-verified: its `solution.patch` applied to a pristine
extraction of the archive builds warning-free and passes all 50 cases.
The patch is kept at `private/attack_evidence/fresh_agent_solution.patch`.

Two things are worth carrying forward honestly:

1. **The agent found a different primary cause than the one I planted.**
   `pager_ensure()` zeroed any page whose id was beyond the *checkpointed*
   `num_pages`, even though the data file legitimately held newer
   evicted images for it. That was an unintended defect of mine, and it
   both inflated the failure counts and gave a very fast empirical
   signal: one `fprintf` in the redo split branch printed a page
   reporting LSN 0 that obviously had to exist on disk, which pointed
   straight at it.
2. **Even so, the intended hazard did not survive.** After fixing that,
   the agent observed the split-redo branch still firing, understood the
   ordering hazard, and closed it by forcing the new sibling to disk
   before the page it was split from can be stolen — a different but
   genuinely correct repair, reached without seeing the reference.

So the redesign fails on its merits and not only because of my
accidental bug, though that bug plainly accelerated the discovery.

## 4. What this suggests for the next attempt

* A defect whose symptom is *structural corruption visible to
  `kv_verify`* gives the solver a fast, precise oracle. Every iteration
  here was cheap because a single command printed the exact damaged page
  and the reason. A defect whose symptom is subtler — correct-looking
  data that only a cross-check can falsify — would remove that
  accelerant.
* Instrumenting one branch was enough to localise the cause. Any design
  where a single `printf` in the obvious suspect function points at the
  answer is unlikely to clear an hour.
* The fixture must contain no unintended defects: the accidental
  `pager_ensure()` bug was found before the intended one and did much of
  the work. A pre-attack audit pass that hunts for *additional* bugs is
  now mandatory, not optional.
* Multi-file repairs did block single-file fixes as intended, so that
  part of the design brief is achievable — it just is not sufficient on
  its own to buy an hour.

## 5. Reproducing any of this

    python3 build/make_reference.py     # rebuild private/fixed + the patch
    python3 build/make_negatives.py     # rebuild the incomplete repairs
    python3 build/make_zip.py           # would rebuild the archive
    bash    build/run_validation.sh     # baseline / reference / negatives matrix

`make_zip.py` is left in place but no archive is committed, so nothing
can be uploaded by accident.

## 6. Licensing

Everything in this task directory was written from scratch for this
benchmark. No third-party code, no external dataset, nothing downloaded,
and nothing reused from any other task. The engine depends only on the C
standard library and the POSIX file API and needs no network at any
point.
