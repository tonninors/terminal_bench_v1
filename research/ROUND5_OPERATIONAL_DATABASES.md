# Round 5 — Operational / interactive database tasks (empirical screen)

Status: **design round only — no Terminal Bench task implemented.**
Lab: real PostgreSQL 16.15 in a container; all schedule results below are
measured, not predicted. Multilingual research was performed (EN, JA, ZH,
RU, ES, DE; sources at the end) — notably, the Russian-language official
docs spell out candidate C's stock skip path, and Chinese-language
operational articles document candidate E's residue cleanup as routine.

## Decision up front

**A. PROTOTYPE CONCURRENCY TASK** — a *conditional* advance, with the
gate-5 tension stated plainly in §1 and codified stop conditions. This is
the first candidate in five rounds whose verifier physics demonstrably
work: three plausible expert strategies fail the behavioral schedules
*deterministically and for different reasons* (8/12, 11/12, 9/12), the
correct fix is a composite of interacting techniques rather than one
primitive, and the whole incident resets deterministically in under two
seconds. The honest problem — measured, not hidden — is that the minimal
prototype's composite fix took the frontier-equivalent author one shot
(~70 lines): as prototyped, gate 5 fails. The advance is therefore to a
**hardened prototype**, not to implementation, and §1.9 defines the
kill-switch if hardening cannot move the needle.

---

## 1. Candidate A — PostgreSQL concurrent money-movement repair

REAL ENGINE/VERSION: PostgreSQL 16.15 (stock image, no extensions).

PROFESSIONAL INCIDENT: a payment service's stored functions (`transfer`,
`reserve`, `settle`, `cancel`) are sequentially correct and pass every
single-connection test, but production concurrency produces double-applied
retries, deadlocks, overspend, and double-settled reservations — the
classic class of bugs that fills real postmortems (and the ES/RU/EN
literature searched this round: encounter-order `FOR UPDATE` deadlocks,
check-then-act races, idempotency-key misuse).

LIVE INITIAL STATE: a running cluster with the schema, the buggy
functions, and seed accounts. The bugs are real logic races, not syntax
errors: check-then-insert idempotency, unordered row updates,
read-check-write balance logic, read-then-transition state machine.
(The prototype widened race windows with in-function `pg_sleep`; a real
fixture keeps the sleeps only in the *buggy* code — which the solver
replaces anyway — while the verifier catches racy *solver* code via
barrier-controlled interleavings plus seeded stress; see DETERMINISM.)

REQUIRED FINAL BEHAVIOR (all stated openly in the prompt): concurrent
duplicate `request_id` calls both return success with exactly one applied
effect; opposite-direction transfers both complete (no deadlock); no
negative balance; exactly one winner between concurrent settle/cancel;
operations on disjoint accounts make progress while an unrelated
operation is mid-transaction; global conservation; exactly one ledger
pair per completed transfer.

DELIVERABLE: `/app/fix.sql`, applied by the verifier to a **fresh**
incident instance.

WHY STATIC REASONING IS INSUFFICIENT: nothing to parse — the difficulty
is choosing concurrency-control machinery whose *interaction* satisfies
seven requirements simultaneously; every candidate mechanism fixes some
schedules and breaks others.

INTERDEPENDENT STEPS: the idempotency fix (insert-first) changes what the
locking fix must protect; the deadlock fix (lock ordering) must span
*every* function touching accounts; the state-machine fix must not
reintroduce blocking that the progress requirement forbids.

RESET ATTACK (gate 1): structurally void — the deliverable is code; the
verifier instantiates a fresh incident and runs behavior; dropping or
recreating data satisfies nothing. **Pass.**

GLOBAL-LOCK ATTACK (gate 2, measured): a one-line
`pg_advisory_xact_lock(42)` in every function scored **11/12** — killed
deterministically and only by the disjoint-progress schedule (S5
timeout). **Pass — and the kill is behavioral, not word-scanning.**

STOCK-TOOL ATTACK (gate 3): no stock command repairs application logic.
**Pass.**

DUMB SOLVER (gate 4, measured): three dumb-plausible fixes were built and
run: `ALTER DATABASE … SET default_transaction_isolation = serializable`
→ **8/12** (serialization failures surface as operation errors; the
unordered-update deadlock even survives); global advisory lock → 11/12;
`FOR UPDATE` in encounter order → **9/12** (deadlock persists; the
idempotency race persists). The buggy baseline scores 5/12 with every
designed bug manifesting deterministically. **Pass: no dumb strategy
survives, each dies differently.**

FRONTIER-EQUIVALENT ATTACK (gate 5, measured, adverse): the composite
expert fix — unique index + insert-first `ON CONFLICT` with
prior-outcome return, sorted `FOR UPDATE`, conditional
`UPDATE … WHERE status='reserved' RETURNING` transitions — was written
**in one pass, ~70 lines, and scored 12/12 with zero debugging
iterations**. For this minimal prototype the round's own criterion
("correct fix derivable in <1 hour → reject") fires. These are
staff-interview-canonical patterns, richly represented in training data.

EMPIRICAL RESULT: baseline 5/12; f1 serializable 8/12; f2 global lock
11/12; f3 unsorted locks 9/12; f4 composite 12/12 (one-shot). Fresh
incident reset: <2 s, fully deterministic.

EXPERTISE REQUIRED: PostgreSQL locking/isolation semantics, idempotency
design, deadlock topology across heterogeneous lock objects.

EXPERT TIME: 2–4 h as prototyped (too low); hardened target 4–8 h.

VERIFIER DESIGN: fresh instance → apply `fix.sql` → run barrier-driven
schedules from Python (two-connection interleavings with statement
timeouts for progress requirements) → seeded high-concurrency stress →
assert only the stated behavioral requirements. Methodology-blind by
construction; any implementation that passes the behaviors passes.

DETERMINISM: the naive-strategy kills are fully deterministic (measured
repeatedly). The honest limit: a *subtly* racy solver fix without
in-code wideners is caught only probabilistically by stress rounds — the
verifier can raise iteration counts and concurrency, but cannot close
that gap to certainty. Scored accordingly.

REVIEWER SCORES: VERIFIABLE 4 (stress probabilism) · WELL-SPECIFIED 4 ·
SOLVABLE 5 · DIFFICULT **2.5 as prototyped / est. 3.5–4 hardened** ·
REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 5.

STOP CONDITIONS (binding for the hardened prototype): kill the candidate
if (a) a one-sitting composite fix again scores 100% on the hardened
requirement matrix; (b) any single technique passes everything; (c) the
verifier cannot kill a deliberately-racy reference solution in ≥99% of
runs with fixed seeds; (d) hardening drifts into obscure-trivia
requirements or artificial constraints. Hardening levers to test, in
order: a heterogeneous lock hierarchy across functions
(`close_account` draining in-flight reservations creates
reservation-row→account-row vs account-row→reservation-row cycle
potential that sorted account locking alone does not fix); retry
semantics returning the *prior result* across every terminal state; a
wider schedule matrix where partial fixes pass most-but-not-all; batch
settlement interacting with per-item cancel.

## 2. Candidate B — failed online index maintenance (measured, rejected)

REAL ENGINE/VERSION: PostgreSQL 16.15.
PROFESSIONAL INCIDENT: authentic — failed `CREATE UNIQUE INDEX
CONCURRENTLY` over duplicate data and an interrupted
`REINDEX INDEX CONCURRENTLY` under an old repeatable-read snapshot were
both reproduced live, leaving `ux` (invalid, not ready) and
`good_idx_ccnew` (invalid) alongside the healthy index — no catalog
editing, all engine-authored states.
EMPIRICAL RESULT: the interesting quirks are real but *thin*: the failed
unique build left the index invalid **and not ready** (the writes
remained unenforced — the famous "invalid index still enforces" behavior
belongs to later-phase failures, worth noting for accuracy), and the
table stayed writable throughout. The stock attacks then ended it:
`REINDEX TABLE CONCURRENTLY` completes and merely warns
`cannot reindex invalid index … skipping`; a **four-line loop** (drop
every `NOT indisvalid` index, reindex, dedup before the unique rebuild)
restored a fully valid state. The JA-language and official documentation
prescribe exactly this ("drop and retry").
RESET/GLOBAL-LOCK: n/a. STOCK-TOOL ATTACK: **fails gate 3 — measured.**
DUMB SOLVER: the four-liner. FRONTIER ATTACK: trivial.
REVIEWER SCORES: VERIFIABLE 5 · WELL-SPECIFIED 4 · SOLVABLE 5 ·
DIFFICULT **1** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 2.
STOP/VERDICT: **rejected**; no repair-order interaction materialized.

## 3. Candidate C — logical replication incident (analytic, rejected)

REAL ENGINE/VERSION: PostgreSQL 16 pub/sub.
SCREEN: the required final state ("subscribed tables exactly equal the
publisher at a sync point; subscription enabled and caught up") is
satisfiable by the stock reset path — disable subscription, truncate the
subscribed tables, re-create the subscription with `copy_data = true`,
`setval` the sequences — all documented; the official RU docs likewise
document the per-conflict stock path (`ALTER SUBSCRIPTION … SKIP` /
`pg_replication_origin_advance`). Preserving subscriber-local *rows
inside replicated tables* would block the reset, but it contradicts
"exactly equal" — and carving policy exceptions is exactly the
"arbitrary constraint to save the candidate" the round forbids.
Subscriber-local *objects outside* the replicated tables survive
truncation trivially. GATES 1/3: **fail.**
REVIEWER SCORES: DIFFICULT 2 · others 4–5 · **rejected** (not
prototyped; the reset path is documented end-to-end).

## 4. Candidate D — query/index operational diagnosis (analytic, rejected)

The round's own reject conditions fire on inspection: every scenario in
the family (generic-plan regression, missing extended statistics, stale
stats after skewed load) is repaired by one documented action
(`plan_cache_mode`, `CREATE STATISTICS` + `ANALYZE`, `ANALYZE`), and the
remaining verifier signal is performance, which cannot be made
Terminal-Bench-deterministic in a shared container without degenerating
into "guess the plan the verifier wants" (checking plan shapes is
verifier-by-methodology in disguise).
REVIEWER SCORES: DIFFICULT 2 · DETERMINISM-carrying VERIFIABLE 2 ·
**rejected.**

## 5. Candidate E — non-Postgres operational incident (analytic, deferred)

Best of the family surveyed (ZH/DE/EN sources): a **failed online
schema-change cutover** (pt-osc-pattern: `_new` table + triggers +
partial backfill residue; gh-ost-pattern: `_gho`/`_ghc` residue) with
MySQL metadata-lock interactions. The physics are right — live server,
stateful, interdependent (finish-the-migration requires idempotent delta
reconciliation under concurrent writes, then an atomic cutover;
rollback-instead is measurably insufficient if the required final
behavior is the *new* schema with all concurrent writes). Two things
defer it rather than kill it: (a) the difficulty concentrates in exactly
the same place as candidate A (correct concurrent reconciliation),
making it A's sibling with a heavier harness (MySQL + binlog-driven
verification (registered as its fallback rather than a co-candidate);
(b) the ZH-language operational literature shows the *cleanup* half is
routine (drop residue), so the task must stand entirely on the
finish-forward half, which needs the same stress-determinism machinery
as A. REVIEWER SCORES (projected): DIFFICULT 3–4 · DETERMINISM 3 ·
REALISTIC 5. **Deferred: revisit only if A's hardening fails.**

## 6. What this round established (beyond the candidates)

1. **The behavioral-verifier physics work.** Methodology-blind schedule
   harnesses kill wrong strategies deterministically and differently —
   the first time in this tournament that *plausible expert answers*
   fail measurably rather than analytically.
2. **The difficulty now lives in the requirement matrix, not the
   mechanism.** Each concurrency primitive is trivially known to the
   frontier; what can still be hard is satisfying N interacting
   behavioral requirements at once, where every partial fix passes most
   schedules. Hardening = growing that matrix honestly.
3. **The determinism boundary is now precisely located**: deterministic
   for naive-strategy kills (barrier-controlled), probabilistic for
   subtle races in solver code (stress-bounded). Any implementation
   decision must carry that caveat into the task's difficulty claims.

---

Lab disposed. No task implemented; no fixture built beyond the throwaway
prototype.

Sources: [PG docs: REINDEX](https://www.postgresql.org/docs/current/sql-reindex.html) ·
[PG docs (JA): CREATE INDEX](https://www.postgresql.jp/docs/16/sql-createindex.html) ·
[PG docs (RU): logical replication conflicts](https://postgrespro.ru/docs/postgresql/16/logical-replication-conflicts) ·
[PG docs: explicit locking](https://www.postgresql.org/docs/current/explicit-locking.html) ·
[PG es-list: evitar deadlocks](https://www.postgresql.org/message-id/b1c45530904071118j2355c8e8ucedde941eb7f22bb@mail.gmail.com) ·
[gh-ost 解析 (ZH)](https://rj03hou.github.io/mysql/gh-ost/) ·
[gh-ost 残留表清理 (ZH)](https://blog.csdn.net/u011702673/article/details/106483527) ·
[Percona: online DDL tools and metadata locks](https://www.percona.com/blog/online-ddl-tools-and-metadata-locks/) ·
[MySQL docs: metadata locking](https://dev.mysql.com/doc/refman/5.7/en/metadata-locking.html) ·
[MySQL bug #79462](https://bugs.mysql.com/bug.php?id=79462)
