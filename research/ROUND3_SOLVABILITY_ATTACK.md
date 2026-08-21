# Round 3 — Solvability attack + frontier-equivalent attack

Status: **design round only — nothing implemented.**
Labs: Debian bookworm (SQLite 3.40.1, Python 3.11) and a real Cassandra
4.1 single node. Every load-bearing claim below is measured; scope caveats
are stated where a measurement was bounded.

## Decision up front (required by §11)

**E. NONE ARE STRONG ENOUGH — CONTINUE RESEARCH.**

C5 is rejected by a measured two-horn pincer (§1–§3): its fault model is
either under-specified — in which case it is *provably* unsolvable, shown
by a byte-identical two-world construction — or fully specified, in which
case a 15-line replay solver reaches golden in 0.02 s. The lost-segment
finalist is rejected (§6): granting the frontier agent watermark semantics
makes the intended reconstruction mechanical, and the bespoke harness risk
stands. The Cassandra zombie candidate is rejected on its own empirical
criterion (§7): a ~100-line per-cell reducer over `sstabledump` output
reproduces the engine's answer exactly. The additional candidate searched
under §9 also fails. No survivor blocks are provided because there are no
survivors; per the round's instruction, no candidate is chosen merely to
make progress. §9.3 states what Round 4 must change.

---

## 1. C5 — formal specification audit (§1)

Every assumption the Round-2 C5 oracle would have used, classified:

| # | assumption | class |
| --- | --- | --- |
| 1 | number of corruption events (per night / total) | **C — hidden** |
| 2 | each event flips bits within a *single* stored value | **C — hidden** |
| 3 | corrupted regions are value payloads only (never headers/serial types) | **C — hidden** (the solver can observe checkers pass, but the *restriction* of the hypothesis space to payload bytes is generator fiat) |
| 4 | one value is corrupted at most once | **C — hidden** |
| 5 | corruption occurred between snapshot captures, never mid-copy | **C — hidden** |
| 6 | no legitimate application write is unlogged | **B at best** — must be asserted by the prompt for the logged window; **impossible to assert honestly for pre-window nights** (the log was rotated, not absent) |
| 7 | corruption did not predate the oldest supplied snapshot | **C — hidden** (nothing in any artifact bounds it) |
| 8 | the audit log itself is undamaged and complete for its window | **B** — prompt assertion |
| 9 | a corrupted value may later be overwritten by a logged write | inferable in principle (**A**), but only *given* #1–#7 |

Assumptions #1–#5 and #7 are category C. Per the round's rule — *"C is
forbidden; if any required assumption is C, C5 is currently UNSOLVABLE"* —
C5 as designed is unsolvable. Could the prompt state them all (promote C to
B)? Yes, textually: "exactly K single-bit events, value-payload bytes only,
at most one per value, strictly between captures, none before S1." That
prompt is a **contrived bit-flip puzzle specification** — it describes the
generator, not an incident — and the round's own criterion then rejects it.
Both branches terminate in rejection.

## 2. C5 — "true state" without hidden authority (§2), measured

The killing measurement (SQLite 3.40.1, byte-level):

Two world-histories were constructed over an identical S1:

* **World H1**: the application legitimately updates `id=7` (100→36) and
  `id=8` (100→77) in one ordinary transaction on a night whose audit log
  was later rotated away. S2 captured.
* **World H2**: the application updates only `id=8` (100→77); afterwards a
  hardware fault flips one bit of `id=7`'s stored integer (100→36). S2
  captured.

Result:

```
S2_worldH1.db  integrity ok  id7=36 id8=77  header counters {3, 3}
S2_worldH2.db  integrity ok  id7=36 id8=77  header counters {3, 3}
S2 byte-identical across worlds: True   (equal SHA-256)
TRUE state H1: id7 = 36        TRUE state H2: id7 = 100
```

The two worlds produce **byte-identical solver-visible artifacts** with
**different true states**. This is an information-theoretic result: no
solver — human, frontier, or oracle — can be correct in both worlds. Note
the construction also neutralizes the subtler engine-native channel I had
hoped to lean on (the file change counter): a legitimate same-night write
equalizes the counters, and the in-place flip equalizes the page bytes.
"True current state" therefore has no objective, solver-derivable meaning
for any transition not covered by the log. The truth exists only in
generation history — exactly what §2 forbids. **C5's truth definition
fails.**

## 3. C5 — frontier-equivalent solver attack (§3), measured

A small authentic prototype was built (five snapshots, JSON audit log with
values, poison injected into the live file between captures, a logged
read-modify-write depending on the poisoned value — the Round-2 "hard last
mile" case).

* **Attack A** (dump / diff / replay): on the only honestly-specifiable
  variant (log covers every supplied transition), the attack reduces to
  *replay S1 + log*. Measured: **~15 effective lines, 0.021 s, exactly 1
  candidate state, reaches golden.** The snapshots S2–S5 are redundant;
  even the flip-before-dependent-write chain is handled *automatically*
  (the poisoned 100→36 was doubled by the logged RMW to 72 in S5; replay
  produces the correct 200 without ever noticing the poison). The insight
  required: none beyond "the log is complete."
* **Attack B** (per-cell temporal constraint graph): strictly subsumed by
  Attack A on the well-specified variant (1 candidate); not needed.
* **Attack C** (brute-force every hypothesis the FINAL PROMPT permits):
  on any variant with an uncovered transition, §2's construction *is* the
  measured result — **≥ 2 surviving world-hypotheses with different final
  states** from identical artifacts. Non-uniqueness is not a risk; it is
  proven.

Per the round's criterion ("if ANY straightforward attack reaches golden
reliably, C5 is too easy"): Attack A reaches golden reliably on the
specifiable variant. Combined with §1–§2: **C5 is under-specified or
trivial, with no middle ground.** The Round-2 recommendation is
withdrawn.

## 4. C5 — expert-difficulty audit (§4)

Answering the direct question: once `sqlite3` opens all five snapshots,
the remaining work is dump → diff → replay JSON audit records → reconcile
discrepancies. No b-tree knowledge, no journal semantics, no page-format
knowledge, no recovery-specialist judgment — temporal CSV reconciliation
that any competent data engineer performs. This independently rejects C5
even before §1–§3. (Round 2 already conceded "V5-class, not provably
above"; Round 3's finding is that it is materially *below* V5-class.)

## 5. C5 — the engine-self-check lever (§5)

Conceded in full. The Round-2 measurement (integrity_check blind to value
flips) establishes realism of *silence*, not difficulty: an agent told —
or noticing — that `integrity_check` cannot see semantic corruption loses
nothing, because (per §3) the solving path never needed corruption
detection at all on the specifiable variant. The lever carries no load.

## 6. Finalist 2 — lost segment (§6)

Granting the agent GTID/watermark semantics, disjoint-key writes, and
state diffing — the round's required assumption — the intended solution
collapses to a three-row decision table applied per row difference
(watermark-covered & absent ⇒ deleted in lost segment; watermark-covered &
present-only-on-B ⇒ written in lost segment; above watermark ⇒ never
received). That is mechanical constraint propagation, well inside
demonstrated V1–V5 abilities. Separately, the replication substrate would
be a bespoke harness — the "synthetic distributed-systems puzzle" reading
is available to any reviewer and cannot be refuted by pointing at a real
engine, because there isn't one. **Rejected on both of the round's
grounds.**

## 7. Cassandra zombie candidate (§7–§8), empirically screened

A real Cassandra 4.1 node was scripted through the relevant semantics:
two overlapping SSTables containing cell, row, partition and range
tombstones; TTL'd cells (expired); a write above a row tombstone
(resurrection, ts 250 > 200); a write below a partition tombstone (stays
dead, ts 150 < 200); a row-delete that *loses* to newer row content
(ts 120 < 150); and a cross-SSTable single-cell update (ts 400). The
engine's own read result and the prediction of a **~100-line per-cell
timestamp/tombstone reducer over `sstabledump` output** were compared:

```
PREDICTED == ACTUAL, row for row, cell for cell (4 rows, all cases)
```

That is exactly the round's rejection trigger: *"Empirically test whether
a simple per-cell timestamp/tombstone reducer predicts the final repair
result exactly... If either does, REJECT."* Merge semantics — including
every tombstone class and TTL — are **local and documented**; nothing
about them requires global reasoning. What the full zombie scenario would
add (gc_grace purge eligibility, repaired/unrepaired SSTable status,
overlap checks, incremental-repair metadata) are further *documented
deterministic rules*: the task "predict what repair/compaction produces"
is, by construction, *simulate the engine's documented algorithms* — the
identify-format → read-docs → implement-rules shape in its purest form,
with the agent explicitly granted source-reading ability. Two additional
strikes: the golden is produced by running the engine, so a solver
container containing the engine answers by tooling alone (difficulty
would rest on tool denial — an environment gimmick); and the build cost
(3-replica cluster, controlled outages, gc_grace manipulation, repair
orchestration, deterministic capture) far exceeds the expert-day budget.

Scope caveat, stated honestly: the full 3-replica repair choreography was
*not* executed; the screen covered single-node merge semantics across
overlapping SSTables. The rejection rests on the measured locality of
merge semantics, the documented-rule nature of the purge/repair layer,
and the budget — not on a claimed repair measurement. **Rejected.**

## 8. One more database-native alternative (§9), searched

Candidate examined: **row-based-replication applied-set reconstruction**
(MySQL RBR/relay-log damage: which events applied exactly once, decided
from before/after row images against the table state — engine-native,
non-MVCC, closed hypothesis space). Rejected on the same §6 standard:
before-images give strong local evidence, so attribution is constraint
propagation of the kind already demonstrated.

The search generalized to a structural observation worth recording,
because it explains the round's attrition pattern. For a task whose answer
is *data* reconstructed from engine artifacts, exactly three regimes
exist:

1. **Complete evidence** → the answer is a deterministic function of the
   artifacts → simulation of documented semantics (however ornate) →
   parser + rules. (Cassandra, F2, C1-after-measurement, well-specified
   C5, RBR.)
2. **Incomplete evidence over an engine-defined finite hidden state**
   (commit/abort, applied/not-applied, chosen/not-chosen) → abduction
   under global constraints → the V5 architecture family, currently
   banned as an isomorph. (F1's non-trivial pole, lost-segment's core.)
3. **Incomplete evidence over an open physical hypothesis space**
   (corruption, damage) → the hypothesis space is not solver-derivable →
   hidden fault model → ill-posed, or contrived-prompt-specified. (C5,
   measured; any bit-rot/torn-write variant.)

Regime 1 fails the anti-parser filter; regime 3 fails well-specifiedness;
regime 2 is the only regime that has ever produced a frontier-resistant
instance under these rules — and it is excluded by the current isomorph
ban. This is not an excuse but a constraint map: it says where Round 4
must dig.

## 9. Round-4 directions (consequence of decision E)

**9.1** Re-scope the isomorph ban precisely. What made V5 valuable was
regime-2 abduction; what the ban should exclude is *MVCC snapshot
visibility* specifically, not abduction over engine-defined hidden state
in general. A regime-2 task whose hidden state is not transaction
visibility — e.g., *which of the engine's own maintenance actions
(checkpoint truncation, segment recycling, page reuse) had reached
durability*, with evidence interlocking across native artifacts — would
be architecturally distinct at the mechanism level while keeping the only
difficulty source that has survived every measurement so far. Round 4
should either authorize that family explicitly or sustain the ban with
eyes open.

**9.2** If the ban stands, the deliverable-shape must change: a
*diagnosis* deliverable (exact damaged-object set, exact divergence
GTID range, exact lost-write inventory — deterministic, verifiable
artifacts) moves difficulty from state reconstruction to fault
localization. The §1 audit discipline applies unchanged: the diagnosis
hypothesis space must itself be engine-defined and solver-visible, or
regime 3 swallows the candidate. One Round-4 workstream should attempt
exactly one such design and subject it to this round's pincer test
before any implementation.

**9.3** Empirical gate to adopt permanently (it killed four candidates
cheaply this round): before any candidate advances, (a) build the
two-world indistinguishability attack for its fault model — if two
world-histories yield identical artifacts with different required
outputs, stop; (b) build the dumbest complete-evidence solver — if it
reaches golden, stop. Both tests are hours, not days, and both are
decisive.

---

Labs disposed. Nothing implemented; no Terminal Bench task was built or
modified in this round.

Sources: [SQLite file format](https://www.sqlite.org/fileformat2.html) ·
[SQLite: How To Corrupt](https://www.sqlite.org/howtocorrupt.html) ·
[Cassandra: About deletes and tombstones](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/compaction/tombstones.html) ·
[Cassandra repair docs](https://cassandra.apache.org/doc/latest/cassandra/managing/operating/repair.html) ·
[sstabledump tool](https://cassandra.apache.org/doc/latest/cassandra/managing/tools/sstable/sstabledump.html)
