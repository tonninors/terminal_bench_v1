/* recover.c - redo recovery.
 *
 * Two passes over the log.  The first pass finds the transactions that
 * reached a commit record and where the log stops being readable (a torn
 * tail left by the crash).  The second pass replays the records of those
 * transactions against the data file, skipping any page whose stored LSN
 * shows the change already reached disk.  Replaying an already recovered
 * database must therefore change nothing.
 */
#include <stdlib.h>
#include <string.h>
#include "internal.h"

typedef struct {
    uint64_t *ids;
    size_t    n, cap;
} txnset_t;

static int txnset_add(txnset_t *t, uint64_t id)
{
    if (t->n == t->cap) {
        size_t cap = t->cap ? t->cap * 2 : 256;
        uint64_t *p = realloc(t->ids, cap * sizeof *p);
        if (!p) return KV_ERR_IO;
        t->ids = p;
        t->cap = cap;
    }
    t->ids[t->n++] = id;
    return KV_OK;
}

/* ids are appended in log order, so the array is sorted */
static int txnset_has(const txnset_t *t, uint64_t id)
{
    size_t lo = 0, hi = t->n;
    while (lo < hi) {
        size_t mid = (lo + hi) / 2;
        if (t->ids[mid] < id) lo = mid + 1; else hi = mid;
    }
    return lo < t->n && t->ids[lo] == id;
}

static page_t *page_for(kvstore_t *s, uint32_t id, uint64_t lsn, int *apply)
{
    page_t *pg = pager_ensure(s->pg, id);
    if (!pg) return NULL;
    *apply = (PHDR(pg)->lsn < lsn);
    return pg;
}

static void stamp(kvstore_t *s, page_t *pg, uint64_t lsn)
{
    PHDR(pg)->lsn = lsn;
    pager_mark_dirty(s->pg, pg);
}

static int redo_one(kvstore_t *s, const wal_rec_t *r, const uint8_t *payload)
{
    page_t *pg, *right;
    int apply = 0;

    switch (r->type) {
    case WR_LEAF_PUT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            if (PHDR(pg)->type != PT_LEAF) node_init(pg, PT_LEAF);
            leaf_put(pg, r->key, payload, r->vlen);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_LEAF_DEL:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            leaf_del(pg, r->key);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_LEAF_SPLIT:
    case WR_INT_SPLIT:
        /* the new sibling is logged as a full image, so either half can
         * be replayed on its own */
        right = page_for(s, r->aux, r->lsn, &apply);
        if (!right) return KV_ERR_IO;
        if (apply) {
            if (r->vlen != PAGE_SIZE) { pager_unpin(s->pg, right); return KV_ERR_CORRUPT; }
            memcpy(right->buf, payload, PAGE_SIZE);
            stamp(s, right, r->lsn);
        }
        pager_unpin(s->pg, right);

        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            PHDR(pg)->nkeys = (uint16_t)r->arg;
            if (r->type == WR_LEAF_SPLIT) PHDR(pg)->link = r->aux;
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_INT_INSERT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            if (PHDR(pg)->type != PT_INTERNAL) node_init(pg, PT_INTERNAL);
            int_insert(pg, r->key, r->arg);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        return KV_OK;

    case WR_NEW_ROOT:
        pg = page_for(s, r->page, r->lsn, &apply);
        if (!pg) return KV_ERR_IO;
        if (apply) {
            node_init(pg, PT_INTERNAL);
            int_set_leftmost(pg, r->aux);
            int_insert(pg, r->key, r->arg);
            stamp(s, pg, r->lsn);
        }
        pager_unpin(s->pg, pg);
        pager_meta(s->pg)->root = r->page;
        pager_meta_dirty(s->pg);
        return KV_OK;

    default:
        return KV_OK;                       /* begin / commit / checkpoint */
    }
}

/* height is bookkeeping, not data: derive it from the recovered tree */
static void recompute_height(kvstore_t *s)
{
    meta_t *m = pager_meta(s->pg);
    uint32_t id = m->root;
    uint32_t h = 1;
    for (uint32_t d = 0; d < MAX_TREE_DEPTH; d++) {
        page_t *pg = pager_get(s->pg, id);
        if (!pg) break;
        if (PHDR(pg)->type == PT_LEAF) { pager_unpin(s->pg, pg); break; }
        uint32_t next = PHDR(pg)->link;
        pager_unpin(s->pg, pg);
        if (next == INVALID_PAGE || next == id) break;
        id = next;
        h++;
    }
    m->height = h;
    pager_meta_dirty(s->pg);
}

int recover_redo(kvstore_t *s)
{
    meta_t *m = pager_meta(s->pg);
    uint64_t ckpt = m->checkpoint_lsn;
    uint64_t max_lsn = ckpt;
    txnset_t committed = {0};
    wal_rec_t r;
    uint8_t payload[PAGE_SIZE];
    int rc;

    /* pass 1: which transactions committed, and where does the log end */
    wal_read_first(s->wal);
    for (;;) {
        rc = wal_read_next(s->wal, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND || rc == KV_ERR_CORRUPT) break;
        if (rc != KV_OK) { free(committed.ids); return rc; }
        if (r.lsn > max_lsn) max_lsn = r.lsn;
        if (r.type == WR_COMMIT && txnset_add(&committed, r.txn) != KV_OK) {
            free(committed.ids);
            return KV_ERR_IO;
        }
    }

    /* pass 2: replay the committed work that the data file is missing */
    wal_read_first(s->wal);
    for (;;) {
        rc = wal_read_next(s->wal, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND || rc == KV_ERR_CORRUPT) break;
        if (rc != KV_OK) { free(committed.ids); return rc; }
        if (r.lsn <= ckpt) continue;
        if (!txnset_has(&committed, r.txn)) continue;
        rc = redo_one(s, &r, payload);
        if (rc != KV_OK) { free(committed.ids); return rc; }
    }
    free(committed.ids);

    recompute_height(s);
    m = pager_meta(s->pg);
    if (m->next_lsn <= max_lsn) {
        m->next_lsn = max_lsn + 1;
        pager_meta_dirty(s->pg);
    }
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    return rc;
}
