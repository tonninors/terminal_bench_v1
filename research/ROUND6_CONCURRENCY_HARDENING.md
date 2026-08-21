# Round 6 — Harden or kill the concurrency candidate (measured)

Status: **design round only — nothing implemented.**
Lab: real PostgreSQL 16.15. Every number below is a measured harness run.

## Decision up front

**B. REJECT — FRONTIER SOLUTION TOO EASY.**

The kill was clean and came from the round's own most important test. The
hardened contract (six operations, three resource classes, exact-prior-
result idempotency, atomic batches, close-account interactions) was built
honestly — no trivia, no artificial bans — and the behavioral matrix grew
from 12 to 19 checks with every designed race manifesting 20/20 in the
buggy baseline. Then the **general resource-lock attack (§5), written in
one authoring pass as F10, scored 19/19 on its first and only run**:
~160 lines, zero debugging iterations, roughly 25 minutes of design time.
Two binding implementation conditions — "the general resource-lock attack
does NOT trivially solve everything" and "F10 does not reach 100% in one
clean pass" — are both violated by direct measurement. Per §12, the
candidate is rejected, and per the round's preamble, it is not preserved
merely because five rounds led here.

Bias disclosure, stated rather than hidden: F10 was authored by the same
person who designed the schedules, so it upper-bounds a fresh agent. The
direction of the bias does not save the candidate — the architecture F10
embodies (claim-first idempotency with persisted results + one canonical
total order over all resources + conditional state transitions) is a
*single, standard, extensively documented design pattern*; nothing about
the hardened contract forces its discovery through failure, and once
adopted, all 19 behaviors follow without iteration. A candidate whose
entire difficulty evaporates under one known pattern cannot claim
frontier resistance.

---

## HARDENED BUSINESS CONTRACT

Exactly-once requests: every externally callable operation takes a
`request_id`; a retry — concurrent or later — returns the **original
result verbatim** (including generated ids and failure outcomes) and
applies no further effect. Money & states: no negative balances;
reservations follow reserved→settled/cancelled exactly once; a closed
account accepts no debit, credit, or new reservation; `close_account`
succeeds only with no active reservations, and racing close/reserve must
be equivalent to a legal serial order; `batch_settle` is atomic —
all-or-nothing across its reservation list. Liveness: overlapping
operations (opposite transfers, overlapping batches, batch vs cancel,
batch vs transfer) complete without deadlock; operations on disjoint
resources make progress while any other operation is mid-transaction.

That is the entire contract — four natural groups, no edge-case wall.
The PROMPT COMPLEXITY AUDIT therefore *passes* (the candidate does not
die of artificiality); it dies of B.

## SCHEMA / OPERATIONS

`accounts(id, balance, status)`, `reservations(id, account, amount,
status)`, `requests(request_id, op, result)`, `ledger(request_id,
account, delta)`; operations `transfer`, `reserve`, `settle`, `cancel`,
`close_account`, `batch_settle` — all sequentially correct in the buggy
baseline (its sequential checks pass), broken only under concurrency.

## SCHEDULE MATRIX (19 checks)

G1 racing-retry exactly-once · G2 opposite transfers · G3 overspend ·
G4 settle/cancel single winner · G5 disjoint-progress (in-flight peer) ·
G6 reservation overspend · G7 conservation + ledger cardinality ·
G8 close/reserve legality · G9 closed-account semantics + retry ·
G10 batch atomicity · G11 batch-vs-cancel · G12 overlapping batches
(+ G12b, added mid-round: fully-overlapping reversed lists) ·
G13 disjoint batch progress · G14/G14b/G15 exact-prior-result retries ·
G16 concurrent identical batches · G17 batch-vs-transfer.

## DETERMINISTIC VS PROBABILISTIC CLASSIFICATION

**D (strictly deterministic):** G5, G7, G9, G10, G13, G14, G14b, G15 —
sequential or harness-held-transaction tests; these alone kill entire
bug classes (result persistence, batch atomicity, closed-account logic).
**P, measured effectively deterministic for the wrong solutions built
this round:** G1/G16 caught the check-then-act idempotency defect
**20/20 per run, three runs** (the race window spans the whole claiming
transaction, not microseconds); G12b caught the incoherent lock
hierarchy at **60/100 per iteration** (suite-level miss probability
≈ 0.4^100). **Standing caveat:** those rates were measured against the
wrong solutions I authored; a differently-shaped subtle defect could
have a narrower window — the Round-5 flag stands in principle and would
have been the next blocker had the candidate survived. One matrix lesson
is recorded: the original G12 shared only one reservation between
batches, so a lock-order cycle was impossible and F6 sailed through
19/19 — schedule design errors produce false confidence, and only the
corrected G12b exposed the defect.

## ATTACK LADDER (all measured)

| solver | score | killed by | notes |
| --- | --- | --- | --- |
| BASELINE (buggy) | 9/19 | every race group at 20/20 violations | sequential checks pass, as designed |
| F5 insert-first + sorted account locks, no result persistence, loop batch | 10/19 | G9/G14/G14b/G15 (generic `duplicate-ignored` ≠ original result), G10/G11 (partial batch), G12, G16 | the archetypal coherent partial solution |
| F6 F5 + persistence, reservations locked in argument order | 19/19 on original matrix; **60/100 deadlocks** under G12b | incoherent hierarchy | exposed only by the corrected schedule |
| F7 advisory-lock canonical ordering + persistence | 18/19 | **G11 exactly, 20/20** — advisory locks bind only cooperating code paths; `cancel`'s row-conditional update slips past the batch's advisory serialization | genuinely instructive failure; a careful pass routes cancel through the same locks and reaches 19/19 |
| F8 row locks + one canonical global order | ≡ F10 by construction | — | |
| F9 SERIALIZABLE + claim table, no explicit ordering | 9/19 | serialization failures surface as behavioral errors in ten groups | |
| **F10 / GENERAL RESOURCE-LOCK ATTACK** | **19/19, first run** | nothing | ~160 LOC, ~25 min design, **0 debug iterations** |
| W check-then-insert idempotency (no unique) | 16/19 | G1+G16 at 20/20, every run | the determinism probe |

STRONGEST WRONG SOLUTION: F7 (18/19, one deterministic interaction
wrong) — exactly the "coherent partial solution" profile §9 asked for;
the candidate's failure mode inventory (wrong retry results, partial
batches, advisory/row-lock mixing, serialization leaks) is rich and
plausible. None of that compensates for F10.

CORRECT REFERENCE RESULT: F10, 19/19. REFERENCE LOC: ~160.
REFERENCE DESIGN TIME: ~25 minutes. REFERENCE DEBUG ITERATIONS: 0.
EXPERT TIME ESTIMATE: 1–3 hours end-to-end including diagnosis — under
the 4–8 h bar, from the wrong side.

## VERIFIER DETERMINISM AUDIT

Fresh-incident reset < 3 s; the D-set covers persistence/atomicity/logic
classes completely; the P-set's measured manifestation was 20/20 (W) and
60% per iteration (F6-class); harness-held row locks and in-flight peers
provide implementation-independent progress tests (they correctly killed
the global-lock design in Round 5 and passed all correct designs here).
Had the candidate survived §5, the remaining §6 obligation — proving no
materially-wrong design passes all D tests and slips the P set — would
have required per-defect-class window analysis, and the honest position
is that it can be argued class-by-class but not proven in general.

## REVIEWER SCORES

VERIFIABLE 4 · WELL-SPECIFIED 5 · SOLVABLE 5 · **DIFFICULT 2
(measured)** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 5.
DIFFICULT is disqualifying; §12's conditions make the decision
mechanical.

## What six rounds now add up to

Three task physics have been tested to destruction with real engines:
static forensics (Rounds 1–4: complete evidence collapses to
simulation, incomplete evidence collapses to ill-posedness, and the
one genuinely abductive family is the banned MVCC-outcome transplant),
live operational repair (Rounds 5–6: behavioral verification works
beautifully, but the difficulty collapses under one canonical
concurrency architecture), and every measured candidate has fallen to a
single-sitting frontier-equivalent solution. The only architecture in
this entire program that has never been measured-trivial is the V5
family itself — engine-defined outcome abduction under destroyed
authority with constraint pinning — which remains untested against a
frontier model and excluded by rule. The tournament's empirical
recommendation is therefore not "keep searching adjacent task shapes":
it is that the difficulty bar being sought lives either in (a) the
banned family, pending an explicit decision to re-admit it with
distance requirements, or (b) task shapes this program has not yet been
permitted to explore (multi-session interactive incidents, adversarial
live workloads, or proof-carrying deliverables). A seventh round of
candidate roulette inside the current constraints is not supported by
the evidence.

---

Lab disposed. Nothing implemented.
