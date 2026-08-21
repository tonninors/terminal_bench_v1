# Blind adversarial design tournament — Systems / Databases candidates

Author: Claude (blind entry — written without sight of any competing entry).
Status: **design only, nothing implemented.**

## Ground rules applied

1. **No reuse of the existing PostgreSQL MVCC architecture.** Nothing below
   reconstructs snapshot visibility from heap tuple headers and transaction
   status metadata, and nothing below is that idea transplanted to another
   engine (InnoDB undo-based read views, for example, are rejected in the
   appendix as an isomorph). The existing `sqlite-wal-crash-recovery` task's
   mechanism (WAL frame salvage) is likewise off-limits.
2. **The anti-parser filter.** A candidate dies if a frontier coding agent
   can solve it as: identify the format → read the documentation → implement
   a parser → apply the documented rules. Parsing is allowed as *table
   stakes*; it must never be the summit. Every candidate below names the
   documented-rules solution explicitly and shows why it produces a wrong
   answer.
3. **Hard requirements.** Unique deterministic outcome (machine-provable at
   generation time); fully solvable from solver-visible files; plain Linux
   terminal, no network, no database server required to solve; generable and
   validatable by a qualified expert within one working day; verifier grades
   one output artifact, outcome-only.
4. **No engine favoritism.** PostgreSQL appears nowhere below. The five
   survivors use LevelDB/RocksDB-format LSM files, SQLite b-trees (twice,
   with disjoint architectures), MySQL/InnoDB physical pages, and a
   MySQL-style replicated pair.

---

## 0. Research digest — real incident classes this is grounded in

Surveyed from public post-mortems, engine bug trackers, recovery-tool
documentation and the engines' own "how corruption happens" literature:

| # | Incident class | Real-world grounding |
| --- | --- | --- |
| R1 | **LSM manifest loss / version-set corruption.** The MANIFEST that records which SST files are live is damaged; the data files themselves are fine. Naive "repair" resurrects deleted keys and stale values because obsolete SSTs still sit in the directory. | Chronic LevelDB failure mode: Chromium's leveldb corruption bug tree; Bitcoin Core issue history (`chainstate` corruption after crash, `-reindex` folklore); LevelDB's own `RepairDB` documentation warns that repaired state may "resurrect" data; RocksDB wiki on MANIFEST and atomic version edits. |
| R2 | **Table data destroyed, secondary structures intact.** Operator or storage fault wipes specific regions of a database file (accidental `dd`/truncate, filesystem hole punch, ransomware partial overwrite); indexes, being physically elsewhere, survive. | SQLite's "How To Corrupt An SQLite Database File" enumerates exactly this class; commercial DB forensics (Sanderson SQLite Forensic Toolkit) exists largely because deleted/orphaned b-tree content is recoverable; `.recover`'s `lost_and_found` design acknowledges partially-attributable pages. |
| R3 | **Torn page writes and doublewrite arbitration.** Power loss mid-write leaves a page half-old/half-new; InnoDB keeps a doublewrite buffer precisely so recovery can choose a good copy; misarbitration (wrong copy chosen, or checksum accidentally valid) yields silent logical corruption. | InnoDB doublewrite buffer documentation; `innodb_checksum_algorithm` migration incidents; MySQL bug tracker torn-page reports; the whole `innodb_force_recovery` escalation ladder exists for this class. |
| R4 | **Split-brain replication divergence.** A partition or failover lets two primaries accept writes; after healing, both datasets are real and neither is authoritative; reconciliation must be argued from logs and invariants, not timestamps. | GitHub's October 2018 post-mortem (43-second partition, orphaned writes on the old primary, days of reconciliation); countless MySQL semi-sync/async failover incidents; the reason GTID sets and `errant transaction` tooling exist. |
| R5 | **Backup chains that lie.** Nightly backups of an already-corrupted primary faithfully preserve the corruption; the operator discovers it weeks later and must find the last honest generation and re-derive the truth from logs. | GitLab's January 2017 database incident (five backup/replication mechanisms, none restorable as hoped); silent bit-rot literature (CERN/ZFS studies); the standard PITR-with-gaps war story in every DBA community. |
| R6 | **Logical replay divergence.** Statement/event replay on a replica silently drifts (non-determinism, direct writes to the replica, skipped errors); physical state and event history disagree. | MySQL replication drift tooling (`pt-table-checksum`) exists because this is endemic; MySQL manual's unsafe-statement list. |

The five candidates below are drawn from R1–R5. R6 folds into candidate C4.

---

## Design axes (what "hard" must mean here)

* **Diagnosis** — the artifacts do not announce what went wrong; identifying
  the failure event is most of the work.
* **Global consistency** — no single record can be judged locally; only
  whole-state candidates can be accepted or rejected.
* **Interacting physical artifacts** — several on-disk structures each
  partially wrong or partially stale, arbitrated against each other.
* **Long-horizon reasoning** — the answer depends on correctly ordering or
  attributing events across a long history, not on one snapshot.

Each candidate leads with a different axis. All five keep the format burden
at "moderate, documented, implementable in hours" — the summit is never the
format.

---

## C1 — "The manifest is gone" (LevelDB/RocksDB-format LSM store)

**Axis: global consistency (which files are even live?).  Incident class R1.**

### Scenario

A service's embedded LSM key-value store (LevelDB file format, compression
disabled in the deployment's options) crashed during a compaction. The
directory survived; the MANIFEST and CURRENT files were lost to the same
fault. On disk: **11 SST files, one WAL (`.log`) file, the info LOG file
with rotated predecessor** — including SSTs that were compaction *inputs*
already superseded, and the half-written compaction *output* that was never
committed to any version. The solver must emit the store's true final
key→value table as CSV.

### Why the documented path fails

* The documented recovery (`RepairDB`, or "merge everything, newest sequence
  number wins") is **wrong twice over**, and LevelDB's own documentation
  admits it: (a) obsolete pre-compaction SSTs still contain deleted keys
  whose tombstones were *dropped* during the very compaction that superseded
  them — merging every file resurrects those keys; (b) bottom-level
  compaction **zeroes sequence numbers**, so "newest seqno wins" ranks
  genuinely-newest data *below* stale data and cannot even order the files.
* The only correct route is to reconstruct the live version: which subset of
  SSTs constitutes a legal LSM version (level invariants: disjoint key
  ranges within a level, file-boundary/overlap relations between levels,
  smallest/largest-seqno metadata consistent with WAL contents, the
  uncommitted compaction output excluded because the WAL contains writes it
  could not have seen). That is a **global consistency proof over file
  sets**, and generation enumerates candidate live sets and aborts unless
  exactly one yields a constraint-consistent store and exactly one output.
* Evidence interlocks: the rotated info LOG names compaction start (but its
  tail, which would name the outcome, is lost with the crash) — diagnostic
  color, not an answer key; the WAL's contents overlap some SSTs and not
  others; per-file seqno ranges bracket each other.

### Solvable, deterministic, one expert day

SST block format (footer, index block, data blocks, restart arrays, varint
key sharing) and the WAL record format are documented and implementable in
2–4 hours without compression. The fixture is generated by driving a real
LevelDB build (or ldb/RocksDB in LevelDB-compat mode) through a scripted
history and killing it at the designed instant with a filesystem-level copy;
MANIFEST/CURRENT are then genuinely absent from the captured set because the
capture models the incident (the fault takes the two named files — this is
file *loss*, which unlike fabricated internal state does not require
byte-level authenticity gymnastics; the SSTs and WAL are untouched bytes).
Verifier: one CSV of key,value pairs, exact match, outcome-only.

### Frontier red-team

* *Attack 1: merge-all by seqno.* Fails by design — resurrections + zeroed
  seqnos; produces a plausible CSV wrong on many keys. This is the primary
  trap and mirrors what `RepairDB` really does.
* *Attack 2: reimplement RepairDB from the source.* Same wrong answer; the
  folklore fix is the trap.
* *Attack 3: brute-force subsets of SSTs.* This is essentially the intended
  solution — but only if the agent first *conceives* that the live set is
  the unknown, formulates the version invariants, and proves uniqueness.
  That is inference, not documentation lookup; an agent that reaches it has
  done the intended work.
* *Attack 4: read the info LOG like an answer sheet.* The LOG is truncated
  before the decisive line, deliberately; it narrows the hypothesis space
  (good — that's diagnosis) without deciding it.
* *Reduction test:* the format work is hours, then the agent faces a
  choose-the-version problem no document answers. **Passes the filter.**
* *Residual risks:* (a) generation must prove no single heuristic (e.g.
  "exclude files not named by WAL overlap") accidentally suffices — the
  candidate-set enumeration at generation time is the guard; (b) the SST
  count must stay small enough that expert enumeration is comfortable but
  large enough that the subset lattice isn't trivial (11 files, ~2^11 raw,
  far fewer after unary constraints — right-sized).

---

## C2 — "The table is gone; the indexes remember" (SQLite, no WAL involvement)

**Axis: combinatorial reconstruction from partial projections.  Incident class R2.**

### Scenario

An operator's misdirected `dd` zeroed a contiguous region of a production
SQLite database file — the region holding the main table's b-tree interior
and leaf pages. Untouched: four secondary indexes on that table (each a
partial projection: different column subsets), a small intact side table,
and a **hot rollback journal** left by a transaction that was in flight at
the moment of the accident. The solver must emit the table's true committed
rows as CSV.

### Why the documented path fails

* SQLite's own salvage path (`.recover` / `lost_and_found`) reads table
  pages; the table pages are zeros. Index entries surfaced as
  `lost_and_found` fragments are keyed but column-incomplete and — the trap
  — include **phantom entries**: the in-flight transaction had already
  modified index b-trees before the accident, and only the hot journal
  proves which index pages carry uncommitted state. Blind salvage emits rows
  that never committed and misses rows whose entries were displaced.
* Correct reconstruction is a join-and-arbitrate problem: each index is a
  projection (columns…, rowid); rows must be reassembled across four
  projections; the hot journal's page pre-images must be *applied* to the
  index trees first (that is what real rollback would have done) to erase
  the phantom state; and the reassembly is only accepted when the complete
  candidate table satisfies the schema's UNIQUE constraints, the side
  table's foreign keys, and cross-index agreement. Generation enumerates
  where entries conflict and aborts unless exactly one assembled table
  survives.
* The long pole is *not* the b-tree format (documented, half a day at most,
  and the standard library's `sqlite3` cannot open the mutilated file
  usefully anyway): it is realizing that journal rollback must be reasoned
  about **selectively** (the journal also contains pre-images of *zeroed*
  pages — applying those would "helpfully" restore table pages to a state
  *older* than committed truth, the second trap), and that the projections
  only pin a unique table jointly.

### Solvable, deterministic, one expert day

Fixture generated by driving real SQLite through a scripted history,
capturing mid-transaction (journal hot by construction — SQLite leaves it
hot on process kill), then modelling the operator accident as the file-level
zeroing it really is (region overwrite of the *copy that the incident
defines* — like C1's lost MANIFEST, external damage is the one thing that
is authentic to fabricate, because the incident *is* external damage).
Uniqueness machine-proven at generation. Verifier: one CSV, exact rows.

### Frontier red-team

* *Attack 1: `sqlite3 .recover`.* Ships as a graded negative; emits phantom
  rows and incomplete columns; fails.
* *Attack 2: naive index join on rowid.* Close — but wrong three ways:
  phantoms included, journal-displaced entries missed, and one index is
  partial (`WHERE` clause) so its absence-signals must not be read as row
  absence. Systemically wrong; graded negative.
* *Attack 3: apply the whole journal, then join.* Restores pre-transaction
  *and pre-commit* state for pages whose committed version lived only in
  the zeroed region — wrong the other direction; graded negative.
* *Reduction test:* format lookup gives the b-tree and journal layouts;
  no document states which journal pages to apply or how to arbitrate
  conflicting projections — that requires modelling what SQLite *would have
  done* and proving global consistency. **Passes the filter.**
* *Residual risks:* (a) partial-index semantics must be spelled out in the
  shipped schema, or ambiguity leaks in; (b) must verify `.recover` (and
  any bundled SQLite CLI) really does fail — measured, not assumed; (c) the
  join must not be pinnable from any *single* covering index — generation
  asserts no index covers all columns and no two indexes jointly cover all
  rows without arbitration.

---

## C3 — "Choose your page" (MySQL/InnoDB torn-write arbitration)

**Axis: interacting physical artifacts.  Incident class R3.**

### Scenario

Power loss tore several in-flight page writes in a small InnoDB tablespace
(`.ibd`, compact row format, one table + one secondary index). Recovered
from the machine: the tablespace file (some pages torn, some fine, some
stale), the **doublewrite buffer** extracted from the system tablespace
(holding recent full page images — some newer than the tablespace's copy,
some older), and the tail segment of the redo log — which is *too short* to
run real recovery (its start LSN postdates several torn pages' last
checkpoint), so `innodb_force_recovery` folklore cannot save it. Output:
the table's true committed rows as CSV.

### Why the documented path fails

* The documented arbitration is local: "if the page checksum fails, take
  the doublewrite copy." Three designed situations break it: (a) a torn
  page whose halves happen to satisfy the legacy checksum (the fixture
  constructs this honestly by choosing which real page images collide under
  the deployment's `innodb_checksum_algorithm=none|crc32` history — a real
  migration hazard); (b) pages where *both* copies are checksum-valid but
  only one is consistent with the B-tree's sibling pointers and the
  secondary index's record set; (c) a page whose only correct image must be
  taken from the *older* copy plus one redo record from the surviving tail.
* So per-page choice is a variable, and the constraint system is global:
  B-tree linkage (prev/next page pointers, key-range monotonicity across
  siblings), primary-vs-secondary index record agreement, transaction-level
  atomicity (a torn commit's rows appear in both trees or neither), and
  the redo tail's LSN arithmetic bounding which image can be current.
  Generation enumerates the per-page image assignments and aborts unless
  exactly one global assembly is consistent and yields one output.

### Solvable, deterministic, one expert day — with one honest caveat

FIL header/trailer, index page header, compact records for a narrow table:
documented (and third-party-documented to death) — a solid half day. The
caveat: redo record decoding is the one InnoDB area that could blow the
budget, so the design uses **at most a handful of redo records of one
simple type** (MLOG_WRITE_STRING-class byte writes), and the expert-day
claim is contingent on holding that line. Fixture from a real MySQL server
driven through the history; torn pages produced by capturing the true
before/after images the server itself wrote at different instants (the
incident — interleaving sector-level old/new halves — is external damage,
authored at the sector level exactly as power loss authors it).

### Frontier red-team

* *Attack 1: checksum-then-doublewrite, the manual's rule.* Wrong on the
  three designed page classes; systemically wrong output; the flagship
  negative.
* *Attack 2: take the newest LSN per page.* Violates atomicity and B-tree
  linkage in designed places (a newest image can belong to the torn,
  never-committed write); graded negative.
* *Attack 3: run a real MySQL server / innodb_force_recovery.* No server in
  the container, and the redo tail is genuinely insufficient — the fixture
  models exactly the case the escalation ladder abandons.
* *Reduction test:* every format involved is documented; no document tells
  you which of two checksum-valid images is true — only the global assembly
  does. **Passes the filter.**
* *Residual risks:* (a) biggest implementation risk of the five (InnoDB
  breadth) — scope discipline is the mitigation and the kill criterion;
  (b) checksum-collision construction must be honest (choose real images
  that collide, never hand-edit bytes); (c) generation must prove no
  single local heuristic (checksums, LSN max, doublewrite-always) survives.

---

## C4 — "Forty-three seconds of split brain" (replicated pair reconciliation)

**Axis: long-horizon causal reasoning.  Incident classes R4 + R6.**

### Scenario

Modelled on the GitHub 2018 pattern. A two-site replicated service (each
site an embedded SQL database; replication is row-based events with
GTID-style per-site sequence sets, shipped as an event log — the on-disk
format is ordinary tables plus an append-only event file per site) suffered
a partition; both sites accepted writes for a window; then site B crashed
and lost its *most recent* event-log segment, while site A kept running and
compacted (purged) its own oldest events. The solver receives: site A's
database + surviving event segments, site B's database + surviving event
segments. Neither artifact alone covers the whole history. Output: the
unique correct converged table state (and, as a second column of the same
CSV artifact, per-row provenance: which site's write survives).

### Why the documented path fails

* The documented plays — "latest timestamp wins", "replay all events over a
  base", "pick the failover winner wholesale" — are each wrong by designed
  construction: clock skew between sites is real and stated; neither event
  history is complete, so pure replay is impossible; and the winning site
  demonstrably *missed* durable writes that only exist in site B's tables
  (their events were in the lost segment — the tables themselves are the
  only witness, the GitHub post-mortem's exact situation).
* The intended reasoning is causal reconstruction: GTID-set arithmetic says
  which events each site had applied; rows present in a site's tables
  without a surviving event prove membership in the lost segment; app-level
  invariants (uniqueness, foreign keys, a monotonically-numbered ledger
  with no gaps, cross-references where one site's write observably built on
  the other's) order the concurrent windows; and the converged state is
  accepted only when the whole reconstruction is consistent. Generation
  enumerates the reconcilable orderings/attributions and aborts unless the
  *state* (not necessarily the interleaving) is unique.
* Long-horizon: several hundred events across the timeline, three distinct
  phases (healthy, partitioned, post-heal degraded), and early decisions
  (which segment was lost, which GTIDs are errant) cascade into every later
  attribution.

### Solvable, deterministic, one expert day

No exotic formats at all — the difficulty budget is 100% reasoning, which
is the point of including one such candidate. Fixture: script the two-site
history against two real embedded databases with a tiny event-shipping
harness (the harness is fixture machinery, not a solver-facing format —
events are shipped in a plain documented record layout), kill/purge per the
scenario. Uniqueness machine-proven by enumerating attribution candidates.

### Frontier red-team

* *Attack 1: last-writer-wins by timestamp.* Designed skew makes it wrong
  on a measured set of rows; flagship negative.
* *Attack 2: replay site A's events over a snapshot.* Loses site B's
  segment-lost writes entirely; negative.
* *Attack 3: union both tables, dedupe by key.* Violates the invariants in
  designed places (both sites wrote the same key differently; unions keep
  the wrong one or both); negative.
* *Reduction test:* there is no format to look up and no rule book; either
  the agent constructs the causal argument or it picks a heuristic that is
  measurably wrong. **Passes the filter** — its risk is elsewhere:
* *Residual risks:* (a) *contrivance* — invariants must feel like a real
  application's, not a puzzle's; the ledger-with-references design needs a
  reviewer-facing justification; (b) determinism discipline: concurrent
  independent writes must never require an arbitrary order to produce the
  state — generation must prove state-uniqueness, else redesign; (c) this
  candidate is the most exposed to "the reasoning is the whole task"
  criticism from reviewers who expect binary forensics — mitigated by the
  event files and GTID arithmetic being genuinely binary/positional.

---

## C5 — "Which night did the backups start lying?" (backup-chain forensics)

**Axis: diagnosis across time.  Incident class R5 (with R6 seasoning).**

### Scenario

A host with failing RAM silently flipped bits in a production embedded
database for weeks; the nightly backup job faithfully archived each night's
(possibly already-poisoned) state. The solver receives: **five nightly
snapshot files** (S1…S5, oldest to newest), the application's append-only
**statement/audit log covering only the last three nights** (log rotation
discarded the rest), and the schema with its constraints. Output: the true
current table state as CSV — plus nothing else; the verifier stays
outcome-only.

### Why the documented path fails

* "Restore the newest backup" — restores three weeks of poison; the state
  is constraint-clean to the eye (bit flips were designed into values, not
  structure) and wrong on a measured set of rows.
* "Restore the oldest backup and replay the log" — the log doesn't reach
  back that far; replaying its window over S1 or S2 produces rows that
  never existed (the classic PITR-gap blunder); wrong differently.
* The real problem is *locating the divergence*: for each adjacent snapshot
  pair inside the log window, replaying the log's sub-window over the older
  snapshot must reproduce the newer one exactly — where it does, both
  snapshots and that log span are jointly certified; where it does not, the
  mismatch set localizes the poison (which snapshot, which pages/rows,
  whether the flip predates or postdates the logged write that touched the
  row — a per-row interleaving argument). Rows never touched by the logged
  window must be arbitrated *across generations* (a value stable across
  S1–S4 that changes in S5 with no logged cause is poison in S5; a value
  that changes at S3 with no logged cause but *with* a logged read-modify-
  write depending on it forces the flip *before* that statement, which
  propagates a correction through the replay). Generation constructs the
  flip schedule so that exactly one globally consistent history — and one
  final state — survives, and proves it by enumeration over flip-placement
  hypotheses.
* Long-horizon and diagnostic: the agent must reason about *when* each
  discrepancy entered, and corrections cascade forward through dependent
  logged statements.

### Solvable, deterministic, one expert day

Lightest build of the five: real embedded database driven through a
scripted multi-night history with the bit-flip fault injected at the
*storage layer between nights* (the incident is external hardware damage —
authentically modelled as such, like C1's lost files and C2's zeroed
region; every database-internal byte remains server-written). Snapshots are
verbatim file copies per night. The statement log is the application's own
audit trail (a real, common artifact), not an oracle: it is incomplete by
rotation, which is the entire problem. Uniqueness machine-proven.

### Frontier red-team

* *Attack 1: newest backup.* Measured-wrong; flagship negative and utterly
  plausible-looking.
* *Attack 2: oldest + full replay.* Impossible (gap); oldest + windowed
  replay: wrong; negative.
* *Attack 3: majority vote across snapshots per row.* Poison that entered
  early wins the vote; designed to fail measurably; negative.
* *Attack 4: replay-and-diff without the interleaving argument.* Finds
  *that* snapshots disagree but corrects in the wrong direction on the
  rows where the flip predates a dependent logged write; the designed
  read-modify-write chains punish it; negative.
* *Reduction test:* formats are trivial on purpose; nothing in any manual
  describes the certification-by-replay argument or the flip-placement
  logic. **Passes the filter.**
* *Residual risks:* (a) replay determinism must be airtight — the log must
  record values, not nondeterministic expressions, and the doc must justify
  that as normal audit-log practice; (b) the "detect via replay-and-diff"
  core idea is conceptually clean enough that a strong agent may derive it
  — the flip-before-dependent-write chains are what keep the last mile
  hard, and generation must verify the naive direction is wrong on ≥ a
  systemic number of rows; (c) two SQLite-based candidates in one slate
  (C2, C5) — acceptable because the architectures share nothing, but a
  tournament judge may prefer engine spread; C5's design is engine-agnostic
  and could be re-based onto another embedded engine if chosen.

---

## Cross-candidate comparison and ranking

| | C1 LSM manifest | C2 indexes-remember | C3 torn pages | C4 split brain | C5 backup chain |
| --- | --- | --- | --- | --- | --- |
| primary axis | global consistency | combinatorial reconstruction | artifact arbitration | long-horizon causality | diagnosis over time |
| engine | LevelDB-format LSM | SQLite | MySQL/InnoDB | replicated pair | embedded SQL |
| format burden | moderate | moderate | **high** | low | low |
| authenticity burden | low (file loss is external) | low (external damage) | **high** (torn sectors, checksum collisions) | medium (harness realism) | low (external damage) |
| anti-parser strength | strong (folklore repair is the trap) | strong (folklore salvage is the trap) | strong (the manual's rule is the trap) | strong (no rule book exists) | strong (every folklore restore is a trap) |
| uniqueness proof | subset enumeration | assembly enumeration | image-assignment enumeration | attribution enumeration | flip-placement enumeration |
| contrivance risk | low | medium (index coverage design) | low-medium | **high-medium** (invariant design) | medium (log completeness framing) |
| expert-day confidence | high | high | **borderline** | high | high |
| headline trap | RepairDB resurrects deleted keys | `.recover` emits phantoms | "checksum fails → doublewrite" picks wrong images | LWW / replay / union all wrong | newest backup restores the poison |

**Ranking.**

1. **C1 — LSM manifest reconstruction.** Best difficulty-per-risk of the
   slate: the documented recovery path is itself the trap (and provably
   produces resurrections), the inference core (which files constitute the
   version) is genuinely global, authenticity is cheap because the incident
   is file loss, and everything fits comfortably in an expert day.
2. **C5 — backup-chain forensics.** Purest diagnosis candidate; nearly zero
   format tax so the entire budget buys reasoning; the flip-before-
   dependent-write chains give it a real last mile. Slightly behind C1 only
   because its core insight (certify snapshot pairs by replay) is derivable
   by a very strong agent, after which the remaining work, while still
   subtle, is smaller than C1's.
3. **C2 — indexes-remember.** Strong and thematically satisfying; ranked
   third for the design care its uniqueness needs (index coverage, partial
   index, journal selectivity all have to be balanced by hand).
4. **C4 — split brain.** The most novel axis and the least parseable, but
   the most exposed to contrivance criticism and to state-uniqueness
   design failure; it needs the most adversarial pre-review of its
   invariants.
5. **C3 — torn pages.** Excellent on paper; ranked last purely on
   execution risk: InnoDB breadth plus honest torn-sector/checksum-collision
   construction is the one slate member that could blow the one-day budget,
   and its authenticity bar (never hand-author bytes that the incident
   itself wouldn't author) is the hardest to clear.

**Tournament recommendation: enter C1, with C5 as the alternate.**

---

## Appendix — candidates considered and self-rejected

| candidate | why rejected |
| --- | --- |
| InnoDB undo-log read-view reconstruction | Isomorphic to the existing PostgreSQL MVCC task (snapshot visibility from transaction metadata) — banned by rule 1 regardless of engine. |
| XA / two-phase-commit in-doubt outcome resolution | Structurally the previous task again: decide commit/abort for a set of in-doubt transactions under global constraints. Different substrate, same architecture. |
| SQLite WAL frame salvage variants (checksum-chain break, salvage past a torn frame) | Collides with the repo's existing `sqlite-wal-crash-recovery` task. |
| "Parse the FTS/blob/overflow format" candidates (SQLite FTS desync, InnoDB off-page blobs, TOAST-alikes) | Fail the anti-parser filter outright: identify format → read docs → implement parser → apply rules. |
| B-tree page-split forensic ("find the interrupted split and stitch it") | Documented recovery algorithms exist end-to-end; a careful parser plus the algorithm solves it locally; no global inference survives red-teaming. |
| Query-statistics / planner poisoning forensics | Outcome is not a deterministic data artifact; verification would drift toward judging methodology. |
| Full PITR with archive-gap (restore + roll forward, pick latest consistent point) | Bookkeeping once formats are read; the "latest recoverable point" falls out of documented rules. C5 keeps the interesting kernel (gap reasoning) and discards the bookkeeping. |

*Nothing in this file is implemented. No fixture, generator, prompt, or
verifier for any candidate exists yet.*
