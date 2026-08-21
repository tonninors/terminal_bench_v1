# Round 4 — Database-native hidden-state recovery (empirical screen)

Status: **design round only — nothing implemented.**
Labs this round: real etcd **v3.5.2** (an affected release), real MySQL
**8.0.28** (pre-8.0.30 XA behavior), Debian bookworm for parsing work.
Both permanent gates from Round 3 were applied to every candidate; the
decisive measurements are quoted inline.

## Decision up front (required)

**E. NONE — CONTINUE RESEARCH.**

No candidate empirically survived both permanent gates *and* the
strong-accept standard. Candidate A (etcd) is the best of the round —
authentic, famous, well-specified, buildable — but it fails the DIFFICULT
criterion: its evidence is complete by design (measured: a ~90-line
dependency-free WAL parser reproduces the exact recovered KV state, and
the WAL's persisted HardState carries the commit index), so the diagnosis
reduces to replay-and-diff, V3-class work the frontier has already
demonstrated. Candidates B and C fail on their own rejection triggers
(per-XID lookup table; two-world/trivial-diff pincer). The D-search found
no engine meeting all constraints, and produced a structural finding that
§6 records: the rescoped ban's target family (regime-2 abduction) barely
exists outside the forbidden transplant, because modern engines persist
their coordination state deliberately. §7 lays out the three honest paths
for Round 5.

---

## 1. Candidate A — etcd consistent-index divergence

**Engine/version.** etcd v3.5.0–v3.5.2 (bug introduced in the v3.5.0
refactor; fixed in v3.5.3 by PR #13854 moving `SetConsistentIndex` into
the transaction lock).

**Official incident grounding.** The project's own postmortem
(`Documentation/postmortems/v3.5-data-inconsistency.md`, issue #13766):
the in-memory consistent-index value was shared, so a concurrent batch-tx
commit hook could persist a CI ahead of the corresponding KV apply; a
crash in that window makes the member skip a committed entry on restart —
permanent, silent, member-local data loss; the maintainers reproduced it
by killing members under load and withdrew the production recommendation
for v3.5.0–3.5.2.

**Authenticity experiment (measured this round, bounded scope).** A real
v3.5.2 member was driven through a put/delete/overwrite history and
killed (`SIGKILL`); the crash-instant data directory was captured
verbatim (`member/wal/*.wal` 64 MB preallocated segment, `member/snap/db`
BoltDB backend). etcd's own restart recovery of that directory yielded
`{k1: v1b, k3: v3}` at revision 6 — the reference. Authentic *divergence*
capture (CI ahead of apply) was **not** performed this round; the
postmortem's own method (kill-under-load repetition, or the project's
gofail failpoints) makes it reachable without byte editing, at a real but
acceptable build cost. Authenticity is not this candidate's problem.

**GATE B — dumbest complete-evidence solver (measured).** A from-scratch
Python parser — no etcd code, no bolt reading, WAL only; ~90 lines —
decoded the WAL framing (8-byte length field with pad encoding), the
`walpb.Record` / `raftpb.Entry` / `InternalRaftRequest` protobufs by
generic wire-walking, filtered internal entries, and replayed puts and
deletes:

```
WAL entries: 9   HardState records: 8
last persisted HardState (term/vote/commit): {term 2, vote ..., commit 9}
PREDICTED KV: {'k1': 'v1b', 'k3': 'v3'}
ACTUAL (etcd's own recovery): {'k1': 'v1b', 'k3': 'v3'}
MATCH: True
```

Two load-bearing facts fall out. First, **the WAL alone reproduces the
state** — evidence is complete; the "hidden" state (which entries
applied) is a computable function of solver-visible artifacts, not an
abduction target. Second, **the commit index itself is persisted** in
every HardState record — committedness, the one fact that looks like
regime-2 hidden state, is written down by the engine. Raft implementations
persist exactly the metadata a would-be abduction task needs.

**Two-world test (GATE A).** Passes — no under-specification: with WAL
union + snapshots + backends from three members, the committed history
and each backend's contents are both determined, so the diagnosis is
unique. The candidate's failure is the opposite pole.

**Strongest frontier-equivalent solver (#8), assessed.** Cross-member
scope adds real content the single-member measurement lacks: a divergence
older than the divergent member's own snapshot is invisible to per-member
replay-diff (the snapshot embeds the flaw), so the entry content must
come from a peer's WAL range, and the skipped apply shifts every
subsequent revision on that member, so naive revision-number joins
misalign systematically. That is genuine, and it defeats solvers 1–7 of
the brief (trusting CI / highest index / majority-by-value / metadata
only). But solver #8 — reconstruct committed log from the WAL union,
recompute expected backend per member, align with the shift, diff — is
~500–600 lines of documented-format parsing (WAL protos measured at ~90
lines; bolt key-bucket revision decoding; snapshot metadata) plus careful
bookkeeping. That is V3-scale mechanical work, and this incident is
*famous*: the postmortem itself is in every frontier model's training
distribution, shape and remedy included.

**Objective output.** `/app/recovery_plan.json` per member:
`backend_consistent_index`, `missing_committed_entries` (index, term,
decoded op), `duplicate_applied_entries`, `required_actions` — fully
deterministic, verifier-friendly.

**Solver-visible artifacts.** `member/*/wal/*.wal`, `member/*/snap/*.snap`,
`member/*/snap/db` per member; raw engine artifacts suffice (measured —
no dumps needed, so no tool-denial issue).

**Uniqueness argument.** WAL union covers every committed entry by
construction of the scenario; HardState commit indexes bound
committedness; backend revision histories are enumerable → one diagnosis.

**Expert path & time.** Parse WALs → reconstruct committed sequence →
recompute expected state per member from its snapshot base → align
revisions → diff → emit plan. 5–8 h expert; build side ~1–2 days
(3-member choreography + divergence capture + gates) — over the one-day
budget, though not absurdly.

**Reviewer scores.** VERIFIABLE 5 · WELL-SPECIFIED 4 · SOLVABLE 4 ·
**DIFFICULT 3** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 5.

**Failure risk / verdict.** Fails strong-accept on DIFFICULT (and
build-budget). The very property that makes etcd trustworthy — complete,
deliberately persisted coordination evidence — makes the recovery a
simulation. **Rejected**, with the note that this is the round's best
candidate and the strongest fallback if a regime-1 task ever becomes
acceptable.

## 2. Candidate B — MySQL XA / binlog / InnoDB reconciliation

**Engine/version.** MySQL 8.0.28 (pre-8.0.30 one-phase XA PREPARE
ordering), grounded in bugs #109434 (rotation window), #110430 (GTID not
persisted to undo header), #98616 (durability ordering), and the 8.0.30
two-phase XA PREPARE change.

**Authenticity experiment (measured, bounded).** A real 8.0.28 server was
driven through three XA lifecycles — committed, rolled back, and
prepared-then-`SIGKILL` — and restarted. Captured, all engine-authored:
`XA RECOVER` reports exactly `xa_prep` in-doubt; the committed row
visible, rolled-back absent, in-doubt invisible;
`gtid_executed = …:1-12`; and `mysqlbinlog` (stock, in the image) renders
the full decision evidence as text across a genuine rotation
(binlog.000002 → 000003 created by the crash restart): per-XID
`XA START/END/PREPARE` events, terminal `XA COMMIT`/`XA ROLLBACK` events,
each with its GTID.

**GATE B — dumbest solver (measured on the captured evidence).** The
reconciliation is a **three-input per-XID lookup table**, readable off
the artifacts by eye:

| xid | engine | binlog terminal | action |
| --- | --- | --- | --- |
| xa_done | not prepared | COMMIT (:9) | NO_ACTION |
| xa_rolled | not prepared | ROLLBACK (:11) | NO_ACTION |
| xa_prep | PREPARED | none (:12 = prepare) | KEEP_PREPARED |

The bug-window cases (#109434: terminal event in a rotated binlog while
the engine stays PREPARED) add rows to the same table with action
COMMIT/ROLLBACK — the correct answer *is* the documented
transaction-coordinator rule applied more carefully than the buggy
server applied it. That is B3's explicit rejection trigger ("if the full
causal solver reduces to a small per-XID lookup table, reject").

**Two-world test.** Passes (binlog + engine state disambiguate every
lifecycle stage) — again the failure is triviality, not ambiguity.

**Authenticity cost.** The interesting windows (crash *between* binlog
sync and engine commit) are not reachable by `SIGKILL` timing; they need
a debug build with `DEBUG_SYNC` — hours of C++ build plus scripting,
over the expert-day budget on its own.

**Reviewer scores.** VERIFIABLE 5 · WELL-SPECIFIED 4 · SOLVABLE 4 ·
**DIFFICULT 2** · REALISTIC 5 · OUTCOME-VERIFIED 5 · REQUIRES-CODE 4.

**Verdict.** **Rejected** — lookup-table difficulty at debug-build cost.

## 3. Candidate C — MySQL undo-truncation zombie rows (Bug #119628)

**Grounding (verified).** Bug #119628 (originally #112262, 2023): a
failed automated undo truncation under `super_read_only` leaves
`trunc.log`; the next startup reconstructs the undo tablespace; a later
crash with an uncommitted transaction leaves that transaction's data
alive after recovery. Real, current (unfixed in 8.0.44; fixed in 8.4;
J-F Gagné wrote it up in January 2026).

**Screen (analytic — the Round-3 pincer applies exactly).** The bug's
essence is *destroyed rollback authority*: the undo records that would
identify and remove the zombie rows are gone. What remains to identify
them? (a) With binlog coverage of the relevant window, zombie rows are
rows absent from the binlog's row events — a trivial table-vs-binlog
diff, which is the round's own rejection clause ("a base backup or
replica makes the answer a trivial diff/replay" — a binlog window is the
same clause). (b) Without binlog coverage, GATE A fails outright: a
zombie row and a legitimately committed row whose undo was purged
normally present identical native evidence (a bare `DB_TRX_ID` in a
range whose commit records no longer exist anywhere) — two worlds,
identical artifacts, different inventories. The deliverable would need
hidden generation truth. **Rejected without prototyping**; no
authenticity experiment can rescue a task that is ill-posed or trivial
by case analysis.

**Reviewer scores.** VERIFIABLE 4 · **WELL-SPECIFIED 2** · SOLVABLE 2 ·
DIFFICULT n/a · REALISTIC 5 · OUTCOME-VERIFIED 4 · REQUIRES-CODE 4.

## 4. Candidate D — the additional engine search

Screened against the requirement "identify the exact engine-native hidden
state being inferred" before proposing:

| engine / mechanism | hidden state candidate | verdict |
| --- | --- | --- |
| MongoDB/WiredTiger rollback-to-stable, replica rollback files | which writes survive RTS | timestamps and rollback files are persisted → replay/diff (regime 1) |
| ZooKeeper txn log + snapshots | applied-vs-logged | same shape as etcd, same completeness (regime 1) |
| LMDB dual meta pages | which meta is current | documented pick-valid rule |
| Redis AOF/RDB hybrid | applied suffix | `redis-check-aof` semantics; documented truncation rule |
| BerkeleyDB log/db LSN mismatch | per-page appliedness | documented catastrophic-recovery procedure |
| InnoDB change buffer | merged vs pending index ops | pending ops are *stored in* the buffer b-tree → apply them (regime 1) |
| InnoDB instant-DDL row versions | per-row schema version | recorded in the record header (complete) |
| MySQL RBR relay-log damage | applied-event set | before-images give strong local evidence (Round-3 §8) |

No qualifying candidate. **D: none proposed.**

## 5. Gate discipline record

GATE A (two-world) was run analytically on A and B (both pass — they are
well-specified) and killed C's no-binlog branch. GATE B (dumbest solver)
was run **empirically** on A (WAL-replay parser, MATCH: True, ~90 lines)
and on B's captured evidence (lookup table read off `mysqlbinlog` text),
and killed C's with-binlog branch analytically. The gates cost hours and
settled everything, as intended.

## 6. The structural finding this round adds

Round 3 established three regimes; Round 4 adds the empirical sharpening:

> **Coordination layers persist their own hidden state on purpose.** The
> etcd WAL carries `HardState.commit` in every append (measured);
> MySQL's XA evidence is written to the binlog and engine state
> precisely so recovery can reconcile it (measured). Where an engine's
> correctness depends on a fact, the engine durably records that fact —
> that is what "engine-defined hidden state" turns out to mean in
> practice. The only genuine evidence-lossy regions in production
> engines are the ones that are lossy *by MVCC design* — hint bits, lazy
> clog, purged undo, dropped tombstones — i.e., precisely the
> transaction-outcome family the current rules forbid transplanting.
> Regime 2 minus the forbidden family is close to the empty set.

The v3.5 etcd bug is the exception that proves the rule: it was a
*violation* of that persistence discipline, immediately treated as a
critical defect, and its artifacts still leave enough evidence (peer
WALs) that recovery is computation rather than abduction.

## 7. Round-5 paths (consequence of decision E)

1. **Authorize one transplant-with-distance.** If a future round permits
   commit/abort-family abduction with mandated distance — different
   engine, different evidence physics, different deliverable (e.g.,
   InnoDB purge-lag/secondary-index outcome forensics with a diagnosis
   artifact) — that family is the only one with a proven frontier-scale
   difficulty record. The distance requirements would need explicit
   sign-off, since the current rules forbid it.
2. **Accept a ceiling and choose professionalism.** Implement candidate
   A as the best regime-1 task: famous incident, immaculate authenticity
   story, deterministic diagnosis deliverable — accepting DIFFICULT ≈ 3
   (V3-class) and a ~2-day build. This is a coherent product decision,
   but it should be made knowingly, not by drift.
3. **Change the deliverable physics, not the engine.** The one unexplored
   shape: tasks where the *output* is a proof-carrying diagnosis (each
   claim in the deliverable must cite the artifact bytes that force it),
   which penalizes plausible-but-wrong answers structurally. This is
   verifier research, not candidate research, and it may be the actual
   bottleneck: every strong candidate since V5 has died on "the frontier
   can compute it," and no candidate has died on "the frontier can
   defend it."

---

Labs disposed (etcd, MySQL, parser containers removed). Nothing
implemented.

Sources: [etcd postmortem](https://github.com/etcd-io/etcd/blob/main/Documentation/postmortems/v3.5-data-inconsistency.md) ·
[etcd #13766](https://github.com/etcd-io/etcd/issues/13766) ·
[etcd PR #13854](https://github.com/etcd-io/etcd/pull/13854) ·
[etcd-dev advisory](https://groups.google.com/g/etcd-dev/c/sad9tgmKU7Y/m/vhArFVevBgAJ) ·
[MySQL bug #119628](https://bugs.mysql.com/bug.php?id=119628) ·
[J-F Gagné on #119628](https://jfg-mysql.blogspot.com/2026/01/undo-log-truncation-bug-in-80-leads-to-data-corruption.html.html) ·
[MySQL bug #112717](https://bugs.mysql.com/bug.php?id=112717) ·
[MySQL bug #99326](https://bugs.mysql.com/bug.php?id=99326)
