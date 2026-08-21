# Round 2 — Cross-red-team review (empirical)

Status: **design round only — nothing implemented.**
Method: every disputed technical claim was tested against real engines in a
throwaway Debian bookworm container (RocksDB 7.8.3 `ldb`/`sst_dump` +
`librocksdb-dev` driver programs; SQLite 3.40.1; shallow source clones of
`google/leveldb` and `facebook/rocksdb` v7.8.3). No claim below is
predicted; each is measured unless explicitly marked analysis.

The verdict summary up front, because it is not kind to Round 1:

| candidate | Round-1 claim | Round-2 empirical verdict |
| --- | --- | --- |
| C1 LSM manifest | RepairDB silently resurrects; live set is a hard global unknown | **REJECTED** — key claims failed measurement (§1–§3) |
| C2 indexes-remember | full journal rollback "over-rewinds" | **REJECTED** — the trap is contrary to measured SQLite semantics (§4) |
| C3 torn pages | checksum-valid torn images force global arbitration | **REJECTED** — depends on engineered checksum collisions = gimmick (§5) |
| C4 split brain | invariants pin one converged state | **REJECTED AS WRITTEN**; a policy-free redesign survives as finalist 2 (§5, §10) |
| C5 backup chain | folklore restores fail; replay-certification is the path | **SURVIVES, hardened** — engine-blindness now measured (§5, §10) |
| F1 SQLite 3.53.3 | (external suggestion) | rejected — unverifiable + collapses to trivial-or-isomorph (§6) |
| F2 ClickHouse parts | (external suggestion) | rejected — engine's own documented recovery covers it; part algebra is rule-based (§7) |
| F3 DuckDB ART | (external suggestion) | rejected — fails its own screen (§8) |

---

## 1. Verdict: the LevelDB-vs-RocksDB sequence-number claim (required output #1)

**The challenge was right; Round 1 conflated the engines.**

Source-level check (shallow clones, full grep of `db/` in both trees):

* `google/leveldb` (current main): **no compaction-time sequence-number
  zeroing exists anywhere.** The only `sequence = 0` hits are variable
  initializations in `repair.cc`. Vanilla LevelDB keeps real seqnos through
  every compaction.
* `facebook/rocksdb` v7.8.3, `db/compaction/compaction_iterator.cc:1190`:
  `ikey_.sequence = 0;` inside the documented bottommost-level zeroing
  logic (surrounding comments at lines 565 and 1197 discuss exactly this).

Behavioral confirmation on RocksDB 7.8.3 (`sst_dump --command=scan` on a
scripted history):

```
000010.sst  'j' seq:0 'k' seq:0 'm' seq:0          <- gen-1 bottommost output: zeroed
000012.sst  'j' seq:4 type:0(del)  'k' seq:5  'n' seq:6   <- gen-2 flush: real seqnos
000013.sst  'k' seq:0 'm' seq:0 'n' seq:0          <- gen-2 output: zeroed, j GONE
```

So: seqno zeroing is **RocksDB-specific**; tombstone dropping at the
bottommost level is confirmed (the `j` tombstone exists in the flush file
and is absent from the committed output). Any fixture relying on zeroing
must be RocksDB and say so. This correction is now moot given §3, but the
record stands.

## 2. Verdict: measured RepairDB behavior (required output #2)

Setup: the full crash-instant directory captured from a real RocksDB 7.8.3
process — two compaction generations executed inside a retention window
(`DisableFileDeletions()`, the exact mechanism a real backup engine pin
uses), obsolete SSTs **and an obsolete WAL** genuinely retained, an
unflushed tail write present only in the live WAL, then `MANIFEST-*` and
`CURRENT` deleted to model the incident.

Measured result of `ldb repair`:

```
Failed: Corruption: force_consistency_checks(DEBUG): VersionBuilder:
L0 file #16 with seqno 4 6 vs. file #12 with seqno 4 6
```

RepairDB converted the retained-but-already-flushed WAL into a new table
whose seqno range collided with the SST that same WAL had produced, failed
its own consistency check, moved both WALs into `lost/`, and left a
database that scans **empty** (0 keys).

So the Round-1 prediction — *silent* resurrection — is **wrong on this
incident class**. RepairDB fails loudly and destructively instead. A loud
failure is a far weaker trap than a silent wrong answer: no competent agent
accepts an empty database and stops. Per the challenge's own criterion
("the central negative must be empirical"), C1's flagship negative does not
exist in the form Round 1 claimed.

## 3. Verdict: C1 uniqueness/feasibility (required output #3) — and the finding that kills C1

The feasibility prototype (parse every on-disk SST and WAL with
`sst_dump`/`ldb dump_wal`, reconstruct final states under candidate
strategies, compare to the golden state captured from the live process at
the crash instant) produced the decisive measurement:

```
merge ALL SSTs + ALL WALs, max seq      CORRECT
merge ALL SSTs only, max seq            WRONG   (missing only the WAL-tail key)
newest-numbered SST + WALs              CORRECT
newest file number wins + WALs          CORRECT
```

**The naive merge-everything strategy reproduces the golden state exactly.**

The underlying reason generalizes, and it is worth recording because it
buries every variant of C1 I tried to save:

> File retention in an LSM directory is *per-obsolescence-event*: a file
> becomes deletable only when the compaction that supersedes it commits,
> and a retention window suppresses deletion of the whole set from that
> moment on. Therefore, inside any single all-retained window, the
> directory always contains the **complete event chain** for every key —
> every overwrite's flush file with its real sequence number, every
> tombstone-carrying file — because the file that would have removed that
> evidence became obsolete (and was retained) at the same instant as its
> co-inputs. Max-seq merge over everything, plus WAL replay, is then
> provably correct. Seqno-zeroing ambiguity (two seq-0 versions of one
> key) can only be *decisive* if some chain file is selectively absent —
> which means either data loss (solvability dies) or a second lineage
> mixed into the directory (an operator restoring an old backup into a
> live dir), where per-file `db_session_id` and `creation_time` table
> properties partition and order the lineages as a documented rule.

So every reachable C1 configuration is either (a) trivially solved by
merge-all (measured), (b) solved by documented metadata rules
(session-id/file-number ordering), or (c) unsolvable. That fails the
anti-parser filter and Section H at once.

**C1 is rejected.** Not for the reason the challenge suggested (uniqueness
was actually fine — distinct outputs = 1 in the prototype), but for the
worse one: the difficulty does not exist. The Round-1 ranking of C1 as #1
was wrong, and the error was exactly the kind of unmeasured prediction the
Round-2 protocol exists to catch.

Accidental-cue audit (for the record, since it generalizes to any future
candidate): RocksDB SSTs carry `db_session_id`, `creation_time`,
`file_creation_time`, and `orig_file_number` table properties, and file
numbers order a single lineage totally. Any LSM fixture that depends on the
solver *not* ordering files must strip or equalize all of these — at which
point the bytes are no longer engine-authored. Another nail.

## 4. Verdict: C2 hot-journal semantics (required output #4)

Measured on SQLite 3.40.1, with the transaction forced to spill
mid-transaction so the main database file genuinely contained uncommitted
page content at the kill instant:

```
child killed mid-transaction (os._exit)
journal bytes: 513536                      (hot journal present)
uncommitted bytes in db BEFORE recovery: True
journal present after open: False          (stock open played it back)
uncommitted bytes AFTER recovery: False
recovered == committed baseline: True      (byte-equal aggregate + integrity_check ok)
```

**The challenge is upheld.** A hot rollback journal's pre-images are the
last committed images of the pages the open transaction touched; full
standard rollback restored *exactly* the committed state. Round 1's second
trap ("applying the whole journal over-rewinds committed truth") is
contrary to measured semantics — the journaled pre-image of a page the
`dd` accident zeroed is a *repair*, not an over-rewind. With that trap
gone, C2 reduces to: let stock rollback clean the indexes (it does, and it
is the documented behavior), then join index projections keyed by rowid —
a parser plus a mechanical join. **C2 is rejected**; no selective-rollback
redesign is admissible because selective rollback is precisely the
"invented rule contrary to engine semantics" the challenge forbids.

## 5. Verdicts on C3, C4, C5 (required output #5)

**C3 — rejected.** Its three designed hard cases lean on
checksum-accidentally-valid torn pages and cross-algorithm checksum
ambiguity. Under the Round-2 filter that is a checksum-collision gimmick:
the difficulty would be *manufactured by the generator*, not by the
incident class. Stripped of those cases, what remains is the documented
doublewrite arbitration rule plus LSN comparison — parser + rules. Add the
already-flagged InnoDB implementation breadth (worst expert-day risk of the
slate) and C3 has nothing left to stand on.

**C4 — rejected as written; redesigned as finalist 2.** The Round-1 design
resolved concurrent write-write conflicts "by invariants," which on honest
inspection means the generator hand-shapes invariants until they encode a
winner — an invented conflict-resolution policy wearing a costume. That
fails "no arbitrary application constraints encoding the answer." The
redesign in §10 removes conflict adjudication entirely: no key is
concurrently written on both sides; every unknown is a *completeness*
question (which durable writes exist only as table rows because the event
segment naming them was lost), answered by engine-style replication
watermarks — applied-GTID-set bookkeeping each site durably stores. The
unknowns become attributable facts, not policy choices.

**C5 — survives, with the challenge's critiques absorbed and one key claim
now measured.** The "is it recognizably database recovery?" charge is
answered three ways: (a) every artifact is a stock engine file or an
industry-standard one (nightly snapshot copies of a SQLite database; an
append-only application audit log — the artifact every compliance regime
mandates; the schema); (b) the workflow is exactly real PITR triage:
identify the last trustworthy generation, roll forward, reconcile; (c) the
decisive engine-native measurement:

```
bit flip inside a stored TEXT value:
  PRAGMA integrity_check -> ok
  PRAGMA quick_check     -> ok
  SELECT returns silently wrong value ('holder-00350' for 'holder-00250')
bit flip in a record header/serial-type area:
  PRAGMA integrity_check -> 'database disk image is malformed'
```

So the engine's own self-checks are provably blind to value-payload
corruption and provably loud about structural corruption. That measured
boundary does double duty: it defines the honest fault model (value bytes
— which is what RAM bit-flip damage to application data is), and it means
a frontier agent's natural self-verification (run the engine's checker;
open the file; query it) **passes on the poisoned snapshot**. The wrong
answer is not detectably wrong by any local means — the C5 property that
survives Section H.

## 6. F1 — SQLite 3.53.3 multi-database regression (required output #6)

Searched for the cited July 2026 report directly and via the official
channels. Result: **not found.** What the official sources do document: the
super-journal (master-journal) atomic-commit protocol and its crash
semantics; a March 2026 "WAL-reset bug" fix (a different mechanism — WAL
mode, not rollback journals); and the long-standing *documented limitation*
that WAL-mode multi-database transactions are not atomic across databases.
The specific claim — a 3.53.3 rollback-journal regression deleting a hot
journal without rollback — could not be verified to exist.

Architectural analysis, independent of verification: the candidate
collapses toward one of two poles. If the artifacts are intact and the
engine behaves as documented, stock per-database recovery plus the
super-journal rule is *correct by design* — trivial. If a regression or
fault makes the documented hot-journal decision wrong, the solver's job is
to decide, per database, rolled-back-or-committed under cross-database
atomicity — a small commit/abort outcome-assignment problem under global
constraints, i.e. **the V5 architecture again**, banned by the ground
rules. And building the fixture on an unverifiable regression fails the
authenticity gate (we cannot reproduce a bug we cannot confirm exists).
**Rejected.**

## 7. F2 — ClickHouse MergeTree part-set recovery (required output #7)

Verified against ClickHouse documentation, the Altinity knowledge base,
and the issue tracker. The incident class is real and well documented —
power loss can leave a freshly merged part with correct file sizes and
bad/zeroed contents. But the same documentation kills the task: ClickHouse
*by design* keeps merge-input parts inactive for `old_parts_lifetime`
(default ~8 minutes) **exactly so that a broken merged part can be
replaced by its inputs**, detects broken parts via `checksums.txt` and
per-file validation, and `force_restore_data` / detach-and-reattach is the
documented, working procedure. The proposed core decision — "the exact set
of parts forming the latest valid non-overlapping partition state" — is
computed by ClickHouse's own `ActiveDataPartSet` algebra as a pure
function of part names (`partition_min_max_level_mutation`) once per-part
validity is known; and per-part validity is decidable *locally*
(checksums.txt, compressed-block magics). So the solution is: validate
parts locally (documented formats), then run a documented covering
algorithm — identify-format → read-docs → implement-parser → apply-rules,
precisely the shape the filter forbids. The greedy highest-level/highest-
mutation rule works on every configuration I could construct on paper
where the data survives at all; making it fail requires damaging validity
evidence in ways that make validity *globally* ambiguous, which is the
checksum-gimmick road again. **Rejected** under F2's own criterion.

## 8. F3 — DuckDB ART silent-empty index (required output #8)

The specific 2026 issue as described could not be located (nearby real
issues exist: ART constraint enforcement corrupting the heap in 1.5.0,
index-related wrong results in older versions). Regardless, it fails its
own screening rule: for any incident where "the table data remains
correct," dropping and rebuilding the index — or simply answering from the
table with the index ignored — solves everything; and DuckDB's deliverable
would be a query result derivable from an intact table. There is no
recovery unknown. **Rejected immediately**, as the challenge anticipated.

## 9. What Section H leaves standing (analysis)

Granting the frontier agent accurate binary reading, source reading,
mass brute force, classic recovery algorithms, global-constraint
reasoning, and self-checking eliminates every candidate whose difficulty
is "formulate the state space and enumerate" *once the formulation is
apparent*. What survives is exactly one property: **an attractor path
whose wrong output passes every check the agent can run locally** —
including the engine's own verifiers — so that self-checking reinforces
the wrong answer instead of exposing it. Both finalists below are chosen
for that property, and both must prove it at generation time (a negative
that the engine itself certifies as healthy).

## 10. The two finalists (required output #9)

### Finalist 1 — "The backups preserved the corruption" (C5 hardened)

SQLite 3.40.x pinned. Five nightly snapshot files; an application audit
log covering only the last three nights (rotation is why PITR gaps exist);
the schema. Silent value-payload corruption (measured invisible to
`integrity_check`/`quick_check`) entered on a specific night and was
faithfully archived ever since. Output: the true current table state as
CSV. Hardening added this round: every solver-visible snapshot must pass
the engine's own checkers (gate-enforced); flips are value-payload-only
(the honest fault model per §5's measured boundary); designed
read-modify-write chains in the logged window make the naive
"replay-and-diff, correct backwards" direction wrong on a systemic number
of rows (the flip predates a dependent logged write, so the correction
must propagate *through* replay, not overwrite it); uniqueness is proven
by enumerating flip-placement hypotheses and aborting unless exactly one
final state survives.

### Finalist 2 — "The lost segment" (C4 redesigned, policy-free)

Two-site replicated pair (embedded SQL databases + append-only binary
event segments + durable applied-watermark bookkeeping per site, the
GTID-set pattern). Partition; both sites accept writes on **disjoint
keys** (no write-write conflict exists anywhere — nothing to adjudicate,
no policy); site B then loses its newest event segment to the crash while
site A has purged its oldest segments. Neither event history is complete;
some of B's durable writes and deletes survive *only* as presences and
absences in B's tables. The unknowns are pure completeness facts: for each
row difference between the sites, was it (i) an A-write B never received
(decidable from B's stored watermark vs the event's GTID), (ii) a B-write
whose event was in the lost segment (forced when B's watermark proves
receipt of everything else and the row exists only in B), or (iii) a
B-delete in the lost segment (forced when the row's creating GTID is
under B's watermark yet the row is absent from B). The converged state
follows deterministically from the attributions; generation enumerates
attribution candidates and aborts unless exactly one state survives.

## 11. Attack plans (required output #10)

**Against finalist 1.** (a) *Restore newest snapshot* — passes every local
check including the engine's own; wrong on the designed row set; flagship
negative. (b) *Oldest + replay* — the gap makes it wrong differently.
(c) *Majority vote across generations* — early-entering poison wins votes;
wrong. (d) *Replay-certify adjacent pairs, then patch mismatches backward*
— the strongest natural attack; a capable agent will find it; it is wrong
on every row where the flip predates a dependent logged write, and
generation must **measure** that this attack loses ≥ a systemic number of
rows or the fixture is rebuilt. (e) *Notice the poison by value
implausibility* — values are designed with no plausibility signal
(amounts stay in-range; text stays well-formed — the measured flip
'holder-00250'→'holder-00350' is the model). Residual honest risk: a
frontier agent that both derives certification-by-replay *and* models
flip/write interleaving per-row has done expert-equivalent work; C5 is
V5-class difficulty, not provably beyond it. That is stated plainly
rather than papered over.

**Against finalist 2.** (a) *Union both tables, dedupe* — resurrects B's
lost-segment deletes; wrong. (b) *Replay all surviving events over the
older common snapshot* — loses every lost-segment write; wrong. (c)
*Trust the failover winner (site A) wholesale* — loses B's durable
partition-window writes; wrong; this is the exact GitHub-2018 wrong
answer. (d) *Timestamp arbitration* — designed skew; wrong. (e) The
strongest attack: reconstruct the watermark logic and enumerate — which
is the intended solution; the open question is how much of that
formulation the artifacts telegraph. Residual honest risks: the
replication harness is bespoke (reviewer may read "toy system"), and the
watermark evidence must be rich enough to force attributions without
being a lookup table — the gate must prove both no-ambiguity and
no-telegraphing. Higher contrivance risk than finalist 1.

## 12. Recommendation: ONE candidate (required output #11)

**Recommended: Finalist 1 — "The backups preserved the corruption."**

ENGINE + VERSION: SQLite 3.40.x (Debian bookworm build), pinned in the
fixture image; the architecture is engine-agnostic but is implemented on
SQLite for authenticity-per-hour.

REAL INCIDENT: silent value-level corruption on a primary (failing
RAM/storage), faithfully propagated into weeks of nightly backups and
discovered late, with log rotation having destroyed the early history —
the R5 class (GitLab 2017 backup-trust failure; CERN silent-corruption
studies; the standard PITR-gap war story).

SOLVER-VISIBLE ARTIFACTS: five nightly snapshot `.db` files (S1–S5,
verbatim file copies); the application's append-only audit log covering
only the last three nights (values recorded, as real audit logs do);
the schema with its constraints. Nothing else.

REQUESTED OUTPUT: `/app/recovered.csv` — the true current state of the
main table.

WHAT IS UNKNOWN: which night(s) the corruption entered, which rows/values
it hit, and the true current values — none of which any single artifact
states.

WHY DOCUMENTED RECOVERY FAILS: restore-newest preserves the poison and
the engine's own `integrity_check`/`quick_check` certify the poisoned
file healthy (measured this round); restore-oldest-and-replay dies on the
rotation gap; majority-across-generations elects early poison; even
replay-and-diff — the smart documented-adjacent move — corrects in the
wrong direction wherever a flip precedes a dependent logged write.

WHY THE STATE IS UNIQUE: generation enumerates flip-placement hypotheses
(which generation, which row/value, before/after which dependent logged
statements) and requires exactly one final state to survive
certification-by-replay against all five snapshots and the log window;
generation aborts otherwise.

WHY A V1–V5-CLASS FRONTIER AGENT SHOULD STILL FAIL: every natural path
terminates in a clean-looking CSV that passes every check the agent can
run — including the engine's own verifiers — so self-checking reinforces
the wrong answer; the correct answer requires conceiving cross-generation
certification *and* per-row flip/write interleaving reasoning, neither of
which any artifact or document names.

HOW A HUMAN EXPERT SOLVES IT: exactly like a real PITR triage — verify
the newest pair against the log by replay; walk the certification
backward to localize the divergence night; classify each mismatched row
by whether logged statements depend on the damaged value; propagate
corrections through replay; emit and re-verify.

ESTIMATED EXPERT TIME: 4–8 hours (formats are deliberately trivial;
the entire budget is reasoning).

VERIFIER: outcome-only — exact keyed-row equality plus canonical digest
of `/app/recovered.csv`; nothing else inspected.

AUTHENTICITY GATE: every snapshot byte is engine-written except the
fault-injected value bytes (the incident *is* external byte damage, the
one thing honestly modeled from outside — consistent with the V5
precedent); gate must prove: flips are value-payload-only, every
solver-visible snapshot passes `integrity_check` and `quick_check`,
replay of the audit window is bit-deterministic, and no structural
artifact (freelist, headers, cell layout) betrays the flip locations.

UNIQUENESS GATE: exhaustive flip-hypothesis enumeration ends at exactly
one surviving final state AND exactly one output CSV; every named attack
(§11a–e) is implemented as a graded negative and measured wrong on a
systemic number of rows; generation aborts on any failure.

MAIN REVIEWER RISK: "constraint puzzle over file copies" perception —
mitigated by stock artifacts, an industry-standard log, a documented
incident class, and a workflow that is recognizably PITR triage; plus the
honest admission that difficulty is V5-class, defended by measurement
(engine-checker blindness) rather than by claim.

IMPLEMENTATION STOP CONDITIONS: abort if any single folklore strategy
(including replay-and-diff-backward) reproduces the golden state; if the
flip-hypothesis enumeration leaves ≠ 1 state; if keeping flips
value-payload-only cannot be reconciled with silent-to-the-engine damage;
if audit-log realism would require recording anything a real audit log
does not record; if the fixture needs > 1 expert working day; or if the
uniqueness argument ever needs an application constraint invented solely
to force the answer.

---

Container lab was disposable; all measurements above are recorded here.
Round-1's C1 ranking is formally retracted per §3. Nothing is implemented.

Sources: [LevelDB source](https://github.com/google/leveldb) ·
[RocksDB v7.8.3 source](https://github.com/facebook/rocksdb) ·
[SQLite atomic commit](https://www.sqlite.org/atomiccommit.html) ·
[SQLite: How To Corrupt](https://www.sqlite.org/howtocorrupt.html) ·
[SQLite temp files / journals](https://sqlite.org/tempfiles.html) ·
[SQLite WAL](https://www.sqlite.org/wal.html) ·
[Altinity KB: broken parts](https://kb.altinity.com/altinity-kb-setup-and-maintenance/suspiciously-many-broken-parts/) ·
[Altinity KB: removing lost parts](https://kb.altinity.com/upgrade/removing-lost-parts/) ·
[ClickHouse #19593](https://github.com/ClickHouse/ClickHouse/issues/19593) ·
[ClickHouse #35704](https://github.com/ClickHouse/ClickHouse/issues/35704) ·
[ClickHouse #5847](https://github.com/ClickHouse/ClickHouse/issues/5847) ·
[DuckDB #23046](https://github.com/duckdb/duckdb/issues/23046) ·
[DuckDB #10251](https://github.com/duckdb/duckdb/issues/10251) ·
[SQLite forum: multi-db transactions](https://sqlite.org/forum/forumpost/ecf53d4eb8)
