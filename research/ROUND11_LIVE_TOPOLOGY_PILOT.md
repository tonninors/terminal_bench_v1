# Round 11 — D2 live topology kill test (measured)

Status: **pilot only — no Terminal Bench task implemented.**
Lab: two real PostgreSQL 16.15 clusters in one container, fully local, no
network required by the solver. All numbers are measured runs.

## Decision up front

**C. REJECT — ONE-SHOT SETUP SOLVES IT.**

The topology pilot cleared every earlier gate: the environment is a real
multi-process replication topology, verification is fully deterministic,
and **all three stock recipes fail** (3/8, 3/8, 4/8). But the §8 attack —
the strongest clean package a competent PostgreSQL SRE writes after
reading the ticket once — scored **8/8 on its first run**, with no
observe-and-react cycle, and reproduced 8/8 on two further full
rebuild-and-verify cycles. Per this round's instruction that is a
rejection, so no fresh-context pilot was run, nothing was hardened
afterwards, and D2 is closed.

---

ENGINE / VERSION: PostgreSQL 16.15, two clusters (`initdb` ×2) on ports
5433/5434 over unix sockets — the smallest topology that exercises the
physics without docker networking.

TOPOLOGY: **A** = production primary (`shop`), **B** = intended new
primary, logical subscriber of A via publication `pub_shop` / subscription
`sub_shop`; role `app` is the application principal on both.

PROFESSIONAL SCENARIO: a planned switchover onto B. The rollout was
prepared weeks earlier and has rotted in three ordinary ways — all
produced by real operations, none by fiat.

INITIAL LIVE STATE (measured after rebuild, identical every time):
- `sub_shop` is **disabled**: an operator inserted a test row directly on
  B (`customers.id = 201`) that later collided with a genuine replicated
  row, and `disable_on_error` stopped replication (measured
  `subenabled = f`);
- `order_items` was created after the publication and was never added to
  it → B holds **0 of 600** rows for that table, silently;
- B's sequences are untouched by logical replication (`customers_id_seq`
  `last_value = 1` against 201 rows) — the classic post-switchover
  primary-key collision waiting to happen;
- A: customers=201 orders=300 items=600; B: 201 / 300 / 0.

FINAL BEHAVIORAL CONTRACT (observable only, no technique named):
production writes on A keep working while B is prepared; B converges to A
before failover; the failover runbook the solver supplies completes; all
committed data is present on B exactly once; new and dependent writes on
B succeed after failover without identifier collisions; the old primary
no longer accepts authoritative writes; no duplicated records.

DELIVERABLE: `/app/setup.sh` (preparation, run before production traffic)
and `/app/promote.sh` (the failover runbook, executed **by the verifier**
at cutover). The split is natural — SREs prepare, then a runbook is
invoked at the failover window — and it is what makes sequence
advancement and fencing possible at the only moment they are correct.

DETERMINISTIC FAILOVER SCRIPT: the verifier owns the sequence — run
`setup.sh` → 15 scripted writes on A → bounded convergence wait (poll a
content fingerprint, ≤120 s, no fixed sleeps) → run `promote.sh` → post
failover writes on B → fence check on A → 8 outcome checks.

RESET / STOCK ATTACK RESULTS (all measured):

| stock recipe | score | why it dies |
| --- | --- | --- |
| A/F re-enable the stalled subscription | 3/8 | conflict recurs; B stays at 201/300/0 |
| C drop + recreate subscription with `copy_data` (the documented recipe) | 3/8 | copies 206/305 but **still 0 order_items** (unpublished), sequences collide, A unfenced |
| F promote the most up-to-date node as-is | 4/8 | missing table, id collisions |

Gate §7 passed: no stock recipe satisfies the contract.

ONE-SHOT SETUP ATTACK (§8, the decisive measurement): a single-pass
package — publish every base table missing from the publication (catalog
diff), discard local divergence and re-seed the subscriber, wait for
`pg_subscription_rel` to report every table streaming; then at promotion:
drop the subscription *before* fencing (so the slot can be removed),
`setval` every serial sequence from `max(pk)`, `ALTER SYSTEM SET
default_transaction_read_only` + reload on A. **8/8 on the first run**,
~20 minutes to author, zero iterations.

INTERACTIVE STATE TRACE — the finding: the pilot *does* contain genuine
state dependence (which tables are unpublished, how far B lags, what the
sequence high-water marks are after the verifier's writes, the ordering
constraint that the slot must be dropped while A is still writable). But
**every one of those states is generically introspectable at runtime from
inside a script**: a catalog diff, a `pg_subscription_rel` poll, a
`max(pk)` query, and a fixed ordering. The solver never has to observe an
outcome, form a judgement, and choose differently — the script computes
the reaction. That is the difference between *reactive code* and the
*human observe→decide→revise loop* this round was hunting for, and only
the former is present.

PLAUSIBLE WRONG STRATEGY / FORWARD-RECOVERY TRACE: `stock_resub` is the
professional wrong strategy — it is the documented recipe, it changes the
live topology, it *looks* healthy (B reaches 206/305, replication
streaming), and it fails only at the contract level (missing table,
collisions, no fencing). Recovery is forward and cheap: add the table to
the publication, refresh, advance sequences. Cheap forward recovery is
realistic, but it also means a wrong first move costs a solver almost
nothing — the §10 requirement is satisfied in letter and undermined in
spirit.

DETERMINISM AUDIT: **passes cleanly** — no scheduler races, no fixed
sleeps, no latency thresholds; convergence is a bounded poll on content
equality; topology rebuild is byte-stable in shape (201/300/0 and
206/305/605 every run). Verdicts reproduced exactly across repeated
full cycles: one-shot 8/8, 8/8, 8/8; stock_resub 3/8, 3/8. This is the
strongest determinism result of any live-environment candidate in the
program, and it is worth recording as the one durable asset from D2.

DESIGNER RESULT: stock recipes 3/8, 3/8, 4/8 → gate passed; one-shot SRE
package **8/8 first run** → gate failed.

FRESH-CONTEXT RESULT: **not reached** — §11 conditions the fresh-context
pilot on the designer attacks failing to kill the candidate. They did not
fail. WALL CLOCK / TOOL CALLS / ARCHITECTURE REVISIONS / FINAL SCORE: not
applicable.

EXPERT TIME: ~20–40 minutes for a competent PostgreSQL SRE (the one-shot
package took ~20 minutes to write and needed no debugging) — far below the
4–8 hour target, an independent disqualification.

REVIEWER SCORES: VERIFIABLE 5 · WELL-SPECIFIED 5 · SOLVABLE 5 ·
**DIFFICULT 1.5** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 4.
DIFFICULT below 4 ⇒ reject under §12, and the standing bar (majority
frontier trial failure, ≥2/3 Gemini failures) is not remotely plausible
for a scenario the designer one-shots.

## What Round 11 adds to the record

D2 was the last unexplored direction from Round 8's mining, and it fails
for the *same structural reason* as Rounds 9 and 10, now confirmed in a
third task physics:

> When every fact the solver needs is derivable at runtime from the
> system itself — as it must be, for the task to be fair and free of
> hidden state — then "inspect and react" is expressible as ordinary code,
> and a competent solver writes that code once. Live multi-process
> topology adds *operations* (start, stop, reconfigure, fail over) but it
> does not add *irreducible judgement*.

The three physics tested — static forensics (V1–V5), long-horizon
stateful migration (Rounds 9–10), and live topology operation (Round 11) —
have now all been measured solvable by a single competent pass, against
real engines, with the difficulty claim held to measurement rather than
intuition throughout. Every direction Round 8's public-benchmark mining
identified has been tested and closed.

---

Lab disposed. Nothing implemented; nothing hardened after the rejection;
no new candidate family opened.
