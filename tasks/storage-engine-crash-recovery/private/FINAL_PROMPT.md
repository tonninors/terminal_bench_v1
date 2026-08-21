# The solver-facing prompt

This is the exact text a solver (and the fresh-context attack agent)
receives.  Nothing else is provided beyond the extracted repository and
terminal access.

---

The repository at `/app/storage-engine` is a small embedded key/value
storage engine written in C: a B+tree over fixed-size pages, made durable
by a write-ahead log with redo recovery.  It builds with `make` and has
its own test suite under `tests/`.

The engine is stable in normal operation and on clean shutdown.  It is
not correct after a crash.  Repair it so that all of the following hold:

* every write whose commit returned is present after recovery, and the
  recovered tree is a well-formed B+tree;
* aborted or incomplete transactions do not become visible, including
  transactions containing multiple inserts, updates, or deletes: after
  recovery, either all operations of a committed transaction are visible
  or none of the operations of an uncommitted transaction are visible;
* recovery is repeatable: running it again, or being interrupted part way
  through and run again, must leave the same database.

Do not change the public API in `include/kvstore.h`, the on-disk format
described in `docs/FORMAT.md`, or the tests.  `make test` must pass.

---

## What the fresh-context agent is NOT given

* `private/verify.sh` (the private verifier)
* `private/fixed/` or `private/reference_fix.patch` (the reference repair)
* `private/negatives/` (the incomplete repairs and generic bypasses)
* `build/make_reference.py`, `build/make_negatives.py`
* `REPORT.md` or any design note

It gets the extracted `repo/` tree and a shell, nothing else.
