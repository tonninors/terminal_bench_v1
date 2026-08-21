# Round 9 — Long-horizon migration pilot (measured)

Status: **pilot only — no Terminal Bench task implemented; D2 not started.**
Lab: live PostgreSQL 16.15. All numbers are measured runs. This round
contains both the strongest positive signal since V5 *and* a clean
rejection under the round's own gating question.

## Decision up front

**E. REJECT — FRESH AGENT SOLVES IT TOO EASILY** (as instantiated), with
one precisely-scoped finding that should survive: the *physics worked* —
for the first time in nine rounds the task genuinely forced an
execution/debug loop — but §18's specific bar ("later state invalidates a
locally plausible earlier **architecture**") was not met: the fresh
agent's very first architecture survived unchanged to a perfect score in
13 minutes 46 seconds. Loop: yes. Architecture invalidation: no.

---

ENGINE / VERSION: PostgreSQL 16.15, no network required by the solver;
everything local and pinned.

REAL INCIDENT STORY: a rolling expand→backfill→compat→cutover→contract
migration normalizing `orders.shipping_address` ("street|city|country"
blob) into a deduplicated `addresses` table + `orders.address_id`. The
production tool crashed during cutover (its `VALIDATE CONSTRAINT` failed),
and finance had already noticed rows whose normalized address disagreed
with the legacy app's last write.

MIGRATION GOAL / INITIAL PARTIAL STATE: the crash state (S4) is produced
by the tool's own execution against scripted dual-writer traffic — 86
NULL and 15 stale `address_id` rows, both sync triggers already dropped
by the cutover script, a stranded `NOT VALID` constraint, stage
bookkeeping still saying `compat`. No catalog surgery anywhere; every
state is the tool's own work.

MIGRATION PACKAGE (solver-visible): `/app/migration/` — the ORIGINAL
buggy tool (`migrate.py`, `sql/{expand,compat,cutover,contract}.sql`) plus
`make_checkpoint.py Sk`, a sanitized deterministic replay tool that
rebuilds `prod` at any supported checkpoint (the solver's execution-loop
enabler; spoiler-scrubbed, verified zero leak strings).

CHECKPOINT STATES: S0 (not started), S2 (backfill interrupted mid-batch +
later legacy traffic), S3 (compat active, both writers ran), S4 (the
crash state). All four generated as prefixes of ONE real history.

BETWEEN-STAGE WRITES: the verifier drives the submitted package
stage-by-stage (`migrate.py next`), injecting deterministic legacy-writer
batches while the package reports a pre-cutover stage and new-writer
batches from compat on — the §5 rolling-compatibility dimension, no
randomness anywhere.

BUSINESS CONTRACT (compact, 6 clauses): completion; last-write-wins
preservation of every acknowledged write exactly once; rolling window
(legacy writer keeps working ≥2 stages when entering at S0/S2; window
already served by history for S3/S4); address dedup + stability of
pre-existing `address_id` content; target-only final schema with the
constraint validated; safe re-run/no-op after completion.

REFERENCE MIGRATION ARCHITECTURE: idempotent stages; fixed forward
trigger (upsert + unconditional id assignment); cursor-free reconciling
backfill; compat reinstalls both triggers (S4 entry); cutover repairs
NULL **and stale** mappings before validating; guarded contract;
schema-derived completion detection.

REFERENCE EXPERT TIME: authoring + debugging the reference took the
designer one failed pass plus one fix iteration (~45 min total); an
independent human expert estimate is ~2–4 h — **below the 4–8 h target**
(a secondary strike, decision-letter G-adjacent, subsumed under E).

GENERIC REBUILD ATTACK (§8, measured): one-shot
introspect-normalize-swap package. S0: crashes (0/1). S2: **killed by the
rolling-window clause** (legacy-ok stages = 0 → 10/11). S3/S4: 11/11
(legitimately — the window is history there). As ONE package across all
checkpoints it fails → **gate passed; not decision B.** The kill is
structural (the business window), not a word-ban.

TEXTBOOK EXPAND-CONTRACT ATTACK (§9, measured): the designer's one-pass
textbook package with idempotent guards scored **11/11, 10/11, 9/11,
9/11** — failing S2/S3/S4 on V5 ("order resolves to its last-written
address", wrong=15) because repair-NULLs misses **stale** non-NULL
mappings left by the trigger's dedup bug on update-with-reuse. One
verifier-driven debug iteration (add reconcile-stale) → 11/11 × 4.
**Gate passed; not decision C — the designer himself could not one-pass
it** (the inverse of Round 6's F10).

STATE-MACHINE ATTACK (§10): architecture ≡ the textbook package
(detect stage → run transition); its measured first-pass failure is the
state-machine result: the *transitions the state machine knows* are not
the transitions the damage requires. **Gate passed; not decision D.**

LOCAL CHECKER INFORMATION LEAK AUDIT (§13): no verifier or ledger is
solver-visible; `make_checkpoint.py` is the only power tool, and its
failures are as informative as the solver makes them (the fresh agent
had to build its own ledger-replay harness to get informative failures —
that construction consumed a large share of its effort). No one-command
defect list exists.

LONG-HORIZON DEPENDENCY TRACE (measured, all natural): the forward
trigger's ON CONFLICT/RETURNING bug (planted, documented class) produced
two damage species — NULLs (visible at backfill) and **stale mappings**
(visible only through last-write-wins checks after resume); the original
backfill's **data-modifying-CTE snapshot bug** (not consciously planted —
discovered by the fresh agent, and real: same-statement UPDATE cannot see
the CTE's inserts) silently skipped most of batch 1 and is the true
reason cutover's VALIDATE failed at scale; the cutover script's early
trigger drop broke legacy writes in the crash state itself; contract's
self-destroying stage marker made the original tool's completion state
unrepresentable. Four interacting defect families from two seeds — the
emergence is the best evidence the scenario physics are honest.

DETERMINISM AUDIT: zero concurrency anywhere; every checkpoint rebuild
byte-reproducible in behavior (counts 625/438, 635/549 identical across
all rebuilds); verifier re-runs on the same package produced identical
verdicts (measured 3× on S4).

OUTCOME-ONLY VERIFIER DESIGN: grades only the submitted package
directory, by executing it against fresh checkpoints with interleaved
traffic and then checking final behavior (11 checks incl. ledger
last-write-wins, dedup, id stability, target-only schema, re-run no-op).
One fairness bug was found and fixed during the pilot: traffic must be
injected only AFTER the package's first step, or the verifier grades
pre-existing incident damage the solver has not yet been allowed to
touch.

DESIGNER ATTACK RESULTS: A8 fails (structural), A9 first-pass fails 3/4
then 4/4 after one iteration, A10 ≡ A9. Designer one-pass failure is a
first for this program.

FRESH-CONTEXT ATTACK RESULT (§17, the decisive measurement):
independent agent; ticket + original package + checkpoint tool +
terminal only; verifier, ledger, solutions, and design notes scrubbed.
TOOL CALLS: **25**. WALL-CLOCK: **13 m 46 s** (~109k tokens).
FIRST SUCCESSFUL CHECKPOINT: its first complete harness run passed all
four simultaneously.
FAILED MIGRATION ATTEMPTS: 0 against checkpoints (3 fix-test iterations
total: 1 package syntax slip, 2 harness/环境 quoting issues — its own
count, honestly reported).
ARCHITECTURE REVISIONS: **0** — its opening architecture (idempotent
reconcile-based stages, blob-authoritative divergence repair with an
explicitly argued authority rule) was correct from the start.
FINAL SCORE: **11/11 on all four checkpoints** under the hidden verifier
against freshly generated incident states.
Beyond solving, it out-diagnosed the designer: found the unplanted CTE
bug (D2), the contract self-destruction (D5), articulated the exact
authority argument for blob-wins, identified the one in-principle
unrecoverable corner (post-crash trigger-less new-writer updates — absent
from the history) and even reasoned that grading must rebuild checkpoints
with the *original* tool because a fixed tool cannot reproduce the S4
assertion.

REVIEWER SCORES: VERIFIABLE 5 · WELL-SPECIFIED 4 · SOLVABLE 5 ·
**DIFFICULT 2.5** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 5.

## What the verdict means (and does not mean)

The Round-8 hypothesis is **half-validated**. The interleaved-traffic
multi-checkpoint design genuinely changes task physics: one-shot
solutions die structurally, the designer could not one-pass his own
scenario, and the fresh agent — for the first time in this program —
engaged in real engineering (state dumps, self-built replay harness,
iterative verification) instead of a retrieval walk. Effort scaled ~4×
over Round 7 on every metric (time, calls, tokens).

But §18 asked for more than a loop: it asked for later state that
**invalidates a locally plausible architecture**, and the measurement is
unambiguous — a strong agent's first architecture (reconcile-everything
idempotently, argue one authority rule, verify against all checkpoints)
absorbed every hazard without revision. The single master-stroke that
made this possible is the *universal blob-authoritative reconcile*: one
rule repairs every divergence species in this scenario. Any hardening
worth attempting would have to make **no single authority rule
universally correct** (row eras with opposite authority, where applying
either rule globally fails the other era's checks) — that is the one
lever this pilot leaves untested. Absent an appetite for exactly that
experiment, the honest reading of nine rounds stands: within
deterministic, well-specified, outcome-verified single-session tasks,
a frontier-class agent's systematic engineering closes every gap this
program has been able to construct, and difficulty claims should only
ever be made on the basis of measured frontier failure rates, never
design intuition.

---

Lab disposed. Nothing implemented; D2 untouched.
