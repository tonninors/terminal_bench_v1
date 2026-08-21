/* kvstore.h - public API of the ministore embedded key/value engine.
 *
 * ministore is a small single-writer embedded storage engine: a B+tree
 * over fixed-size pages, made durable by a write-ahead log with redo
 * recovery.  The on-disk format and this API are frozen; see
 * docs/FORMAT.md.
 */
#ifndef MINISTORE_KVSTORE_H
#define MINISTORE_KVSTORE_H

#include <stddef.h>
#include <stdint.h>

typedef struct kvstore kvstore_t;

#define KV_OK             0
#define KV_ERR_IO        (-1)
#define KV_ERR_CORRUPT   (-2)
#define KV_ERR_NOTFOUND  (-3)
#define KV_ERR_INVAL     (-4)
#define KV_ERR_NOSPACE   (-5)

/* Largest value ministore stores inline in a leaf slot. */
#define KV_MAX_VALUE_LEN 52

/* Open (creating if necessary) the database at `path`.  The write-ahead
 * log lives at "<path>.wal".  If the log contains work that never reached
 * the data file, kv_open() runs redo recovery before returning. */
kvstore_t *kv_open(const char *path);

/* Flush everything and close.  Returns KV_OK on success. */
int kv_close(kvstore_t *s);

/* Transactions.  A transaction may touch any number of keys and any
 * number of pages.  Either every operation of a committed transaction is
 * durable, or - for a transaction that was aborted or interrupted - none
 * of them is.  kv_put and kv_delete called outside kv_begin run inside a
 * transaction of their own. */
int kv_begin(kvstore_t *s);
int kv_commit(kvstore_t *s);
int kv_abort(kvstore_t *s);

/* Insert `key`, or replace its value if it already exists. */
int kv_put(kvstore_t *s, uint64_t key, const void *val, uint32_t len);

/* Look up `key`.  Returns KV_OK, or KV_ERR_NOTFOUND. */
int kv_get(kvstore_t *s, uint64_t key, void *buf, uint32_t buflen,
           uint32_t *out_len);

/* Remove `key` if present.  Leaf pages are not merged or rebalanced. */
int kv_delete(kvstore_t *s, uint64_t key);

/* Flush every dirty page and record a checkpoint in the log. */
int kv_checkpoint(kvstore_t *s);

/* Walk the leaf chain from `start`, calling `cb` for each live pair until
 * it returns non-zero.  Returns KV_OK or an error. */
int kv_scan(kvstore_t *s, uint64_t start,
            int (*cb)(uint64_t key, const void *val, uint32_t len, void *ctx),
            void *ctx);

/* Structural self-check of the tree: page types, key ordering, child
 * reachability, leaf chain and key count.  Writes a human readable
 * explanation into `err` when the tree is not well formed. */
int kv_verify(kvstore_t *s, char *err, size_t errlen);

/* Number of live keys, counted by walking the leaf chain. */
int64_t kv_count(kvstore_t *s);

/* Tree height (1 = the root is a leaf) and page count. */
uint32_t kv_height(kvstore_t *s);
uint32_t kv_pages(kvstore_t *s);

/* Frames in the buffer pool.  This is a fixed property of the build, not
 * something that grows with the database or with a transaction. */
uint32_t kv_pool_frames(kvstore_t *s);

const char *kv_strerror(int rc);

#endif /* MINISTORE_KVSTORE_H */
