# Version history — v1, v2, v3

Internal document. Not part of the solver-facing bundle.

V1 was solved by a frontier model, which recovered all 210 expected rows. V2
keeps the solver-facing contract byte-for-byte identical — same three input
files, same output file, same `FINAL_PROMPT.txt` — and changes only the
PostgreSQL history that produced the pages, so that more of PostgreSQL's
semantics has to be integrated before the answer comes out right.

**The task did not get bigger in any way that matters.** The relation grew from
4 blocks to 6 (32 KiB → 48 KiB) and from 210 to 265 visible rows. That is a
1.3× change in size against the difficulty changes tabulated below.

---

## v3 — the decoded transaction table is gone

v2 was still solved by a frontier model. v3 removes `tx_status.csv`, the file
that handed the solver a ready-made `xid,status` mapping, and replaces it with
the cluster's own metadata: `pg_xact/` (the commit log) and `pg_subtrans/` (the
subtransaction parent map), copied verbatim as raw SLRU segments.

The solver-facing contract is otherwise unchanged: same heap pages, same
`table_schema.json`, same `/app/recovered.csv`, same CSV requirements. Only the
sentence naming the transaction-state input changed in `FINAL_PROMPT.txt`.

### What that buys

Decoding the two files is a modest amount of bit arithmetic. The difficulty is
what they *mean*, and specifically that **`pg_current_snapshot()` exports
top-level xids only** — it does not export the snapshot's subtransaction array.
So a tuple written inside a `SAVEPOINT` carries a subtransaction id that:

* reads `COMMITTED` in `pg_xact` once its parent commits, and
* matches no entry in `snapshot_xip`, and
* therefore looks, to a solver that stops there, like ordinary finished work.

It is not. Its topmost parent was still in flight when the snapshot was taken,
and only `pg_subtrans` says so.

| | v2 | v3 |
| --- | --- | --- |
| transaction state input | `tx_status.csv` (decoded `xid,status`) | **`pg_xact/0000` + `pg_subtrans/0000`, raw SLRU** |
| visible rows (the answer) | 265 | **353** |
| blocks / heap bytes | 6 / 49152 | **8 / 65536** |
| physical tuples | 555 | **725** |
| transactions referenced by the pages | 46 | **61** |
| subtransaction xids stamping tuples | 0 | **10** |
| deepest subtransaction chain | – | **3 hops** |
| tuple stamps needing `pg_subtrans` | – | **24** |
| aborted children under committed parents | – | **3** |
| *visible* subtransactions | – | **3** |
| lock-only `xmax` needing the infomask | 33 | 33 |
| committed transactions inside `snapshot_xip` | 4 | **5** |
| committed transactions in range but outside `xip` | 5 | **10** |

### The two new failure modes, and why both are punished

A solver can get subtransactions wrong in two opposite directions, and the
fixture makes both cost keys:

| strategy | keys wrong | why |
| --- | ---: | --- |
| never open `pg_subtrans` | **18** | subxids of an in-flight parent look committed and unlisted, so their work is wrongly applied |
| resolve the parent, then use *its* commit status | **16** | savepoints that were rolled back are `ABORTED` in their own right; inheriting `COMMITTED` resurrects them |

A third guard stops a blanket rule from working: the fixture also contains
subtransactions whose topmost parent finished *before* the snapshot, so their
work **is** visible. "Has a `pg_subtrans` parent" cannot be read as "invisible".

### Every strategy, measured against v3

| # | strategy | v2 keys off | v3 keys off |
| --- | --- | ---: | ---: |
| 1 | greatest xmin per key | 132 | **189** |
| 2 | ignore xmax entirely | 96 | **127** |
| 3 | committed xmax means deleted | 104 | **128** |
| 4a | HOT redirects walked as tuples | 15 | **15** |
| 4b | heap-only versions skipped | 77 | **102** |
| 5 | aborted treated as committed | 79 | **101** |
| 6 | in-progress treated as committed | 56 | **67** |
| 7a | snapshot_xip ignored | 56 | **80** |
| 7b | every recent xid is in progress | 63 | **74** |
| 8 | hint bits used as the commit log | 144 | **179** |
| 9 | every physical tuple | 204 | **286** |
| 10 | newest committed, no header state | 73 | **103** |
| 11 | pg_subtrans ignored | n/a (new) | **18** |
| 12 | subxact inherits parent status | n/a (new) | **16** |

Every one of the fourteen is wrong on at least 15 independent primary keys, and
`build/harness_test.py` asserts that floor on every run.

### Still true in v3

* genuine PostgreSQL 16 heap pages, 8192 bytes, block-number order;
* the HOT chains, `LP_REDIRECT`/`LP_DEAD` artefacts and lock-only `xmax` cases
  v2 introduced, unchanged;
* `snapshot_xip` sparse and interleaved with committed transactions;
* no MultiXact, combo CID, frozen tuple, wraparound, prepared transaction or
  out-of-line TOAST dependency — all still asserted absent;
* everything synthetic and self-created;
* `/app/recovered.csv` the only output, verifier outcome-only.

---

## v1 to v2 — the earlier comparison

## 1. Headline numbers

| | V1 | V2 |
| --- | --- | --- |
| visible rows (the answer) | 210 | **265** |
| blocks / bytes | 4 / 32768 | **6 / 49152** |
| physical tuples on the pages | 323 | **555** |
| distinct primary keys on disk | 236 | **301** |
| keys with several physical versions | 87 | **168** |
| keys with **3 or more** physical versions | 3 | **40** |
| deepest version chain | 3 | **7** |
| keys whose visible version is strictly *inside* its chain | 0 | **12** |
| transactions in `tx_status.csv` | 19 | **46** |
| committed / aborted / in-progress | 12 / 5 / 2 | **33 / 9 / 4** |
| lock-only `xmax` tuples | 3 | **53** |
| …that actually **require** reading the infomask | **0** | **33** |
| committed transactions listed in `snapshot_xip` | 1 | **4** |
| committed transactions inside `[xmin, xmax)` but *absent* from `xip` | 0 | **5** |
| tuples with no `HEAP_XMIN_COMMITTED` hint | 46 | **126** |
| tuples from aborted transactions with no `HEAP_XMIN_INVALID` hint | 36 | **12**¹ |
| visible rows carrying a non-zero `xmax` | 47 | **173** |
| visible rows living in a heap-only tuple | 43 | **77** |

¹ lower in absolute terms but the aborted population tripled (53 tuples vs 40);
what matters is that the hints remain incomplete, which
`fixture_test.py::test_hint_bits_alone_are_insufficient` asserts directly.

## 2. How wrong each naive strategy is

Same twelve strategies, same measurement code (`build/measure_negatives.py`),
run against both fixtures. "keys off" counts primary keys that are missing,
extra, duplicated, or carrying wrong values — the metric the verifier actually
grades.

| # | strategy | V1 keys off | V2 keys off | change |
| --- | --- | ---: | ---: | ---: |
| 1 | greatest `xmin` per key | 107 | **132** | +25 |
| 2 | ignore `xmax` entirely | 16 | **96** | **+80** |
| 3 | a committed `xmax` means deleted | 80 | **104** | +24 |
| 4a | HOT redirects walked as tuples | 37 | 15 | −22 |
| 4b | heap-only versions skipped | 43 | **77** | +34 |
| 5 | aborted treated as committed | 48 | **79** | +31 |
| 6 | in-progress treated as committed | 37 | **56** | +19 |
| 7a | `snapshot_xip` ignored | 20 | **56** | +36 |
| 7b | every recent xid treated as in progress | **9** | **63** | **+54** |
| 8 | hint bits used as the commit log | 116 | **144** | +28 |
| 9 | every physical tuple | 113 | **204** | +91 |
| 10 | newest committed, no header state | 69 | **73** | +4 |

Two entries deserve comment.

**Strategy 7b was the hole in V1.** Treating every xid at or above
`snapshot_xmin` as still running was wrong on only **9** keys in V1 — close
enough that a solver never had to understand that `snapshot_xip` is a *set*
rather than a range boundary. In V2 it is wrong on 63.

**Strategy 4a is the one number that went down**, from 37 to 15, because fewer
chains get pruned in V2 (see §3) so there are fewer `LP_REDIRECT` slots to
mishandle. It still fails — 15 duplicated primary keys — and the substantive
HOT failure mode moved into 4b, which nearly doubled. The two together cover
both directions of getting HOT wrong.

## 3. The three mechanisms that produced the change

### a. A horizon holder, so physical history survives

V2 opens a read-only `REPEATABLE READ` session part-way through generation and
keeps it open until capture. It is never assigned an xid, so it appears in no
snapshot and in no commit log — but it pins the vacuum horizon, and from that
point PostgreSQL prunes nothing.

Everything downstream follows from that one change:

* **HOT chains survive.** V1 had 3 keys with 3+ versions and a deepest chain of
  3, because pruning collapsed them as fast as they were made. V2 has 40 keys
  with 3+ versions and chains up to 7 deep, and for 12 keys the visible version
  sits strictly *inside* the chain — neither the oldest nor the newest tuple.
* **Aborted versions stay on the page** instead of being reclaimed: 53 tuples
  from aborted transactions, and for 47 keys the greatest-`xmin` tuple is one of
  them. That is why strategies 1 and 5 got worse.
* **Lock-only `xmax` becomes load-bearing.** This is the important one. In V1 all
  3 lock-only tuples had `HEAP_XMAX_INVALID` set, because pruning ran over those
  pages after the locker finished and `HeapTupleSatisfiesVacuum` stamps that hint
  on a lock-only tuple whose locker is gone. A solver could therefore honour
  `HEAP_XMAX_INVALID` alone — or even just get lucky — and never learn that
  `HEAP_XMAX_LOCK_ONLY` exists. In V2 **none** of the 53 lock-only tuples carries
  `HEAP_XMAX_INVALID`, and 33 of them name an `xmax` that both committed and is
  visible to the target snapshot. Reading `xmax` without the infomask now deletes
  those rows outright.

  The locking transactions are deliberately run *after* the horizon holder opens
  but *before* any concurrent writer, so their xids land below `snapshot_xmin` —
  plainly visible, no snapshot escape hatch.

### b. `snapshot_xip` interleaved with committed transactions

V1's snapshot was `770:774:770,771,772` — the in-progress list was a contiguous
tail of the xid range, so "anything ≥ `snapshot_xmin` is still running" was a
near-perfect substitute for reading the list.

V2's snapshot is `781:795:781,783,785,787,788,790,791,792,793`. Nine writers were
opened *interleaved* with five transactions that committed between them, so
782, 784, 786, 789 and 794 are committed xids sitting inside `[xmin, xmax)` and
absent from `xip`; their work is visible. Meanwhile 781, 783, 785 and 787 are in
`xip` and later committed; their work is not. 788 is in `xip` and aborted.

The two failure directions are now both punished, and
`fixture_test.py::test_snapshot_xip_is_interleaved_with_committed_transactions`
asserts the interleaving rather than merely the counts.

### c. Several mechanisms per key

V1 largely gave each primary key one job. V2 stacks them, so no single rule
reproduces a row. Representative groups:

| group | history | what the solver must combine |
| --- | --- | --- |
| `B_commit_abort_lock` | committed update → aborted update → committed `FOR UPDATE` | the live version has a committed `xmin`, a **committed lock** in `xmax`, and a dead successor holding the greatest `xmin` on the key |
| `B_abort_then_commit` | aborted update → committed update | the discarded attempt outranks the kept one by `xmin` |
| `A_upd_then_lock` | committed update → committed lock of the new version | `xmin` and `xmax` name different committed transactions and the row is still live |
| `B_presnap_chain` | two committed updates, then three more after the snapshot | 6 versions, visible one in the middle |
| `C_xip_lock_c` | locked by a writer that commits *after* the snapshot | lock-only **and** `xip` |
| `A_churn_long` | 22 pruned HOT rounds, then extended again after the snapshot | an `LP_REDIRECT` root plus a live chain whose visible member is neither end |

## 4. What did *not* change

* `FINAL_PROMPT.txt` — byte-identical, and re-verified against the new fixture by
  `build/final_audit.py` (every statement it makes still holds, and every
  requirement is still enforced by a verifier check).
* The solver receives exactly `/app/heap_pages.bin`, `/app/tx_status.csv`,
  `/app/table_schema.json` and writes exactly `/app/recovered.csv`.
* `tx_status.csv` keeps the `xid,status` schema with the same three states.
* `table_schema.json` keeps the same keys, the same seven columns and the same
  types.
* The verifier is unchanged in kind: outcome-only, row-order independent, and it
  still accepts any correct rendering (both alternate constructions still pass).
* Still excluded and still asserted absent: out-of-line TOAST, MultiXact `xmax`,
  combo CIDs, frozen tuples, wraparound, prepared transactions.
* The `/app/evidence/` duplicate copies were **removed**, so `/app` now holds
  exactly the three files the prompt names.

## 5. Honest limits

* Strategy 4a is the weakest remaining trap at 15 keys. It still fails, and it is
  a duplicate-key failure the verifier catches unambiguously, but a solver that
  mishandles redirects and nothing else is closer to correct than one that
  mishandles any other mechanism.
* This is a difficulty *increase*, not a proof of unsolvability. No frontier-model
  trial was run against V2 in this environment; the evidence here is the measured
  divergence of twelve independent wrong strategies, not an observed model
  failure.
