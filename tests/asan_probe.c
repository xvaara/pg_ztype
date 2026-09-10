/* Positive control for sanitizer runs (tests/test_ztype.py, sanitizer_self_check): a
 * deliberate heap overflow that AddressSanitizer must report. If it does not, the runtime
 * never reached the server and the whole run would prove nothing.
 */
#include "postgres.h"
#include "fmgr.h"
#include <stdlib.h>

PG_MODULE_MAGIC;

PG_FUNCTION_INFO_V1(asan_probe);
Datum
asan_probe(PG_FUNCTION_ARGS)
{
    volatile char *p = malloc(8);
    p[8] = 1; /* one past the end */
    free((void *) p);
    PG_RETURN_VOID();
}
