# Internal notes — ministore crash-recovery task (NOT solver facing)

Everything in `private/` stays out of the solver bundle and out of the
task image. `build/make_zip.py` refuses to package any path whose name
contains `private`, `fixed`, `reference`, `negative`, `solution`,
`answer`, `oracle` or `golden`.

## The planted regression

One line, in `repo/src/btree.c`:

```c
static int log_root_promotion(...)
{
    ...
    r.aux  = pager_meta(s->pg)->root;      /* left child of the new root */
```

`promote_root()` builds the new root correctly in memory — it captures
`old_root` before allocating, sets it as the leftmost child, and only
then publishes `m->root = new_id`. The logging helper, however, reads
`meta->root` *after* that publication, so the `NEW_ROOT` record names the
new root as its own leftmost child.

Consequences, and why the shape is right for the task:

* in-memory behaviour is correct, so every normal insert/update/delete
  and every clean shutdown works — `test_basic.sh` is 26/26 on the buggy
  build;
* a clean close checkpoints, so replay never touches those records;
* only redo of a `NEW_ROOT` record inside the replayed window rebuilds
  the root wrongly. The recovered root points at itself, which
  `kv_verify` reports as `page N reachable twice: the tree contains a
  cycle`;
* `waldump` shows the smoking gun directly: `NEW_ROOT page=3 aux=3`.

Scenario separation is deliberate and was measured:

| replayed window contains | buggy build |
| --- | --- |
| leaf splits only (checkpointed base) | passes |
| internal-node splits, no promotion (height-3 base) | passes |
| a root promotion | fails |
| two levels of growth | fails |

That gives the expert a real localisation signal without naming the
cause anywhere in the shipped tree.

## The reference fix

`private/reference_fix.patch`, two hunks: pass the captured `old_root`
into the logging helper instead of re-reading the meta page. Applies to
the extracted bundle with

    patch -p1 < reference_fix.patch     # after rewriting the a/ b/ prefixes

Result: 53/53 across the three test files.

Alternative fixes that are also correct (and must pass, since grading is
behavioural): moving `m->root = new_id` below the logging call, or having
recovery derive the left child from the meta root it already holds while
replaying.

## Plausible incomplete fixes (all measured failing)

| directory | idea | result |
| --- | --- | --- |
| `nf1_reorder_bookkeeping` | moves the height bump around; looks like an ordering fix, changes nothing | 5 + 5 failures |
| `nf2_recovery_assumes_first_root` | patches recovery, assuming the tree always grew out of page 1 | 2 + 5 failures |
| `nf3_replay_everything` | fixes the record, then drops the page-LSN gate "to be safe" | 2 failures, both in the idempotence file |
| `nf4_recovery_keeps_old_root` | fixes the record but stops republishing the root during redo | 5 + 5 failures |
| `nf5_only_first_promotion` | special-cases the first promotion (patches the first failing test) | 2 + 5 failures |

`nf3` is the reason the idempotence file contains the
`idem_partial_replay` case: replay has to start from a checkpoint and be
interrupted part way for double application to be observable at all. An
earlier, weaker version of that test let `nf3` pass — it was strengthened
until it did not.

## Regenerating and revalidating

    python3 build/make_zip.py          # rebuild dist/storage-engine-inputs.zip
    bash   build/run_validation.sh     # full matrix in throwaway containers

`run_validation.sh` rebuilds the archive, extracts it as
`/app/storage-engine`, and runs the buggy baseline, the reference fix and
every negative, asserting the expected verdict for each.
