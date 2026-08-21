/* internal.h - on-disk layout and internal interfaces (not installed). */
#ifndef MINISTORE_INTERNAL_H
#define MINISTORE_INTERNAL_H

#include <stdint.h>
#include <stdio.h>
#include "kvstore.h"

#define MS_MAGIC        0x4D53544Fu   /* "MSTO" */
#define MS_VERSION      3
#define PAGE_SIZE       1024
#define PAGE_HDR_SIZE   16
#define META_PAGE       0
#define INVALID_PAGE    0xFFFFFFFFu
#define MAX_TREE_DEPTH  24

/* ---------------------------------------------------------------- pages */
#define PT_META      1
#define PT_LEAF      2
#define PT_INTERNAL  3

typedef struct __attribute__((packed)) {
    uint64_t lsn;    /* log sequence number of the last change applied  */
    uint16_t type;   /* PT_*                                            */
    uint16_t nkeys;  /* live entries                                    */
    uint32_t link;   /* leaf: next leaf page; internal: leftmost child   */
} page_hdr_t;

typedef struct __attribute__((packed)) {
    uint64_t key;
    uint16_t vlen;
    uint8_t  pad[2];
    uint8_t  val[KV_MAX_VALUE_LEN];
} leaf_slot_t;                       /* 64 bytes */

typedef struct __attribute__((packed)) {
    uint64_t key;                    /* separator: keys >= key live in child */
    uint32_t child;
    uint32_t pad;
} int_ent_t;                         /* 16 bytes */

#define LEAF_CAP ((PAGE_SIZE - PAGE_HDR_SIZE) / (int)sizeof(leaf_slot_t))
#define INT_CAP  ((PAGE_SIZE - PAGE_HDR_SIZE) / (int)sizeof(int_ent_t))

typedef struct __attribute__((packed)) {
    page_hdr_t hdr;
    uint32_t magic;
    uint32_t version;
    uint32_t page_size;
    uint32_t num_pages;
    uint32_t root;
    uint32_t height;
    uint64_t checkpoint_lsn;
    uint64_t next_lsn;
} meta_t;

typedef struct page {
    uint32_t id;
    int      dirty;
    uint32_t pin;
    uint64_t used;                   /* LRU clock                       */
    uint8_t  buf[PAGE_SIZE];
} page_t;

#define PHDR(p)      ((page_hdr_t *)((p)->buf))
#define LEAF_SLOTS(p) ((leaf_slot_t *)((p)->buf + PAGE_HDR_SIZE))
#define INT_ENTS(p)   ((int_ent_t *)((p)->buf + PAGE_HDR_SIZE))

/* ------------------------------------------------------------------ wal */
#define WR_BEGIN       1
#define WR_COMMIT      2
#define WR_LEAF_PUT    3
#define WR_LEAF_DEL    4
#define WR_LEAF_SPLIT  5
#define WR_INT_INSERT  6
#define WR_INT_SPLIT   7
#define WR_NEW_ROOT    8
#define WR_CHECKPOINT  9

#define WAL_MAGIC 0x57414C31u        /* "WAL1" */
#define WAL_MAX_PAYLOAD PAGE_SIZE    /* a split logs the new page in full */

typedef struct __attribute__((packed)) {
    uint32_t magic;
    uint32_t len;       /* header + payload                              */
    uint32_t crc;       /* over everything after this field              */
    uint32_t type;
    uint64_t lsn;
    uint64_t txn;
    uint32_t page;      /* primary page the record changes               */
    uint32_t aux;       /* secondary page (new sibling / left child)      */
    uint64_t key;
    uint32_t arg;       /* split position, or child page id              */
    uint32_t vlen;      /* payload bytes following the header            */
} wal_rec_t;

typedef struct wal wal_t;
typedef struct pager pager_t;

/* ---------------------------------------------------------------- pager */
pager_t *pager_open(const char *path, int *created);
int      pager_close(pager_t *p);
page_t  *pager_get(pager_t *p, uint32_t id);           /* pins the page   */
void     pager_unpin(pager_t *p, page_t *pg);
void     pager_mark_dirty(pager_t *p, page_t *pg);
page_t  *pager_alloc(pager_t *p, uint32_t *out_id);
page_t  *pager_ensure(pager_t *p, uint32_t id);        /* grow to fit id  */
int      pager_flush_all(pager_t *p);
int      pager_sync(pager_t *p);
uint32_t pager_num_pages(pager_t *p);
meta_t  *pager_meta(pager_t *p);
void     pager_meta_dirty(pager_t *p);
void     pager_attach_wal(pager_t *p, wal_t *w);
void     pager_reset_cache(pager_t *p);

/* ------------------------------------------------------------------ wal */
wal_t   *wal_open(const char *path, uint64_t next_lsn);
int      wal_close(wal_t *w);
int      wal_append(wal_t *w, wal_rec_t *r, const void *payload,
                    uint32_t vlen, uint64_t *out_lsn);
int      wal_flush_upto(wal_t *w, uint64_t lsn);
int      wal_sync(wal_t *w);
uint64_t wal_next_lsn(wal_t *w);
void     wal_set_next_lsn(wal_t *w, uint64_t lsn);
int      wal_read_first(wal_t *w);
int      wal_read_next(wal_t *w, wal_rec_t *r, void *payload, uint32_t cap);
int      wal_truncate(wal_t *w);
uint64_t wal_durable_lsn(wal_t *w);

/* --------------------------------------------------------------- nodes  */
void node_init(page_t *pg, uint16_t type);
int  leaf_find(page_t *pg, uint64_t key);              /* index or -1     */
int  leaf_lower_bound(page_t *pg, uint64_t key);
int  leaf_put(page_t *pg, uint64_t key, const void *val, uint32_t vlen);
int  leaf_del(page_t *pg, uint64_t key);
void leaf_split(page_t *left, page_t *right, uint32_t right_id, int at);
int  int_child(page_t *pg, uint64_t key);              /* descend         */
int  int_insert(page_t *pg, uint64_t key, uint32_t child);
void int_split(page_t *left, page_t *right, int at, uint64_t *sep_out);
void int_set_leftmost(page_t *pg, uint32_t child);

/* -------------------------------------------------------------- btree   */
struct kvstore {
    pager_t *pg;
    wal_t   *wal;
    uint64_t txn;
    char    *path;
};

int btree_put(kvstore_t *s, uint64_t key, const void *val, uint32_t vlen);
int btree_del(kvstore_t *s, uint64_t key);
int btree_get(kvstore_t *s, uint64_t key, void *buf, uint32_t cap,
              uint32_t *out_len);

/* ------------------------------------------------------------ recovery  */
int recover_redo(kvstore_t *s);

/* ---------------------------------------------------------------- util  */
uint32_t ms_crc32(const void *data, size_t len);
void     ms_crash_hook(const char *point);   /* fault injection           */

#endif /* MINISTORE_INTERNAL_H */
