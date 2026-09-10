/* Native working memory of one codec call, from libzstd's own accounting. ztype's zstd
 * objects live outside PostgreSQL's memory contexts and work_mem, so this is the table an
 * administrator needs to size a backend: what a compression context costs per level and
 * pledged input size (zt_compress pledges the exact size, so zstd sizes the window to the
 * smaller of the input and the level's window), what a compression or decompression
 * dictionary object costs per dictionary size and level (these are the cached parts,
 * counted against ztype.dictionary_cache_size), and what a decompression context costs
 * per frame: with a stable output buffer, as full decodes run (the datum is the window, no
 * copy), and streaming into a bounded buffer, as `prefix` runs, where zstd allocates the
 * frame's window itself. Contexts are measured after their first call, which is when zstd
 * allocates the workspace, so no full compression at level 22 is needed; decode frames are
 * made at level 1 with each level's window size, since the decoder's memory follows the
 * frame header, not the level that wrote it. Run by tests/bench_memory.py (`make
 * bench-memory`), which checks the backend's resident size against these numbers.
 */
#define ZSTD_STATIC_LINKING_ONLY
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>

static const int levels[] = {1, 3, 6, 9, 19, 22};
static const size_t inputs[] = {1024, 64 * 1024, 1024 * 1024, 16 * 1024 * 1024, 128 * 1024 * 1024};
static const size_t dicts[] = {8 * 1024, 110 * 1024, 1024 * 1024};
#define N(a) (sizeof(a) / sizeof((a)[0]))
#define CHUNK (256 * 1024)

static void check(size_t rc)
{
    if (ZSTD_isError(rc))
    {
        fprintf(stderr, "zstd: %s\n", ZSTD_getErrorName(rc));
        exit(1);
    }
}

static void *xmalloc(size_t n)
{
    void *p = malloc(n);
    if (!p)
    {
        fprintf(stderr, "out of memory (%zu bytes)\n", n);
        exit(1);
    }
    return p;
}

/* Compressible but not trivial: words from a small vocabulary plus a counter, like log text. */
static void fill(char *buf, size_t n, unsigned seed)
{
    static const char *words[] = {"order", "status", "confirmed", "warehouse", "helsinki", "customer",
                                  "delivery", "invoice", "2024-05-17T09:31:44Z", "quantity", "price", "note"};
    size_t pos = 0;
    unsigned x = seed;
    while (pos < n)
    {
        x = x * 1103515245u + 12345u;
        pos += snprintf(buf + pos, n - pos, "%s=%u ", words[(x >> 16) % N(words)], x >> 8);
    }
}

static const char *mb(size_t bytes)
{
    static char out[8][32];
    static int i;
    char *s = out[i++ % 8];
    if (bytes >= 10 * 1048576) snprintf(s, 32, "%zu MB", (bytes + 524288) / 1048576);
    else if (bytes >= 1048576) snprintf(s, 32, "%.1f MB", bytes / 1048576.0);
    else snprintf(s, 32, "%zu kB", (bytes + 512) / 1024);
    return s;
}

/* A compression context after its first call with the given level, pledged size and
 * optional dictionary object: the workspace is sized then. */
static size_t cctx_bytes(int level, size_t pledged, const char *src, ZSTD_CDict *cd, char *dst, size_t cap)
{
    ZSTD_CCtx *cc = ZSTD_createCCtx();
    ZSTD_inBuffer in = {src, pledged < CHUNK ? pledged : CHUNK, 0};
    ZSTD_outBuffer out = {dst, cap, 0};
    size_t size;
    check(ZSTD_CCtx_setParameter(cc, ZSTD_c_compressionLevel, level));
    check(ZSTD_CCtx_setParameter(cc, ZSTD_c_checksumFlag, 1));
    check(ZSTD_CCtx_setPledgedSrcSize(cc, pledged));
    if (cd) check(ZSTD_CCtx_refCDict(cc, cd));
    check(ZSTD_compressStream2(cc, &out, &in, in.size == pledged ? ZSTD_e_end : ZSTD_e_continue));
    size = ZSTD_sizeof_CCtx(cc);
    ZSTD_freeCCtx(cc);
    return size;
}

/* A complete frame over `n` bytes with the window a given level would use for that input,
 * written at level 1: the decoder's memory follows the frame header. */
static size_t make_frame(int level, const char *src, size_t n, char *dst, size_t cap)
{
    ZSTD_CCtx *cc = ZSTD_createCCtx();
    ZSTD_compressionParameters params = ZSTD_getCParams(level, n, 0);
    ZSTD_inBuffer in = {src, n, 0};
    ZSTD_outBuffer out = {dst, cap, 0};
    check(ZSTD_CCtx_setParameter(cc, ZSTD_c_compressionLevel, 1));
    check(ZSTD_CCtx_setParameter(cc, ZSTD_c_windowLog, params.windowLog));
    check(ZSTD_CCtx_setParameter(cc, ZSTD_c_checksumFlag, 1));
    check(ZSTD_CCtx_setPledgedSrcSize(cc, n));
    while (ZSTD_compressStream2(cc, &out, &in, ZSTD_e_end) != 0)
        ;
    ZSTD_freeCCtx(cc);
    return out.pos;
}

/* A decompression context after its first call on the frame: stable output (the whole
 * result buffer is the window) or streaming into a bounded output buffer. */
static size_t dctx_bytes(const char *frame, size_t n, char *dst, size_t cap, int stable)
{
    ZSTD_DCtx *dc = ZSTD_createDCtx();
    ZSTD_inBuffer in = {frame, n, 0};
    ZSTD_outBuffer out = {dst, stable ? cap : CHUNK, 0};
    size_t size;
    if (stable) check(ZSTD_DCtx_setParameter(dc, ZSTD_d_stableOutBuffer, 1));
    check(ZSTD_decompressStream(dc, &out, &in));
    size = ZSTD_sizeof_DCtx(dc);
    ZSTD_freeDCtx(dc);
    return size;
}

int main(int argc, char **argv)
{
    /* "raw": one "kind level input bytes" line per measurement for tests/bench_memory.py to
     * compare against a backend; otherwise the Markdown tables. */
    int raw = argc > 1 && strcmp(argv[1], "raw") == 0;
    size_t max_in = inputs[N(inputs) - 1];
    char *src = xmalloc(max_in), *dst = xmalloc(ZSTD_compressBound(max_in)), *back = xmalloc(max_in);
    char *dict = xmalloc(dicts[N(dicts) - 1]);
    size_t i, j;

    fill(src, max_in, 7);
    fill(dict, dicts[N(dicts) - 1], 11);
    if (!raw) printf("libzstd %s\n\n", ZSTD_versionString());

    if (!raw)
    {
        printf("Compression context, by level and pledged input size (no dictionary):\n\n| level |");
        for (j = 0; j < N(inputs); j++) printf(" %s |", mb(inputs[j]));
        printf("\n|---:|");
        for (j = 0; j < N(inputs); j++) printf("---:|");
        printf("\n");
    }
    for (i = 0; i < N(levels); i++)
    {
        if (!raw) printf("| %d |", levels[i]);
        for (j = 0; j < N(inputs); j++)
        {
            size_t b = cctx_bytes(levels[i], inputs[j], src, NULL, dst, ZSTD_compressBound(max_in));
            if (raw) printf("cctx %d %zu %zu\n", levels[i], inputs[j], b); else printf(" %s |", mb(b));
        }
        if (!raw) printf("\n");
    }

    if (!raw)
    {
        printf("\nCompression context with a 110 kB dictionary referenced, by level and pledged input size:\n\n| level |");
        for (j = 0; j < N(inputs); j++) printf(" %s |", mb(inputs[j]));
        printf("\n|---:|");
        for (j = 0; j < N(inputs); j++) printf("---:|");
        printf("\n");
    }
    for (i = 0; i < N(levels); i++)
    {
        ZSTD_CDict *cd = ZSTD_createCDict(dict, dicts[1], levels[i]);
        if (!raw) printf("| %d |", levels[i]);
        for (j = 0; j < N(inputs); j++)
        {
            size_t b = cctx_bytes(levels[i], inputs[j], src, cd, dst, ZSTD_compressBound(max_in));
            if (raw) printf("cctx_dict %d %zu %zu\n", levels[i], inputs[j], b); else printf(" %s |", mb(b));
        }
        if (!raw) printf("\n");
        ZSTD_freeCDict(cd);
    }

    if (!raw)
    {
        printf("\nDictionary objects (cached per backend, counted against ztype.dictionary_cache_size):\n\n"
               "| dictionary | decompression object |");
        for (i = 0; i < N(levels); i++) printf(" compression object, level %d |", levels[i]);
        printf("\n|---|---:|");
        for (i = 0; i < N(levels); i++) printf("---:|");
        printf("\n");
    }
    for (j = 0; j < N(dicts); j++)
    {
        ZSTD_DDict *dd = ZSTD_createDDict(dict, dicts[j]);
        if (raw) printf("ddict 0 %zu %zu\n", dicts[j], ZSTD_sizeof_DDict(dd));
        else printf("| %s | %s |", mb(dicts[j]), mb(ZSTD_sizeof_DDict(dd)));
        ZSTD_freeDDict(dd);
        for (i = 0; i < N(levels); i++)
        {
            ZSTD_CDict *cd = ZSTD_createCDict(dict, dicts[j], levels[i]);
            if (raw) printf("cdict %d %zu %zu\n", levels[i], dicts[j], ZSTD_sizeof_CDict(cd));
            else printf(" %s |", mb(ZSTD_sizeof_CDict(cd)));
            ZSTD_freeCDict(cd);
        }
        if (!raw) printf("\n");
    }

    if (!raw)
    {
        printf("\nDecompression context, by the level that wrote the frame and its content size; "
               "full decode (stable output buffer) / `prefix` (streaming):\n\n| level |");
        for (j = 0; j < N(inputs); j++) printf(" %s |", mb(inputs[j]));
        printf("\n|---:|");
        for (j = 0; j < N(inputs); j++) printf("---:|");
        printf("\n");
    }
    for (i = 0; i < N(levels); i++)
    {
        if (!raw) printf("| %d |", levels[i]);
        for (j = 0; j < N(inputs); j++)
        {
            size_t n = make_frame(levels[i], src, inputs[j], dst, ZSTD_compressBound(max_in));
            size_t full = dctx_bytes(dst, n, back, inputs[j], 1);
            size_t stream = dctx_bytes(dst, n, back, inputs[j], 0);
            if (raw) printf("dctx_full %d %zu %zu\ndctx_stream %d %zu %zu\n", levels[i], inputs[j], full, levels[i], inputs[j], stream);
            else printf(" %s / %s |", mb(full), mb(stream));
        }
        if (!raw) printf("\n");
    }
    free(src);
    free(dst);
    free(back);
    free(dict);
    return 0;
}
