# Round 10 — Opposing-authority kill test (measured)

Status: **pilot only — no Terminal Bench task implemented; D2 not started.**
Lab: live PostgreSQL 16.15. All numbers are measured runs.

## Decision up front

**B. REJECT D1 PERMANENTLY — GENERALIZED RECONCILIATION SOLVES IT.**

The hypothesis was fully instantiated and it worked at its narrow goal:
a per-cohort rollout produces genuine opposing authority eras, and every
single-rule architecture — including **Round 9's winning architecture,
unchanged** — is measurably destroyed by it. But the critical attack G
(generalized per-row/per-phase reconciliation), written by the designer
in **one pass with zero architecture revisions**, scored **11/11 on all
four checkpoints**. Per this round's own instruction, that is a
permanent rejection of D1, no fresh-context agent run, and no further
twist.

---

## What was built

**Scenario (PostgreSQL 16, same migration as Round 9).** `orders` belong
to `merchants`; merchants are assigned to rollout **waves**. The
migration normalizes `orders.shipping_address` ("street|city|country")
into `addresses` + `orders.address_id`, and the new application version
is rolled out **one wave at a time** — the ordinary
cohort/canary deployment pattern. Stage sequence: `expand → backfill →
rollout_w1 → rollout_w2 → contract` (same stage count as Round 9; the
two rollout stages *are* the cohort rollout, not padding).

**Where authority comes from (no generator fiat).** A merchant's orders
are written by the legacy app — blob only — until that merchant's wave is
switched; from then on its orders are written by the new app —
`address_id` only — and nothing reads or writes its blob again, so the
blob goes stale by ordinary deployment semantics. The switch instant is
recorded by the deployment itself in `rollout(wave, cutover_at)`, and
both application versions maintain `orders.updated_at` (the migration's
maintenance passes deliberately do not). Authority for a row is therefore
**derivable from solver-visible data**: blob-authoritative iff the row's
wave has not cut over, or the row's last write predates its wave's
cutover; normalized-authoritative otherwise. Two experts reading the
contract derive the same final state; no hidden provenance is involved.

**The incident.** The original tool was written for a single-cohort
rollout: it drops the legacy sync trigger at the *first* wave's cutover,
which strands the still-legacy cohort, and its `contract` then aborts on
the resulting NULLs. Checkpoints S0 / S2 / S3 / S4 are prefixes of one
replayed history (measured build: 500/504/512/516 orders), and S4's
contract abort is asserted by the generator, not simulated.

**Opposing authority is real in a single checkpoint (measured at S4):**

| cohort era | rows | NULL `address_id` | blob ≠ normalized |
| --- | --- | --- | --- |
| wave 1, post-cutover (normalized authoritative) | 15 | 0 | **8** |
| wave 1, pre-cutover (blob authoritative) | 243 | 0 | 0 |
| wave 2, post-cutover (normalized authoritative) | 5 | 0 | **2** |
| wave 2, pre-cutover (blob authoritative) | 253 | **4** | **6** |

Ten rows where the normalized side must win and ten where the blob must
win — in the same table, at the same checkpoint.

**Verifier (outcome-only).** Drives the *submitted package* stage by
stage, injecting deterministic cohort traffic between stages (legacy
writes for cohorts not yet switched, new-version writes for those
switched), then grades 11 behavioral checks: completion, all traffic
acknowledged, rolling window honoured, post-migration writer works,
every acknowledged write preserved, **each order shows its last
acknowledged write**, no extra orders, address dedup, stable address
ids, `address_id` populated, target-only schema.

## Attack results (all measured, keys-off = rows failing last-write-wins)

| # | attack | S2 | S4 | outcome |
| --- | --- | --- | --- | --- |
| A | blob always wins | 10/11 (**9 wrong**) | 10/11 (**8 wrong**) | killed |
| B | normalized always wins | 11/11 | 10/11 (**4 wrong**) | killed |
| C | newest physical value wins | not implementable | — | degenerate (below) |
| D | generic rebuild-current-state | ≡ A semantics + fails the rolling window (measured in Round 9's A8) | | killed |
| E | textbook expand-contract | 10/11 (**3 wrong**) | 10/11 (**4 wrong**) | killed |
| F | Round-9 fresh-agent architecture, unchanged | ≡ A: 10/11, 10/11 | | **killed** |
| **G** | **generalized per-row/per-phase reconciliation** | **11/11** | **11/11** | **solves it** |

G also passed S0 and S3 at 11/11 — **four for four, first pass, zero
debugging iterations, ~25 minutes of authoring**, and the same verdict
three times on re-run.

*On attack C:* PostgreSQL keeps no per-column write time, and the row's
`xmin`/`updated_at` record only *that* the row was last written, not by
which side. Measured schema: `orders(id, merchant_id, address_id,
updated_at)` after expand — no per-column provenance exists. So
"newest physical value wins" cannot be expressed as a distinct rule; it
either degenerates into A or becomes G by consulting `rollout`.

*On F, the point of the round:* Round 9's winning architecture — one
universal blob-authoritative reconcile — is exactly attack A here, and it
is now measurably wrong on 8–9 rows per checkpoint. The opposing-authority
construction did precisely what it was designed to do.

## Why this closes D1 rather than motivating another twist

The round asked whether opposing authority could force a genuine
architecture redesign. The measurement says: **no, because a
well-specified authority boundary is itself an architecture.** The
moment the contract states that authority follows the cohort rollout —
which requirements 4–6 *demand* it state, so that two experts agree and
no hidden history decides correctness — the correct architecture is
"derive the authority predicate, reconcile per row, never touch the other
side." That is one join and one guarded UPDATE. Making it *harder* would
require making authority *less* derivable, which immediately violates
requirements 5 and 6 and reproduces the C5 ill-posedness result from
Round 3. Adding more eras multiplies rows in the same predicate, not
architectures.

This is the same wall, now hit from the last remaining direction:
- **specified enough to be fair ⇒ the correct architecture is legible**
  (Rounds 7, 9, 10);
- **unspecified enough to be surprising ⇒ ill-posed** (Rounds 3, 4).

D1 is therefore closed permanently, and it is closed on evidence: not
because the scenario was weak — it killed five of six attacks, including
the architecture that beat Round 9 — but because the one architecture
that is *correct by construction* is also the one a competent solver
writes first.

## Determinism, specification and cost audits

- **Determinism:** no concurrency anywhere; checkpoint builds reproduce
  identical shapes; G's verdict identical on three consecutive runs.
- **No hidden history:** the ledger is a verifier artifact only; every
  fact needed to decide authority (`rollout`, `merchants.wave`,
  `orders.updated_at`) is in the solver's database.
- **No lookup table:** authority is a two-column predicate, not an
  enumeration of rows or entities.
- **Prompt would not need to explain the trick:** the contract states
  the rollout semantics and last-write-wins; it never mentions
  divergence, eras, or reconciliation.
- **Expert time:** the reference/G architecture took ~25 minutes to
  author and debug — far under the 4–8 hour target, a second independent
  disqualification.

## Reviewer scores

VERIFIABLE 5 · WELL-SPECIFIED 5 · SOLVABLE 5 · **DIFFICULT 1.5** ·
REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 4.

## Standing conclusion after ten rounds

Every architecture this program can construct that is simultaneously
deterministic, well-specified, outcome-verified and free of hidden
history has now been measured solvable — by a real frontier trial (V1–V5),
by an independent fresh-context agent (Rounds 7 and 9), or by a
single-pass designer attack (Rounds 6, 8-derived, and 10). The remaining
untested surface is not another database scenario; it is a different
*evaluation contract* (multi-session or adversarial-workload physics, or
deliverables whose correctness cannot be reached by one legible
architecture), and that is a decision for the tournament owners rather
than another candidate round.

---

Lab disposed. Nothing implemented; D2 untouched; no further twist added.
