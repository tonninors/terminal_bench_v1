/* pager.c - page cache and data-file I/O.
 *
 * The buffer pool is deliberately small, the way an embedded engine on a
 * constrained device is configured: any working set larger than NFRAMES
 * pages evicts, and eviction writes the frame back.  Frames are written
 * back lazily and in no particular order.  The write-ahead rule is
 * enforced here: a dirty page is never written to the data file before
 * the log record that describes its newest change is durable.
 */
#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "internal.h"

#define NFRAMES 32

struct pager {
    int      fd;
    uint64_t clock;
    wal_t   *wal;
    page_t   meta;
    page_t   frames[NFRAMES];
};

static int page_read(pager_t *p, uint32_t id, uint8_t *buf)
{
    ssize_t n = pread(p->fd, buf, PAGE_SIZE, (off_t)id * PAGE_SIZE);
    if (n < 0) return KV_ERR_IO;
    if (n < PAGE_SIZE) memset(buf + n, 0, PAGE_SIZE - n);
    return KV_OK;
}

static int page_write(pager_t *p, uint32_t id, const uint8_t *buf)
{
    ssize_t n = pwrite(p->fd, buf, PAGE_SIZE, (off_t)id * PAGE_SIZE);
    return (n == PAGE_SIZE) ? KV_OK : KV_ERR_IO;
}

static int frame_writeback(pager_t *p, page_t *f)
{
    if (!f->dirty) return KV_OK;
    /* write-ahead rule */
    if (p->wal) {
        int rc = wal_flush_upto(p->wal, PHDR(f)->lsn);
        if (rc != KV_OK) return rc;
    }
    int rc = page_write(p, f->id, f->buf);
    if (rc != KV_OK) return rc;
    ms_crash_hook("page_write");
    f->dirty = 0;
    return KV_OK;
}

meta_t *pager_meta(pager_t *p) { return (meta_t *)p->meta.buf; }
void pager_meta_dirty(pager_t *p) { p->meta.dirty = 1; }
uint32_t pager_num_pages(pager_t *p) { return pager_meta(p)->num_pages; }
void pager_attach_wal(pager_t *p, wal_t *w) { p->wal = w; }

pager_t *pager_open(const char *path, int *created)
{
    pager_t *p = calloc(1, sizeof *p);
    if (!p) return NULL;
    p->fd = open(path, O_RDWR | O_CREAT, 0644);
    if (p->fd < 0) { free(p); return NULL; }
    for (int i = 0; i < NFRAMES; i++) p->frames[i].id = INVALID_PAGE;

    off_t sz = lseek(p->fd, 0, SEEK_END);
    p->meta.id = META_PAGE;
    if (sz < PAGE_SIZE) {
        meta_t *m = (meta_t *)p->meta.buf;
        memset(p->meta.buf, 0, PAGE_SIZE);
        m->hdr.type = PT_META;
        m->magic = MS_MAGIC;
        m->version = MS_VERSION;
        m->page_size = PAGE_SIZE;
        m->num_pages = 2;            /* meta + empty root leaf */
        m->root = 1;
        m->height = 1;
        m->checkpoint_lsn = 0;
        m->next_lsn = 1;
        p->meta.dirty = 1;
        if (page_write(p, META_PAGE, p->meta.buf) != KV_OK) goto fail;
        page_t root;
        memset(&root, 0, sizeof root);
        node_init(&root, PT_LEAF);
        PHDR(&root)->link = INVALID_PAGE;
        if (page_write(p, 1, root.buf) != KV_OK) goto fail;
        p->meta.dirty = 0;
        if (created) *created = 1;
    } else {
        if (page_read(p, META_PAGE, p->meta.buf) != KV_OK) goto fail;
        meta_t *m = (meta_t *)p->meta.buf;
        if (m->magic != MS_MAGIC || m->page_size != PAGE_SIZE) goto fail;
        if (created) *created = 0;
    }
    return p;
fail:
    close(p->fd);
    free(p);
    return NULL;
}

int pager_close(pager_t *p)
{
    int rc = pager_flush_all(p);
    if (rc == KV_OK) rc = pager_sync(p);
    close(p->fd);
    free(p);
    return rc;
}

static page_t *frame_for(pager_t *p, uint32_t id, int fresh)
{
    if (id == META_PAGE) { p->meta.pin++; return &p->meta; }
    for (int i = 0; i < NFRAMES; i++) {
        if (p->frames[i].id == id) {
            p->frames[i].used = ++p->clock;
            p->frames[i].pin++;
            return &p->frames[i];
        }
    }
    page_t *victim = NULL;
    for (int i = 0; i < NFRAMES; i++) {
        if (p->frames[i].id == INVALID_PAGE) { victim = &p->frames[i]; break; }
        if (p->frames[i].pin) continue;
        if (!victim || p->frames[i].used < victim->used) victim = &p->frames[i];
    }
    if (!victim || victim->pin) return NULL;
    if (victim->id != INVALID_PAGE && frame_writeback(p, victim) != KV_OK)
        return NULL;
    victim->id = id;
    victim->dirty = 0;
    victim->pin = 1;
    victim->used = ++p->clock;
    if (fresh) memset(victim->buf, 0, PAGE_SIZE);
    else if (page_read(p, id, victim->buf) != KV_OK) {
        victim->id = INVALID_PAGE;
        return NULL;
    }
    return victim;
}

page_t *pager_get(pager_t *p, uint32_t id)
{
    if (id == INVALID_PAGE || id >= pager_num_pages(p)) return NULL;
    return frame_for(p, id, 0);
}

void pager_unpin(pager_t *p, page_t *pg)
{
    (void)p;
    if (pg && pg->pin) pg->pin--;
}

void pager_mark_dirty(pager_t *p, page_t *pg)
{
    (void)p;
    if (pg) pg->dirty = 1;
}

page_t *pager_alloc(pager_t *p, uint32_t *out_id)
{
    meta_t *m = pager_meta(p);
    uint32_t id = m->num_pages;
    page_t *pg = frame_for(p, id, 1);
    if (!pg) return NULL;
    m->num_pages++;
    p->meta.dirty = 1;
    pg->dirty = 1;
    if (out_id) *out_id = id;
    return pg;
}

/* Make `id` addressable, growing the file if redo refers to a page that
 * the data file has never seen. */
page_t *pager_ensure(pager_t *p, uint32_t id)
{
    meta_t *m = pager_meta(p);
    int fresh = 0;
    if (id >= m->num_pages) {
        m->num_pages = id + 1;
        p->meta.dirty = 1;
        fresh = 1;
    }
    page_t *pg = frame_for(p, id, 0);
    if (pg && fresh) {
        memset(pg->buf, 0, PAGE_SIZE);
        pg->dirty = 1;
    }
    return pg;
}

int pager_flush_all(pager_t *p)
{
    for (int i = 0; i < NFRAMES; i++) {
        if (p->frames[i].id == INVALID_PAGE) continue;
        int rc = frame_writeback(p, &p->frames[i]);
        if (rc != KV_OK) return rc;
    }
    if (p->meta.dirty) {
        if (p->wal) {
            int rc = wal_flush_upto(p->wal, PHDR(&p->meta)->lsn);
            if (rc != KV_OK) return rc;
        }
        int rc = page_write(p, META_PAGE, p->meta.buf);
        if (rc != KV_OK) return rc;
        ms_crash_hook("meta_write");
        p->meta.dirty = 0;
    }
    return KV_OK;
}

int pager_sync(pager_t *p)
{
    return fsync(p->fd) == 0 ? KV_OK : KV_ERR_IO;
}

/* Drop every cached frame without writing it back: used by the tests to
 * observe the state that survived on disk. */
void pager_reset_cache(pager_t *p)
{
    for (int i = 0; i < NFRAMES; i++) {
        p->frames[i].id = INVALID_PAGE;
        p->frames[i].dirty = 0;
        p->frames[i].pin = 0;
    }
}
