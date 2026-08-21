# Status: FINAL ATTEMPT ABANDONED — NOTHING TO UPLOAD, STOPPING

`dist/` is intentionally empty. No fixture from this task line may be
shipped, and per the brief no further fixture will be designed.

The fourth attempt was not rejected by a fresh-context agent — it never
reached one. It failed my own validation (§9: "verify the reference
repair fixes it"), and the reason generalises into an argument that the
constraint set in the final brief cannot be satisfied by this engine
architecture. That argument, and the two measurements behind it, are the
substance of this report.

`repo/` is left as a **correct, defect-free engine** that passes all 50
public cases, so anything built later starts from a clean baseline rather
than from a half-planted bug.

---

## 1. What the final attempt was

Per §2, a checkpoint-horizon / active-transaction interaction. The engine
gained a documented log budget: once the write-ahead log passes
`MS_LOG_LIMIT`, the next operation takes a checkpoint, which flushes the
pages and recycles the log. The planted defect placed that check one step
too early — inside `kv_put`/`kv_delete`, after the B+tree work but
*before* the commit record:

```c
rc = btree_put(s, key, val, len);
if (rc != KV_OK) return rc;
rc = keep_log_bounded(s);        /* flushes pages, recycles the log */
if (rc != KV_OK) return rc;
return txn_commit(s);
```

A crash in that window publishes an unfinished transaction: its pages are
on disk, the records that describe it have been recycled, and the engine
has no undo. This is a real ARIES-class hole (steal without undo, log
recycled past an active transaction) and it produced exactly the physics
§1 asked for. Measured on the shipped tree:

| crash at auto-checkpoint | `kv_verify` | contents |
| --- | --- | --- |
| #1 | **ok** — 710 keys, height 2, 54 pages | app had committed 709 |
| #2 | **ok** — 1388 keys, height 3, 112 pages | app had committed 1387 |
| #3 | **ok** — 1895 keys, height 3, 198 pages | app had committed 1894 |
| #4 | **ok** — 2690 keys, height 3, 236 pages | app had committed 2689 |

Structurally valid every time, silently wrong, and the failure message
says only that the contents differ from what was committed. It also
resisted the bypass list: forcing pages at commit, flushing everything at
checkpoint, and enlarging the buffer pool all leave it intact, because
the damage is done *before* the commit rather than by a missing redo.

## 2. Why it is not usable — measured

**The damage is confined to the operation that was in flight, and that
operation is in-doubt by definition.**

The shipped (defective) engine and the reference repair are externally
indistinguishable:

| engine | app-recorded commits | keys in database |
| --- | --- | --- |
| defective (checkpoint before commit) | 1387 | 1388 |
| reference repair (checkpoint after commit) | 1387 | 1388 |

With the repair the crash lands *after* the commit record and *before*
the application records its progress, so the extra key is genuinely
committed and its presence is correct. With the defect the same extra key
is uncommitted and its presence is wrong. Nothing observable from outside
the engine separates the two, because in this API one call *is* one
transaction: a crash inside the call leaves that call's outcome
undetermined, which is not a bug but the normal in-doubt window every
storage engine has.

An oracle could only tell them apart by consulting engine-internal state
— which is exactly the hidden-history dependency the brief forbids.

## 3. The general tension this exposes

Two measurements bound the design space from opposite sides.

**(a) "Force every dirty page at commit" (bypass B) neutralises redo
defects.** Applied to the previous redesign's genuine redo defect, it
took the suite from 6 crash failures to 1 — and the survivor was only the
*crash during page write-back* case, where the crash falls inside the
flush loop itself:

    bypass B on the previous defect: basic 28/0, crash 7/1, idempotence 14/0

So a defect whose damage is "committed work that redo failed to restore"
is legal-bypassable, because forcing pages at commit makes redo vacuous
for everything except a crash during the flush.

**(b) Defects that survive bypass B put damage on disk before the commit,
and in a one-call-per-transaction engine that damage is unobservable**
(§2 above).

The remaining corner is the intersection: a defect that fires only on a
crash *during* page write-back. That is reachable — it is what survived
bypass B — but in the previous design its damage was structural (`key N
on leaf page M is outside its subtree range`), which §1 forbids and §5
calls an overly precise oracle. Constructing one that is simultaneously
(i) write-back-crash-triggered, (ii) logical-only with a valid tree,
(iii) resistant to the rest of the bypass list, and (iv) not repairable
by a single condition (§7) is where four attempts have now converged, and
I could not build it without violating one of the four.

I am not claiming this is impossible in principle. I am reporting that I
could not construct it, and that each attempt failed a *different* stated
gate rather than getting closer:

| attempt | defect | outcome |
| --- | --- | --- |
| 1 | stale root id in the promotion log record | rejected on review: one line, contradicted by FORMAT.md |
| 2 | physiological split redo, per-half LSN gating | fresh agent: complete correct fix in ~34 min |
| 3 (this) | checkpoint inside the transaction | unobservable: repair is externally identical |

## 4. What is left in the tree

* `repo/` — a correct WAL-backed B+tree storage engine, 50/50 on its own
  suite, with the two defects found along the way genuinely fixed: the
  split record now carries the new sibling as a formatted image, and
  `pager_ensure()` no longer discards a legitimately evicted page image
  because the checkpointed `num_pages` lagged. It also carries the
  tooling built for this round: `--spread` insertion order, `expect`
  (exact committed-set checking) and `--progress` (an application-side
  record of which writes returned).
* `build/` — the packaging, reference and negative generators, and the
  validation harness. `make_zip.py` still works but no archive is
  committed.
* `private/attack_evidence/` — the patch the fresh-context agent produced
  against attempt 2, kept as evidence.
* `dist/` — empty, by design.

## 5. Recommendation

Stop this task line. The three fixtures produced here were each rejected
on evidence, and the fourth attempt showed the remaining design space is
squeezed between a legal conservative bypass on one side and an
unobservable in-doubt window on the other. Any further work should come
back with a different task shape — or with an explicit decision to relax
one of the constraints (for example, permitting a multi-operation
transaction API, which would make "uncommitted work became visible"
observable, or accepting that some conservative repairs are legitimate
solutions rather than bypasses).

## 6. Licensing

Everything in this task directory was written from scratch for this
benchmark. No third-party code, no external dataset, nothing downloaded,
nothing reused from any other task. The engine depends only on the C
standard library and the POSIX file API and needs no network.
