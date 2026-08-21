/* kvcli.c - command line driver used by the tests and for debugging. */
#include <fcntl.h>
#include <inttypes.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "internal.h"

/* Deterministic value for a key, so any run can be checked again later. */
static uint32_t value_for(uint64_t key, char *buf, uint32_t cap)
{
    uint32_t len = 16 + (uint32_t)(key % 24);
    if (len > cap) len = cap;
    for (uint32_t i = 0; i < len; i++)
        buf[i] = (char)('a' + (char)((key + i * 7u) % 26u));
    return len;
}

static int usage(void)
{
    fprintf(stderr,
        "usage: kvcli <command> [args]\n"
        "  fill <db> <count> [--start N] [--checkpoint-every N]\n"
        "  update <db> <count> [--start N]\n"
        "  del <db> <key>\n"
        "  get <db> <key>\n"
        "  recover <db>                 open (running recovery) and close\n"
        "  verify <db> [expect_count]   structural check and key sweep\n"
        "  stats <db>\n"
        "  dump <db>                    every live pair, in key order\n"
        "  waldump <db> [limit]\n");
    return 2;
}

static int arg_num(int argc, char **argv, const char *name, long defval,
                   long *out)
{
    *out = defval;
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], name) && i + 1 < argc)
            *out = strtol(argv[i + 1], NULL, 10);
    return 0;
}

/* Insertion order.  --spread walks the same set of keys in a fixed
 * scattered order (a stride permutation), the way a real workload keyed
 * by an identifier does, so the pages in play do not stay in one corner
 * of the tree. */
static uint64_t nth_key(long i, long start, long count, int spread)
{
    if (!spread) return (uint64_t)(start + i);
    return (uint64_t)(start + ((i * 7919L) % count));
}

/* The application records how many writes it has been told succeeded.
 * A crash cannot lose this: the count is written with a plain write(2)
 * after kv_put returns, so it survives the process dying. */
static int progress_open(int argc, char **argv)
{
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], "--progress") && i + 1 < argc)
            return open(argv[i + 1], O_WRONLY | O_CREAT | O_TRUNC, 0644);
    return -1;
}

static void progress_note(int fd, long n)
{
    char buf[24];
    int len;
    if (fd < 0) return;
    len = snprintf(buf, sizeof buf, "%012ld\n", n);
    if (pwrite(fd, buf, (size_t)len, 0) != len) { /* nothing useful to do */ }
}

static int cmd_fill(int argc, char **argv)
{
    long start = 0, ckpt = 0;
    int spread = 0;
    if (argc < 2) return usage();
    const char *db = argv[0];
    long count = strtol(argv[1], NULL, 10);
    arg_num(argc, argv, "--start", 1, &start);
    arg_num(argc, argv, "--checkpoint-every", 0, &ckpt);
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], "--spread")) spread = 1;
    if (spread && (count % 7919L) == 0) {
        fprintf(stderr, "kvcli: --spread needs a count that is not a "
                        "multiple of 7919\n");
        return 2;
    }

    int pfd = progress_open(argc, argv);
    kvstore_t *s = kv_open(db);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", db); return 1; }
    char val[KV_MAX_VALUE_LEN];
    for (long i = 0; i < count; i++) {
        uint64_t key = nth_key(i, start, count, spread);
        uint32_t len = value_for(key, val, sizeof val);
        int rc = kv_put(s, key, val, len);
        if (rc != KV_OK) {
            fprintf(stderr, "kvcli: put %" PRIu64 ": %s\n", key, kv_strerror(rc));
            return 1;
        }
        progress_note(pfd, i + 1);
        if (ckpt && ((i + 1) % ckpt) == 0) kv_checkpoint(s);
    }
    if (pfd >= 0) close(pfd);
    int rc = kv_close(s);
    if (rc != KV_OK) { fprintf(stderr, "kvcli: close: %s\n", kv_strerror(rc)); return 1; }
    printf("filled %ld keys from %ld\n", count, start);
    return 0;
}

static int cmd_update(int argc, char **argv)
{
    long start = 0;
    if (argc < 2) return usage();
    const char *db = argv[0];
    long count = strtol(argv[1], NULL, 10);
    arg_num(argc, argv, "--start", 1, &start);

    kvstore_t *s = kv_open(db);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", db); return 1; }
    char val[KV_MAX_VALUE_LEN];
    for (long i = 0; i < count; i++) {
        uint64_t key = (uint64_t)(start + i);
        uint32_t len = value_for(key, val, sizeof val);
        int rc = kv_put(s, key, val, len);
        if (rc != KV_OK) { fprintf(stderr, "kvcli: update: %s\n", kv_strerror(rc)); return 1; }
    }
    if (kv_close(s) != KV_OK) return 1;
    printf("updated %ld keys from %ld\n", count, start);
    return 0;
}

static int cmd_simple(int argc, char **argv, int del)
{
    if (argc < 2) return usage();
    kvstore_t *s = kv_open(argv[0]);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", argv[0]); return 1; }
    uint64_t key = strtoull(argv[1], NULL, 10);
    int rc;
    if (del) {
        rc = kv_delete(s, key);
        printf("delete %" PRIu64 ": %s\n", key, kv_strerror(rc));
    } else {
        char buf[KV_MAX_VALUE_LEN + 1];
        uint32_t len = 0;
        rc = kv_get(s, key, buf, sizeof buf - 1, &len);
        if (rc == KV_OK) { buf[len] = 0; printf("%" PRIu64 " = %s\n", key, buf); }
        else printf("%" PRIu64 ": %s\n", key, kv_strerror(rc));
    }
    kv_close(s);
    return rc == KV_OK ? 0 : 1;
}

static int cmd_recover(int argc, char **argv)
{
    if (argc < 1) return usage();
    kvstore_t *s = kv_open(argv[0]);
    if (!s) { fprintf(stderr, "kvcli: recovery failed to open %s\n", argv[0]); return 1; }
    int rc = kv_close(s);
    if (rc != KV_OK) { fprintf(stderr, "kvcli: close: %s\n", kv_strerror(rc)); return 1; }
    printf("recovered\n");
    return 0;
}

static int cmd_verify(int argc, char **argv)
{
    if (argc < 1) return usage();
    kvstore_t *s = kv_open(argv[0]);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", argv[0]); return 1; }

    char err[256];
    int rc = kv_verify(s, err, sizeof err);
    if (rc != KV_OK) {
        fprintf(stderr, "kvcli: tree is not well formed: %s\n",
                err[0] ? err : kv_strerror(rc));
        kv_close(s);
        return 1;
    }
    int64_t n = kv_count(s);
    if (argc >= 2) {
        long expect = strtol(argv[1], NULL, 10);
        char val[KV_MAX_VALUE_LEN], got[KV_MAX_VALUE_LEN];
        for (long i = 1; i <= expect; i++) {
            uint32_t want_len = value_for((uint64_t)i, val, sizeof val);
            uint32_t got_len = 0;
            int grc = kv_get(s, (uint64_t)i, got, sizeof got, &got_len);
            if (grc != KV_OK) {
                fprintf(stderr, "kvcli: key %ld missing after recovery (%s)\n",
                        i, kv_strerror(grc));
                kv_close(s);
                return 1;
            }
            if (got_len != want_len || memcmp(got, val, want_len)) {
                fprintf(stderr, "kvcli: key %ld has the wrong value\n", i);
                kv_close(s);
                return 1;
            }
        }
        if (n != expect) {
            fprintf(stderr, "kvcli: expected %ld keys, found %lld\n",
                    expect, (long long)n);
            kv_close(s);
            return 1;
        }
    }
    printf("ok: %lld keys, height %u, %u pages\n", (long long)n,
           kv_height(s), kv_pages(s));
    kv_close(s);
    return 0;
}

/* expect <db> <total> <committed> [--spread]
 *
 * The workload inserts `total` keys in a known order; a crash left the
 * first `committed` of them durable.  Check that exactly those keys are
 * present, with their proper values, and nothing else. */
static int cmd_expect(int argc, char **argv)
{
    int spread = 0;
    if (argc < 3) return usage();
    const char *db = argv[0];
    long total = strtol(argv[1], NULL, 10);
    long committed = strtol(argv[2], NULL, 10);
    for (int i = 0; i < argc; i++)
        if (!strcmp(argv[i], "--spread")) spread = 1;
    if (committed > total) return usage();

    kvstore_t *s = kv_open(db);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", db); return 1; }

    char err[256];
    int rc = kv_verify(s, err, sizeof err);
    if (rc != KV_OK) {
        fprintf(stderr, "kvcli: tree is not well formed: %s\n",
                err[0] ? err : kv_strerror(rc));
        kv_close(s);
        return 1;
    }

    char want[KV_MAX_VALUE_LEN], got[KV_MAX_VALUE_LEN];
    for (long i = 0; i < committed; i++) {
        uint64_t key = nth_key(i, 1, total, spread);
        uint32_t want_len = value_for(key, want, sizeof want);
        uint32_t got_len = 0;
        int grc = kv_get(s, key, got, sizeof got, &got_len);
        if (grc != KV_OK) {
            fprintf(stderr, "kvcli: committed key %" PRIu64 " is missing "
                    "after recovery (%s)\n", key, kv_strerror(grc));
            kv_close(s);
            return 1;
        }
        if (got_len != want_len || memcmp(got, want, want_len)) {
            fprintf(stderr, "kvcli: committed key %" PRIu64 " came back with "
                    "the wrong value\n", key);
            kv_close(s);
            return 1;
        }
    }
    int64_t n = kv_count(s);
    if (n != committed) {
        fprintf(stderr, "kvcli: %lld keys are present, but exactly %ld were "
                "committed\n", (long long)n, committed);
        kv_close(s);
        return 1;
    }
    printf("ok: exactly %ld committed keys, height %u, %u pages\n",
           committed, kv_height(s), kv_pages(s));
    kv_close(s);
    return 0;
}

static int dump_cb(uint64_t k, const void *v, uint32_t n, void *ctx)
{
    (void)ctx;
    printf("%" PRIu64 " ", k);
    fwrite(v, 1, n, stdout);
    printf("\n");
    return 0;
}

static int cmd_dump(int argc, char **argv)
{
    if (argc < 1) return usage();
    kvstore_t *s = kv_open(argv[0]);
    if (!s) { fprintf(stderr, "kvcli: cannot open %s\n", argv[0]); return 1; }
    int rc = kv_scan(s, 0, dump_cb, NULL);
    kv_close(s);
    if (rc != KV_OK) { fprintf(stderr, "kvcli: scan: %s\n", kv_strerror(rc)); return 1; }
    return 0;
}

static int cmd_stats(int argc, char **argv)
{
    if (argc < 1) return usage();
    kvstore_t *s = kv_open(argv[0]);
    if (!s) return 1;
    printf("keys=%lld height=%u pages=%u leaf_cap=%d int_cap=%d\n",
           (long long)kv_count(s), kv_height(s), kv_pages(s),
           LEAF_CAP, INT_CAP);
    kv_close(s);
    return 0;
}

static const char *rec_name(uint32_t t)
{
    switch (t) {
    case WR_BEGIN:      return "BEGIN";
    case WR_COMMIT:     return "COMMIT";
    case WR_LEAF_PUT:   return "LEAF_PUT";
    case WR_LEAF_DEL:   return "LEAF_DEL";
    case WR_LEAF_SPLIT: return "LEAF_SPLIT";
    case WR_INT_INSERT: return "INT_INSERT";
    case WR_INT_SPLIT:  return "INT_SPLIT";
    case WR_NEW_ROOT:   return "NEW_ROOT";
    case WR_CHECKPOINT: return "CHECKPOINT";
    default:            return "?";
    }
}

static int cmd_waldump(int argc, char **argv)
{
    if (argc < 1) return usage();
    char path[4096];
    snprintf(path, sizeof path, "%s.wal", argv[0]);
    long limit = (argc >= 2) ? strtol(argv[1], NULL, 10) : 0;

    wal_t *w = wal_open(path, 1);
    if (!w) { fprintf(stderr, "kvcli: cannot open %s\n", path); return 1; }
    wal_read_first(w);
    wal_rec_t r;
    uint8_t payload[PAGE_SIZE];
    long n = 0;
    for (;;) {
        int rc = wal_read_next(w, &r, payload, sizeof payload);
        if (rc == KV_ERR_NOTFOUND) break;
        if (rc == KV_ERR_CORRUPT) { printf("-- log ends here (torn tail)\n"); break; }
        printf("lsn=%-6" PRIu64 " txn=%-6" PRIu64 " %-11s page=%-5u aux=%-5u "
               "key=%-8" PRIu64 " arg=%-5u vlen=%u\n",
               r.lsn, r.txn, rec_name(r.type), r.page, r.aux, r.key, r.arg,
               r.vlen);
        if (limit && ++n >= limit) break;
    }
    wal_close(w);
    return 0;
}

int main(int argc, char **argv)
{
    if (argc < 2) return usage();
    const char *cmd = argv[1];
    int n = argc - 2;
    char **a = argv + 2;
    if (!strcmp(cmd, "fill"))    return cmd_fill(n, a);
    if (!strcmp(cmd, "update"))  return cmd_update(n, a);
    if (!strcmp(cmd, "get"))     return cmd_simple(n, a, 0);
    if (!strcmp(cmd, "del"))     return cmd_simple(n, a, 1);
    if (!strcmp(cmd, "recover")) return cmd_recover(n, a);
    if (!strcmp(cmd, "verify"))  return cmd_verify(n, a);
    if (!strcmp(cmd, "expect"))  return cmd_expect(n, a);
    if (!strcmp(cmd, "stats"))   return cmd_stats(n, a);
    if (!strcmp(cmd, "dump"))    return cmd_dump(n, a);
    if (!strcmp(cmd, "waldump")) return cmd_waldump(n, a);
    return usage();
}
