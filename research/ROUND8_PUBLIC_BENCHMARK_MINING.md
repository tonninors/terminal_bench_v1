# Round 8 — Public benchmark mining (analysis only)

Status: **analysis only — no task designed or implemented.**
Primary sources, all public and inspected directly this round:
- the Terminal-Bench 2.0 task set (`github.com/laude-institute/terminal-bench-2`, 89 tasks, cloned and parsed);
- the Harbor registry (`registry.json`: TB-2.0 = 89 tasks, TB-Pro = 200, plus 80 adapters);
- the official 2.0→2.1 per-task delta table and release notes (`tbench.ai/news/terminal-bench-2-1`);
- **PR #53** (`harbor-framework/terminal-bench-2#53`), the task-by-task 2.1 change log — the single richest artifact-vs-difficulty source;
- the Snorkel and Artificial-Analysis leaderboards (aggregate + delta numbers);
- task `task.toml` metadata (expert/junior time estimates, categories, resource caps) and `instruction.md` bodies for the full hard tier.

Evidence caveat, stated up front: TB reports **agent+model pass rates, not per-task-per-model failure transcripts publicly**. Run traces are not published, so §3's failure classification is inferred from task structure, verifier code, expert/junior time ratios, and the 2.1 change reasons — not from raw transcripts. Every inference is labelled.

---

## 1. Public hard-task sample (≥15; the full 30-task hard tier)

TB-2.0 difficulty distribution (measured from `task.toml`): 4 easy / 55 medium / 30 hard. Published pass@5 by tier: **easy 25%, medium 14.5%, hard 10%** — and "frontier systems still fail 97%+ of the hardest tasks." Expert-time estimates for the hard tier: median **330 min**, range 15 min–2400 min. All 30 hard tasks set `allow_internet = true` (none is a sealed-artifact task — the opposite of V1–V7).

The hard tier, with structure fields I coded from instruction + test inspection (INT=interactive/stateful env; ANN=failure mode announced in prompt; REV=solving requires revising an early hypothesis; MUT=env mutates during solving; XTOOL=multiple tools/subsystems coordinated):

| task | category | expert min | INT | ANN | REV | MUT | XTOOL | inferred main failure mode |
| --- | --- | --- | --- | --- | --- | --- | --- | --- |
| gpt2-codegolf | SW-eng | 2400 | – | Y | Y | – | Y | size-constrained numerics under a 5 KB ceiling; correct-but-too-big |
| fix-ocaml-gc | SW-eng | 1440 | Y | Y | Y | Y | Y | debug a real GC bug in a bootstrapping compiler; long build loop |
| regex-chess | SW-eng | 1440 | – | Y | Y | – | – | implement chess move-gen as pure regex; deep constraint encoding |
| write-compressor | SW-eng | 1440 | – | Y | – | – | Y | ratio + roundtrip target; iterative refinement |
| circuit-fibsqrt | SW-eng | 960 | – | Y | Y | – | Y | digital-logic synthesis to a spec |
| feal-linear-cryptanalysis | math | 960 | – | Y | Y | – | – | full cryptanalytic attack implementation |
| sparql-university | data-query | 800 | – | Y | Y | – | – | multi-constraint SPARQL over an ontology; get all clauses right at once |
| sam-cell-seg | data-sci | 600 | – | Y | – | – | Y | ML pipeline to an accuracy bar |
| bn-fit-modify | sci-comp | 480 | – | Y | Y | – | Y | modify a fitted Bayesian model correctly |
| feal-differential-cryptanalysis | math | 480 | – | Y | Y | – | – | as above (differential) |
| make-doom-for-mips | SW-eng | 480 | Y | Y | Y | Y | Y | cross-compile + run DOOM on a MIPS target; toolchain+linkage |
| make-mips-interpreter | SW-eng | 480 | Y | Y | Y | Y | Y | implement a MIPS VM + syscalls until DOOM boots |
| model-extraction-relu-logits | math | 480 | – | Y | Y | – | – | extract NN weights from logit queries |
| video-processing | video | 400 | – | Y | – | – | Y | multi-stage AV pipeline |
| path-tracing | SW-eng | 360 | – | Y | Y | – | Y | implement a renderer to a reference image |
| install-windows-3.11 | sys-admin | 300 | Y | Y | Y | Y | Y | drive a QEMU VM to a booted GUI state via monitor socket |
| torch-pipeline-parallelism | SW-eng | 240 | – | Y | Y | – | Y | correct pipeline-parallel schedule; shape/ordering bugs |
| torch-tensor-parallelism | SW-eng | 240 | – | Y | Y | – | Y | correct tensor-parallel sharding; double-scatter shape bug |
| mcmc-sampling-stan | data-sci | 180 | – | Y | – | – | Y | statistical model to a convergence bar |
| polyglot-rust-c | SW-eng | 180 | – | Y | – | – | Y | one source valid in two languages |
| cancel-async-tasks | SW-eng | 120 | Y | Y | Y | Y | – | async cancellation semantics incl. the *queued-task* edge case |
| extract-moves-from-video | file-ops | 120 | – | Y | – | – | Y | CV extraction pipeline |
| fix-code-vulnerability | security | 120 | – | Y | Y | – | – | find + classify + fix a CWE in a real codebase |
| path-tracing-reverse | SW-eng | 120 | Y | Y | Y | – | Y | reverse a binary to <2 KB equivalent C |
| password-recovery | security | 100 | Y | Y | Y | Y | Y | forensic recovery of a deleted file's content |
| dna-assembly | sci-comp | 60 | – | Y | – | – | Y | bioinformatics assembly to a metric |
| protein-assembly | sci-comp | 60 | – | Y | – | – | Y | as above |
| llm-inference-batching-scheduler | ML | 45 | – | Y | Y | – | – | shape-aware batching optimum |
| train-fasttext | model-train | 30 | – | Y | – | – | Y | train to an accuracy target |
| configure-git-webserver | sys-admin | 15 | Y | Y | Y | Y | Y | multi-service wiring: git push → hook → webserver |

Structural totals over the hard tier: **INT 9/30, MUT 7/30, XTOOL ~21/30, REV ~20/30, ANN 30/30.** Every hard task *announces its goal* (none is blind), and difficulty concentrates in **long-horizon implementation/debugging that must survive an execution loop** — build, run, observe failure, revise — not in a single inference.

## 2. Feature matrix vs V1–V7

Scoring each family 0/1 (– / ●) on the requested columns. V1–V7 collapse to two rows because they share physics (static forensic reconstruction; R5–R6 add one live-repair row; R7 adds one live-diagnosis row).

| feature | TB hard tier (modal) | V1–V5 static forensics | R5–R6 concurrency repair | R7 blind diagnosis |
| --- | --- | --- | --- | --- |
| static artifact reasoning | ● (some) | ● | – | – |
| live-state inspection | ● | – | ● | ● |
| **environment mutation during solving** | ● (7/30) | – | – | – |
| **long-horizon state tracking** | ● | – | – | – (minutes) |
| **irreversible / dependent actions** | ● (build/VM/FS) | – | – | – |
| cross-tool coordination | ● (21/30) | – | – | – |
| cross-subsystem diagnosis | ● (some) | – | – | ● |
| search-space size | large | bounded (2^15) | tiny | tiny |
| cheap self-checker available | often (build/run) | ● (the trap) | ● | ● |
| standard-pattern solution | mixed | ● | ● | ● |
| training-data saturation | mixed | high | high | high |
| **interactive terminal behavior** | ● (9/30) | – | – | – (query only) |
| **need to recover from a failed attempt** | ● (20/30) | – | partial | – |
| local-vs-global reasoning | both | global | global | global |
| output determinism | ● (all TB verifiers) | ● | ● | ● |
| expert time | median 330 min | ~4–8 h claimed | ~1–3 h | minutes |

**Features strongly present in public hard tasks but absent from V1–V7 (the gap set):**

1. **Environment mutation during solving + irreversibility** — the agent's own actions (a build that half-completes, a VM driven into a state, a filesystem edited, a git history rewritten) change the world it must continue reasoning about. 7/30 hard tasks are MUT; **0/7 of my families are.** Every V1–V7 task is a pure function from a frozen artifact set (or a one-shot fix) to an output; nothing the solver does mid-task alters the diagnostic surface except R7's trivial one-hop.
2. **Long-horizon execution loops with recovery from failed attempts** — the modal hard-task solution is *build → run → observe → revise* repeated over minutes-to-hours (fix-ocaml-gc, make-mips-interpreter, install-windows-3.11). 20/30 require recovering from a wrong first attempt. **My tasks are all single-shot**: the answer is computed once and graded; there is no execution loop the agent must survive, and R7 measured the fatal consequence — a strong solver finished in 3.4 minutes because there was no loop to get lost in.
3. **Interactive/stateful environment driving** — 9/30 drive a live process to a target state (QEMU monitor socket, a git server round-trip, an async runtime's cancellation edge case). **0/7 of mine** require holding a live process in a particular state across steps.

These three are one coherent cluster: **the difficulty in the public hard tier lives in *staying correct across a long, stateful, partly-irreversible execution*, not in *computing a hard answer from a fixed input*.** V1–V7 optimized entirely the second axis — and the frontier is strong there (V5 solved byte-exact).

## 3. Failure-trace classification (inferred; no public transcripts)

Mapped the requested failure taxonomy onto the hard tier using structure + 2.1 change reasons:

- **correct diagnosis, wrong execution** — the dominant inferred mode: torch-*-parallelism (right algorithm, shape bug), gpt2-codegolf (right approach, over the size cap), write-compressor (right method, misses the ratio). The 30×-plus junior/expert time ratios (fix-ocaml-gc 14400/1440; feal 19200/960) mark tasks where *knowing* the approach is cheap and *executing it correctly* is the whole cost — the inverse of my tasks.
- **loses state across a long workflow / cannot recover after an early destructive choice** — structurally reachable only in MUT tasks (install-windows-3.11, make-doom-for-mips, configure-git-webserver); **structurally impossible in V1–V7.**
- **fixes first symptom and stops** — the one mode R7 targeted; measured ineffective there (one-hop landscape).
- **wrong subsystem selected / assumes conventional layout** — fix-ocaml-gc, fix-code-vulnerability.
- **tool interaction failure** — the whole XTOOL column.

The taxonomy itself is evidence: **five of its ten entries are workflow/state/recovery failures that a single-shot task cannot exhibit.** V1–V7 could only ever exercise the "wrong answer" entries — and did, and lost.

## 4. Terminal-Bench 2.1 corrections — the negative-example catalogue

PR #53 + the delta table give a clean separation of *artifact difficulty* from *genuine difficulty*. The 2.1 fixes fell into three buckets, all **things that made a task look hard for non-difficulty reasons** — our negative examples:

- **External-dependency drift (9 tasks):** remote fetches that rotted — extract-moves-from-video (remote video host), protein-assembly (RCSB FASTA API URL changed), mteb-retrieve/mteb-leaderboard (transitive pip resolution to incompatible majors), make-doom-for-mips (stale apt index → 404). *Lesson: any solver-visible dependency on a moving external resource is fake difficulty; V1–V7's sealed-artifact discipline was right on this axis.*
- **Resource-cap misspecification (8 tasks):** 4 GB→8 GB bumps (torch-*, filter-js-from-html, mteb-*), `nproc`-returns-host-count wrapper (caffe-cifar-10, compile-compcert), timeout raises. *Lesson: difficulty that is really "the oracle can't fit in the cap" is a bug. Determinism includes resource determinism.*
- **Task misspecification (11 tasks):** description ≠ test — query-optimize asked PostgreSQL but tested Spark SQL (rewritten); sam-cell-seg arg-naming; torch-tensor-parallelism's pre-scattered-input clarification; install-windows-3.11's monitor-socket path; **filter-js-from-html's still-open worry** that stating the exact BeautifulSoup normalization "makes the task too easy." *Lesson: the line between "underspecified" and "trivial once specified" is thin and is a live reviewer concern — exactly the failure that killed R7 (fully specified ⇒ retrieval walk) and the C5 pincer (unspecified ⇒ ill-posed).*

Notably: several genuinely hard tasks moved **0% or negative** in 2.1 (gpt2-codegolf +0.0; mcmc-sampling-stan −10.0; overfull-hbox −18.6; query-optimize −7.1; protein-assembly −7.1) — difficulty that *survived* the artifact cleanup is the real signal, and it clusters in the long-horizon-execution and precise-numeric-target tasks, **not** in any static-inference task.

## 5. Database/systems filter — start from the failure mode, not the bug

The corpus already contains the static DB families and rates them **medium, not hard**: `db-wal-recovery` (WAL salvage — the existing repo task's cousin), `sqlite-db-truncate` (truncated-file row recovery), `query-optimize` (rewrite to identical output), `pytorch-model-recovery`, `git-leak-recovery`, `sqlite-with-gcov`. This is direct confirmation of the six-round result: **single-artifact database reconstruction/optimization tops out at medium.** No hard task in TB-2.0 is a static database-forensics task. The hard DB-adjacent difficulty, where it exists, is in execution loops (build/run/verify), not inference.

Applying the mandated direction — *begin from an empirically observed agent failure mode, then ask what DB task naturally needs that capability* — to the gap set from §2:

- Failure mode **"loses state / can't recover across a long stateful workflow"** → what DB work naturally needs it? A **multi-stage migration/cutover** where each step commits real state and a wrong early step is expensive to unwind.
- Failure mode **"correct diagnosis, wrong execution across an irreversible sequence"** → a **live remediation under an evolving catalog** where the agent must run, observe the changed catalog, and continue — the R7 interdependence idea, but scaled from one hop to a genuine loop.

## 6. At most two abstract database-task directions

### Direction D1 — Long-horizon online migration/cutover under an execution loop

**EMPIRICAL FAILURE PROPERTY:** long-horizon state tracking + irreversible/dependent actions + recover-from-failed-attempt — the cluster owning the highest-expert-time TB hard tasks (make-mips-interpreter, install-windows-3.11, fix-ocaml-gc), where the junior/expert ratio shows *execution*, not *knowing*, is the cost.
**WHY V1–V7 DID NOT TEST IT:** every V1–V7 task is single-shot — a frozen artifact set graded once (R7 measured the consequence: 3.4-minute solve, no loop to survive). None had the agent commit intermediate state that reshapes the remaining work.
**HOW IT MAPS TO DATABASE WORK:** a schema/engine **migration with a real cutover** — e.g. transform a live dataset across N dependent DDL+backfill+swap stages where stage k's correctness depends on stage k-1's committed result, an intentionally plausible wrong ordering corrupts a later stage, and the agent must detect the corruption from the database's own state and repair forward (not reset — reset loses required history). Difficulty is *maintaining a correct model of evolving committed state across the sequence*, the exact thing single-shot tasks cannot test.
**DETERMINISTIC VERIFICATION:** fresh incident → apply the submitted `migrate.sh` once → assert final schema + data invariants + preserved history + behavioral checks; outcome-only, no methodology, no concurrency (the *sequence* is long-horizon, not *parallel* — sidestepping the Round-5/6 determinism risk). Every stage is deterministic SQL.
**WHY NOT A COPY:** TB has no multi-stage migration task; `query-optimize`/`db-wal-recovery` are single-shot medium tasks. It shares no content with any existing task and is not another forensic reconstruction.
**Honest risk (must be piloted, not assumed):** a migration whose stages are each individually standard could collapse to "apply N known transforms" — the F10 failure. The gating question a pilot must answer: does a wrong early stage produce a *diagnostic surface that misleads the obvious forward fix*, forcing revision? If not, it is medium.

### Direction D2 — Drive-a-live-process-to-a-target-state database operations

**EMPIRICAL FAILURE PROPERTY:** interactive/stateful environment driving + cross-tool coordination — the INT+XTOOL cluster (configure-git-webserver, install-windows-3.11, cancel-async-tasks' queued-task edge case), where difficulty is holding a live system in a precise state across steps, not computing an answer.
**WHY V1–V7 DID NOT TEST IT:** all my tasks read frozen files or apply one fix; none drove a live server through a stateful protocol to a target.
**HOW IT MAPS TO DATABASE WORK:** an operational objective requiring a **live multi-component database topology** driven to a specific consistent state — e.g. establish and prove a working logical-replication + failover posture across two live servers such that a specified failover leaves an exactly-defined committed state, wiring replication + a promotion step + application reconnection (the DB analogue of configure-git-webserver's push→hook→serve chain).
**DETERMINISTIC VERIFICATION:** after the agent's `setup.sh`, the verifier performs a *scripted deterministic* failover and asserts the resulting committed state and that specified operations still succeed — end-state invariants, never latency, never races the agent didn't control.
**WHY NOT A COPY:** no TB task drives a DB replication topology; the split-brain idea from R2/R4 was *forensic reconstruction of a past divergence*, whereas this is *operating a live topology to a target* — a different physics (the very gap this round found).
**Honest risk (higher than D1):** Rounds 5–6 showed live multi-component verification carries the worst determinism exposure; the scripted-failover design contains it only if every timing point is harness-controlled. If any required symptom depends on race manifestation, it fails GATE (determinism). D2 is ranked second for exactly this reason.

## 7. Decision

**B. TWO PLAUSIBLE CAPABILITY GAPS FOUND.**

The mining is conclusive on the diagnosis, if not yet on a task: **V1–V7 tested only the "compute a hard answer from a frozen input" axis, and the public hard tier's difficulty lives almost entirely on the orthogonal "stay correct across a long, stateful, partly-irreversible execution" axis** — a cluster (env-mutation, long-horizon recovery, interactive driving) present in ~20–23 of 30 hard tasks and in 0 of my 7 families. Two database directions map onto that cluster without copying any existing task: **D1 (long-horizon migration/cutover with a forward-repair loop)** as the primary, lower-determinism-risk candidate, and **D2 (driving a live DB topology to a target state)** as the secondary, higher-risk one. Neither is proposed for implementation. Per the standing rule earned in Rounds 6–7, the next step for whichever direction is chosen is a **cheap independent fresh-context pilot before any build** — specifically to answer each direction's stated gating risk, since designer-side success would prove nothing.

---

Sources: [terminal-bench-2 tasks](https://github.com/laude-institute/terminal-bench-2) ·
[Harbor registry](https://github.com/laude-institute/harbor) ·
[TB 2.1 release notes](https://www.tbench.ai/news/terminal-bench-2-1) ·
[PR #53 task-by-task changes](https://github.com/harbor-framework/terminal-bench-2/pull/53) ·
[Snorkel TB 2.1 leaderboard](https://snorkel.ai/leaderboard/terminal-bench-2-1/) ·
[Snorkel TB 2.0 leaderboard](https://snorkel.ai/leaderboard/terminal-bench-2-0/) ·
[Artificial Analysis TB v2.1](https://artificialanalysis.ai/evaluations/terminalbench-v2-1) ·
[Artificial Analysis TB-Hard](https://artificialanalysis.ai/evaluations/terminalbench-hard)
