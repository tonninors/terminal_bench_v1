/* kvstore.c - public entry points, transactions, checkpoints, self-check. */
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include "internal.h"

static char *wal_path_of(const char *path)
{
    size_t n = strlen(path);
    char *p = malloc(n + 5);
    if (!p) return NULL;
    memcpy(p, path, n);
    memcpy(p + n, ".wal", 5);
    return p;
}

const char *kv_strerror(int rc)
{
    switch (rc) {
    case KV_OK:            return "ok";
    case KV_ERR_IO:        return "io error";
    case KV_ERR_CORRUPT:   return "corrupt database";
    case KV_ERR_NOTFOUND:  return "not found";
    case KV_ERR_INVAL:     return "invalid argument";
    case KV_ERR_NOSPACE:   return "no space in page";
    default:               return "unknown error";
    }
}

kvstore_t *kv_open(const char *path)
{
    kvstore_t *s = calloc(1, sizeof *s);
    if (!s) return NULL;
    int created = 0;
    s->path = strdup(path);
    s->pg = pager_open(path, &created);
    if (!s->pg) { free(s->path); free(s); return NULL; }

    char *wp = wal_path_of(path);
    if (!wp) { pager_close(s->pg); free(s->path); free(s); return NULL; }
    s->wal = wal_open(wp, pager_meta(s->pg)->next_lsn);
    free(wp);
    if (!s->wal) { pager_close(s->pg); free(s->path); free(s); return NULL; }
    pager_attach_wal(s->pg, s->wal);

    if (recover_redo(s) != KV_OK) {
        wal_close(s->wal);
        pager_close(s->pg);
        free(s->path);
        free(s);
        return NULL;
    }
    /* transaction ids must stay unique across restarts; log sequence
     * numbers always run ahead of the transaction count, so starting from
     * the current one is enough */
    s->txn = pager_meta(s->pg)->next_lsn;
    return s;
}

static int txn_begin(kvstore_t *s)
{
    wal_rec_t r;
    memset(&r, 0, sizeof r);
    s->txn++;
    r.type = WR_BEGIN;
    r.txn = s->txn;
    return wal_append(s->wal, &r, NULL, 0, NULL);
}

static int txn_commit(kvstore_t *s)
{
    wal_rec_t r;
    memset(&r, 0, sizeof r);
    r.type = WR_COMMIT;
    r.txn = s->txn;
    int rc = wal_append(s->wal, &r, NULL, 0, NULL);
    if (rc != KV_OK) return rc;
    rc = wal_sync(s->wal);                 /* the commit is durable here */
    if (rc != KV_OK) return rc;
    pager_meta(s->pg)->next_lsn = wal_next_lsn(s->wal);
    pager_meta_dirty(s->pg);
    ms_crash_hook("commit");
    return KV_OK;
}

static int keep_log_bounded(kvstore_t *s);

/* ---------------------------------------------------------- transactions */

/* Keep the page as it stands, so that the transaction can be put back
 * the way it found things.  Only the first time a page is touched in a
 * transaction matters.
 *
 * The image also goes into the log.  Keeping it in memory is enough to
 * undo a transaction the process decides to abandon, and no use at all
 * for one the process does not survive: the buffer pool will write these
 * pages back long before the transaction commits, and a checkpoint
 * forced by the log budget will write them back on purpose.  Whatever is
 * needed to put them back therefore has to be on disk before they go,
 * which is the same write-ahead rule the redo records already follow. */
int txn_snapshot(kvstore_t *s, page_t *pg)
{
    if (!s->in_txn || !pg) return KV_OK;
    for (size_t i = 0; i < s->undo_n; i++)
        if (s->undo[i].page == pg->id) return KV_OK;
    {
        wal_rec_t r;
        uint64_t lsn = 0;
        int rc;
        memset(&r, 0, sizeof r);
        r.type = WR_UNDO;
        r.txn  = s->txn;
        r.page = pg->id;
        rc = wal_append(s->wal, &r, pg->buf, PAGE_SIZE, &lsn);
        if (rc != KV_OK) return rc;
        /* the page may not reach the data file before this record does;
         * the pager holds back any page whose LSN is not durable yet */
        if (PHDR(pg)->lsn < lsn) PHDR(pg)->lsn = lsn;
    }
    if (s->undo_n == s->undo_cap) {
        size_t cap = s->undo_cap ? s->undo_cap * 2 : 64;
        undo_page_t *p = realloc(s->undo, cap * sizeof *p);
        if (!p) return KV_ERR_IO;
        s->undo = p;
        s->undo_cap = cap;
    }
    s->undo[s->undo_n].page = pg->id;
    memcpy(s->undo[s->undo_n].img, pg->buf, PAGE_SIZE);
    s->undo_n++;
    return KV_OK;
}

/* Put a page back the way the image found it.
 *
 * The meta page is not restored wholesale: the tree fields belong to the
 * transaction and have to go back, but how far the log has been absorbed
 * and which sequence numbers have been handed out are facts about the
 * log, not about the transaction, and undoing those would replay work
 * that is already durable. */
void undo_restore(page_t *pg, const uint8_t *img)
{
    if (pg->id == META_PAGE) {
        meta_t *cur = (meta_t *)pg->buf;
        const meta_t *was = (const meta_t *)img;
        cur->hdr = was->hdr;
        cur->num_pages = was->num_pages;
        cur->root = was->root;
        cur->height = was->height;
    } else {
        memcpy(pg->buf, img, PAGE_SIZE);
    }
}

static void txn_forget(kvstore_t *s)
{
    free(s->undo);
    s->undo = NULL;
    s->undo_n = s->undo_cap = 0;
}

int kv_begin(kvstore_t *s)
{
    if (!s) return KV_ERR_INVAL;
    if (s->in_txn) return KV_ERR_INVAL;
    int rc;
    s->txn_first_lsn = wal_next_lsn(s->wal);   /* the BEGIN record's own lsn */
    rc = txn_begin(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 1;
    txn_forget(s);
    /* the meta page carries the root and the page count, which structural
     * work changes too */
    return txn_snapshot(s, pager_meta_page(s->pg));
}

int kv_commit(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    int rc = txn_commit(s);
    if (rc != KV_OK) return rc;
    s->in_txn = 0;
    s->txn_first_lsn = 0;
    txn_forget(s);
    return keep_log_bounded(s);
}

int kv_abort(kvstore_t *s)
{
    if (!s || !s->in_txn) return KV_ERR_INVAL;
    /* newest first, so a page touched several times ends up at its
     * oldest remembered image */
    int rc;
    wal_rec_t r;
    for (size_t i = s->undo_n; i-- > 0; ) {
        page_t *pg = pager_ensure(s->pg, s->undo[i].page);
        if (!pg) return KV_ERR_IO;
        undo_restore(pg, s->undo[i].img);
        pager_mark_dirty(s->pg, pg);
        pager_unpin(s->pg, pg);
    }
    s->in_txn = 0;

    /* The rolled back pages have to be on disk before the log is allowed
     * to say this transaction needs no undoing: until they are, the only
     * thing that can repair them is the undo records still in the log. */
    rc = pager_flush_all(s->pg);
    if (rc == KV_OK) rc = pager_sync(s->pg);
    if (rc != KV_OK) return rc;

    memset(&r, 0, sizeof r);
    r.type = WR_END;
    r.txn  = s->txn;
    rc = wal_append(s->wal, &r, NULL, 0, NULL);
    if (rc == KV_OK) rc = wal_sync(s->wal);
    if (rc != KV_OK) return rc;

    s->txn_first_lsn = 0;
    txn_forget(s);
    return keep_log_bounded(s);
}

/* The log is not allowed to grow without bound.  The budget is checked
 * after every operation, not only at commit, because one transaction may
 * write far more than MS_LOG_LIMIT on its own: a transaction is not
 * allowed to hold the log open indefinitely.  Once the log passes the
 * budget the engine takes a checkpoint, which flushes the pages and
 * reclaims the records the data file no longer needs. */
static int keep_log_bounded(kvstore_t *s)
{
    if (wal_bytes(s->wal) < s->log_mark + MS_LOG_LIMIT) return KV_OK;
    int rc = kv_checkpoint(s);
    if (rc == KV_OK) s->log_mark = wal_bytes(s->wal);
    return rc;
}

int kv_put(kvstore_t *s, uint64_t key, const void *val, uint32_t len)
{
    int rc, own;
    if (!s || !val) return KV_ERR_INVAL;
    if (len > KV_MAX_VALUE_LEN) return KV_ERR_INVAL;
    own = !s->in_txn;
    if (own && (rc = kv_begin(s)) != KV_OK) return rc;
    rc = btree_put(s, key, val, len);
    if (rc != KV_OK) {
        if (own) kv_abort(s);
        return rc;
    }
    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_delete(kvstore_t *s, uint64_t key)
{
    int rc, own;
    if (!s) return KV_ERR_INVAL;
    own = !s->in_txn;
    if (own && (rc = kv_begin(s)) != KV_OK) return rc;
    rc = btree_del(s, key);
    if (rc != KV_OK) {
        if (own) kv_abort(s);
        return rc;
    }
    if (own) return kv_commit(s);
    return keep_log_bounded(s);
}

int kv_get(kvstore_t *s, uint64_t key, void *buf, uint32_t buflen,
           uint32_t *out_len)
{
    if (!s) return KV_ERR_INVAL;
    return btree_get(s, key, buf, buflen, out_len);
}

uint32_t kv_pool_frames(kvstore_t *s)
{
    return s ? pager_frames(s->pg) : 0;
}

int kv_checkpoint(kvstore_t *s)
{
    wal_rec_t r;
    uint64_t lsn = 0;
    int rc;

    if (!s) return KV_ERR_INVAL;
    rc = pager_flush_all(s->pg);
    if (rc != KV_OK) return rc;
    rc = pager_sync(s->pg);
    if (rc != KV_OK) return rc;

    memset(&r, 0, sizeof r);
    r.type = WR_CHECKPOINT;
    r.txn = s->txn;
    rc = wal_append(s->wal, &r, NULL, 0, &lsn);
    if (rc != KV_OK) return rc;
    rc = wal_sync(s->wal);
    if (rc != KV_OK) return rc;

    meta_t *m = pager_meta(s->pg);
    m->checkpoint_lsn = lsn;
    m->next_lsn = wal_next_lsn(s->wal);
    pager_meta_dirty(s->pg);
    rc = pager_flush_all(s->pg);
    if (rc != KV_OK) return rc;
    rc = pager_sync(s->pg);
    if (rc != KV_OK) return rc;

    /* Every page is on disk and the meta page records how far the log has
     * been absorbed, so nothing before this point will ever be replayed
     * again - but replay is not the only thing the log is for.  A
     * transaction still running has pages on disk that this checkpoint
     * has just published, and the only description of what they held
     * before it touched them is in the records this would throw away.
     * The log may not be recycled out from underneath it. */
    if (s->in_txn) {
        ms_crash_hook("checkpoint");
        return KV_OK;
    }
    rc = wal_truncate(s->wal);
    if (rc != KV_OK) return rc;
    s->log_mark = 0;
    ms_crash_hook("checkpoint");
    return rc;
}

int kv_close(kvstore_t *s)
{
    if (!s) return KV_ERR_INVAL;
    if (s->in_txn) kv_abort(s);
    int rc = kv_checkpoint(s);
    int rc2 = wal_close(s->wal);
    int rc3 = pager_close(s->pg);
    txn_forget(s);
    free(s->path);
    free(s);
    if (rc != KV_OK) return rc;
    if (rc2 != KV_OK) return rc2;
    return rc3;
}

uint32_t kv_height(kvstore_t *s) { return pager_meta(s->pg)->height; }
uint32_t kv_pages(kvstore_t *s) { return pager_meta(s->pg)->num_pages; }

/* leftmost leaf, following the leftmost child pointer down */
static int leftmost_leaf(kvstore_t *s, uint32_t *out)
{
    uint32_t id = pager_meta(s->pg)->root;
    for (int d = 0; d < MAX_TREE_DEPTH; d++) {
        page_t *pg = pager_get(s->pg, id);
        if (!pg) return KV_ERR_CORRUPT;
        uint16_t t = PHDR(pg)->type;
        uint32_t link = PHDR(pg)->link;
        pager_unpin(s->pg, pg);
        if (t == PT_LEAF) { *out = id; return KV_OK; }
        if (t != PT_INTERNAL || link == INVALID_PAGE || link == id)
            return KV_ERR_CORRUPT;
        id = link;
    }
    return KV_ERR_CORRUPT;
}

int kv_scan(kvstore_t *s, uint64_t start,
            int (*cb)(uint64_t, const void *, uint32_t, void *), void *ctx)
{
    uint32_t id;
    int rc = leftmost_leaf(s, &id);
    if (rc != KV_OK) return rc;
    uint32_t guard = pager_num_pages(s->pg) + 1;
    while (id != INVALID_PAGE && guard--) {
        page_t *pg = pager_get(s->pg, id);
        if (!pg) return KV_ERR_CORRUPT;
        if (PHDR(pg)->type != PT_LEAF) { pager_unpin(s->pg, pg); return KV_ERR_CORRUPT; }
        leaf_slot_t *sl = LEAF_SLOTS(pg);
        for (int i = 0; i < PHDR(pg)->nkeys; i++) {
            if (sl[i].key < start) continue;
            if (cb && cb(sl[i].key, sl[i].val, sl[i].vlen, ctx)) {
                pager_unpin(s->pg, pg);
                return KV_OK;
            }
        }
        uint32_t next = PHDR(pg)->link;
        pager_unpin(s->pg, pg);
        id = next;
    }
    return guard ? KV_OK : KV_ERR_CORRUPT;
}

static int count_cb(uint64_t k, const void *v, uint32_t n, void *ctx)
{
    (void)k; (void)v; (void)n;
    (*(int64_t *)ctx)++;
    return 0;
}

int64_t kv_count(kvstore_t *s)
{
    int64_t n = 0;
    int rc = kv_scan(s, 0, count_cb, &n);
    return rc == KV_OK ? n : -1;
}

/* ------------------------------------------------------------- verify */
typedef struct {
    kvstore_t *s;
    uint8_t   *seen;
    uint32_t   npages;
    int64_t    keys;
    uint32_t   first_leaf;
    uint32_t   last_leaf;
    char      *err;
    size_t     errlen;
} vctx_t;

static void verr(vctx_t *v, const char *fmt, ...)
{
    va_list ap;
    if (!v->err || !v->errlen || v->err[0]) return;
    va_start(ap, fmt);
    vsnprintf(v->err, v->errlen, fmt, ap);
    va_end(ap);
}

static int visit(vctx_t *v, uint32_t id, uint64_t lo, uint64_t hi, int depth)
{
    if (depth > MAX_TREE_DEPTH) {
        verr(v, "tree deeper than %d levels at page %u", MAX_TREE_DEPTH, id);
        return KV_ERR_CORRUPT;
    }
    if (id == INVALID_PAGE || id >= v->npages) {
        verr(v, "child page %u out of range", id);
        return KV_ERR_CORRUPT;
    }
    if (v->seen[id]) {
        verr(v, "page %u reachable twice: the tree contains a cycle", id);
        return KV_ERR_CORRUPT;
    }
    v->seen[id] = 1;

    page_t *pg = pager_get(v->s->pg, id);
    if (!pg) { verr(v, "cannot read page %u", id); return KV_ERR_CORRUPT; }
    uint16_t type = PHDR(pg)->type;
    int n = PHDR(pg)->nkeys;
    int rc = KV_OK;

    if (type == PT_LEAF) {
        leaf_slot_t *sl = LEAF_SLOTS(pg);
        for (int i = 0; i < n; i++) {
            if (i && sl[i].key <= sl[i - 1].key) {
                verr(v, "leaf page %u keys out of order at slot %d", id, i);
                rc = KV_ERR_CORRUPT;
                break;
            }
            if (sl[i].key < lo || sl[i].key >= hi) {
                verr(v, "key %llu on leaf page %u is outside its subtree range",
                     (unsigned long long)sl[i].key, id);
                rc = KV_ERR_CORRUPT;
                break;
            }
        }
        v->keys += n;
        if (v->first_leaf == INVALID_PAGE) v->first_leaf = id;
        v->last_leaf = id;
        pager_unpin(v->s->pg, pg);
        return rc;
    }
    if (type != PT_INTERNAL) {
        verr(v, "page %u has unexpected type %u", id, type);
        pager_unpin(v->s->pg, pg);
        return KV_ERR_CORRUPT;
    }

    int_ent_t *e = INT_ENTS(pg);
    uint32_t leftmost = PHDR(pg)->link;
    uint32_t children[INT_CAP + 1];
    uint64_t bounds[INT_CAP + 2];
    int nc = 0;
    children[nc] = leftmost;
    bounds[nc] = lo;
    for (int i = 0; i < n; i++) {
        if (i && e[i].key <= e[i - 1].key) {
            verr(v, "internal page %u separators out of order at %d", id, i);
            pager_unpin(v->s->pg, pg);
            return KV_ERR_CORRUPT;
        }
        bounds[++nc] = e[i].key;
        children[nc] = e[i].child;
    }
    bounds[nc + 1] = hi;
    pager_unpin(v->s->pg, pg);

    for (int i = 0; i <= nc; i++) {
        rc = visit(v, children[i], bounds[i], bounds[i + 1], depth + 1);
        if (rc != KV_OK) return rc;
    }
    return KV_OK;
}

int kv_verify(kvstore_t *s, char *err, size_t errlen)
{
    if (!s) return KV_ERR_INVAL;
    if (err && errlen) err[0] = 0;

    vctx_t v;
    memset(&v, 0, sizeof v);
    v.s = s;
    v.npages = pager_num_pages(s->pg);
    v.seen = calloc(v.npages, 1);
    v.err = err;
    v.errlen = errlen;
    v.first_leaf = INVALID_PAGE;
    if (!v.seen) return KV_ERR_IO;

    int rc = visit(&v, pager_meta(s->pg)->root, 0, UINT64_MAX, 0);
    int64_t tree_keys = v.keys;
    free(v.seen);
    if (rc != KV_OK) return rc;

    int64_t chain_keys = kv_count(s);
    if (chain_keys < 0) {
        if (err && errlen && !err[0])
            snprintf(err, errlen, "leaf chain is not walkable");
        return KV_ERR_CORRUPT;
    }
    if (chain_keys != tree_keys) {
        if (err && errlen && !err[0])
            snprintf(err, errlen,
                     "leaf chain holds %lld keys but the tree holds %lld",
                     (long long)chain_keys, (long long)tree_keys);
        return KV_ERR_CORRUPT;
    }
    return KV_OK;
}
