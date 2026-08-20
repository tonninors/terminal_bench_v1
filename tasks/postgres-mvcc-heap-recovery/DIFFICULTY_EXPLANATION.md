# Difficulty Explanation — postgres-mvcc-heap-recovery (fixture v5)

## What the task asks

The solver receives the raw heap pages of three related PostgreSQL 16 tables,
the cluster's on-disk commit log (`pg_xact/`) and subtransaction map
(`pg_subtrans/`), and a schema document with the declared constraints and a
target MVCC snapshot. The cluster crashed and its WAL was lost; because
PostgreSQL writes clog pages back lazily (normally a checkpoint or SLRU
eviction does it, and crash recovery replays the WAL to close the gap), the
surviving commit log genuinely does not record an outcome for every
transaction the pages reference. The solver must reproduce, as
`/app/recovered.csv`, exactly the `accounts` rows visible under that snapshot.

## Why this is hard — the difficulty is global reconciliation

Versions 1–4 of this task were, at bottom, *decoding* tasks: hard formats
(heap pages, SLRU bitmaps, subtransaction ancestry, MultiXact geometry), but
every fact a tuple needed was present somewhere, and a careful local reader
could resolve each tuple on its own. Each version was eventually solved by a
frontier model. v5 changes the nature of the problem rather than the amount
of format:

1. **The transaction log is authentically incomplete.** 15 transactions that
   the snapshot proves *finished* have zero bits in the on-disk clog — their
   outcomes were on a clog page that never reached disk before the crash. No
   local rule resolves them: zero bits nominally mean "in progress", but the
   snapshot refutes that reading, and both of the folklore repairs
   (zero-fill = aborted; take the bits at face value) silently lose 71 of the
   199 correct rows while producing a clean-looking CSV.

2. **The direct evidence is real but deliberately partial.** Genuine hint
   bits — stamped by PostgreSQL itself during the incident history, never
   edited — pin 7 of the 15 unresolved transactions. Five of those seven
   facts sit on `ledger_entries` tuples, not on the output relation, so they
   only help a solver that treats a transaction's fate as one global fact
   (atomicity across relations). The other 8 transactions carry no hint
   anywhere in any heap.

3. **The remaining 8 outcomes are only recoverable by reasoning over complete
   candidate states.** The declared constraints (primary keys, a composite
   unique key, two foreign keys, NOT NULL, row-local CHECKs) are facts about
   the *visible state as a whole*, not about single tuples. Examples of the
   reasoning the fixture forces:
   - a hinted-committed child row whose only possible foreign-key provider is
     an unresolved insert forces that insert committed;
   - a hinted-committed re-insert of a key forces the unresolved *delete* of
     the older version committed (else the key would be visible twice);
   - two unresolved inserts of the same key cannot both have committed;
   - an error-aborted transaction left a duplicate-key tuple in the heap that
     must be recognised as impossible-to-commit.
   Dependency chains reach depth 3 (hint on W2 → forces W1 → forces W3;
   hint on W10 → forces W8 → forces W15), and the same unresolved
   transactions touch several relations, so per-relation or per-tuple
   reasoning breaks the chains.

4. **The answer is machine-proven unique — and nothing weaker pins it.**
   Exhaustive enumeration over all 2^15 = 32768 outcome assignments is part
   of fixture generation: the hint bits alone leave 256 candidates, adding
   PK/UNIQUE consistency leaves 9, and only the full constraint system
   (foreign keys included) leaves exactly 1 — which also yields exactly one
   distinct output CSV. Generation aborts if either count differs from one.
   The surviving assignment equals, transaction for transaction, the outcome
   the live server actually took, and the resulting CSV equals the state
   PostgreSQL itself reported under the target snapshot.

The v1–v4 skills are still prerequisites: page and tuple headers, line
pointers, HOT redirects, infomask semantics including lock-only xmax, SLRU
geometry for both pg_xact and pg_subtrans, subtransaction ancestry, and
correct snapshot arithmetic (xip is load-bearing: three still-open writers
have work on the pages). But a parser of v3/v4 quality now produces a
confidently wrong answer that drops 71 keys, because the challenge begins
after parsing.

## Measured failure of the natural wrong approaches

`build/measure_negatives.py` scores twelve realistic wrong strategies against
the correct 199-row answer. All twelve fail the verifier; keys off = missing
+ extra + wrong-valued + duplicated:

| strategy | keys off |
| --- | --- |
| zero-fill: missing clog = aborted | 73 |
| missing clog = still in progress | 73 |
| trust surviving clog, discard unresolved tuples | 73 |
| hint bits only, unhinted → aborted | 70 |
| fate resolved per tuple instead of per xid | 73 |
| each relation recovered independently | 73 |
| foreign keys ignored | 69 |
| PK/UNIQUE ignored | 4 (duplicate keys + resurrected work) |
| first locally valid candidate accepted | 69 |
| greedy one-hop hint propagation | 1 (duplicate key that needs the delete-forcing chain) |
| newest-xmin per key (the v1 shortcut) | 3 |
| a v3/v4-quality visibility parser with no inference layer | 73 |

Nine of the twelve are wrong on 69–73 of 199 keys; the other three are
precision traps that the exact-equality verifier always catches. The
zero-fill trap in particular produces a structurally plausible CSV (valid
header, no duplicates, 128 rows) that is confidently wrong.

## Why an expert can still solve it

Every step is standard, documented PostgreSQL knowledge plus disciplined
reasoning — nothing requires guessing:

- the heap page, tuple header and SLRU formats are in the PostgreSQL source
  and documentation (`htup_details.h`, `slru.c`, `transam.c`);
- the observation "these xids are below snapshot_xmin yet unrecorded, so the
  clog tail is stale" follows from how clog write-back works;
- hint-bit semantics (`SetHintBits`) are documented source behaviour;
- the constraint reasoning is bounded: 15 unknowns, and the hint facts cut
  the space to 256 candidates, well within exhaustive checking by hand-written
  code — no SAT solver or clever search is needed;
- the schema document states the incident and declares every constraint, and
  the prompt states that the inputs jointly determine one correct result.

An expert with PostgreSQL-internals experience should need roughly 4–8 hours:
2–3 for a correct three-relation parser and transaction-state reader (v3-level
work), 1–2 to recognise the stale tail and classify the evidence, and 2–3 to
build the candidate-state checker and verify uniqueness. It fits comfortably
inside one working day; no step depends on luck, timing, hidden files or
brute force beyond a 256-candidate loop.

v1 (210 rows), v2 (265), v3 (353) and v4 (429) were each solved exactly by a
frontier model, which is why v5 exists. v5 has not yet been trialed against a
frontier model, so no claim is made here about whether one will solve it; the
design goal is that the failure mode of a v1–v4-style solver is no longer
"decode more formats" but "recover outcomes the artifacts only determine
jointly", which defeats every local strategy listed above by construction.

## Scope control (what is deliberately excluded)

MultiXact xmax values, frozen tuples, combo command ids, out-of-line TOAST,
compressed inline datums, xid wraparound and prepared transactions are all
absent from the fixture, and the oracle refuses rather than guesses if it
meets one. The binary-format burden is intentionally held at the v3 level so
that the added difficulty is the inference layer, not more decoding.

All solver-facing data and database contents are synthetic and self-created for
this benchmark. The tables, values, transaction histories, names and dates were
invented for this fixture; nothing is copied from or derived from any external
dataset, dump, or documentation example, and the task runs with no network
access.
