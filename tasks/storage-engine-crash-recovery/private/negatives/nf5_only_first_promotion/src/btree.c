/* btree.c - B+tree maintenance on top of the pager and the log.
 *
 * Every user level operation runs as one logged transaction: WR_BEGIN,
 * the physiological records describing each page change, then WR_COMMIT.
 * Redo replays those records against whatever survived in the data file,
 * so each record must describe the change exactly as it was applied here.
 */
#include <string.h>
#include "internal.h"

static int emit(kvstore_t *s, wal_rec_t *r, const void *payload,
                uint32_t vlen, page_t *a, page_t *b)
{
    uint64_t lsn = 0;
    r->txn = s->txn;
    int rc = wal_append(s->wal, r, payload, vlen, &lsn);
    if (rc != KV_OK) return rc;
    if (a) { PHDR(a)->lsn = lsn; pager_mark_dirty(s->pg, a); }
    if (b) { PHDR(b)->lsn = lsn; pager_mark_dirty(s->pg, b); }
    return KV_OK;
}

/* Walk from the root to the leaf that owns key, recording the internal
 * pages passed through. */
static int descend(kvstore_t *s, uint64_t key, uint32_t *path, int *levels,
                   uint32_t *leaf_out)
{
    meta_t *m = pager_meta(s->pg);
    uint32_t id = m->root;
    int n = 0;
    for (int depth = 0; depth < MAX_TREE_DEPTH; depth++) {
        page_t *pg = pager_get(s->pg, id);
        if (!pg) return KV_ERR_CORRUPT;
        uint16_t type = PHDR(pg)->type;
        if (type == PT_LEAF) {
            pager_unpin(s->pg, pg);
            *levels = n;
            *leaf_out = id;
            return KV_OK;
        }
        if (type != PT_INTERNAL) {
            pager_unpin(s->pg, pg);
            return KV_ERR_CORRUPT;
        }
        if (path && n < MAX_TREE_DEPTH) path[n] = id;
        n++;
        int child = int_child(pg, key);
        pager_unpin(s->pg, pg);
        if (child < 0 || (uint32_t)child == INVALID_PAGE) return KV_ERR_CORRUPT;
        id = (uint32_t)child;
    }
    return KV_ERR_CORRUPT;                  /* deeper than any real tree */
}

int btree_get(kvstore_t *s, uint64_t key, void *buf, uint32_t cap,
              uint32_t *out_len)
{
    uint32_t path[MAX_TREE_DEPTH], leaf_id = 0;
    int levels = 0;
    int rc = descend(s, key, path, &levels, &leaf_id);
    if (rc != KV_OK) return rc;
    page_t *leaf = pager_get(s->pg, leaf_id);
    if (!leaf) return KV_ERR_CORRUPT;
    int i = leaf_find(leaf, key);
    if (i < 0) { pager_unpin(s->pg, leaf); return KV_ERR_NOTFOUND; }
    leaf_slot_t *sl = &LEAF_SLOTS(leaf)[i];
    uint32_t n = sl->vlen;
    if (out_len) *out_len = n;
    if (buf) memcpy(buf, sl->val, n < cap ? n : cap);
    pager_unpin(s->pg, leaf);
    return KV_OK;
}

static int insert_into_parent(kvstore_t *s, uint32_t *path, int level,
                              uint64_t sep, uint32_t right_child);

static int log_root_promotion(kvstore_t *s, uint32_t new_root, uint64_t sep,
                              uint32_t right_child, page_t *rootpg)
{
    wal_rec_t r;
    memset(&r, 0, sizeof r);
    r.type = WR_NEW_ROOT;
    r.page = new_root;
    /* the first promotion always grows out of the original root page */
    r.aux  = (pager_meta(s->pg)->height == 2) ? 1u
                                              : pager_meta(s->pg)->root;
    r.key  = sep;
    r.arg  = right_child;
    return emit(s, &r, NULL, 0, rootpg, NULL);
}

/* The root filled up: build a new root one level above it. */
static int promote_root(kvstore_t *s, uint64_t sep, uint32_t right_child)
{
    meta_t *m = pager_meta(s->pg);
    uint32_t old_root = m->root;
    uint32_t new_id = 0;
    page_t *nr = pager_alloc(s->pg, &new_id);
    if (!nr) return KV_ERR_IO;

    node_init(nr, PT_INTERNAL);
    int_set_leftmost(nr, old_root);
    int_insert(nr, sep, right_child);

    m->root = new_id;
    m->height++;
    pager_meta_dirty(s->pg);

    int rc = log_root_promotion(s, new_id, sep, right_child, nr);
    pager_unpin(s->pg, nr);
    return rc;
}

/* Put (sep, right_child) into the internal page at path[level], splitting
 * it and recursing upwards when it is full. */
static int insert_into_parent(kvstore_t *s, uint32_t *path, int level,
                              uint64_t sep, uint32_t right_child)
{
    if (level < 0) return promote_root(s, sep, right_child);

    uint32_t pid = path[level];
    page_t *parent = pager_get(s->pg, pid);
    if (!parent) return KV_ERR_CORRUPT;
    int rc;
    wal_rec_t r;

    if (PHDR(parent)->nkeys < INT_CAP) {
        int_insert(parent, sep, right_child);
        memset(&r, 0, sizeof r);
        r.type = WR_INT_INSERT;
        r.page = pid;
        r.key  = sep;
        r.arg  = right_child;
        rc = emit(s, &r, NULL, 0, parent, NULL);
        pager_unpin(s->pg, parent);
        return rc;
    }

    /* full: split the parent, place the separator in the half that owns
     * it, and push the middle separator one level up */
    uint32_t new_id = 0;
    page_t *rightpg = pager_alloc(s->pg, &new_id);
    if (!rightpg) { pager_unpin(s->pg, parent); return KV_ERR_IO; }
    int at = PHDR(parent)->nkeys / 2;
    uint64_t sep_up = 0;
    int_split(parent, rightpg, at, &sep_up);

    memset(&r, 0, sizeof r);
    r.type = WR_INT_SPLIT;
    r.page = pid;
    r.aux  = new_id;
    r.arg  = (uint32_t)at;
    rc = emit(s, &r, rightpg->buf, PAGE_SIZE, parent, rightpg);
    if (rc != KV_OK) goto out;

    page_t *target = (sep < sep_up) ? parent : rightpg;
    uint32_t target_id = (sep < sep_up) ? pid : new_id;
    int_insert(target, sep, right_child);
    memset(&r, 0, sizeof r);
    r.type = WR_INT_INSERT;
    r.page = target_id;
    r.key  = sep;
    r.arg  = right_child;
    rc = emit(s, &r, NULL, 0, target, NULL);
    if (rc != KV_OK) goto out;

    pager_unpin(s->pg, parent);
    pager_unpin(s->pg, rightpg);
    return insert_into_parent(s, path, level - 1, sep_up, new_id);
out:
    pager_unpin(s->pg, parent);
    pager_unpin(s->pg, rightpg);
    return rc;
}

int btree_put(kvstore_t *s, uint64_t key, const void *val, uint32_t vlen)
{
    uint32_t path[MAX_TREE_DEPTH], leaf_id = 0;
    int levels = 0, rc;
    wal_rec_t r;

    rc = descend(s, key, path, &levels, &leaf_id);
    if (rc != KV_OK) return rc;

    page_t *leaf = pager_get(s->pg, leaf_id);
    if (!leaf) return KV_ERR_CORRUPT;

    int present = leaf_find(leaf, key) >= 0;
    if (present || PHDR(leaf)->nkeys < LEAF_CAP) {
        leaf_put(leaf, key, val, vlen);
        memset(&r, 0, sizeof r);
        r.type = WR_LEAF_PUT;
        r.page = leaf_id;
        r.key  = key;
        rc = emit(s, &r, val, vlen, leaf, NULL);
        pager_unpin(s->pg, leaf);
        return rc;
    }

    /* the leaf is full: split it and insert into the correct half */
    uint32_t right_id = 0;
    page_t *right = pager_alloc(s->pg, &right_id);
    if (!right) { pager_unpin(s->pg, leaf); return KV_ERR_IO; }
    int at = PHDR(leaf)->nkeys / 2;
    leaf_split(leaf, right, right_id, at);

    memset(&r, 0, sizeof r);
    r.type = WR_LEAF_SPLIT;
    r.page = leaf_id;
    r.aux  = right_id;
    r.arg  = (uint32_t)at;
    rc = emit(s, &r, right->buf, PAGE_SIZE, leaf, right);
    if (rc != KV_OK) goto out;

    uint64_t sep = LEAF_SLOTS(right)[0].key;
    page_t *target = (key < sep) ? leaf : right;
    uint32_t target_id = (key < sep) ? leaf_id : right_id;
    leaf_put(target, key, val, vlen);
    memset(&r, 0, sizeof r);
    r.type = WR_LEAF_PUT;
    r.page = target_id;
    r.key  = key;
    rc = emit(s, &r, val, vlen, target, NULL);
    if (rc != KV_OK) goto out;

    pager_unpin(s->pg, leaf);
    pager_unpin(s->pg, right);
    return insert_into_parent(s, path, levels - 1, sep, right_id);
out:
    pager_unpin(s->pg, leaf);
    pager_unpin(s->pg, right);
    return rc;
}

int btree_del(kvstore_t *s, uint64_t key)
{
    uint32_t path[MAX_TREE_DEPTH], leaf_id = 0;
    int levels = 0;
    wal_rec_t r;
    int rc = descend(s, key, path, &levels, &leaf_id);
    if (rc != KV_OK) return rc;
    page_t *leaf = pager_get(s->pg, leaf_id);
    if (!leaf) return KV_ERR_CORRUPT;
    if (leaf_del(leaf, key) != KV_OK) {
        pager_unpin(s->pg, leaf);
        return KV_ERR_NOTFOUND;
    }
    memset(&r, 0, sizeof r);
    r.type = WR_LEAF_DEL;
    r.page = leaf_id;
    r.key  = key;
    rc = emit(s, &r, NULL, 0, leaf, NULL);
    pager_unpin(s->pg, leaf);
    return rc;
}
