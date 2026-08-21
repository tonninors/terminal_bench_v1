# Round 7 — Blind root-cause diagnosis pilot (measured)

Status: **design round only — nothing implemented.**
Lab: live PostgreSQL 16.15. Every number is a measured run. This round
contains the least-designer-biased attack of the entire program: an
independent fresh-context agent with only the ticket and terminal access.

## Decision up front

**B. REJECT — DIAGNOSIS TOO EASY.**

The incident was built exactly to the round's specification — authentic
composite causes from one operational event, symptom-only prompt,
deterministic behavioral verifier, measured misdirection, measured
interdependence — and it worked precisely as designed against static
checks: baseline 2/11, obvious fix 4/11, cause-1-only 7/11 with the
diagnostic landscape visibly changing, reference 11/11. Then the §8
attack ran: an **independent fresh-context agent** (same model class as
the frontier, zero knowledge of the incident, generator artifacts
scrubbed from the container) received only the blind ticket and terminal
access. It traversed the designed misdirection path — four plausible
wrong hypotheses eliminated by catalog checks — found both root causes,
repaired the accrued damage, proactively fixed two *latent* faults the
ticket never surfaced, added integrity assertions, self-verified all
five behaviors, and delivered a repair scoring **11/11 on a fresh
incident copy** — in **3 minutes 21 seconds and 12 tool invocations**.
Its repair was strictly more thorough than my reference. Under §15's
standard this is as close to an independent frontier result as the
program can produce locally, and it is decisive.

---

ENGINE + VERSION: PostgreSQL 16.15 (stock image).

REAL-WORLD INCIDENT STORY: a storage outage two weeks ago; the on-call
team restored from a logical dump and manually re-imported the missed
transactions from application logs, following the widely-blogged bulk-
load recipe of putting the loading role into replica mode — and never
reset it.

COMPONENT CAUSE 1: `ALTER ROLE shop_app SET session_replication_role =
'replica'` left in place — every application session since runs with all
user triggers (audit, denormalized-total maintenance) **and** the
internal foreign-key enforcement triggers disabled. A documented,
famous footgun; catalogs look perfectly healthy (`tgenabled='O'`,
constraints validated), and a superuser hand-testing triggers sees them
fire — only the app role is poisoned.

COMPONENT CAUSE 2: the backfill inserted rows with explicit ids, so the
id sequences were left behind the table maxima — signups fail with
duplicate-key immediately; orders carry the same fault latently.

COMPONENT CAUSE 3 (accrued damage, not an independent fault): while
enforcement was off, orphan line items entered via the backfill CSV
(referencing products removed between export and load), the nightly
cleanup deleted a still-referenced product, totals drifted on 13 orders,
and the audit trail went silent.

WHY THEY PLAUSIBLY COEXIST: one restore event explains all three —
replica-mode loading is *the* standard recipe for exactly this kind of
backfill, explicit-id imports are how log-derived rows are reloaded, and
the damage class is the mechanical consequence of the first cause. No
component exists to defeat a model; a professional could meet this
combination after any rushed logical restore.

SOLVER-FACING SYMPTOMS: signups fail with `customers_pkey` duplicates
while order creation works; order totals drift since the restore; the
audit table records nothing though its configuration "looks unchanged";
reports show line items for products that no longer exist, and the
catalog cleanup deleted a referenced product.

DRAFT BLIND PROMPT: written as a production ticket (full text preserved
in the round's working notes and the agent transcript); ~200 words,
symptoms + five required behaviors + "the app connects as shop_app" +
deliverable path. Leak audit: no mechanism vocabulary (no trigger/
replication/sequence/FK terminology beyond raw symptom strings).

LIVE INITIAL STATE: 70 customers, ~17 products, 43 orders, ~110 line
items, 283 audit rows (all pre-incident), 11 orphan items, 13 drifted
totals, sequences at 50/35/100 vs maxima 70/1008/5024 — every damaged
byte produced by real sessions under the authentic settings; the
generator performs no direct state surgery.

REQUIRED FINAL BEHAVIOR: signups work; totals correct everywhere and
maintained under new writes; new changes audited; no orphan items
(policy stated: drop them) and referenced-product deletion refused;
all existing data and audit history preserved.

DELIVERABLE: `/tmp/repair.sql`, applied once by a superuser to a fresh
incident copy; behaviors then checked from new application sessions.
Outcome-only; any equivalent repair passes.

OBVIOUS DIAGNOSIS ATTACK: the duplicate-key signup symptom aliases
"sequence desync after import" (the single-cause diagnosis every DBA
forum suggests); the audit/total symptoms alias "broken triggers."

OBVIOUS FIX RESULT (measured): sequence resync alone → **4/11** (both
signup behaviors pass; the system looks substantially healthier; seven
behaviors still fail). Trigger recreation alone → **2/11** (no change —
and the recreated triggers fire for the superuser's own hand tests,
deepening the misdirection). Requirement 7 satisfied exactly.

SENIOR-DBA ATTACK (§8, reduced designer bias): independent
fresh-context agent, ticket + terminal only, generator files deleted
first, no verifier access, free to experiment (grading against a fresh
copy).
TIME TO FIRST HYPOTHESIS: under one minute (first catalog query).
TIME TO COMPLETE FIX: **3 min 21 s wall-clock** (12 tool uses,
~59k tokens), including self-verification of all five behaviors.
WRONG HYPOTHESES: 4, in order — disabled triggers (`tgenabled`),
dropped/NOT VALID constraints (`convalidated`), broken trigger
functions (`prosrc`), shadow schema/search_path — each eliminated with
one catalog query; the role config was spotted during hypothesis 4's
check.
REPAIR ITERATIONS: 2 (its first draft used `VALIDATE CONSTRAINT` as a
proof step; it noticed the no-op itself and replaced it with explicit
assertions). Its final script also fixed the *latent* orders/items/
audit sequence desyncs the ticket never mentioned, guarded setvals with
`greatest()`, and proactively verified grants.
FINAL VERIFIER SCORE: **11/11 on a fresh incident copy** (re-measured
independently by me).

INTERDEPENDENCE TRACE (measured, natural): initial 2/11. After only
`ALTER ROLE ... RESET`: 7/11 — totals maintenance, auditing, and FK
refusal all return for new sessions, and operations that silently
"worked" before (deleting referenced products) now fail loudly: the
diagnostic landscape genuinely changes, surfacing the accrued-damage
causes. No hidden flags; pure engine behavior.

RESET ATTACK: void — recreating the database loses seeded customers,
orders, and audit history, failing V10/V11; a dump/reload preserves the
damaged data and (via dumpall globals) the poisoned role setting.

STOCK-TOOL ATTACK: none applies — vacuum/reindex/analyze/amcheck touch
nothing relevant; no single diagnostic command names all causes
(`pg_db_role_setting` reveals cause 1 only).

DETERMINISM AUDIT: fully deterministic — zero concurrency anywhere;
identical symptom counts across repeated rebuilds (283/13/11 every
time); incident rebuild ≈ 5 s; verifier verdicts reproduce exactly.
The best verifier profile of any candidate in seven rounds.

PROMPT COMPLEXITY AUDIT: pass — a ~200-word ticket a solver understands
in two minutes; five behavior groups; no edge-case wall.

EXPERT TIME ESTIMATE: the design target was 4–8 h; the measured
strong-solver time is **~3.5 minutes**, and a competent human senior
DBA familiar with the replica-role footgun would plausibly need well
under an hour. The gap between design target and measurement is the
rejection.

REVIEWER SCORES: VERIFIABLE 5 · WELL-SPECIFIED 5 · SOLVABLE 5 ·
**DIFFICULT 1** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 3.

FRONTIER-PILOT PLAN: not packaged — decision B. The §8 attack already
approximates an independent frontier trial (fresh context, zero
incident knowledge, same model class); an external pilot of this
incident would spend budget to confirm a measured result.

## Why this closes the composite-blind-diagnosis direction, not just this incident

The round's requirements pull against each other in a way this pilot
makes measurable. Requirement 3 demands every component be *authentic
and independently documented*; requirement 9 demands the misdirection
arise *naturally*. But a documented failure mode carries its documented
signature, and the frontier has absorbed the entire troubleshooting
literature: "triggers exist but don't fire" retrieves the
session_replication_role footgun as a standard checklist item;
"duplicate key after import" retrieves setval. The designed aliasing
*worked* — the agent really did walk through four wrong hypotheses —
but each elimination cost one catalog query, so the walk took minutes,
not hours. Hardening by adding more documented components only extends
the checklist walk linearly; hardening with undocumented components
violates authenticity. For incidents whose evidence is fully inspectable
in catalogs and whose component faults live in public troubleshooting
lore, blind composite diagnosis is a **retrieval task wearing a
detective costume**. What the pilot did validate — and what survives
this rejection — is the verifier physics: symptom-only prompting,
behavioral outcome-only grading, natural interdependence, and full
determinism all worked exactly as specified, and the fresh-context
independent attack protocol produced the most trustworthy difficulty
measurement of the program. Any future direction should keep both.

---

Lab disposed. Nothing implemented; no new candidate family started.
