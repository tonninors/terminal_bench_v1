#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "internal.h"

static uint32_t crc_table[256];
static int crc_ready;

static void crc_init(void)
{
    for (uint32_t i = 0; i < 256; i++) {
        uint32_t c = i;
        for (int k = 0; k < 8; k++)
            c = (c & 1) ? 0xEDB88320u ^ (c >> 1) : c >> 1;
        crc_table[i] = c;
    }
    crc_ready = 1;
}

uint32_t ms_crc32(const void *data, size_t len)
{
    const uint8_t *p = data;
    uint32_t c = 0xFFFFFFFFu;
    if (!crc_ready) crc_init();
    for (size_t i = 0; i < len; i++)
        c = crc_table[(c ^ p[i]) & 0xFF] ^ (c >> 8);
    return c ^ 0xFFFFFFFFu;
}

/* Fault injection used by the crash tests.  Setting MINISTORE_CRASH_POINT
 * and MINISTORE_CRASH_COUNT makes the process die abruptly - as it would
 * on power loss - the Nth time the named point is reached. */
void ms_crash_hook(const char *point)
{
    static const char *want;
    static long remaining = -1;
    if (remaining < 0) {
        const char *pt = getenv("MINISTORE_CRASH_POINT");
        const char *ct = getenv("MINISTORE_CRASH_COUNT");
        want = pt;
        remaining = (pt && ct) ? strtol(ct, NULL, 10) : 0;
        if (remaining <= 0) { remaining = 0; want = NULL; }
    }
    if (!want || strcmp(want, point) != 0) return;
    if (--remaining == 0) {
        /* no flush, no cleanup: exactly what a power cut looks like */
        _exit(90);
    }
}
