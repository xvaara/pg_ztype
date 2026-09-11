/* Independent client decoder: frame, optional dictionary, expected bytes in files. */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <zstd.h>

static void *read_file(const char *path, size_t *size)
{
    FILE *f = fopen(path, "rb");
    long n;
    void *p;
    if (!f || fseek(f, 0, SEEK_END) || (n = ftell(f)) < 0) exit(2);
    rewind(f);
    p = malloc((size_t)n + 1);
    if (!p || fread(p, 1, (size_t)n, f) != (size_t)n) exit(2);
    fclose(f);
    *size = (size_t)n;
    return p;
}

int main(int argc, char **argv)
{
    size_t fn, dn, en, n;
    void *frame, *dict, *expected, *out;
    ZSTD_DCtx *ctx;
    if (argc != 4) return 2;
    frame = read_file(argv[1], &fn);
    dict = read_file(argv[2], &dn);
    expected = read_file(argv[3], &en);
    out = malloc(en + 1);
    ctx = ZSTD_createDCtx();
    if (!out || !ctx) return 2;
    n = ZSTD_decompress_usingDict(ctx, out, en, frame, fn, dict, dn);
    if (ZSTD_isError(n) || n != en || memcmp(out, expected, en)) {
        fprintf(stderr, "frame round trip failed: %s\n", ZSTD_getErrorName(n));
        return 1;
    }
    printf("%u\n", ZSTD_getDictID_fromFrame(frame, fn));
    ZSTD_freeDCtx(ctx);
    free(frame); free(dict); free(expected); free(out);
    return 0;
}
