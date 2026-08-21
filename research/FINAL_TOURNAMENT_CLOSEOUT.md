# Final tournament closeout — V1 through Round 11

Scope: this document closes the Systems→Databases task-design program. It
records what was tested, at which evidence level, and why nothing in the
program is currently submittable. No new candidate is proposed, designed,
or researched here.

## Evidence levels used below

| level | meaning | strength |
| --- | --- | --- |
| **REAL FRONTIER FAILURE TEST** | the built task was run through the Outlier frontier Failure Test | the only level that can *accept* a task |
| **FRESH-CONTEXT AGENT** | independent agent, own context, given only the solver-visible ticket/environment; generator, oracle, verifier and design notes removed | strong rejection evidence; approximates a frontier trial |
| **DESIGNER ATTACK** | attack written by the task's own designer | can only *reject*; designer knowledge inflates attack strength, so success proves easiness, failure proves nothing |

That asymmetry is the program's central methodological finding and is
applied throughout.

---

## 1. Architectures tested

### V1–V5 — actual frontier trials

| # | family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- | --- |
| V1 | MVCC row recovery from raw heap pages | PostgreSQL 16 | **REAL FRONTIER FAILURE TEST** | page/tuple format + visibility rules | solved exactly (210/210 rows) | not recorded | frontier-solved |
| V2 | + lock-only xmax, HOT chains, mixed per-key history | PostgreSQL 16 | **REAL FRONTIER FAILURE TEST** | interaction of infomask semantics | solved exactly (265 rows) | not recorded | frontier-solved |
| V3 | + raw `pg_xact` / `pg_subtrans` SLRU decoding, subxact ancestry | PostgreSQL 16 | **REAL FRONTIER FAILURE TEST** | multi-artifact decoding, no decoded state table | solved exactly (353 rows) | not recorded | frontier-solved |
| V4 | + MultiXact offsets/members, frozen tuples | PostgreSQL 16 | **REAL FRONTIER FAILURE TEST** | third SLRU geometry, silent misread traps | solved exactly (429 rows) | not recorded | frontier-solved |
| V5 | lost clog tail: transaction-outcome **abduction** under PK/FK/CHECK constraints | PostgreSQL 16 | **REAL FRONTIER FAILURE TEST** | 2^15 candidate enumeration pinned to exactly one assignment by global constraints; authenticity gate proved the stale-clog state reachable without file surgery | **solved byte-identically** — 42262 bytes, sha256 `71faf96e…`, git blob `2eae7d57…`, equal to the committed golden | not recorded | frontier-solved; the built task exists at `tasks/postgres-mvcc-heap-recovery` but cannot be submitted as a difficulty claim |

V5 is the program's most important negative result: the frontier executed
the full intended expert pipeline — three-relation page parsing, stale-clog
evidence classification, hint collection, exhaustive enumeration under
constraints — to an exact result.

### Rounds 1–4 — static-forensics candidate space (designer attacks, measured)

| family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- |
| C1 LSM live-version reconstruction (lost MANIFEST) | RocksDB 7.8.3 (LevelDB conflation corrected) | DESIGNER ATTACK | choose the live SST set from engine invariants | **merge-all-max-seq reproduced the golden exactly**; `ldb repair` fails loudly (empty DB), not silently | seconds | difficulty does not exist: retention keeps complete event chains |
| C2 table-from-indexes + hot journal | SQLite 3.40.1 | DESIGNER ATTACK | reassemble a wiped table from index projections | **forced-spill kill test: stock hot-journal rollback restored the committed state exactly** | seconds | the proposed second trap contradicts documented engine semantics |
| C3 torn pages / doublewrite arbitration | MySQL InnoDB | DESIGNER ATTACK (analytic) | choose among checksum-valid page images | difficulty existed only via engineered checksum collisions | — | gimmick + InnoDB build cost |
| C4 split-brain reconciliation | replicated pair | DESIGNER ATTACK (analytic) | reconcile divergent histories | requires an invented conflict policy; policy-free redesign became a 3-case decision table | — | artificial or mechanical |
| C5 backup-chain silent corruption | SQLite 3.40.1 | DESIGNER ATTACK | locate the poisoned generation across snapshots | **two-world construction: byte-identical artifacts, different truths** (ill-posed); with full log coverage a **15-line replay solver reached golden in 0.02 s** | 0.02 s | under-specified or trivial, no middle ground |
| Cassandra zombie/repair prediction | Cassandra 4.1 (live) | DESIGNER ATTACK | predict repair/compaction outcome | **~100-line per-cell reducer over `sstabledump` matched the engine exactly** across cell/row/partition/range tombstones, TTL, resurrection | minutes | merge semantics are local and documented |
| etcd consistent-index divergence | etcd v3.5.2 (affected release) | DESIGNER ATTACK | reconstruct committed entries vs backend state | **~90-line WAL-only parser reproduced etcd's own recovered state**; every HardState persists the commit index | minutes | evidence complete by design |
| MySQL XA / binlog reconciliation | MySQL 8.0.28 | DESIGNER ATTACK | coordinator forensics across a rotation | reconciliation is a **per-XID three-input lookup table** readable off `mysqlbinlog` | minutes | lookup-table difficulty at debug-build cost |
| MySQL undo-truncation zombies (#119628) | MySQL 8.0 | DESIGNER ATTACK (analytic) | identify rows that survived rollback | trivial with binlog coverage; two-world ill-posed without | — | pincer |

### Rounds 5–6 — operational concurrency repair (designer attacks)

| family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- |
| Concurrent money-movement repair (hardened: 6 operations, 3 resource classes, 19-check behavioral matrix) | PostgreSQL 16 (live) | **DESIGNER ATTACK** | interaction of idempotency, lock ordering, state machines and disjoint-progress requirements | buggy baseline 9/19; no-result-persistence 10/19; incoherent hierarchy 19/19 then 60/100 deadlocks under a corrected probe; advisory-canonical 18/19; SERIALIZABLE+claim 9/19 — but the **general resource-lock attack (F10) scored 19/19 in one authoring pass**, ~160 LOC, zero debug iterations | ~25 min | one canonical architecture satisfies every requirement |
| Failed online index maintenance | PostgreSQL 16 (live) | DESIGNER ATTACK | invalid `_ccnew`/`_ccold` states, repair ordering | **4-line loop** (drop invalids + `REINDEX TABLE CONCURRENTLY`) completes the repair | minutes | stock tooling solves it |
| Logical-replication subscription repair | PostgreSQL 16 | DESIGNER ATTACK (analytic) | conflict skip / origin advance | truncate + resubscribe + `setval` is the documented reset path | — | stock recipe |
| Query/planner diagnosis | PostgreSQL / MySQL | DESIGNER ATTACK (analytic) | statistics/plan interactions | one documented action fixes each variant; perf signal not TB-deterministic | — | trivial and non-deterministic |

### Round 7 — blind root-cause diagnosis (fresh-context)

| family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- |
| Blind composite incident (symptom-only ticket; poisoned role-level `session_replication_role` + sequence desync + accrued integrity damage) | PostgreSQL 16 (live) | **FRESH-CONTEXT AGENT** | failure mode not named; obvious fixes improve symptoms without satisfying the contract; first repair changes the diagnostic landscape | ladder behaved as designed (baseline 2/11, obvious fix 4/11, one-cause fix 7/11, reference 11/11) but the fresh agent scored **11/11**, walking the four designed wrong hypotheses at one catalog query each | **3 m 21 s, 12 tool calls** | documented faults carry documented signatures; composite blind diagnosis is a retrieval checklist |

### Rounds 8–10 — long-horizon migration (D1)

| family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- |
| Interrupted rolling migration, four restart checkpoints, interleaved dual-writer traffic | PostgreSQL 16 (live) | **FRESH-CONTEXT AGENT** | resume from any checkpoint while preserving every acknowledged write | generic rebuild dies structurally; the designer's own textbook package scored 11/10/9/9 and needed a debug iteration — but the fresh agent scored **11/11 on all four checkpoints with ZERO architecture revisions**, and out-diagnosed the designer | **13 m 46 s, 25 tool calls** | a loop was forced; architecture invalidation was not |
| Opposing-authority cohort rollout (same migration, per-wave cutover: blob authoritative before, normalized after) | PostgreSQL 16 (live) | **DESIGNER ATTACK** | no single source-of-truth rule is correct for all rows | construction worked — 10 rows must resolve normalized, 10 blob, in one checkpoint; blob-always (**= Round 9's winning architecture**) 8–9 wrong, normalized-always 4 wrong, textbook 3–4 wrong — but **generalized per-row/per-phase reconciliation (G) scored 11/11 on all four checkpoints in one pass**, zero revisions | ~25 min | a well-specified authority boundary *is* an architecture |

### Round 11 — live topology operation (D2, designer attack)

| family | engine | evidence | strongest difficulty mechanism | decisive result | solve time | why not submittable |
| --- | --- | --- | --- | --- | --- | --- |
| Two-cluster logical-replication switchover (conflict-disabled subscription, unpublished table, unreplicated sequences), `setup.sh` + verifier-invoked `promote.sh` | PostgreSQL 16 ×2 (live) | **DESIGNER ATTACK** | operate a live topology to a state that survives a scripted failover | all stock recipes fail (3/8, 3/8, 4/8 — including the documented drop/recreate-subscription recipe) but the **one-shot SRE package scored 8/8 on its first run**, reproduced 8/8 across three full rebuild-and-verify cycles | ~20 min | every needed fact is generically introspectable at runtime; topology adds operations, not judgement |

---

## 2. Statements this program establishes

**1. No candidate from this research program currently has positive
empirical evidence sufficient to claim the Terminal Bench difficulty
bar.** V1–V5 were solved outright in real frontier trials. Every
post-V5 candidate was rejected by a fresh-context agent (Rounds 7, 9) or
by a single-pass designer attack (Rounds 6, 10, 11) — and designer
attacks can only reject, never accept. Nothing in the program has ever
been shown to *fail* a frontier model, which is what the bar (majority
trial failure, ≥2/3 Gemini failures) requires.

**2. No rejected candidate should be implemented merely to make
progress.** Each rejection above is backed by a measurement, not an
impression: a solver that reached golden, a two-world indistinguishability
construction, a stock command that completes the repair, or a one-pass
package that scores full marks. Implementing any of them would produce a
task whose difficulty claim is already falsified in this repository.

**3. The strongest reusable engineering assets are:**
- **Deterministic outcome-only verifiers** — outcome-graded CSV digests
  (V1–V5); barrier-driven concurrency schedules that kill wrong strategies
  reproducibly (Rounds 5–6); stage-driven migration verifiers with
  interleaved scripted traffic (Rounds 9–10); bounded content-fingerprint
  convergence polling with no sleeps or latency thresholds and verdicts
  identical across repeated cycles (Round 11). None inspects methodology.
- **Authentic real-engine fixture generation** — the V5 Stage-0
  authenticity gate (proving a stale clog tail is reachable with no file
  surgery), and the standing discipline that every damaged byte is written
  by the engine itself, applied through Rounds 7–11.
- **Fresh-context attack methodology** — an independent agent given only
  the solver-visible ticket and environment, with generator, oracle,
  verifier and design notes removed. It produced the program's two most
  trustworthy difficulty measurements (Rounds 7 and 9).
- **Early kill gates** — the two-world indistinguishability test and the
  dumbest-complete-evidence solver (adopted after Round 3), plus the
  reset / stock-tool / one-shot gates (Rounds 5–11). They retired four
  candidates in hours each and prevented multi-day builds of unsolvable
  or trivial tasks.

**4. Difficulty remains the blocking criterion.** Verifiability,
specification, solvability, realism, outcome-only grading and
code-requirement were all satisfied repeatedly and are not the obstacle.
In every scored round, DIFFICULTY was the only criterion below the bar.

**What the experiments do *not* establish.** They do not show that hard
database tasks are impossible in general. They show that, across the
specific families tested — static forensics, constraint abduction,
engine-behavior prediction, coordination-layer recovery, concurrency
repair, blind composite diagnosis, long-horizon stateful migration, and
live topology operation — every construction that was simultaneously
deterministic, well-specified, outcome-verified and free of hidden state
was solved by one competent pass, whether by a real frontier trial, an
independent fresh-context agent, or a single-pass designer attack. Task
families and evaluation contracts outside that tested set were not
examined here.

---

RECOMMENDATION:
STOP CANDIDATE DEVELOPMENT AND RETURN TO THE TERMINAL BENCH WORKFLOW /
QM FOR NEXT TASK OR GUIDANCE.
