# Six-round postmortem — Systems/Databases task tournament

Status: record-keeping and synthesis only. Nothing is implemented, and no
Round-7 candidate is proposed as a task here.

**The correction this document incorporates.** Rounds 3–6 repeatedly
treated the V5 architecture (constraint abduction over destroyed
transaction-outcome authority) as "the only family never shown easy" and,
in Round 6, as "untested against a frontier model." That was factually
wrong. V5 was run through the Outlier frontier Failure Test and the model
produced `recovered.csv` **byte-identical to the golden**: 42262 bytes,
SHA-256 `71faf96e14ba820b8eb6d8988eb7c36b6097b9272e57c1dad0da7428ab6c9510`,
git blob `2eae7d57ab020558d79d7ed45e93d05ef04a3db8` — the same blob SHA as
the repository's committed `build/internal/golden.csv` (re-verified
locally while writing this document). Every argument in this program that
leaned on "V5 might still be beyond the frontier" is retracted. The
frontier executed the entire intended expert pipeline — three-relation
page parsing, stale-clog evidence classification, hint collection,
exhaustive 2^15 candidate enumeration under PK/FK/CHECK constraints — to
an exact result, including the funnel the task was designed around.

---

## 1. What architectures were actually tested?

| family | rounds | representative candidates |
| --- | --- | --- |
| Static format reconstruction (parse + documented visibility rules) | V1–V4 | PostgreSQL heap/clog/subtrans/MultiXact recovery |
| Constraint abduction over destroyed authority | V5 | lost clog tail; hints + PK/FK/CHECK pin 1 of 32768 assignments |
| Physical-artifact reconciliation | R2 | LSM live-set (C1), table-from-indexes + hot journal (C2), torn pages (C3) |
| Cross-history diagnosis over redundant copies | R2–R3 | backup-chain forensics (C5), split-brain reconciliation (C4) |
| Deterministic engine-behavior prediction | R3 | Cassandra tombstone/repair merge prediction |
| Coordination-layer hidden-state recovery | R4 | etcd consistent-index divergence, MySQL XA reconciliation, undo-truncation zombies |
| Live operational repair with behavioral verification | R5–R6 | invalid-index repair, logical-replication repair, concurrency-bug repair (hardened payment service) |

## 2. Evidence levels: true frontier trials vs local frontier-equivalent attacks

**True frontier Failure Tests (independent model, real task): V1, V2, V3,
V4, V5 — all five solved exactly.** That is the complete set. Everything
after V5 — every Round-2-through-6 verdict — rests on *designer-authored*
attacks only: my own dumbest-solver, near-expert, and "frontier-
equivalent" implementations (RepairDB runs, merge-all measurements,
replay solvers, the F5–F10 ladder, the 19/19 one-pass F10).

The asymmetry between those evidence levels is the methodological core of
this postmortem. A designer-authored attack that *succeeds* is strong
evidence a task is easy (the attack used only standard patterns the
attacker would grant any frontier agent, and designer knowledge only
inflates attack strength — the bias points the right way for
rejections). But a designer-authored attack that *fails*, or a designed
trap that looks strong, is **never** evidence a task is hard: V5's own
history proves it. Through six rounds of local red-teaming I treated
V5-class difficulty as an open question; the one real trial answered it
in the solver's favor, byte for byte. Designer-side F10 (19/19 in one
pass) must therefore be read as an *upper bound on task difficulty*, not
as a simulated frontier run — and the tournament's only decisive
instrument for *accepting* a task is an actual frontier trial, which was
never part of the loop after V5. Any future round should put a cheap
real-model pilot *before* full implementation, not after.

## 3. Exact defeat mechanism per candidate

| candidate | defeated by |
| --- | --- |
| V1–V4 | frontier trials: documented formats + local rules, executed correctly |
| V5 | frontier trial: full abduction pipeline executed; output byte-identical to golden |
| C1 LSM manifest | measured: per-obsolescence retention keeps complete event chains → merge-all-max-seq reproduces golden; RepairDB fails loudly (empty DB), not silently |
| C2 indexes-remember | measured: hot-journal rollback restores committed state exactly (forced-spill kill test); remainder is a rowid join |
| C3 torn pages | self-rejected: difficulty existed only via engineered checksum collisions (gimmick) + InnoDB build risk |
| C4 split brain | conflict resolution requires invented policy; policy-free redesign became a mechanical watermark decision table |
| C5 backup chain | measured pincer: two-world construction yields **byte-identical artifacts** with different truths (ill-posed); full log coverage yields a 15-line replay solver (trivial) |
| F1 SQLite multi-db | unverifiable incident; non-trivial pole is a V5 isomorph |
| F2 ClickHouse parts | engine's own documented recovery covers it; part-name algebra + local validity = rules |
| F3 DuckDB ART | rebuild-from-table answers it |
| etcd consistent-index | measured: 90-line WAL-only parser reproduces recovered state; HardState persists the commit index — evidence complete by design |
| MySQL XA | measured: reconciliation is a per-XID three-input lookup table off `mysqlbinlog` text; bug windows need debug builds |
| MySQL undo zombies | pincer: trivial with binlog coverage, two-world ill-posed without |
| Cassandra zombie | measured: 100-line local per-cell reducer == engine read result across all tombstone/TTL classes |
| R5-B invalid indexes | measured: drop-invalids loop + `REINDEX TABLE CONCURRENTLY` = 4 lines (stock) |
| R5-C logical replication | truncate + resubscribe + `setval` = documented reset path |
| R5-D query diagnosis | one-command fixes; performance verification not TB-deterministic |
| R6 hardened concurrency | measured: one canonical architecture (claim-first persistence + total resource order + conditional transitions) — 19/19 in a single authoring pass |

## 4. Properties that repeatedly made tasks easy for frontier agents

1. **Complete evidence by design.** Engines durably record whatever their
   own correctness depends on (etcd HardState.commit, XA state in binlog
   + engine, LSM event chains under retention). Complete evidence ⇒ the
   answer is a computable function ⇒ replay/diff/simulate.
2. **Documented-path reachability.** Wherever correct output = the
   engine's own documented algorithm applied carefully, the agent
   implements the algorithm — including when the *server's* implementation
   of it was buggy.
3. **Single-pattern sufficiency.** Requirement interaction is not
   difficulty if one canonical architecture satisfies everything at once
   (max-seq merge; claim + canonical lock order; drop-and-reindex).
   Round 6 measured this exactly.
4. **Enumerable hypothesis spaces with cheap internal checkers.** V5's
   2^15-candidate funnel *was the solver's friend*: a finite space plus a
   self-validating constraint checker converts abduction into
   computation. The property designed in for uniqueness-proof purposes
   doubled as a solving aid.
5. **Training-data saturation.** Famous postmortems, textbook concurrency
   patterns, staff-interview idioms: the priors arrive pre-solved.
6. **Announced failure modes.** Every task told the solver what kind of
   thing was wrong (stale clog, lost MANIFEST, races in these functions).
   Naming the incident class collapses the diagnosis space to zero.

## 5. Properties that made tasks underspecified or artificial

1. **Open physical fault models** (bit flips, torn bytes): the hypothesis
   space lives in the generator's head → category-C assumptions →
   two-world indistinguishability (C5, undo zombies).
2. **Destroyed authority without redundant pinning**: when the engine
   deletes the only record of a fact and nothing constrains it, truth
   stops being solver-derivable (purged tombstones, truncated undo).
3. **Constraints invented to encode answers**: C4's invariants, C3's
   checksum collisions — the generator smuggling the key into the lock.
4. **Policy questions wearing recovery costumes**: split-brain "winners."
5. **Determinism forced by contrivance**: perf thresholds, plan-shape
   checks, tool denial, artificial sleeps in solver-visible code.

## 6. Task physics NOT yet empirically explored

- **Blind diagnosis**: every task so far announced its failure class in
  the prompt. No task has required *discovering the failure mode itself*
  from a live symptomatic system.
- **Long-horizon interactive state tracking**: all tasks were solvable by
  a stateless pipeline over a fixed artifact set or a single fix applied
  once; none required maintaining and revising a model of evolving state
  across many dependent actions.
- **Dependent investigation/repair sequences with irreversibility**:
  Round 5 named "early wrong operation changes subsequent state" as a
  goal; no built prototype actually exercised it.
- **Adversarial live workloads**: no task ran ongoing load *during* the
  agent's work, with the repair required not to lose in-flight effects.
- **Defensibility as the graded object**: every verifier graded a
  computed artifact; none graded whether claims are *evidence-backed* —
  no task has ever failed an agent for being right for the wrong reasons
  or unable to substantiate.

## 7. Which unexplored directions fit the actual reviewer guidelines?

The Terminal Bench difficulty guidance cited in Round 5 explicitly
prizes: *discovering what is broken in the actual environment*,
*interdependent steps where early mistakes cascade*, and *long-horizon
state tracking*. Blind diagnosis matches the first clause literally;
dependent repair sequences match the second; live-workload repair and
long-horizon tracking match the third. All can keep outcome-only
verification (fresh deterministic incident + behavioral final grading),
none requires GUI/network, and none inspects methodology. Defensibility
grading is compatible *if* the deliverable itself is the graded artifact
and every check on it is mechanical — it grades an outcome (a report
whose citations recompute), not a process.

## 8. At most three fundamentally new directions, ranked

All three below avoid: static state reconstruction, transaction-outcome
reconstruction, deterministic engine replay, ordinary concurrency-control
repair, and stock operational recovery.

### 1. Blind root-cause incident ("the prompt names symptoms, not the fault")

A live database environment exhibits precisely reproducible symptoms
("this workload's step 7 intermittently fails with X since deploy N; fix
it without breaking behaviors A–D"). The fault is a *composite* of two or
three individually documented, individually mundane causes whose combined
symptoms **alias** a more common single cause — so the trained prior
actively misleads, symptom-suppressing fixes pass some behaviors and fail
others, and the first repair changes what the remaining symptoms look
like (interdependence). Nothing in the prompt names the mechanism; the
diagnosis space is genuinely open. Verifier: fully deterministic — fresh
incident instance, apply the submitted `repair.sh`/`fix.sql`, run the
fixed behavioral workload, grade final behavior only.
*Why it may resist the frontier where everything else fell*: it is the
first shape where §4's properties are absent by construction — no
announced failure class, no single pattern, priors harmful rather than
helpful. *Honest risks*: composite-fault design can slide into §5
artificiality (gate: every component fault must be a documented real
incident class and the composition operationally plausible); and per §2,
only a real frontier pilot — not designer attacks — can validate the
difficulty.

### 2. Evidence-bound diagnosis (the deliverable must recompute)

The deliverable is a structured incident report — root cause,
affected-object inventory, exact loss/impact set — where **every claim
must carry machine-checkable citations** (query text + expected result,
file/offset + expected bytes) and the verifier *recomputes each citation*
against the environment, failing any claim whose evidence does not
support it, any inventory that is incomplete, and any citation that does
not reproduce. Outcome-only: the graded object is the report artifact;
recomputation is mechanical; no methodology inspection. This attacks the
one capability boundary six rounds never tested (§6): being *demonstrably
right*, not just right. Coherent-but-wrong answers — the failure profile
every round hunted for — become structurally detectable, because wrong
diagnoses cannot cite recomputable support. *Honest risks*: citation
schema design is genuinely novel verifier engineering; badly designed, it
degenerates into "fill in the template" (the schema must admit many
correct evidence choices), and the completeness check needs a
machine-provable ground truth inventory.

### 3. Repair under live fire (dependent interventions while writes continue)

A deterministic, harness-driven workload keeps writing throughout the
agent's session; the incident degrades service in a way that requires a
*sequence* of interventions whose order matters, and where a wrong early
intervention loses acknowledged effects that the verifier will look for
at the end. Grading is end-state and invariant-based, which keeps it
deterministic in verdict despite concurrent activity: every acknowledged
write present exactly once, conservation, health behaviors restored —
never byte-exact state, never latency numbers. This is the strongest
match to "long-horizon state tracking" and "early mistakes cascade."
*Honest risks*: highest environment-flakiness exposure of the three
(agent actions interleave with load), and the "cascading mistake" must be
a natural consequence of the system, not a tripwire; ranked third for
exactly the determinism-engineering burden Rounds 5–6 quantified.

**If none survives its pilot**: the honest fallback recorded here is not
another database-recovery variant — it is that, within this domain and
constraint set, the program has exhausted task shapes whose difficulty
survives contact with a frontier model, and the next move belongs to the
tournament owners (different capability boundary, different domain, or
acceptance of V3/V4-class difficulty with impeccable professionalism).

---

*Process rule going forward, earned twice over: designer-side attacks may
reject; only real frontier trials may accept. Pilot cheaply, early, and
before full implementation.*
