/* node.c - operations on a single B+tree page.
 *
 * Leaf pages hold sorted (key, value) slots and a link to the next leaf.
 * Internal pages hold a leftmost child in the page header plus sorted
 * (separator, child) entries: keys >= separator live in that child.
 */
#include <string.h>
#include "internal.h"

void node_init(page_t *pg, uint16_t type)
{
    memset(pg->buf, 0, PAGE_SIZE);
    PHDR(pg)->type = type;
    PHDR(pg)->nkeys = 0;
    PHDR(pg)->lsn = 0;
    PHDR(pg)->link = INVALID_PAGE;
}

int leaf_lower_bound(page_t *pg, uint64_t key)
{
    leaf_slot_t *s = LEAF_SLOTS(pg);
    int lo = 0, hi = PHDR(pg)->nkeys;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (s[mid].key < key) lo = mid + 1; else hi = mid;
    }
    return lo;
}

int leaf_find(page_t *pg, uint64_t key)
{
    int i = leaf_lower_bound(pg, key);
    leaf_slot_t *s = LEAF_SLOTS(pg);
    if (i < PHDR(pg)->nkeys && s[i].key == key) return i;
    return -1;
}

int leaf_put(page_t *pg, uint64_t key, const void *val, uint32_t vlen)
{
    leaf_slot_t *s = LEAF_SLOTS(pg);
    int n = PHDR(pg)->nkeys;
    int i = leaf_lower_bound(pg, key);
    if (vlen > KV_MAX_VALUE_LEN) return KV_ERR_INVAL;
    if (i < n && s[i].key == key) {           /* update in place */
        s[i].vlen = (uint16_t)vlen;
        memset(s[i].val, 0, KV_MAX_VALUE_LEN);
        memcpy(s[i].val, val, vlen);
        return KV_OK;
    }
    if (n >= LEAF_CAP) return KV_ERR_NOSPACE;
    memmove(&s[i + 1], &s[i], (size_t)(n - i) * sizeof *s);
    memset(&s[i], 0, sizeof s[i]);
    s[i].key = key;
    s[i].vlen = (uint16_t)vlen;
    memcpy(s[i].val, val, vlen);
    PHDR(pg)->nkeys = (uint16_t)(n + 1);
    return KV_OK;
}

int leaf_del(page_t *pg, uint64_t key)
{
    leaf_slot_t *s = LEAF_SLOTS(pg);
    int n = PHDR(pg)->nkeys;
    int i = leaf_find(pg, key);
    if (i < 0) return KV_ERR_NOTFOUND;
    memmove(&s[i], &s[i + 1], (size_t)(n - i - 1) * sizeof *s);
    PHDR(pg)->nkeys = (uint16_t)(n - 1);
    return KV_OK;
}

void leaf_split(page_t *left, page_t *right, uint32_t right_id, int at)
{
    leaf_slot_t *ls = LEAF_SLOTS(left), *rs = LEAF_SLOTS(right);
    int n = PHDR(left)->nkeys;
    node_init(right, PT_LEAF);
    memcpy(rs, &ls[at], (size_t)(n - at) * sizeof *ls);
    PHDR(right)->nkeys = (uint16_t)(n - at);
    PHDR(right)->link = PHDR(left)->link;
    PHDR(left)->nkeys = (uint16_t)at;
    PHDR(left)->link = right_id;
}

void int_set_leftmost(page_t *pg, uint32_t child)
{
    PHDR(pg)->link = child;
}

int int_child(page_t *pg, uint64_t key)
{
    int_ent_t *e = INT_ENTS(pg);
    int lo = 0, hi = PHDR(pg)->nkeys;
    while (lo < hi) {                       /* first entry with key > k */
        int mid = (lo + hi) / 2;
        if (e[mid].key <= key) lo = mid + 1; else hi = mid;
    }
    if (lo == 0) return (int)PHDR(pg)->link;
    return (int)e[lo - 1].child;
}

int int_insert(page_t *pg, uint64_t key, uint32_t child)
{
    int_ent_t *e = INT_ENTS(pg);
    int n = PHDR(pg)->nkeys;
    if (n >= INT_CAP) return KV_ERR_NOSPACE;
    int lo = 0, hi = n;
    while (lo < hi) {
        int mid = (lo + hi) / 2;
        if (e[mid].key < key) lo = mid + 1; else hi = mid;
    }
    memmove(&e[lo + 1], &e[lo], (size_t)(n - lo) * sizeof *e);
    memset(&e[lo], 0, sizeof e[lo]);
    e[lo].key = key;
    e[lo].child = child;
    PHDR(pg)->nkeys = (uint16_t)(n + 1);
    return KV_OK;
}

/* The separator at `at` moves up to the parent; its child becomes the
 * leftmost child of the new right page. */
void int_split(page_t *left, page_t *right, int at, uint64_t *sep_out)
{
    int_ent_t *le = INT_ENTS(left), *re = INT_ENTS(right);
    int n = PHDR(left)->nkeys;
    node_init(right, PT_INTERNAL);
    *sep_out = le[at].key;
    PHDR(right)->link = le[at].child;
    memcpy(re, &le[at + 1], (size_t)(n - at - 1) * sizeof *le);
    PHDR(right)->nkeys = (uint16_t)(n - at - 1);
    PHDR(left)->nkeys = (uint16_t)at;
}
