/* wal.c - append-only write-ahead log.
 *
 * Records are self-describing and CRC protected so that a torn tail left
 * by a crash can be detected and ignored.  Log truncation is not
 * implemented yet: the log grows until the database is rebuilt.
 */
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "internal.h"

struct wal {
    int      fd;
    off_t    end;          /* append offset                     */
    off_t    read_off;     /* iteration cursor                  */
    uint64_t next_lsn;
    uint64_t written_lsn;  /* highest lsn handed to the OS      */
    uint64_t durable_lsn;  /* highest lsn known to be on disk   */
};

wal_t *wal_open(const char *path, uint64_t next_lsn)
{
    wal_t *w = calloc(1, sizeof *w);
    if (!w) return NULL;
    w->fd = open(path, O_RDWR | O_CREAT, 0644);
    if (w->fd < 0) { free(w); return NULL; }
    w->end = lseek(w->fd, 0, SEEK_END);
    w->next_lsn = next_lsn ? next_lsn : 1;
    w->written_lsn = w->durable_lsn = next_lsn ? next_lsn - 1 : 0;
    return w;
}

int wal_close(wal_t *w)
{
    int rc = (fsync(w->fd) == 0) ? KV_OK : KV_ERR_IO;
    close(w->fd);
    free(w);
    return rc;
}

uint64_t wal_next_lsn(wal_t *w) { return w->next_lsn; }

/* Recovery learns from the log how far sequence numbers actually got, so
 * that the records written after it continue past the ones already
 * stamped on the pages. */
void wal_set_next_lsn(wal_t *w, uint64_t lsn)
{
    if (lsn <= w->next_lsn) return;
    w->next_lsn = lsn;
    if (w->written_lsn < lsn - 1) w->written_lsn = lsn - 1;
    if (w->durable_lsn < lsn - 1) w->durable_lsn = lsn - 1;
}
uint64_t wal_durable_lsn(wal_t *w) { return w->durable_lsn; }

int wal_append(wal_t *w, wal_rec_t *r, const void *payload, uint32_t vlen,
               uint64_t *out_lsn)
{
    uint8_t buf[sizeof(wal_rec_t) + WAL_MAX_PAYLOAD];
    if (vlen > WAL_MAX_PAYLOAD) return KV_ERR_INVAL;

    r->magic = WAL_MAGIC;
    r->vlen  = vlen;
    r->len   = (uint32_t)sizeof(wal_rec_t) + vlen;
    r->lsn   = w->next_lsn++;

    memcpy(buf, r, sizeof *r);
    if (vlen) memcpy(buf + sizeof *r, payload, vlen);
    /* crc covers everything after the crc field itself */
    size_t off = offsetof(wal_rec_t, crc) + sizeof(uint32_t);
    ((wal_rec_t *)buf)->crc = ms_crc32(buf + off, r->len - off);

    ssize_t n = pwrite(w->fd, buf, r->len, w->end);
    if (n != (ssize_t)r->len) return KV_ERR_IO;
    w->end += r->len;
    w->written_lsn = r->lsn;
    if (out_lsn) *out_lsn = r->lsn;
    return KV_OK;
}

int wal_flush_upto(wal_t *w, uint64_t lsn)
{
    if (lsn <= w->durable_lsn) return KV_OK;
    if (fsync(w->fd) != 0) return KV_ERR_IO;
    w->durable_lsn = w->written_lsn;
    return KV_OK;
}

int wal_sync(wal_t *w)
{
    if (fsync(w->fd) != 0) return KV_ERR_IO;
    w->durable_lsn = w->written_lsn;
    return KV_OK;
}

int wal_read_first(wal_t *w)
{
    w->read_off = 0;
    return KV_OK;
}

/* Returns KV_OK on a good record, KV_ERR_NOTFOUND at a clean end of log,
 * KV_ERR_CORRUPT on a torn or damaged tail. */
int wal_read_next(wal_t *w, wal_rec_t *r, void *payload, uint32_t cap)
{
    ssize_t n = pread(w->fd, r, sizeof *r, w->read_off);
    if (n == 0) return KV_ERR_NOTFOUND;
    if (n != (ssize_t)sizeof *r) return KV_ERR_CORRUPT;
    if (r->magic != WAL_MAGIC) return KV_ERR_CORRUPT;
    if (r->len < sizeof *r || r->len > sizeof *r + WAL_MAX_PAYLOAD)
        return KV_ERR_CORRUPT;

    uint8_t buf[sizeof(wal_rec_t) + WAL_MAX_PAYLOAD];
    n = pread(w->fd, buf, r->len, w->read_off);
    if (n != (ssize_t)r->len) return KV_ERR_CORRUPT;
    size_t off = offsetof(wal_rec_t, crc) + sizeof(uint32_t);
    if (ms_crc32(buf + off, r->len - off) != r->crc) return KV_ERR_CORRUPT;

    if (r->vlen) {
        if (r->vlen > cap) return KV_ERR_CORRUPT;
        memcpy(payload, buf + sizeof *r, r->vlen);
    }
    w->read_off += r->len;
    return KV_OK;
}

int wal_truncate(wal_t *w)
{
    if (ftruncate(w->fd, 0) != 0) return KV_ERR_IO;
    w->end = 0;
    return wal_sync(w);
}
