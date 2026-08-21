# Status: REJECTED — nothing to upload

`dist/` is empty by design. The fifth fixture (explicit multi-operation
transactions) was built in full, validated end to end, and then failed
its own acceptance gates on two independent grounds. Per the brief, this
fixture is not hardened again.

`repo/` is left as a **working engine with the planted defect still in
it**, together with the reference repair, the negatives, the private
verifier and the attack evidence, so the measurements below can be
reproduced.

---

## 1. What was built

An embedded key/value storage engine (C11 + POSIX file API, ~1700 LOC)
with an explicit multi-operation transaction API:

```c
kv_begin(s); kv_put(...); kv_delete(...); kv_commit(s);   /* or kv_abort(s) */
```

Two budgets are fixed and documented, and neither scales with the size of
a transaction:

* the buffer pool is **32 frames** (`src/pager.c`, stated in `README.md`);
* the log is checkpointed once it passes **`MS_LOG_LIMIT` (32 KiB)**, and
  the check runs after every operation, not only at commit.

Measured on the shipped tree: a transaction of 150 operations touches
~150 distinct leaves (far past 32 frames) and drives the log to **32,880
bytes**, past the 32 KiB budget. So a large transaction really is forced
to have pages written back before it commits, and really does trigger a
checkpoint part way through. That came from the documented architecture,
not from a test trick.

**The defect.** `txn_snapshot()` keeps each page as it was before the
running transaction first changed it — *in memory only*. `kv_abort()`
uses those images and works correctly while the process is alive. Nothing
about them survives the process. The engine steals (the pool evicts an
unfinished transaction's pages; the log budget forces a checkpoint that
flushes them on purpose and then recycles the records) but has no durable
undo information and no undo pass in recovery.

**Behaviour of the shipped tree.** Public suite: **58 passed, 4 failed**,
the four being exactly the crash cases in `tests/test_transactions.sh`.
Private verifier: **11 passed, 31 failed** — 30 contract failures plus
the public suite. Of the 30 contract failures,
**25 are purely logical** ("transaction N took effect for only 100 of its
150 operations") with `kv_verify` passing, and 5 are structural — those 5
come from transactions that *insert*, where an uncommitted page split
reaches disk unevenly. That is the ratio §5 asked for: the tree normally
stays well formed and the logical contents are the decisive oracle, with
a deliberate structural minority to keep "roll the data back but not the
structure" from working. No case anywhere lost committed work, so the
engine carries the one intended defect and no unintended second one.

## 2. Why it was rejected

### Ground A — a generic workaround satisfies the full explicit contract

The brief required that "retaining every dirty page in memory until
commit must not be a sufficient implementation strategy", and that the
fixture be rejected if any simple generic workaround satisfies the
contract.

I built `bypass_F3_no_steal_correct`: buffer pool raised to 8192 frames,
pages of the running transaction never written back, and checkpoints
simply skipped while a transaction is live (so the meta page never claims
to have absorbed log it has not). Nothing an unfinished transaction does
reaches the disk, so there is nothing to undo.

**Result: it passes all 41 crash cases of the private verifier.** Its
only failure is `public_suite`, and within that, the single assertion I
added late — that `kvcli stats` still reports `frames=32`.

`neg_memory_undo_plus_no_midtxn_checkpoint` (memory-only undo, a big
pool, and the budget check kept out of the write path) behaves the same
way: **41 of 42, failing only the pool assertion**.

So the behavioural contract as stated does not distinguish "implement
durable undo" from "do not steal". The only thing separating them is a
buffer-pool size check that exists for no reason other than to forbid the
second answer. The brief explicitly forbids adding a requirement merely
to kill an alternative correct solution, and no-steal is not a cheat —
it is a textbook recovery strategy. Removing that assertion makes the
fixture bypassable in one line (`#define NFRAMES 8192`); keeping it makes
the fixture depend on an arbitrary gate. Either way it fails.

### Ground B — the fresh-context agent produced a complete repair

A fresh agent was given only the exact prompt (`private/FINAL_PROMPT.md`),
the extracted solver-facing repository in an isolated container, and a
shell. No verifier, no reference, no negatives, no notes.

It produced a **complete, correct repair**:

* public suite **62 passed, 0 failed**;
* **private verifier 42 passed, 0 failed** — a full pass on a matrix it
  never saw;
* it also ran a 95-case crash sweep of its own design, 0 failures, and
  made the engine faster (`batch 3x3000`: 3679 ms → 244 ms).

Its diagnosis was exactly right and reached without hints: "redo-only
logging combined with a STEAL buffer pool, which is an unsound
combination and cannot be patched inside recovery." Its repair is
architecturally different from mine and arguably better — rather than
logging before-images, it removes the steal while *keeping* the 32-frame
pool: an evicted uncommitted frame is written back as the image the
transaction found, the page is recorded as owing the data file an image,
and the next read replays the log records onto it. It also independently
found and fixed the secondary hazard my own reference had to handle (an
aborted transaction's records must not be replayed by the online
rebuild).

Elapsed: **65 minutes**. That is nominally five minutes past the one-hour
hard stop, and I am not going to claim it as a survival. For most of that
window the same Docker daemon was running my validation campaign — up to
fourteen parallel fsync-bound test suites — and the agent's own report
shows much of its time went on repeated runs of a suite that takes 5½
minutes. On a quiet machine this is comfortably inside the hour. Treating
the 65 minutes as a pass would be gaming my own gate.

Either ground alone requires rejection. Together they are decisive.

## 3. Full validation results

Private verifier, 42 cases. Every tree but the reference and the agent's
repair had to fail, and did.

| tree | private verifier | note |
| --- | --- | --- |
| reference repair (mine) | **42 / 0** | durable undo + undo pass + recycling horizon |
| fresh agent's repair | **42 / 0** | independent architecture, no hints |
| shipped engine | 11 / 31 | the planted defect |
| bypass A: budget check out of the write path | 21 / 21 | eviction alone still steals |
| bypass B: never recycle the log | 6 / 36 (timed out) | log grows without bound; pathological |
| bypass C: replay every record | 7 / 35 | resurrects uncommitted work |
| bypass D: force every dirty page at commit | 14 / 28 | damage is done *before* commit |
| bypass E: buffer pool 8192 | 17 / 25 | budget still checkpoints mid-transaction |
| bypass F: crude no-steal | 29 / 13 | own bugs, not a fair test |
| bypass F2: no-steal, no recycling | 24 / 18 | advances `checkpoint_lsn` unsafely |
| **bypass F3: no-steal done correctly** | **41 / 1** | **only the pool assertion — Ground A** |
| neg: undo records logged, never used | 5 / 37 | |
| neg: leaf pages only get before-images | 33 / 9 | structural damage survives |
| neg: roll back data, not structure | 34 / 8 | structural damage survives |
| neg: rollback but still recycles | 9 / 33 | horizon matters |
| neg: memory undo + no mid-txn checkpoint | 41 / 1 | only the pool assertion |

Trees marked with an unreached fault point (E, F, F2, and the memory-undo
negative) were re-measured after the verifier was corrected to check a
workload that ran to completion rather than failing it for never
reaching the scripted fault; the rest had no such case, so their numbers
are the same under both versions.

Bypass D is worth singling out: forcing every dirty page at commit — the
move that neutralised the *previous* fixture's defect — leaves **20
genuine partial-transaction failures** here, because the damage is done
by eviction before the commit rather than by a missing redo. That part of
the design worked.

## 4. Two real defects found and fixed along the way

Both were in my own packaging and would have shipped:

* **`build/make_zip.py` lost the execute bit.** `zipfile` on Windows
  writes `create_system = 0`, so the Unix mode bits in `external_attr`
  are ignored on extraction. Every `tests/*.sh` extracted `rw-r--r--`,
  and the Makefile ran `@tests/run_all.sh` directly, so `make test`
  failed with `Permission denied` before a solver changed anything. The
  fresh agent hit this and worked around it with `chmod +x`. Fixed by
  setting `create_system = 3` and by changing the Makefile to
  `@bash tests/run_all.sh`.
* **Stale documentation.** `docs/FORMAT.md` claimed a 48-byte log record
  header (it is 56) and that split records "describe the split rather
  than the pages it produced" (they carry the whole new page).
  `src/wal.c` still said truncation was unimplemented. All corrected.

The correctness audit of the engine itself found no unintended defect:
across 42 private cases and a 47-point crash sweep, the shipped engine
never lost committed work and never produced a value nobody wrote.

## 5. What this task line has now established

| attempt | defect | outcome |
| --- | --- | --- |
| 1 | stale root id in the promotion log record | rejected on review: one line, contradicted by FORMAT.md |
| 2 | physiological split redo, per-half LSN gating | fresh agent: complete fix in ~34 min |
| 3 | checkpoint inside the transaction | unobservable: repair externally identical |
| 4 | (same, silent-logical variant) | abandoned in validation, same reason |
| 5 | steal without durable undo, multi-op transactions | **this report: no-steal satisfies the contract; agent solved it** |

Attempt 5 fixed what killed attempts 3 and 4 — multi-operation
transactions genuinely make "part of an uncommitted transaction is
visible" an objective, externally checkable error, and the oracle is
clean. What it could not fix is that the *correct* class of repair is
not unique: an engine that never steals is a legitimate answer to the
same contract, and it is reachable by changing one constant plus a
guard. Forbidding it requires a rule about memory that the behavioural
contract does not imply.

That is a property of the problem, not of this particular fixture.
"Crash recovery must be atomic" admits a conservative implementation
that avoids the hard case entirely, and the conservative implementation
is short. Any future attempt in this family needs a contract under which
the conservative answer is *observably* wrong — for instance a workload
whose transactions genuinely cannot fit in any admissible memory, which
in turn needs an engine fast enough that such workloads are testable
(this one fsyncs per commit and takes 5½ minutes for its own suite).

## 6. Reproducing

```bash
python3 build/make_reference.py     # private/fixed + reference_fix.patch
python3 build/make_negatives.py     # private/negatives/*
bash private/verify.sh <tree>       # 42 cases; exit 0 only if all pass
bash build/run_validation.sh <tree> # public suite + private verifier
```

`build/make_zip.py` still works and is now correct, but no archive is
committed.

## 7. Licensing

Everything in this task directory was written from scratch for this
benchmark. No third-party code, no external dataset, nothing downloaded,
nothing reused from any other task. The engine depends only on the C
standard library and the POSIX file API and needs no network. The one
piece of third-party-authored material is
`private/attack_evidence/fresh_agent_solution_v2.patch`, which is the
attacking agent's output, kept as evidence and not shipped.
