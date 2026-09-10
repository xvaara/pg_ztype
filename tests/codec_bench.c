/* Standalone libzstd microbenchmark behind the README's per-value codec cost claims: how
 * much of encoding or decoding a 0.6 kB document is context creation versus the codec
 * itself, for a fresh context per call (what ztype did before 2026-09-08 for decoding and
 * still does for compression unless the numbers say otherwise) against one reused context
 * reset per call. Compression mirrors zt_compress: checksum on, pledged source size,
 * compressStream2 with ZSTD_e_end. No PostgreSQL involved. `make bench-codec`.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <zstd.h>

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static void check(size_t rc)
{
    if (ZSTD_isError(rc))
    {
        printf("zstd: %s\n", ZSTD_getErrorName(rc));
        exit(1);
    }
}

static size_t decode(ZSTD_DCtx *c, const char *src, size_t n, char *dst, size_t cap)
{
    ZSTD_inBuffer in = {src, n, 0};
    ZSTD_outBuffer out = {dst, cap, 0};
    size_t rc = 1;
    while (rc != 0)
    {
        rc = ZSTD_decompressStream(c, &out, &in);
        check(rc);
    }
    return out.pos;
}

static size_t encode(ZSTD_CCtx *c, int level, const char *src, size_t n, char *dst, size_t cap)
{
    ZSTD_inBuffer in = {src, n, 0};
    ZSTD_outBuffer out = {dst, cap, 0};
    check(ZSTD_CCtx_setParameter(c, ZSTD_c_compressionLevel, level));
    check(ZSTD_CCtx_setParameter(c, ZSTD_c_checksumFlag, 1));
    check(ZSTD_CCtx_setPledgedSrcSize(c, n));
    for (;;)
    {
        size_t rc = ZSTD_compressStream2(c, &out, &in, ZSTD_e_end);
        check(rc);
        if (rc == 0) return out.pos;
    }
}

static const int levels[] = {1, 3, 6, 9, 19};

int main(void)
{
    char raw[4096];
    int n = 0;
    n += sprintf(raw + n, "{\"id\":\"3b1f2a9c8d7e6f5a4b3c2d1e0f9a8b7c\",\"status\":\"confirmed\",\"warehouse\":\"helsinki-01\",\"unit\":\"pcs\",");
    n += sprintf(raw + n, "\"created\":\"2024-05-17T09:31:44.120Z\",\"updated\":\"2024-05-18T14:02:11.004Z\",\"quantity\":12,\"price\":143.25,");
    n += sprintf(raw + n, "\"product\":\"Industrial ethernet cable Cat6A 5 m\",\"customer\":\"Pohjolan Rakennus Oy\",\"address\":\"Teollisuuskatu 14, Helsinki\",");
    n += sprintf(raw + n, "\"note\":\"Customer requested delivery before noon. Please leave the package at the loading dock.\",\"tags\":[\"priority\",\"contract\"],");
    n += sprintf(raw + n, "\"history\":[{\"at\":\"2024-05-17T09:31:44.120Z\",\"by\":\"maria.koskinen\",\"action\":\"created\"},{\"at\":\"2024-05-18T14:02:11.004Z\",\"by\":\"system.import\",\"action\":\"confirmed\"}]}");
    char comp[8192], dst[8192];
    const int N = 300000;
    double t0, t1;
    ZSTD_CCtx *cc;
    ZSTD_DCtx *dc;
    size_t cn;

    cc = ZSTD_createCCtx();
    cn = encode(cc, 6, raw, n, comp, sizeof comp);
    ZSTD_freeCCtx(cc);
    printf("document: %d bytes raw, %zu bytes at level 6; %d iterations per line\n\n", n, cn, N);

    printf("decompression\n");
    t0 = now();
    for (int i = 0; i < N; i++) { dc = ZSTD_createDCtx(); ZSTD_freeDCtx(dc); }
    t1 = now();
    printf("  createDCtx + freeDCtx:            %7.3f us\n", (t1 - t0) / N * 1e6);
    t0 = now();
    for (int i = 0; i < N; i++) { dc = ZSTD_createDCtx(); decode(dc, comp, cn, dst, sizeof dst); ZSTD_freeDCtx(dc); }
    t1 = now();
    printf("  fresh context per decode:         %7.3f us\n", (t1 - t0) / N * 1e6);
    dc = ZSTD_createDCtx();
    t0 = now();
    for (int i = 0; i < N; i++) { check(ZSTD_DCtx_reset(dc, ZSTD_reset_session_and_parameters)); decode(dc, comp, cn, dst, sizeof dst); }
    t1 = now();
    printf("  reused context, reset per decode: %7.3f us   (sizeof DCtx %zu)\n", (t1 - t0) / N * 1e6, ZSTD_sizeof_DCtx(dc));
    ZSTD_freeDCtx(dc);

    printf("\ncompression                        fresh ctx   reused ctx   sizeof CCtx\n");
    for (size_t l = 0; l < sizeof levels / sizeof levels[0]; l++)
    {
        int level = levels[l];
        int iters = level >= 19 ? N / 10 : N;
        double fresh, reused;
        t0 = now();
        for (int i = 0; i < iters; i++) { cc = ZSTD_createCCtx(); encode(cc, level, raw, n, comp, sizeof comp); ZSTD_freeCCtx(cc); }
        t1 = now();
        fresh = (t1 - t0) / iters * 1e6;
        cc = ZSTD_createCCtx();
        t0 = now();
        for (int i = 0; i < iters; i++) { check(ZSTD_CCtx_reset(cc, ZSTD_reset_session_only)); encode(cc, level, raw, n, comp, sizeof comp); }
        t1 = now();
        reused = (t1 - t0) / iters * 1e6;
        printf("  level %2d:                        %7.3f us  %7.3f us   %8zu\n", level, fresh, reused, ZSTD_sizeof_CCtx(cc));
        ZSTD_freeCCtx(cc);
    }
    t0 = now();
    for (int i = 0; i < N; i++) { cc = ZSTD_createCCtx(); ZSTD_freeCCtx(cc); }
    t1 = now();
    printf("  createCCtx + freeCCtx (no work):  %7.3f us\n", (t1 - t0) / N * 1e6);
    return 0;
}
