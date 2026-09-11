/* ztype: versioned zstd containers for text, native jsonb and bytea. Compressed
 * payloads carry zstd content checksums; the envelope itself is checked structurally.
 */
#include "postgres.h"
#include "fmgr.h"
#include "varatt.h"
#include "access/detoast.h"
#include "access/genam.h"
#include "access/table.h"
#include "access/xact.h"
#include "catalog/indexing.h"
#include "catalog/objectaccess.h"
#include "catalog/objectaddress.h"
#include "catalog/pg_attrdef_d.h"
#include "catalog/pg_class.h"
#include "catalog/pg_constraint.h"
#include "catalog/pg_depend.h"
#include "catalog/pg_index.h"
#include "catalog/pg_inherits.h"
#include "catalog/pg_publication_rel_d.h"
#include "catalog/pg_rewrite_d.h"
#include "catalog/pg_statistic_ext_d.h"
#include "catalog/pg_trigger.h"
#include "catalog/pg_type.h"
#include "catalog/pg_collation_d.h"
#include "catalog/pg_statistic.h"
#include "commands/extension.h"
#include "commands/vacuum.h"
#include "commands/tablecmds.h"
#include "executor/spi.h"
#include "nodes/nodeFuncs.h"
#include "utils/acl.h"
#include "utils/fmgroids.h"
#include "utils/inval.h"
#include "utils/lsyscache.h"
#include "storage/lmgr.h"
#include "utils/rel.h"
#include "utils/syscache.h"
#include "utils/typcache.h"
#include "mb/pg_wchar.h"
#include "miscadmin.h"
#include "nodes/miscnodes.h"
#include "access/htup_details.h"
#include "common/hashfn.h"
#include "funcapi.h"
#include "libpq/pqformat.h"
#include "utils/array.h"
#include "utils/builtins.h"
#include "utils/jsonb.h"
#include "utils/guc.h"
#include "utils/memutils.h"
#include "utils/snapmgr.h"
/* ZSTD_d_stableOutBuffer lives in zstd's experimental section: zt_decompress writes
 * frames straight into the datum with it. Setting it is an ordinary parameter call,
 * so nothing here depends on unstable structures, but the name needs the define.
 */
#define ZSTD_STATIC_LINKING_ONLY
#include <zstd.h>
#include <zdict.h>
#if ZSTD_VERSION_NUMBER < 10500
#error "ztype requires libzstd 1.5.0 or newer (stable output buffers in streaming decompression)"
#endif

PG_MODULE_MAGIC;

#define ZT_VERSION "0.9" /* what this library reports through ztype.build_info(); the SQL script carries the same */
#define ZT_MAGIC 0x5a540003U /* bumped when the envelope layout changes */
/* ztype-owned JSONB payload format: 1 = PostgreSQL's native JsonbContainer layout.
 * Bump it if that layout ever changes. Readers compare stored values against this
 * constant, never against the running PostgreSQL major.
 */
#define ZT_JSONB_FORMAT 1
#define ZT_LEVEL 6
#define ZT_MAX_LEVEL 22
#define ZT_MAX_SLOT 33554431 /* typmod carries 25 slot bits above the 5 level bits */
#define ZT_TM_PENDING (1 << 30) /* typmod bit above the slot: stored rows may still carry an older policy */
#define ZT_MIN_COMPRESS 64 /* shorter values are always stored raw */
#define ZT_CHUNK (256 * 1024) /* codec input per interrupt check */
#define ZT_DECODE_STEP (1024 * 1024) /* decoded bytes per interrupt check, at the frame's average ratio */
#define ZT_MAX_DICT (1024 * 1024)
#define ZT_MAX_SAMPLES 20000
#define ZT_MAX_SAMPLE_BYTES 65536
#define ZT_TRAIN_BUDGET (64 * 1024 * 1024)
#define ZT_BATCH 128
#define ZT_TEXT 1
#define ZT_JSONB 2
#define ZT_BYTEA 3
#define ZT_RAW 0
#define ZT_ZSTD 1

/* Native-endian storage, like jsonb itself; format versions the JSON payload layout
 * (zero for text and bytea) and level records the compression level the value was
 * written with, so a same-type coercion can tell whether the policy already applies.
 * The dictionary is identified by the zstd dictionary ID inside the frame, never by
 * slot. No envelope checksum: compressed payloads are covered by the zstd content
 * checksum, raw payloads only by exact-length checks.
 */
typedef struct ZtValue
{
    int32 vl_len_;
    uint32 magic;
    uint32 rawlen;
    uint8 format;
    uint8 level;
    uint8 kind;
    uint8 codec;
    char data[FLEXIBLE_ARRAY_MEMBER];
} ZtValue;
#define ZT_HDR offsetof(ZtValue, data)

/* The cache lives for the backend; only the last requested level is compiled.
 * The list is kept in most-recently-used order and trimmed from the tail to
 * the ztype.dictionary_cache_size budget. `size` is what the entry costs:
 * the dictionary datum plus the zstd objects, which malloc outside
 * PostgreSQL's memory accounting.
 *
 * An entry is loaded under some snapshot of the transaction that first needed
 * it, so the registry row was committed before that snapshot, unless this very
 * transaction wrote it. Every later snapshot of this backend therefore sees the
 * row too, and registered dictionaries never change: once the loading
 * transaction commits the entry is good for the rest of the session. Until then
 * it is provisional: `gen` is the backend's transaction counter and `subxid` the
 * subtransaction that loaded it, and an abort of either drops it, so a rolled
 * back registration cannot leave a dictionary behind that a later write would
 * reference by ID. PREPARE TRANSACTION drops the transaction's entries as well,
 * since its outcome is not known.
 */
typedef struct ZtDict
{
    int slot;
    unsigned id;
    int level;
    uint64 gen;
    SubTransactionId subxid;
    ZSTD_CDict *cd;
    ZSTD_DDict *dd;
    bytea *bytes;
    Size size;
    struct ZtDict *next;
} ZtDict;

/* A miss on a logical write is remembered for as long as the lookup could not
 * give a different answer: a snapshot with the same xmin, xmax and in-progress
 * set sees the same committed rows, and the same command id means we did not
 * register anything ourselves. It is warned about once per slot per
 * transaction. Misses live one transaction, in their own context, never in the
 * session cache: a slot that was not visible may be in the next transaction.
 */
typedef struct ZtSnap
{
    TransactionId xmin;
    TransactionId xmax;
    uint32 xcnt;
    uint32 xiphash;
    CommandId cid;
} ZtSnap;

typedef struct ZtMiss
{
    int slot;
    ZtSnap seen;
    struct ZtMiss *next;
} ZtMiss;

static ZtSnap
zt_snapshot_key(void)
{
    Snapshot snap = GetActiveSnapshot();
    ZtSnap key;
    key.xmin = snap->xmin;
    key.xmax = snap->xmax;
    key.xcnt = snap->xcnt;
    key.xiphash = snap->xcnt ? hash_bytes((const unsigned char *) snap->xip, snap->xcnt * sizeof(TransactionId)) : 0;
    key.cid = GetCurrentCommandId(false);
    return key;
}

static MemoryContext dict_context = NULL; /* child of TopMemoryContext, created on first load */
static ZtDict *dict_cache = NULL;
static uint64 zt_gen = 0; /* counts this backend's transactions; entries of a finished one are promoted or dropped */
static MemoryContext miss_context = NULL; /* child of TopTransactionContext */
static ZtMiss *miss_cache = NULL;

/* Budget (GUC, kB) and accounting for the cache. Entries and bytes describe the
 * live cache; loads and evictions count for the backend's lifetime so a test or
 * an operator can read the difference over an interval.
 */
static int zt_cache_kb = 64 * 1024;
static int cache_entries = 0;
static Size cache_bytes = 0;
static int64 cache_loads = 0;
static int64 cache_evictions = 0;

/* PostgreSQL frees the miss context with the transaction; forget the list with it. */
static void
zt_miss_cleanup(void *arg)
{
    miss_cache = NULL;
    miss_context = NULL;
}

/* Unlink and free one entry, zstd objects included, keeping the accounting exact. */
static void
zt_cache_free(ZtDict *victim, ZtDict *victim_prev)
{
    if (victim_prev)
        victim_prev->next = victim->next;
    else
        dict_cache = victim->next;
    cache_bytes -= victim->size;
    cache_entries--;
    ZSTD_freeCDict(victim->cd);
    ZSTD_freeDDict(victim->dd);
    pfree(victim->bytes);
    pfree(victim);
}

/* Drop the provisional entries of the current transaction loaded in subtransaction
 * `subxid` or one of its descendants (InvalidSubTransactionId: all of them).
 * Subtransaction IDs grow within a transaction and one that starts while another
 * is open is its descendant, so "at least subxid" is exactly that set. Called from
 * transaction callbacks, so it must not error.
 */
static void
zt_cache_drop_since(SubTransactionId subxid)
{
    ZtDict *prev = NULL, *d = dict_cache;
    while (d)
    {
        ZtDict *next = d->next;
        if (d->gen == zt_gen && d->subxid >= subxid)
            zt_cache_free(d, prev);
        else
            prev = d;
        d = next;
    }
}

/* Drop least recently used entries other than `keep` until `need` more bytes fit
 * the budget. Stops when only `keep` is left, so the dictionary in use always
 * fits and a budget smaller than one entry costs reloads, never an error.
 */
static void
zt_cache_reserve(ZtDict *keep, Size need)
{
    Size budget = (Size) zt_cache_kb * 1024;
    while (cache_bytes + need > budget)
    {
        ZtDict *prev = NULL, *victim = NULL, *victim_prev = NULL, *d;
        for (d = dict_cache; d; prev = d, d = d->next)
            if (d != keep)
            {
                victim = d;
                victim_prev = prev;
            }
        if (!victim)
            break;
        cache_evictions++;
        zt_cache_free(victim, victim_prev);
    }
}

/* An aborted savepoint must not leave a dictionary available to later writes: its
 * provisional entries go, and so do the misses, whose snapshot keys it may have set.
 */
static void
zt_subxact(SubXactEvent event, SubTransactionId subid,
           SubTransactionId parent, void *arg)
{
    if (event != SUBXACT_EVENT_ABORT_SUB)
        return;
    zt_cache_drop_since(subid);
    if (miss_context)
        MemoryContextDelete(miss_context);
}

/* Commit promotes the transaction's entries to the session; abort and PREPARE drop
 * them. Either way the next transaction is a new generation. The miss context is
 * TopTransactionContext's child and PostgreSQL frees it after this callback.
 */
static void
zt_xact(XactEvent event, void *arg)
{
    switch (event)
    {
        case XACT_EVENT_ABORT:
        case XACT_EVENT_PARALLEL_ABORT:
        case XACT_EVENT_PREPARE:
            zt_cache_drop_since(InvalidSubTransactionId);
            /* fall through */
        case XACT_EVENT_COMMIT:
        case XACT_EVENT_PARALLEL_COMMIT:
            zt_gen++;
            break;
        default:
            break;
    }
}

void _PG_init(void);
void
_PG_init(void)
{
    DefineCustomIntVariable("ztype.dictionary_cache_size",
                            "Budget for dictionaries cached by one backend.",
                            "Counts dictionary bytes plus their zstd compression and decompression "
                            "objects. Least recently used dictionaries are dropped to stay within "
                            "it; the dictionary in use always fits.",
                            &zt_cache_kb, 64 * 1024, 0, MAX_KILOBYTES,
                            PGC_USERSET, GUC_UNIT_KB, NULL, NULL, NULL);
    MarkGUCPrefixReserved("ztype"); /* reject typos like SET ztype.cache_size */
    RegisterSubXactCallback(zt_subxact, NULL);
    RegisterXactCallback(zt_xact, NULL);
}

/* Report codec failures consistently, including allocation and malformed frames.
 * With an ErrorSaveContext (only ztype.validate passes one) the failure is saved and
 * false is returned instead of raising, so an integrity sweep needs no longjmp; every
 * other caller passes NULL through zt_check and raises exactly as before.
 */
static bool
zt_check_soft(size_t result, Node *escontext)
{
    if (ZSTD_isError(result))
    {
        errsave(escontext, (errcode(ERRCODE_DATA_CORRUPTED),
                            errmsg("ztype: %s", ZSTD_getErrorName(result))));
        return false;
    }
    return true;
}

static void
zt_check(size_t result)
{
    (void) zt_check_soft(result, NULL);
}

/* Session-cached decompression context. Creating a ZSTD_DCtx costs about as much as
 * decoding a small value, so one context lives for the backend's lifetime and is reset
 * per use. The reset (session and parameters) also drops the previous dictionary
 * reference, which may point at an entry the transaction-local cache has since freed:
 * zstd never dereferences it during the reset. An error inside the codec leaves the
 * context mid-stream; the next acquire resets it, and the release after a call that
 * grew its streaming buffers past ZT_DCTX_KEEP frees it so a backend does not keep a
 * large window alive. Full decodes write into the datum directly and only ever grow
 * the input buffer to one block; a prefix of a large value is what can grow it. A
 * context still in use (only possible from a nested call while decoding, which
 * nothing does today) gets a fresh temporary one instead.
 */
#define ZT_DCTX_KEEP (1024 * 1024)
static ZSTD_DCtx *zt_dctx;
static bool zt_dctx_busy;

static ZSTD_DCtx *
zt_dctx_acquire(void)
{
    ZSTD_DCtx *ctx;
    if (zt_dctx_busy)
    {
        ctx = ZSTD_createDCtx();
        if (!ctx) ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("ztype: could not allocate decompressor")));
        return ctx;
    }
    if (zt_dctx && ZSTD_isError(ZSTD_DCtx_reset(zt_dctx, ZSTD_reset_session_and_parameters)))
    {
        ZSTD_freeDCtx(zt_dctx); /* cannot happen after a session reset; start over if it does */
        zt_dctx = NULL;
    }
    if (!zt_dctx)
    {
        zt_dctx = ZSTD_createDCtx();
        if (!zt_dctx) ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("ztype: could not allocate decompressor")));
    }
    zt_dctx_busy = true;
    return zt_dctx;
}

/* Safe from PG_FINALLY: never errors, and never dereferences a dictionary. */
static void
zt_dctx_release(ZSTD_DCtx *ctx)
{
    if (ctx != zt_dctx)
    {
        ZSTD_freeDCtx(ctx);
        return;
    }
    zt_dctx_busy = false;
    if (ZSTD_sizeof_DCtx(ctx) > ZT_DCTX_KEEP)
    {
        ZSTD_freeDCtx(ctx);
        zt_dctx = NULL;
    }
}

/* Only trained, ID-bearing dictionaries are supported, never raw-content ones. */
static unsigned
zt_dictionary_id(bytea *bytes)
{
    Size len = VARSIZE_ANY_EXHDR(bytes);
    unsigned id;
    ZSTD_DDict *dd;
    if (len < 8 || len > ZT_MAX_DICT)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: dictionary must contain 8 to %d bytes", ZT_MAX_DICT)));
    id = ZSTD_getDictID_fromDict(VARDATA_ANY(bytes), len);
    if (id == 0)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: a trained dictionary with a nonzero ID is required")));
    dd = ZSTD_createDDict(VARDATA_ANY(bytes), len);
    if (!dd)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: invalid dictionary or insufficient memory")));
    ZSTD_freeDDict(dd);
    return id;
}

/* Saved plans for the fixed registry lookups. Each is prepared once per backend, as a
 * generic plan (a primary-key or unique-index probe, so parameter values cannot change
 * it), kept across SPI_finish and reused by every later lookup; without this every
 * dictionary load parsed and planned its query again. The plan cache owns invalidation:
 * a change to the registry (DROP EXTENSION, ALTER TABLE) marks the plan stale and the
 * next execution replans it, or raises the ordinary error if the table is gone.
 * Privileges are checked at each execution against the current user, as for any saved
 * plan, so a plan prepared under one role grants nothing to another. Must be called
 * between SPI_connect and SPI_finish.
 */
static SPIPlanPtr
zt_plan(SPIPlanPtr *saved, const char *sql, Oid argtype)
{
    if (!*saved)
    {
        SPIPlanPtr plan = SPI_prepare_cursor(sql, 1, &argtype, CURSOR_OPT_GENERIC_PLAN);
        if (!plan)
            elog(ERROR, "ztype: could not prepare registry lookup: %s", SPI_result_code_string(SPI_result));
        if (SPI_keepplan(plan) != 0)
            elog(ERROR, "ztype: could not save registry lookup plan");
        *saved = plan;
    }
    return *saved;
}
static SPIPlanPtr zt_plan_by_slot, zt_plan_by_id, zt_plan_by_name, zt_plan_inspect;

/* Look up one immutable dictionary under the caller's snapshot. SQL codec entry
 * points run as the extension owner, with a fixed search_path. A temporary
 * snapshot also supports type input during parsing/utility commands.
 * missing_ok is for logical writes only (slot lookups): a slot that is not
 * visible yet, typically during a restore, yields NULL and a warning instead of
 * an error. Decoding always passes false: a frame needs its dictionary.
 * escontext is non-NULL only under ztype.validate: the strict failures are saved
 * rather than raised and NULL is returned, so the caller must ask
 * SOFT_ERROR_OCCURRED to tell "no dictionary" from "dictionary missing".
 */
static ZtDict *
zt_dictionary(int slot, unsigned id, bool missing_ok, Node *escontext)
{
    ZtDict *d;
    Oid argtype = INT8OID;
    Datum arg = Int64GetDatum(slot ? slot : (int64) id);
    bool pushed = !ActiveSnapshotSet();
    bool isnull;
    Datum value;
    bytea *bytes;
    MemoryContext old;
    int actual_slot;
    unsigned actual_id;

    ZtMiss *miss = NULL;
    ZtSnap key;
    ZtDict *prev = NULL;
    ZSTD_DDict *dd;

    Assert(!missing_ok || slot);
    for (d = dict_cache; d; prev = d, d = d->next)
        if (slot ? d->slot == slot : d->id == id)
        {
            if (prev) /* most recently used first */
            {
                prev->next = d->next;
                d->next = dict_cache;
                dict_cache = d;
            }
            zt_cache_reserve(d, 0); /* a lowered budget trims on hits too */
            return d;
        }
    if (pushed)
        PushActiveSnapshot(GetTransactionSnapshot());
    if (missing_ok)
    {
        key = zt_snapshot_key();
        for (miss = miss_cache; miss; miss = miss->next)
            if (miss->slot == slot)
            {
                if (memcmp(&miss->seen, &key, sizeof(key)) == 0)
                {
                    if (pushed) PopActiveSnapshot();
                    return NULL;
                }
                break;
            }
    }

    if (!dict_context)
        dict_context = AllocSetContextCreate(TopMemoryContext,
                                             "ztype dictionaries", ALLOCSET_SMALL_SIZES);
    if (missing_ok && !miss_context)
    {
        MemoryContextCallback *cb;
        miss_context = AllocSetContextCreate(TopTransactionContext,
                                             "ztype dictionary misses", ALLOCSET_SMALL_SIZES);
        cb = MemoryContextAlloc(miss_context, sizeof(*cb));
        cb->func = zt_miss_cleanup;
        cb->arg = NULL;
        MemoryContextRegisterResetCallback(miss_context, cb);
    }
    if (SPI_connect() != SPI_OK_CONNECT)
        elog(ERROR, "ztype: SPI_connect failed");
    if (SPI_execute_plan(slot
            ? zt_plan(&zt_plan_by_slot, "SELECT slot, dict FROM ztype.dictionaries WHERE slot = $1", argtype)
            : zt_plan(&zt_plan_by_id, "SELECT slot, dict FROM ztype.dictionaries WHERE dict_id = $1", argtype),
            &arg, NULL, true, 1) != SPI_OK_SELECT)
        elog(ERROR, "ztype: dictionary lookup failed");
    if (SPI_processed != 1 && missing_ok)
    {
        SPI_finish();
        if (pushed) PopActiveSnapshot();
        if (!miss)
        {
            miss = MemoryContextAllocZero(miss_context, sizeof(*miss));
            miss->slot = slot;
            miss->next = miss_cache;
            miss_cache = miss;
            ereport(WARNING, (errcode(ERRCODE_UNDEFINED_OBJECT),
                              errmsg("ztype: dictionary slot %d is not available; writing without a dictionary", slot),
                              errhint("Register the dictionary, then rewrite the affected rows, for example UPDATE t SET col = col::text.")));
        }
        miss->seen = key;
        return NULL;
    }
    if (SPI_processed != 1)
    {
        /* SPI owns the current context, so release it before a soft error is saved. */
        SPI_finish();
        if (pushed) PopActiveSnapshot();
        ereturn(escontext, NULL, (errcode(ERRCODE_UNDEFINED_OBJECT),
                                  errmsg("ztype: dictionary %s %u is not available",
                                         slot ? "slot" : "ID", slot ? (unsigned) slot : id)));
    }
    value = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
    if (isnull) elog(ERROR, "ztype: null dictionary slot");
    actual_slot = DatumGetInt32(value);
    value = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2, &isnull);
    if (isnull) elog(ERROR, "ztype: null dictionary bytes");
    old = MemoryContextSwitchTo(dict_context);
    bytes = DatumGetByteaPCopy(value);
    MemoryContextSwitchTo(old);
    SPI_finish();
    if (pushed) PopActiveSnapshot();

    /* The registry CHECK validated the bytes at registration; the decompression
     * object is built once here and identifies the dictionary. Nothing native
     * is held while an error can still be raised, and the copy lives in a
     * session context, so a failure here frees it rather than leaving it behind.
     */
    dd = ZSTD_createDDict(VARDATA(bytes), VARSIZE(bytes) - VARHDRSZ);
    if (!dd)
    {
        pfree(bytes);
        ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("ztype: could not allocate dictionary")));
    }
    actual_id = ZSTD_getDictID_fromDDict(dd);
    if (actual_id == 0 || (id && actual_id != id) || actual_slot < 1 || actual_slot > ZT_MAX_SLOT)
    {
        ZSTD_freeDDict(dd);
        pfree(bytes);
        ereturn(escontext, NULL, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid dictionary metadata")));
    }
    d = MemoryContextAllocZero(dict_context, sizeof(*d));
    d->slot = actual_slot;
    d->id = actual_id;
    d->gen = zt_gen;
    d->subxid = GetCurrentSubTransactionId();
    d->bytes = bytes;
    d->dd = dd;
    d->size = sizeof(*d) + VARSIZE(bytes) + ZSTD_sizeof_DDict(dd);
    zt_cache_reserve(NULL, d->size);
    d->next = dict_cache;
    dict_cache = d;
    cache_entries++;
    cache_bytes += d->size;
    cache_loads++;
    return d;
}

/* One canonical typmod encoding: 5 level bits, 25 slot bits and the pending bit (bit 30); -1 means
 * (6,0). The pending bit is not part of the policy a write applies: it only records that
 * the column was re-pointed without a rewrite (ztype.set_column_policy) and rows written
 * before that may still carry the earlier policy. Every write and every coercion treats
 * a pending modifier exactly like the plain one.
 */
static void
zt_policy(int32 tm, int *level, int *slot)
{
    if (tm == -1) tm = ZT_LEVEL;
    *level = tm & 31;
    *slot = (tm >> 5) & ZT_MAX_SLOT;
    if (tm < 0 || *level < 1 || *level > ZT_MAX_LEVEL)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: invalid type modifier")));
}

/* Resolve a registered dictionary name to its slot. Runs at parse/DDL time under
 * the caller's snapshot, so a dictionary added earlier in the same transaction is
 * visible; the SQL wrapper is SECURITY DEFINER because the table is admin-only.
 */
static int
zt_slot_by_name(const char *name)
{
    Oid argtype = TEXTOID;
    Datum arg = CStringGetTextDatum(name);
    bool pushed = !ActiveSnapshotSet();
    bool isnull;
    int slot;
    if (pushed)
        PushActiveSnapshot(GetTransactionSnapshot());
    if (SPI_connect() != SPI_OK_CONNECT)
        elog(ERROR, "ztype: SPI_connect failed");
    if (SPI_execute_plan(zt_plan(&zt_plan_by_name, "SELECT slot FROM ztype.dictionaries WHERE name = $1", argtype),
                         &arg, NULL, true, 1) != SPI_OK_SELECT)
        elog(ERROR, "ztype: dictionary lookup failed");
    if (SPI_processed != 1)
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT),
                        errmsg("ztype: dictionary \"%s\" is not registered", name),
                        errhint("Register it first with ztype.train_and_add() or ztype.add_dictionary().")));
    slot = DatumGetInt32(SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull));
    SPI_finish();
    if (pushed) PopActiveSnapshot();
    if (isnull || slot < 1 || slot > ZT_MAX_SLOT)
        ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid dictionary metadata")));
    return slot;
}

/* Registered names are never all digits, so a digit-only modifier is always a number. */
static bool
zt_all_digits(const char *s)
{
    if (*s == '\0') return false;
    for (; *s; s++)
        if (*s < '0' || *s > '9') return false;
    return true;
}

/* Convert a digit-only modifier component. False, with nothing raised, when the number
 * does not fit in int32, so an unrepresentable modifier gets ztype's own range message
 * instead of PostgreSQL's "out of range" from the integer parser.
 */
static bool
zt_typmod_int(const char *s, int32 *out)
{
    ErrorSaveContext escontext = {T_ErrorSaveContext};
    *out = pg_strtoint32_safe(s, (Node *) &escontext);
    return !escontext.error_occurred;
}

/* Modifiers are (level [, dictionary [, pending]]); the dictionary is a slot number or a
 * registered name (quoted string or bare identifier). Names are resolved once, when the
 * DDL or cast is parsed, and the typmod stores only the slot. The literal word `pending`
 * as a third element is the state ztype.set_column_policy leaves a column in; it is
 * accepted here so that dumps, pg_upgrade and CREATE TABLE ... (LIKE ...) round-trip it.
 */
PG_FUNCTION_INFO_V1(ztext_typmod_in);
Datum
ztext_typmod_in(PG_FUNCTION_ARGS)
{
    ArrayType *arr = PG_GETARG_ARRAYTYPE_P(0);
    Datum *elems;
    int n;
    int32 level, slot = 0, pending = 0;
    if (ARR_ELEMTYPE(arr) != CSTRINGOID)
        ereport(ERROR, (errcode(ERRCODE_ARRAY_ELEMENT_ERROR), errmsg("ztype: typmod array must be type cstring[]")));
    deconstruct_array_builtin(arr, CSTRINGOID, &elems, NULL, &n);
    if (n < 1 || n > 3 || !zt_all_digits(DatumGetCString(elems[0])) ||
        !zt_typmod_int(DatumGetCString(elems[0]), &level) || level < 1 || level > ZT_MAX_LEVEL ||
        (n == 3 && strcmp(DatumGetCString(elems[2]), "pending") != 0))
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: expected (level 1..%d [, dictionary slot 0..%d or registered name [, pending]])", ZT_MAX_LEVEL, ZT_MAX_SLOT)));
    if (n >= 2)
    {
        const char *dict = DatumGetCString(elems[1]);
        if (zt_all_digits(dict))
        {
            if (!zt_typmod_int(dict, &slot) || slot < 0 || slot > ZT_MAX_SLOT)
                ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                                errmsg("ztype: dictionary slot must be 0..%d", ZT_MAX_SLOT)));
        }
        else
            slot = zt_slot_by_name(dict);
    }
    if (n == 3)
        pending = ZT_TM_PENDING;
    PG_RETURN_INT32(level | (slot << 5) | pending);
}

PG_FUNCTION_INFO_V1(ztext_typmod_out);
Datum
ztext_typmod_out(PG_FUNCTION_ARGS)
{
    int32 tm = PG_GETARG_INT32(0);
    int level, slot;
    if (tm == -1) PG_RETURN_CSTRING(pstrdup(""));
    zt_policy(tm, &level, &slot);
    if (tm & ZT_TM_PENDING)
        PG_RETURN_CSTRING(psprintf("(%d,%d,pending)", level, slot));
    PG_RETURN_CSTRING(slot ? psprintf("(%d,%d)", level, slot) : psprintf("(%d)", level));
}

/* Compress with a zstd content checksum, retaining raw bytes when compression saves
 * no space. Logical writes (strict = false) tolerate a slot that is not visible yet;
 * explicit recompression does not. frame_only is the output pass-through's mode: no
 * 64-byte floor and no raw fallback, the result is always one frame (the bound is
 * ZSTD_compressBound, which every input fits), and a value too large for that bound
 * is an error instead of a raw envelope.
 */
static ZtValue *
zt_compress_internal(const char *src, Size len, int32 tm, uint8 kind, bool strict, bool frame_only)
{
    int level, slot;
    size_t bound;
    size_t written = len;
    ZtDict *dict = NULL;
    ZSTD_CCtx *ctx;
    ZtValue *v;

    zt_policy(tm, &level, &slot);
    if (len > MaxAllocSize - ZT_HDR)
        ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED), errmsg("ztype: value is too large")));
    if (slot)
        dict = zt_dictionary(slot, 0, !strict, NULL); /* even for tiny values, so a bad slot surfaces early */
    bound = ZSTD_compressBound(len);
    if (ZSTD_isError(bound) || bound > MaxAllocSize - ZT_HDR)
    {
        if (frame_only)
            ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED), errmsg("ztype: frame is too large")));
        bound = len; /* large values can still use the raw representation */
    }
    v = palloc(ZT_HDR + bound);
    v->magic = ZT_MAGIC;
    v->rawlen = len;
    v->format = kind == ZT_JSONB ? ZT_JSONB_FORMAT : 0;
    v->level = level;
    v->kind = kind;
    v->codec = ZT_RAW;
    if (frame_only || (len >= ZT_MIN_COMPRESS && bound > len))
    {
        if (dict && (!dict->cd || dict->level != level))
        {
            ZSTD_CDict *cd = ZSTD_createCDict(VARDATA(dict->bytes),
                                             VARSIZE(dict->bytes) - VARHDRSZ, level);
            Size csize;
            if (!cd) ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("ztype: could not allocate compression dictionary")));
            if (dict->cd)
            {
                csize = ZSTD_sizeof_CDict(dict->cd);
                ZSTD_freeCDict(dict->cd);
                dict->size -= csize;
                cache_bytes -= csize;
            }
            dict->cd = cd;
            dict->level = level;
            csize = ZSTD_sizeof_CDict(cd);
            dict->size += csize;
            cache_bytes += csize;
            zt_cache_reserve(dict, 0); /* the compression object may push others out */
        }
        ctx = ZSTD_createCCtx();
        if (!ctx) ereport(ERROR, (errcode(ERRCODE_OUT_OF_MEMORY), errmsg("ztype: could not allocate compressor")));
        PG_TRY();
        {
            /* Stream the input in chunks so a cancel request is honoured while a
             * large value compresses; the pledged size still lands in the frame
             * header, which zt_frame requires. Output that would exceed the bound
             * means the value is incompressible and stays raw.
             */
            ZSTD_inBuffer in = {src, 0, 0};
            ZSTD_outBuffer out = {v->data, bound, 0};
            bool fits = true;
            zt_check(ZSTD_CCtx_setParameter(ctx, ZSTD_c_compressionLevel, level));
            zt_check(ZSTD_CCtx_setParameter(ctx, ZSTD_c_checksumFlag, 1));
            zt_check(ZSTD_CCtx_setPledgedSrcSize(ctx, len));
            if (dict) zt_check(ZSTD_CCtx_refCDict(ctx, dict->cd));
            for (;;)
            {
                ZSTD_EndDirective mode;
                size_t rc;
                CHECK_FOR_INTERRUPTS();
                in.size = Min(len, in.pos + ZT_CHUNK);
                mode = in.size == len ? ZSTD_e_end : ZSTD_e_continue;
                rc = ZSTD_compressStream2(ctx, &out, &in, mode);
                zt_check(rc);
                if (mode == ZSTD_e_end && rc == 0)
                    break;
                if (out.pos >= out.size)
                {
                    fits = false;
                    break;
                }
            }
            if (frame_only && !fits) /* cannot happen at ZSTD_compressBound; never label raw bytes a frame */
                ereport(ERROR, (errcode(ERRCODE_PROGRAM_LIMIT_EXCEEDED), errmsg("ztype: frame exceeds the compression bound")));
            written = fits ? out.pos : len;
        }
        PG_FINALLY();
        {
            ZSTD_freeCCtx(ctx);
        }
        PG_END_TRY();
        if (frame_only || written < len) v->codec = ZT_ZSTD;
    }
    if (v->codec == ZT_RAW)
    {
        memcpy(v->data, src, len);
        written = len;
    }
    SET_VARSIZE(v, ZT_HDR + written);
    return v;
}

/* Storage keeps its raw fallback; output frames use the same codec without it. */
static ZtValue *
zt_compress(const char *src, Size len, int32 tm, uint8 kind, bool strict)
{
    return zt_compress_internal(src, len, tm, kind, strict, false);
}

/* One-entry decode cache. A query that reads several keys of one zjsonb row, or
 * filters and projects the same ztext column, decodes the same stored bytes once per
 * reference, and PostgreSQL never shares those evaluations. The cache keeps a private
 * copy of the last successfully decoded compressed value and its output in
 * TopMemoryContext; a hit is decided by comparing the stored bytes, so it is exactly
 * as pure as the decode it replaces, dictionary included (frames name their
 * dictionary by ID and registered dictionaries are permanent). Only values up to
 * ZT_DECODE_CACHE_MAX raw bytes are kept, so a backend retains at most about twice
 * that. Raw (uncompressed) values are a memcpy already and bypass it.
 */
#define ZT_DECODE_CACHE_MAX (256 * 1024)
#define ZT_DECODE_CACHE_ENTRIES 2
typedef struct ZtDecoded
{
    ZtValue *in;            /* the stored bytes, header included; NULL when the slot is empty */
    struct varlena *out;    /* the decoded value, in the same allocation */
} ZtDecoded;
static ZtDecoded decode_cache[ZT_DECODE_CACHE_ENTRIES]; /* most recently used first */
static int64 decode_cache_hits, decode_cache_misses;

/* A fresh palloc'd copy of the cached output (with its trailing NUL) or NULL. A hit moves
 * its entry to the front, so the two operands of a binary operator both stay cached.
 */
static struct varlena *
zt_decode_cached(ZtValue *v)
{
    struct varlena *out;
    ZtDecoded hit;
    int i;
    for (i = 0; i < ZT_DECODE_CACHE_ENTRIES; i++)
        if (decode_cache[i].in && VARSIZE(v) == VARSIZE(decode_cache[i].in) &&
            memcmp(v, decode_cache[i].in, VARSIZE(v)) == 0)
            break;
    if (i == ZT_DECODE_CACHE_ENTRIES)
        return NULL;
    hit = decode_cache[i];
    memmove(&decode_cache[1], &decode_cache[0], i * sizeof(ZtDecoded));
    decode_cache[0] = hit;
    out = palloc(VARSIZE(hit.out) + 1);
    memcpy(out, hit.out, VARSIZE(hit.out) + 1);
    decode_cache_hits++;
    return out;
}

/* Called only after a decode that passed every check. One allocation for both copies
 * so an out-of-memory error leaves the previous entries intact; the least recently
 * used entry is released after the new one exists.
 */
static void
zt_decode_remember(ZtValue *v, struct varlena *out)
{
    Size in_size = MAXALIGN(VARSIZE(v));
    char *block;
    decode_cache_misses++;
    if (v->rawlen > ZT_DECODE_CACHE_MAX)
        return;
    block = MemoryContextAlloc(TopMemoryContext, in_size + VARSIZE(out) + 1);
    memcpy(block, v, VARSIZE(v));
    memcpy(block + in_size, out, VARSIZE(out) + 1);
    if (decode_cache[ZT_DECODE_CACHE_ENTRIES - 1].in)
        pfree(decode_cache[ZT_DECODE_CACHE_ENTRIES - 1].in);
    memmove(&decode_cache[1], &decode_cache[0], (ZT_DECODE_CACHE_ENTRIES - 1) * sizeof(ZtDecoded));
    decode_cache[0].in = (ZtValue *) block;
    decode_cache[0].out = (struct varlena *) (block + in_size);
}

/* Validate the envelope before sizes reach allocators or native jsonb readers.
 * False (never raised) when the caller passes an ErrorSaveContext.
 */
static bool
zt_header(ZtValue *v, uint8 kind, Node *escontext)
{
    if (VARSIZE(v) < ZT_HDR || v->magic != ZT_MAGIC)
        ereturn(escontext, false, (errcode(ERRCODE_DATA_CORRUPTED),
                        errmsg("ztype: invalid or unsupported storage format"),
                        errhint("The value was written by an unsupported ztype storage format.")));
    if (v->kind != kind || v->codec > ZT_ZSTD || v->rawlen > MaxAllocSize - ZT_HDR ||
        v->level < 1 || v->level > ZT_MAX_LEVEL ||
        (kind == ZT_JSONB && v->rawlen < sizeof(uint32)))
        ereturn(escontext, false, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid value header")));
    if (v->format != (kind == ZT_JSONB ? ZT_JSONB_FORMAT : 0))
    {
        if (kind == ZT_JSONB)
            ereturn(escontext, false, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
                            errmsg("ztype: unsupported JSON payload format %u", (unsigned) v->format),
                            errhint("The value was written by a ztype build with a different JSON payload format.")));
        ereturn(escontext, false, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid value header")));
    }
    return true;
}

/* The frame header alone: zstd's magic, a declared content size equal to the envelope's
 * and the content checksum flag. Needs no more of the payload than
 * ZSTD_FRAMEHEADERSIZE_MAX bytes, so it also runs on a TOAST slice.
 */
static bool
zt_frame_header(ZtValue *v, Node *escontext)
{
    Size len = VARSIZE(v) - ZT_HDR;
    if (len < 5 || memcmp(v->data, "\x28\xb5\x2f\xfd", 4) != 0 ||
        ZSTD_getFrameContentSize(v->data, len) != v->rawlen ||
        !((unsigned char) v->data[4] & 4)) /* require content checksum */
        ereturn(escontext, false, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid frame header")));
    return true;
}

/* Structural check of exactly one complete zstd frame over the whole payload: the header
 * above, then the block chain walked to the frame's end, which must be the stored end.
 * Returns the dictionary ID the frame names, zero for none; loads no dictionary and
 * verifies no checksum (only a decode does). With an ErrorSaveContext a zero can also mean
 * a rejected frame, which the caller separates with SOFT_ERROR_OCCURRED.
 */
static unsigned
zt_frame_id(ZtValue *v, Node *escontext)
{
    Size len = VARSIZE(v) - ZT_HDR;
    size_t frame_size;
    if (!zt_frame_header(v, escontext))
        return 0;
    frame_size = ZSTD_findFrameCompressedSize(v->data, len);
    if (!zt_check_soft(frame_size, escontext))
        return 0;
    if (frame_size != len)
        ereturn(escontext, 0, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: unexpected trailing frame data")));
    return ZSTD_getDictID_fromFrame(v->data, len);
}

/* Decoders additionally resolve the dictionary strictly, after structural validation. */
static ZtDict *
zt_frame(ZtValue *v, Node *escontext)
{
    unsigned id = zt_frame_id(v, escontext);
    if (SOFT_ERROR_OCCURRED(escontext))
        return NULL;
    return id ? zt_dictionary(0, id, false, escontext) : NULL;
}

/* Decode directly into a PostgreSQL datum and demand an exact length match.
 * escontext is non-NULL only under ztype.validate: every data problem is then saved and
 * NULL returned, nothing longjmps, and the one-entry decode cache is bypassed in both
 * directions so a sweep neither reads it nor seeds it with values it walked once.
 */
static struct varlena *
zt_decompress(ZtValue *v, uint8 kind, Node *escontext)
{
    struct varlena *out;
    ZtDict *dict = NULL;
    ZSTD_DCtx *ctx;
    volatile bool failed = false;
    if (!zt_header(v, kind, escontext))
        return NULL;
    if (v->codec == ZT_RAW)
    {
        if (VARSIZE(v) - ZT_HDR != v->rawlen)
            ereturn(escontext, NULL, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid raw value length")));
    }
    else if (!escontext && (out = zt_decode_cached(v)) != NULL)
        return out;
    else
    {
        dict = zt_frame(v, escontext);
        if (SOFT_ERROR_OCCURRED(escontext))
            return NULL;
    }
    out = palloc(VARHDRSZ + v->rawlen + 1);
    if (v->codec == ZT_RAW)
        memcpy(VARDATA(out), v->data, v->rawlen);
    else
    {
        ctx = zt_dctx_acquire();
        PG_TRY();
        {
            /* The datum is the decoder's window: with a stable output buffer that holds
             * the whole declared content, zstd allocates no output buffer of its own and
             * skips the copy through one (the input buffer, one block, remains). The
             * output size is therefore fixed up front, and interrupt checks come from
             * feeding the input in steps sized to yield about ZT_DECODE_STEP bytes each
             * at the frame's average ratio, so a highly compressible frame gets small
             * steps and cancellation stays prompt. A frame that claims less content than
             * it holds fails inside zstd (destination too small) as data corruption.
             * A soft error must not return from here: it sets the flag and breaks, so
             * PG_FINALLY still releases the decompression context.
             */
            Size total = VARSIZE(v) - ZT_HDR;
            Size step = Max((Size) ((uint64) total * ZT_DECODE_STEP / Max(v->rawlen, 1)), 64);
            ZSTD_inBuffer in = {v->data, 0, 0};
            ZSTD_outBuffer ob = {VARDATA(out), v->rawlen, 0};
            size_t rc = 1;
            failed = !zt_check_soft(ZSTD_DCtx_setParameter(ctx, ZSTD_d_stableOutBuffer, 1), escontext);
            if (!failed && dict)
                failed = !zt_check_soft(ZSTD_DCtx_refDDict(ctx, dict->dd), escontext);
            while (!failed && rc != 0)
            {
                CHECK_FOR_INTERRUPTS();
                in.size = Min(total, in.pos + step);
                rc = ZSTD_decompressStream(ctx, &ob, &in);
                if (!zt_check_soft(rc, escontext))
                {
                    failed = true;
                    break;
                }
                if (rc != 0 && in.pos == total)
                {
                    errsave(escontext, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: truncated frame")));
                    failed = true;
                    break;
                }
            }
            if (!failed && (ob.pos != v->rawlen || in.pos != total))
            {
                errsave(escontext, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: decompressed length mismatch")));
                failed = true;
            }
        }
        PG_FINALLY();
        {
            zt_dctx_release(ctx);
        }
        PG_END_TRY();
        if (failed)
            return NULL;
    }
    SET_VARSIZE(out, VARHDRSZ + v->rawlen);
    VARDATA(out)[v->rawlen] = '\0';
    /* noError only under a soft context: the hard path keeps PostgreSQL's own message. */
    if (kind == ZT_TEXT &&
        !pg_verify_mbstr(GetDatabaseEncoding(), VARDATA(out), v->rawlen, escontext != NULL))
        ereturn(escontext, NULL, (errcode(ERRCODE_CHARACTER_NOT_IN_REPERTOIRE),
                        errmsg("ztype: decoded text is not valid in encoding \"%s\"",
                               GetDatabaseEncodingName())));
    if (!escontext && v->codec == ZT_ZSTD)
        zt_decode_remember(v, out);
    return out;
}

/* Type input always accepts logical values, never client-supplied compressed bytes. */
PG_FUNCTION_INFO_V1(ztext_in);
Datum ztext_in(PG_FUNCTION_ARGS)
{
    char *src = PG_GETARG_CSTRING(0);
    PG_RETURN_POINTER(zt_compress(src, strlen(src), PG_GETARG_INT32(2), ZT_TEXT, false));
}

/* Malformed input is reported through the caller's error context when there is one, so
 * pg_input_is_valid() and COPY ... ON_ERROR behave exactly as for jsonb and bytea. The
 * base-type parse runs first and nothing is stored when it fails: zt_compress never runs.
 */
PG_FUNCTION_INFO_V1(zjsonb_in);
Datum zjsonb_in(PG_FUNCTION_ARGS)
{
    Datum parsed;
    Jsonb *jb;
    if (!DirectInputFunctionCallSafe(jsonb_in, PG_GETARG_CSTRING(0), InvalidOid, -1,
                                     fcinfo->context, &parsed))
        PG_RETURN_NULL();
    jb = DatumGetJsonbP(parsed);
    PG_RETURN_POINTER(zt_compress(VARDATA(jb), VARSIZE(jb) - VARHDRSZ, PG_GETARG_INT32(2), ZT_JSONB, false));
}

PG_FUNCTION_INFO_V1(zbytea_in);
Datum zbytea_in(PG_FUNCTION_ARGS)
{
    Datum parsed;
    bytea *b;
    if (!DirectInputFunctionCallSafe(byteain, PG_GETARG_CSTRING(0), InvalidOid, -1,
                                     fcinfo->context, &parsed))
        PG_RETURN_NULL();
    b = DatumGetByteaPP(parsed);
    PG_RETURN_POINTER(zt_compress(VARDATA_ANY(b), VARSIZE_ANY_EXHDR(b), PG_GETARG_INT32(2), ZT_BYTEA, false));
}

/* Assignment casts receive the target typmod; same-type assignment preserves bytes. */
PG_FUNCTION_INFO_V1(text_to_ztext);
Datum text_to_ztext(PG_FUNCTION_ARGS)
{
    text *t = PG_GETARG_TEXT_PP(0);
    PG_RETURN_POINTER(zt_compress(VARDATA_ANY(t), VARSIZE_ANY_EXHDR(t), PG_GETARG_INT32(1), ZT_TEXT, false));
}

PG_FUNCTION_INFO_V1(jsonb_to_zjsonb);
Datum jsonb_to_zjsonb(PG_FUNCTION_ARGS)
{
    Jsonb *jb = PG_GETARG_JSONB_P(0);
    PG_RETURN_POINTER(zt_compress(VARDATA(jb), VARSIZE(jb) - VARHDRSZ, PG_GETARG_INT32(1), ZT_JSONB, false));
}

PG_FUNCTION_INFO_V1(bytea_to_zbytea);
Datum bytea_to_zbytea(PG_FUNCTION_ARGS)
{
    bytea *b = PG_GETARG_BYTEA_PP(0);
    PG_RETURN_POINTER(zt_compress(VARDATA_ANY(b), VARSIZE_ANY_EXHDR(b), PG_GETARG_INT32(1), ZT_BYTEA, false));
}

/* Any kind, for entry points that take all three types (coercion, recompress, inspect). */
static uint8
zt_kind(ZtValue *v)
{
    if (VARSIZE(v) < ZT_HDR || v->kind < ZT_TEXT || v->kind > ZT_BYTEA)
        ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid value header")));
    return v->kind;
}

/* Dictionary ID recorded in a compressed value's frame; zero for raw values. */
static unsigned
zt_frame_dict_id(ZtValue *v)
{
    return v->codec == ZT_ZSTD ? ZSTD_getDictID_fromFrame(v->data, VARSIZE(v) - ZT_HDR) : 0;
}

/* Same-type length coercion, the step PostgreSQL applies whenever a value's
 * modifier differs from the target's: after reading a literal or a typmod -1
 * parameter with the plain type, when moving values between columns, and in
 * ALTER COLUMN TYPE (which therefore rewrites, like any type change). It gives
 * the value the target level and dictionary and is a no-op when the value
 * already has them. The dictionary lookup runs even for values too short to
 * compress, so a bad slot warns the same way on every path.
 */
PG_FUNCTION_INFO_V1(ztype_coerce);
Datum ztype_coerce(PG_FUNCTION_ARGS)
{
    ZtValue *v = (ZtValue *) PG_GETARG_VARLENA_P(0);
    int32 tm = PG_GETARG_INT32(1);
    int level, slot;
    unsigned want = 0;
    uint8 kind = zt_kind(v);
    struct varlena *raw;
    bool matches;

    if (tm == -1)
        PG_RETURN_POINTER(v);
    zt_policy(tm, &level, &slot);
    zt_header(v, kind, NULL);
    if (slot)
    {
        ZtDict *dict = zt_dictionary(slot, 0, true, NULL);
        want = dict ? dict->id : 0;
    }
    if (v->codec == ZT_ZSTD)
        matches = v->level == level && zt_frame_dict_id(v) == want;
    else /* a raw value of compressible size may still shrink with a dictionary */
        matches = v->level == level && (want == 0 || v->rawlen < ZT_MIN_COMPRESS);
    if (matches)
        PG_RETURN_POINTER(v);
    raw = zt_decompress(v, kind, NULL);
    PG_RETURN_POINTER(zt_compress(VARDATA(raw), VARSIZE(raw) - VARHDRSZ, tm, kind, false));
}

/* Binary protocol carries logical values only, like the text protocol. */
PG_FUNCTION_INFO_V1(ztext_recv);
Datum ztext_recv(PG_FUNCTION_ARGS)
{
    text *t = DatumGetTextPP(DirectFunctionCall1(textrecv, PG_GETARG_DATUM(0)));
    PG_RETURN_POINTER(zt_compress(VARDATA_ANY(t), VARSIZE_ANY_EXHDR(t), PG_GETARG_INT32(2), ZT_TEXT, false));
}

PG_FUNCTION_INFO_V1(zjsonb_recv);
Datum zjsonb_recv(PG_FUNCTION_ARGS)
{
    Jsonb *jb = DatumGetJsonbP(DirectFunctionCall1(jsonb_recv, PG_GETARG_DATUM(0)));
    PG_RETURN_POINTER(zt_compress(VARDATA(jb), VARSIZE(jb) - VARHDRSZ, PG_GETARG_INT32(2), ZT_JSONB, false));
}

PG_FUNCTION_INFO_V1(zbytea_recv);
Datum zbytea_recv(PG_FUNCTION_ARGS)
{
    bytea *b = DatumGetByteaPP(DirectFunctionCall1(bytearecv, PG_GETARG_DATUM(0)));
    PG_RETURN_POINTER(zt_compress(VARDATA_ANY(b), VARSIZE_ANY_EXHDR(b), PG_GETARG_INT32(2), ZT_BYTEA, false));
}

PG_FUNCTION_INFO_V1(ztext_send);
Datum ztext_send(PG_FUNCTION_ARGS)
{
    struct varlena *t = zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_TEXT, NULL);
    PG_RETURN_DATUM(DirectFunctionCall1(textsend, PointerGetDatum(t)));
}

PG_FUNCTION_INFO_V1(zjsonb_send);
Datum zjsonb_send(PG_FUNCTION_ARGS)
{
    struct varlena *jb = zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_JSONB, NULL);
    PG_RETURN_DATUM(DirectFunctionCall1(jsonb_send, PointerGetDatum(jb)));
}

PG_FUNCTION_INFO_V1(zbytea_send);
Datum zbytea_send(PG_FUNCTION_ARGS)
{
    struct varlena *b = zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_BYTEA, NULL);
    PG_RETURN_DATUM(DirectFunctionCall1(byteasend, PointerGetDatum(b)));
}

/* The three logical casts share the checked decoder without extra copies. */
PG_FUNCTION_INFO_V1(ztext_to_text);
Datum ztext_to_text(PG_FUNCTION_ARGS)
{
    PG_RETURN_POINTER(zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_TEXT, NULL));
}

PG_FUNCTION_INFO_V1(zjsonb_to_jsonb);
Datum zjsonb_to_jsonb(PG_FUNCTION_ARGS)
{
    PG_RETURN_POINTER(zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_JSONB, NULL));
}

PG_FUNCTION_INFO_V1(zbytea_to_bytea);
Datum zbytea_to_bytea(PG_FUNCTION_ARGS)
{
    PG_RETURN_POINTER(zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_BYTEA, NULL));
}

/* Output stays compatible with PostgreSQL's ordinary text representations. */
PG_FUNCTION_INFO_V1(ztext_out);
Datum ztext_out(PG_FUNCTION_ARGS)
{
    text *t = (text *) zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_TEXT, NULL);
    /* Type-output callers may pfree the result; it must be an allocation base. */
    PG_RETURN_CSTRING(text_to_cstring(t));
}

PG_FUNCTION_INFO_V1(zjsonb_out);
Datum zjsonb_out(PG_FUNCTION_ARGS)
{
    Jsonb *jb = (Jsonb *) zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_JSONB, NULL);
    PG_RETURN_DATUM(DirectFunctionCall1(jsonb_out, JsonbPGetDatum(jb)));
}

PG_FUNCTION_INFO_V1(zbytea_out);
Datum zbytea_out(PG_FUNCTION_ARGS)
{
    bytea *b = (bytea *) zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_BYTEA, NULL);
    PG_RETURN_DATUM(DirectFunctionCall1(byteaout, PointerGetDatum(b)));
}

/* Compressed output pass-through, ztype.zstd(value [, portable]): the stored zstd frame
 * without the envelope, for a client that decodes zstd itself or forwards the bytes as
 * Content-Encoding: zstd. The contract is "always one frame": a value stored raw (too
 * short, or one zstd could not shrink) is encoded at level 1 per call, without a
 * dictionary. A frame that names a dictionary is returned as stored, since a client
 * holding the dictionary (ztype.dictionary_id and ztype.dictionary) decodes it as it is;
 * with portable = true it is decoded and re-encoded at level 1 without one, so a browser
 * can read it, at the cost of a decode plus an encode per call. The pass-through path
 * runs the envelope and frame structure checks and nothing else: no dictionary is loaded
 * and no content checksum verified, which is the client decoder's job. Only the portable
 * path reads the registry, through the strict lookup in zt_decompress, and that is why the
 * SQL wrappers are SECURITY DEFINER like the casts: an ordinary role may decode a
 * dictionary column, so it may ask for a portable frame of it too. No zjsonb overload by
 * decision: its payload decodes to PostgreSQL's binary jsonb, useless to a client, and
 * ztype.zstd(doc::text) makes the render-and-encode cost visible in the call.
 */
static bytea *
zt_output_frame(ZtValue *v, Size len)
{
    bytea *out = palloc(VARHDRSZ + len);
    SET_VARSIZE(out, VARHDRSZ + len);
    memcpy(VARDATA(out), v->data, len);
    return out;
}

static Datum
zt_zstd(FunctionCallInfo fcinfo, uint8 kind)
{
    ZtValue *v = (ZtValue *) PG_GETARG_VARLENA_P(0);
    bool portable = PG_GETARG_BOOL(1);
    zt_header(v, kind, NULL);
    if (v->codec == ZT_RAW)
    {
        if (VARSIZE(v) - ZT_HDR != v->rawlen)
            ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid raw value length")));
        v = zt_compress_internal(v->data, v->rawlen, 1, kind, true, true);
    }
    else if (zt_frame_id(v, NULL) != 0 && portable)
    {
        struct varlena *raw = zt_decompress(v, kind, NULL);
        v = zt_compress_internal(VARDATA(raw), VARSIZE(raw) - VARHDRSZ, 1, kind, true, true);
    }
    PG_RETURN_BYTEA_P(zt_output_frame(v, VARSIZE(v) - ZT_HDR));
}

PG_FUNCTION_INFO_V1(ztext_zstd);
Datum ztext_zstd(PG_FUNCTION_ARGS) { return zt_zstd(fcinfo, ZT_TEXT); }
PG_FUNCTION_INFO_V1(zbytea_zstd);
Datum zbytea_zstd(PG_FUNCTION_ARGS) { return zt_zstd(fcinfo, ZT_BYTEA); }

/* ztype.dictionary_id(value): the zstd dictionary ID the stored frame names, NULL for a
 * frame without one and for a raw value. The envelope and the frame header only, like
 * ztype.inspect, so an out-of-line value costs its first TOAST chunk: an application keys
 * its dictionary cache on this before fetching the frame.
 */
static Datum
zt_dictionary_id_of(FunctionCallInfo fcinfo, uint8 kind)
{
    ZtValue *v = (ZtValue *) PG_DETOAST_DATUM_SLICE(PG_GETARG_DATUM(0), 0, ZT_HDR - VARHDRSZ + ZSTD_FRAMEHEADERSIZE_MAX);
    unsigned id;
    zt_header(v, kind, NULL);
    if (v->codec == ZT_RAW)
        PG_RETURN_NULL();
    zt_frame_header(v, NULL);
    id = zt_frame_dict_id(v);
    if (id == 0)
        PG_RETURN_NULL();
    PG_RETURN_INT64((int64) id);
}

PG_FUNCTION_INFO_V1(ztext_dictionary_id);
Datum ztext_dictionary_id(PG_FUNCTION_ARGS) { return zt_dictionary_id_of(fcinfo, ZT_TEXT); }
PG_FUNCTION_INFO_V1(zbytea_dictionary_id);
Datum zbytea_dictionary_id(PG_FUNCTION_ARGS) { return zt_dictionary_id_of(fcinfo, ZT_BYTEA); }

/* ztype.zstd(text | bytea, level): one dictionary-free frame of any base-type value at
 * the given level, however short or incompressible the input. The documented way to send
 * a zjsonb column: ztype.zstd(doc::text). Touches no registry, so it needs no definer.
 */
PG_FUNCTION_INFO_V1(ztype_zstd_base);
Datum ztype_zstd_base(PG_FUNCTION_ARGS)
{
    struct varlena *raw = PG_GETARG_VARLENA_PP(0);
    int level = PG_GETARG_INT32(1);
    ZtValue *v;
    if (level < 1 || level > ZT_MAX_LEVEL)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: expected a level 1..%d", ZT_MAX_LEVEL)));
    v = zt_compress_internal(VARDATA_ANY(raw), VARSIZE_ANY_EXHDR(raw), level, ZT_BYTEA, true, true);
    PG_RETURN_BYTEA_P(zt_output_frame(v, VARSIZE(v) - ZT_HDR));
}

/* Read only the envelope from TOAST; raw_length counts bytes, not characters. */
PG_FUNCTION_INFO_V1(ztype_raw_length);
Datum ztype_raw_length(PG_FUNCTION_ARGS)
{
    ZtValue *v = (ZtValue *) PG_DETOAST_DATUM_SLICE(PG_GETARG_DATUM(0), 0, ZT_HDR - VARHDRSZ);
    int32 len;
    if (VARSIZE(v) < ZT_HDR)
        ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid or unsupported storage format")));
    zt_header(v, v->kind, NULL); /* wrappers expose only text/bytea */
    if (v->kind != ZT_TEXT && v->kind != ZT_BYTEA)
        ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: invalid raw_length type")));
    len = v->rawlen;
    pfree(v);
    PG_RETURN_INT32(len);
}

/* Prefix is a byte budget clipped to the database encoding, not character count.
 * Compressed values still fetch all TOAST bytes; only decoding stops early.
 */
PG_FUNCTION_INFO_V1(ztype_prefix);
Datum ztype_prefix(PG_FUNCTION_ARGS)
{
    ZtValue *v = (ZtValue *) PG_GETARG_VARLENA_P(0);
    int32 want = PG_GETARG_INT32(1);
    int n;
    char *out;
    text *t;
    ZtDict *dict;
    ZSTD_DCtx *ctx;
    ZSTD_inBuffer in;
    ZSTD_outBuffer ob;

    zt_header(v, ZT_TEXT, NULL);
    n = Min(Max(want, 0), v->rawlen);
    if (n == 0) PG_RETURN_TEXT_P(cstring_to_text(""));
    if (v->codec == ZT_RAW || n == v->rawlen)
    {
        t = (text *) zt_decompress(v, ZT_TEXT, NULL);
        PG_RETURN_TEXT_P(cstring_to_text_with_len(VARDATA(t), pg_mbcliplen(VARDATA(t), v->rawlen, n)));
    }
    if ((t = (text *) zt_decode_cached(v)) != NULL)
        PG_RETURN_TEXT_P(cstring_to_text_with_len(VARDATA(t), pg_mbcliplen(VARDATA(t), v->rawlen, n)));
    out = palloc0(n + MAX_MULTIBYTE_CHAR_LEN);
    dict = zt_frame(v, NULL);
    ctx = zt_dctx_acquire();
    in.src = v->data; in.size = VARSIZE(v) - ZT_HDR; in.pos = 0;
    ob.dst = out; ob.size = n; ob.pos = 0;
    PG_TRY();
    {
        if (dict) zt_check(ZSTD_DCtx_refDDict(ctx, dict->dd));
        while (ob.pos < ob.size)
        {
            size_t before_in = in.pos, before_out = ob.pos;
            size_t rc;
            CHECK_FOR_INTERRUPTS();
            rc = ZSTD_decompressStream(ctx, &ob, &in);
            zt_check(rc);
            if (ob.pos < ob.size && (rc == 0 || (before_in == in.pos && before_out == ob.pos)))
                ereport(ERROR, (errcode(ERRCODE_DATA_CORRUPTED), errmsg("ztype: truncated prefix frame")));
        }
    }
    PG_FINALLY();
    {
        zt_dctx_release(ctx);
    }
    PG_END_TRY();
    n = pg_mbcliplen(out, n, n);
    pg_verify_mbstr(GetDatabaseEncoding(), out, n, false);
    PG_RETURN_TEXT_P(cstring_to_text_with_len(out, n));
}

/* Native JSON accessors can return SQL NULL; DirectFunctionCall2 cannot. */
static Datum
zt_json_field(FunctionCallInfo fcinfo, PGFunction fn)
{
    LOCAL_FCINFO(inner, 2);
    Jsonb *jb = (Jsonb *) zt_decompress((ZtValue *) PG_GETARG_VARLENA_P(0), ZT_JSONB, NULL);
    Datum result;
    InitFunctionCallInfoData(*inner, NULL, 2, InvalidOid, NULL, NULL);
    inner->args[0].value = JsonbPGetDatum(jb);
    inner->args[0].isnull = false;
    inner->args[1].value = PG_GETARG_DATUM(1);
    inner->args[1].isnull = false;
    result = fn(inner);
    fcinfo->isnull = inner->isnull;
    return result;
}

PG_FUNCTION_INFO_V1(zjsonb_object_field);
Datum zjsonb_object_field(PG_FUNCTION_ARGS)
{
    return zt_json_field(fcinfo, jsonb_object_field);
}

PG_FUNCTION_INFO_V1(zjsonb_object_field_text);
Datum zjsonb_object_field_text(PG_FUNCTION_ARGS)
{
    return zt_json_field(fcinfo, jsonb_object_field_text);
}

/* Equality is the base type's equality on the decoded values: bytewise for text (ztext
 * is not collatable, so it compares like text under the C collation) and bytea, jsonb's
 * structural equality for zjsonb. Two cheap decisions come from the stored bytes alone:
 * identical envelopes are equal, because decoding is a pure function of the bytes, and
 * text or bytea envelopes declaring different raw lengths are unequal. Neither verifies
 * the payload; ztype.validate is the sweep for that. Everything else decodes both sides
 * through the checked decoder (a corrupt frame errors as the cast would).
 */
static bool
zt_equal(ZtValue *a, ZtValue *b, uint8 kind)
{
    struct varlena *da, *db;
    bool eq;
    zt_header(a, kind, NULL);
    zt_header(b, kind, NULL);
    if (VARSIZE(a) == VARSIZE(b) && memcmp(a, b, VARSIZE(a)) == 0)
        return true;
    if (kind != ZT_JSONB && a->rawlen != b->rawlen)
        return false;
    da = zt_decompress(a, kind, NULL);
    db = zt_decompress(b, kind, NULL);
    if (kind == ZT_JSONB)
        eq = DatumGetBool(DirectFunctionCall2(jsonb_eq, PointerGetDatum(da), PointerGetDatum(db)));
    else
        eq = VARSIZE(da) == VARSIZE(db) &&
             memcmp(VARDATA(da), VARDATA(db), VARSIZE(da) - VARHDRSZ) == 0;
    pfree(da);
    pfree(db);
    return eq;
}

/* The hash of the decoded value, consistent with zt_equal: hash_any over the bytes for
 * text and bytea (what hashtext and hashvarlena compute under a deterministic collation)
 * and jsonb's own hash for zjsonb, so equal logical values hash alike whatever policy
 * stored them. Always a full decode.
 */
static Datum
zt_hash(ZtValue *v, uint8 kind, bool extended, uint64 seed)
{
    struct varlena *d = zt_decompress(v, kind, NULL);
    Datum h;
    if (kind == ZT_JSONB)
        h = extended ? DirectFunctionCall2(jsonb_hash_extended, PointerGetDatum(d), UInt64GetDatum(seed))
                     : DirectFunctionCall1(jsonb_hash, PointerGetDatum(d));
    else
        h = extended ? hash_any_extended((unsigned char *) VARDATA(d), VARSIZE(d) - VARHDRSZ, seed)
                     : hash_any((unsigned char *) VARDATA(d), VARSIZE(d) - VARHDRSZ);
    pfree(d);
    return h;
}

#define ZT_EQ_FUNCS(name, kind) \
PG_FUNCTION_INFO_V1(name##_eq); \
Datum name##_eq(PG_FUNCTION_ARGS) \
{ \
    PG_RETURN_BOOL(zt_equal((ZtValue *) PG_GETARG_VARLENA_P(0), (ZtValue *) PG_GETARG_VARLENA_P(1), kind)); \
} \
PG_FUNCTION_INFO_V1(name##_ne); \
Datum name##_ne(PG_FUNCTION_ARGS) \
{ \
    PG_RETURN_BOOL(!zt_equal((ZtValue *) PG_GETARG_VARLENA_P(0), (ZtValue *) PG_GETARG_VARLENA_P(1), kind)); \
} \
PG_FUNCTION_INFO_V1(name##_hash); \
Datum name##_hash(PG_FUNCTION_ARGS) \
{ \
    return zt_hash((ZtValue *) PG_GETARG_VARLENA_P(0), kind, false, 0); \
} \
PG_FUNCTION_INFO_V1(name##_hash_extended); \
Datum name##_hash_extended(PG_FUNCTION_ARGS) \
{ \
    return zt_hash((ZtValue *) PG_GETARG_VARLENA_P(0), kind, true, PG_GETARG_INT64(1)); \
}

ZT_EQ_FUNCS(ztext, ZT_TEXT)
ZT_EQ_FUNCS(zjsonb, ZT_JSONB)
ZT_EQ_FUNCS(zbytea, ZT_BYTEA)

/* ANALYZE. Without an ordering operator PostgreSQL's standard analyzer would count distinct
 * values pairwise with the equality function: up to 200 comparisons per sample row, each
 * decoding two values (measured 2026-09-10: 31 s against 0.14 s for jsonb on 20,000 small
 * documents). Instead each sample row is decoded once and the base type's own analyzer
 * (std_typanalyze on text under the C collation, jsonb or bytea, hence the sort-based
 * statistics) runs over the decoded values through a fetch function of ours. Values whose
 * envelope declares more than ZT_ANALYZE_WIDE decoded bytes are never decoded: the
 * standard analyzer would drop them as too wide anyway and count them as distinct, which
 * the distinct estimate below does too, so memory stays bounded by the sample size times
 * ZT_ANALYZE_WIDE. What reaches pg_statistic: null fraction and stored width computed here,
 * the distinct estimate, and the most-common-value slot with its values re-encoded under the
 * column's policy and its operator set to the type's own equality; the histogram and
 * correlation slots are dropped, there being no ordering operator to use them with.
 */
#define ZT_ANALYZE_WIDE 1024 /* analyze.c's WIDTH_THRESHOLD */
typedef struct ZtAnalyze
{
    VacAttrStats base;              /* what the base type's analyzer sees; must be first */
    VacAttrStats *real;
    AnalyzeAttrFetchFunc fetch;     /* the real fetch function, over the stored rows */
    uint8 kind;
    int *rowmap;                    /* base row number -> sample row (nulls and narrow values) */
} ZtAnalyze;

static Datum
zt_analyze_fetch(VacAttrStatsP stats, int rownum, bool *isnull)
{
    ZtAnalyze *az = (ZtAnalyze *) stats;
    Datum d = az->fetch(az->real, az->rowmap[rownum], isnull);
    if (*isnull)
        return (Datum) 0;
    return PointerGetDatum(zt_decompress((ZtValue *) PG_DETOAST_DATUM(d), az->kind, NULL));
}

static void
zt_compute_stats(VacAttrStats *stats, AnalyzeAttrFetchFunc fetch, int samplerows, double totalrows)
{
    ZtAnalyze *az = (ZtAnalyze *) stats->extra_data;
    VacAttrStats *base = &az->base;
    int nulls = 0, wide = 0, narrow = 0, i;
    double width = 0, distinct;
    az->fetch = fetch;
    az->rowmap = palloc(samplerows * sizeof(int));
    for (i = 0; i < samplerows; i++)
    {
        bool isnull;
        Datum d = fetch(stats, i, &isnull);
        ZtValue *v;
        if (isnull)
        {
            nulls++;
            az->rowmap[narrow++] = i;
            continue;
        }
        width += VARSIZE_ANY(DatumGetPointer(d));
        v = (ZtValue *) PG_DETOAST_DATUM_SLICE(d, 0, ZT_HDR - VARHDRSZ);
        if (VARSIZE(v) >= ZT_HDR && v->magic == ZT_MAGIC && v->rawlen > ZT_ANALYZE_WIDE)
            wide++;
        else
            az->rowmap[narrow++] = i;
        pfree(v);
    }
    if (samplerows == 0)
        return;
    stats->stats_valid = true;
    stats->stanullfrac = (double) nulls / samplerows;
    stats->stawidth = nulls < samplerows ? width / (samplerows - nulls) : 0;
    if (narrow == nulls)
    {
        /* Nothing but nulls and wide values: as the standard analyzer, assume the latter distinct. */
        stats->stadistinct = nulls < samplerows ? -1.0 * (1.0 - stats->stanullfrac) : 0;
        return;
    }
    base->compute_stats(base, zt_analyze_fetch, narrow, totalrows * narrow / samplerows);
    if (!base->stats_valid)
    {
        stats->stats_valid = false;
        return;
    }
    /* The base's estimate covers the narrow rows; each wide row counts as its own value. */
    distinct = base->stadistinct >= 0 ? base->stadistinct : -base->stadistinct * totalrows * narrow / samplerows;
    distinct += (double) wide * totalrows / samplerows;
    stats->stadistinct = distinct > 0.1 * totalrows ? -(distinct / totalrows) : distinct;
    for (i = 0; i < STATISTIC_NUM_SLOTS; i++)
    {
        int j;
        Datum *values;
        MemoryContext old;
        if (base->stakind[i] != STATISTIC_KIND_MCV)
            continue;
        /* compute_stats runs in a per-column context reset before pg_statistic is written. */
        old = MemoryContextSwitchTo(stats->anl_context);
        values = palloc(base->numvalues[i] * sizeof(Datum));
        for (j = 0; j < base->numvalues[i]; j++)
        {
            struct varlena *d = (struct varlena *) DatumGetPointer(base->stavalues[i][j]);
            values[j] = PointerGetDatum(zt_compress(VARDATA(d), VARSIZE(d) - VARHDRSZ, stats->attrtypmod, az->kind, false));
        }
        MemoryContextSwitchTo(old);
        stats->stakind[0] = STATISTIC_KIND_MCV;
        stats->staop[0] = lookup_type_cache(stats->attrtypid, TYPECACHE_EQ_OPR)->eq_opr;
        stats->stacoll[0] = InvalidOid;
        stats->numnumbers[0] = base->numnumbers[i];
        stats->stanumbers[0] = base->stanumbers[i];
        stats->numvalues[0] = base->numvalues[i];
        stats->stavalues[0] = values;
        break;
    }
}

static bool
zt_typanalyze(VacAttrStats *stats, uint8 kind)
{
    Oid base_type = kind == ZT_TEXT ? TEXTOID : kind == ZT_JSONB ? JSONBOID : BYTEAOID;
    ZtAnalyze *az = MemoryContextAllocZero(stats->anl_context, sizeof(ZtAnalyze));
    HeapTuple tup = SearchSysCache1(TYPEOID, ObjectIdGetDatum(base_type));
    int i;
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "cache lookup failed for type %u", base_type);
    az->base = *stats;
    az->base.attrtypid = base_type;
    az->base.attrtype = MemoryContextAlloc(stats->anl_context, sizeof(FormData_pg_type));
    memcpy(az->base.attrtype, GETSTRUCT(tup), sizeof(FormData_pg_type));
    ReleaseSysCache(tup);
    az->base.attrcollid = kind == ZT_TEXT ? C_COLLATION_OID : InvalidOid;
    for (i = 0; i < STATISTIC_NUM_SLOTS; i++)
    {
        az->base.statypid[i] = base_type;
        az->base.statyplen[i] = -1;
        az->base.statypbyval[i] = false;
        az->base.statypalign[i] = TYPALIGN_INT;
    }
    if (!std_typanalyze(&az->base))
        return false;
    az->real = stats;
    az->kind = kind;
    stats->compute_stats = zt_compute_stats;
    stats->minrows = az->base.minrows;
    stats->extra_data = az;
    return true;
}

PG_FUNCTION_INFO_V1(ztext_typanalyze);
Datum ztext_typanalyze(PG_FUNCTION_ARGS)
{
    PG_RETURN_BOOL(zt_typanalyze((VacAttrStats *) PG_GETARG_POINTER(0), ZT_TEXT));
}

PG_FUNCTION_INFO_V1(zjsonb_typanalyze);
Datum zjsonb_typanalyze(PG_FUNCTION_ARGS)
{
    PG_RETURN_BOOL(zt_typanalyze((VacAttrStats *) PG_GETARG_POINTER(0), ZT_JSONB));
}

PG_FUNCTION_INFO_V1(zbytea_typanalyze);
Datum zbytea_typanalyze(PG_FUNCTION_ARGS)
{
    PG_RETURN_BOOL(zt_typanalyze((VacAttrStats *) PG_GETARG_POINTER(0), ZT_BYTEA));
}

/* Explicit recompression applies policy even when source and target types match,
 * and unlike logical writes it fails if the requested dictionary is missing.
 */
PG_FUNCTION_INFO_V1(ztype_recompress);
Datum ztype_recompress(PG_FUNCTION_ARGS)
{
    ZtValue *v = (ZtValue *) PG_GETARG_VARLENA_P(0);
    int level = PG_GETARG_INT32(1), slot = PG_GETARG_INT32(2);
    struct varlena *raw;
    if (level < 1 || level > ZT_MAX_LEVEL || slot < 0 || slot > ZT_MAX_SLOT)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: invalid recompression policy")));
    raw = zt_decompress(v, zt_kind(v), NULL);
    PG_RETURN_POINTER(zt_compress(VARDATA(raw), VARSIZE(raw) - VARHDRSZ, level | (slot << 5), v->kind, true));
}

/* Does a stored value already carry the policy (level, slot)? The test the catch-up
 * recipes use: the level must match, a raw value (too short, or one zstd could not shrink)
 * passes at that level however it got there, and a compressed one must name the slot's
 * dictionary (none for slot 0). Reads the envelope and the frame header only, so an
 * out-of-line value costs its first TOAST chunk and no decompression. Strict about the
 * slot: an unregistered one is an error, like recompress, because "matches" would be
 * meaningless. Note the one difference from the coercion, which recompresses a raw value
 * of compressible size when a dictionary is wanted: here it matches, so a rerun of a
 * batched catch-up selects nothing (README, "Resumable catch-up").
 */
static bool
zt_matches_policy(ZtValue *v, int level, int slot)
{
    unsigned id;
    if (v->level != level)
        return false;
    if (v->codec != ZT_ZSTD)
        return true;
    id = zt_frame_dict_id(v);
    if (slot == 0)
        return id == 0;
    if (id == 0)
        return false;
    return zt_dictionary(slot, 0, false, NULL)->id == id;
}

PG_FUNCTION_INFO_V1(ztype_matches_policy);
Datum ztype_matches_policy(PG_FUNCTION_ARGS)
{
    ZtValue *v = (ZtValue *) PG_DETOAST_DATUM_SLICE(PG_GETARG_DATUM(0), 0, ZT_HDR - VARHDRSZ + ZSTD_FRAMEHEADERSIZE_MAX);
    int level = PG_GETARG_INT32(1), slot = PG_GETARG_INT32(2);
    if (level < 1 || level > ZT_MAX_LEVEL || slot < 0 || slot > ZT_MAX_SLOT)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: invalid policy")));
    zt_header(v, zt_kind(v), NULL);
    PG_RETURN_BOOL(zt_matches_policy(v, level, slot));
}

/* Rewrite-free policy changes. ztype.set_column_policy re-points a column at a new level
 * and dictionary by changing pg_attribute.atttypmod in place, exactly what ALTER COLUMN
 * TYPE would store, without the rewrite: rows keep the policy they were written with,
 * new writes get the new one. What makes that sound is the pending bit in the modifier
 * (ztext(9,2,pending)): PostgreSQL elides the same-type coercion whenever source and
 * target modifiers are equal, so a column whose modifier claimed (9,2) while holding
 * (6,1) rows would hand those rows unchanged to any other (9,2) column. A pending
 * modifier equals no plain one, so every move out of the column is coerced, and a plain
 * (9,2) column stays uniform. Moves into another pending column of the same modifier are
 * not coerced, which is fine: that column holds no promise until its own finish.
 *
 * The rows are caught up by ordinary UPDATEs with ztype.matches_policy as the predicate,
 * and ztype.finish_column_policy scans every row under an ACCESS EXCLUSIVE lock, refuses
 * while any is off the policy, and otherwise clears the bit. ALTER COLUMN TYPE to a plain
 * modifier is the other way out: it rewrites and clears the bit with it.
 *
 * Both take the table's ACCESS EXCLUSIVE lock, recurse to partitions and inheritance
 * children like ALTER TABLE, and require the caller to own every table touched (they run
 * with the caller's rights; the registry is read only through the PUBLIC inventory and
 * ztype.dictionary_slot). Dependent objects are handled the way a catalog-only change
 * needs: an expression index or a CHECK constraint or trigger WHEN clause keeps a Var
 * whose typmod is the column's, and the planner matches an index expression by node
 * equality, typmod included (measured 2026-09-09: a stale typmod turns the index scan
 * into a sequential one), so those trees are rewritten in place with the new typmod. A
 * view or rule, or extended statistics, are refused, as ALTER TABLE refuses the view:
 * a view's own result columns would keep the old modifier.
 */
typedef struct ZtRetypmod
{
    AttrNumber attnum;
    int32 typmod;
    bool changed;
} ZtRetypmod;

static Node *
zt_retypmod_mutator(Node *node, ZtRetypmod *ctx)
{
    if (node == NULL)
        return NULL;
    if (IsA(node, Var))
    {
        Var *var = (Var *) copyObject(node);
        if (var->varlevelsup == 0 && var->varattno == ctx->attnum && var->vartypmod != ctx->typmod)
        {
            var->vartypmod = ctx->typmod;
            ctx->changed = true;
        }
        return (Node *) var;
    }
    return expression_tree_mutator(node, zt_retypmod_mutator, ctx);
}

/* Rewrite the serialized expression columns `attnums` of one catalog tuple and store it
 * back when a Var changed. `tup` must carry a valid t_self (a syscache or scan tuple).
 */
static void
zt_retypmod_tuple(Relation catalog, HeapTuple tup, const AttrNumber *attnums, int natts, ZtRetypmod *ctx)
{
    TupleDesc desc = RelationGetDescr(catalog);
    Datum *values = palloc0(sizeof(Datum) * desc->natts);
    bool *nulls = palloc0(sizeof(bool) * desc->natts);
    bool *replace = palloc0(sizeof(bool) * desc->natts);
    bool any = false;
    int i;
    for (i = 0; i < natts; i++)
    {
        bool isnull;
        Datum d = heap_getattr(tup, attnums[i], desc, &isnull);
        Node *tree;
        if (isnull)
            continue;
        tree = stringToNode(TextDatumGetCString(d));
        ctx->changed = false;
        tree = zt_retypmod_mutator(tree, ctx);
        if (!ctx->changed)
            continue;
        values[attnums[i] - 1] = CStringGetTextDatum(nodeToString(tree));
        replace[attnums[i] - 1] = true;
        any = true;
    }
    if (any)
    {
        HeapTuple newtup = heap_modify_tuple(tup, desc, values, nulls, replace);
        CatalogTupleUpdate(catalog, &newtup->t_self, newtup);
        heap_freetuple(newtup);
    }
    pfree(values);
    pfree(nulls);
    pfree(replace);
}

static void
zt_retypmod_index(Oid indexoid, ZtRetypmod *ctx)
{
    static const AttrNumber cols[] = {Anum_pg_index_indexprs, Anum_pg_index_indpred};
    Relation catalog = table_open(IndexRelationId, RowExclusiveLock);
    HeapTuple tup = SearchSysCache1(INDEXRELID, ObjectIdGetDatum(indexoid));
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "cache lookup failed for index %u", indexoid);
    LockRelationOid(indexoid, AccessExclusiveLock);
    zt_retypmod_tuple(catalog, tup, cols, 2, ctx);
    ReleaseSysCache(tup);
    table_close(catalog, RowExclusiveLock);
}

static void
zt_retypmod_constraint(Oid conoid, ZtRetypmod *ctx)
{
    static const AttrNumber cols[] = {Anum_pg_constraint_conbin};
    Relation catalog = table_open(ConstraintRelationId, RowExclusiveLock);
    HeapTuple tup = SearchSysCache1(CONSTROID, ObjectIdGetDatum(conoid));
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "cache lookup failed for constraint %u", conoid);
    zt_retypmod_tuple(catalog, tup, cols, 1, ctx);
    ReleaseSysCache(tup);
    table_close(catalog, RowExclusiveLock);
}

static void
zt_retypmod_trigger(Oid trigoid, ZtRetypmod *ctx)
{
    static const AttrNumber cols[] = {Anum_pg_trigger_tgqual};
    Relation catalog = table_open(TriggerRelationId, RowExclusiveLock);
    ScanKeyData key;
    SysScanDesc scan;
    HeapTuple tup;
    ScanKeyInit(&key, Anum_pg_trigger_oid, BTEqualStrategyNumber, F_OIDEQ, ObjectIdGetDatum(trigoid));
    scan = systable_beginscan(catalog, TriggerOidIndexId, true, NULL, 1, &key);
    tup = systable_getnext(scan);
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "could not find tuple for trigger %u", trigoid);
    zt_retypmod_tuple(catalog, tup, cols, 1, ctx);
    systable_endscan(scan);
    table_close(catalog, RowExclusiveLock);
}

/* Everything recorded in pg_depend as depending on the column. One object can carry
 * several rows (a trigger on UPDATE OF the column whose WHEN clause also reads it), and a
 * catalog tuple may be updated once per command, so the objects are collected first.
 */
static void
zt_retypmod_dependents(Oid relid, AttrNumber attnum, const char *colname, int32 typmod)
{
    Relation depRel = table_open(DependRelationId, AccessShareLock);
    ScanKeyData key[3];
    SysScanDesc scan;
    HeapTuple tup;
    List *objects = NIL;
    ListCell *lc;
    ZtRetypmod ctx = {attnum, typmod, false};
    ScanKeyInit(&key[0], Anum_pg_depend_refclassid, BTEqualStrategyNumber, F_OIDEQ, ObjectIdGetDatum(RelationRelationId));
    ScanKeyInit(&key[1], Anum_pg_depend_refobjid, BTEqualStrategyNumber, F_OIDEQ, ObjectIdGetDatum(relid));
    ScanKeyInit(&key[2], Anum_pg_depend_refobjsubid, BTEqualStrategyNumber, F_INT4EQ, Int32GetDatum((int32) attnum));
    scan = systable_beginscan(depRel, DependReferenceIndexId, true, NULL, 3, key);
    while ((tup = systable_getnext(scan)) != NULL)
    {
        Form_pg_depend dep = (Form_pg_depend) GETSTRUCT(tup);
        ObjectAddress *address;
        bool seen = false;
        foreach(lc, objects)
        {
            address = lfirst(lc);
            if (address->classId == dep->classid && address->objectId == dep->objid)
                seen = true;
        }
        if (seen)
            continue;
        address = palloc(sizeof(*address));
        address->classId = dep->classid;
        address->objectId = dep->objid;
        address->objectSubId = dep->objsubid;
        objects = lappend(objects, address);
    }
    systable_endscan(scan);
    table_close(depRel, AccessShareLock);
    foreach(lc, objects)
    {
        ObjectAddress *address = lfirst(lc);
        char relkind;
        switch (address->classId)
        {
            case RelationRelationId:
                relkind = get_rel_relkind(address->objectId);
                if (relkind == RELKIND_INDEX || relkind == RELKIND_PARTITIONED_INDEX)
                {
                    zt_retypmod_index(address->objectId, &ctx);
                    continue;
                }
                break;
            case ConstraintRelationId:
                zt_retypmod_constraint(address->objectId, &ctx);
                continue;
            case TriggerRelationId:
                zt_retypmod_trigger(address->objectId, &ctx);
                continue;
            case AttrDefaultRelationId: /* re-coerced to the column's modifier on every use */
            case PublicationRelRelationId: /* column lists and row filters are evaluated, never matched */
                continue;
            default:
                break;
        }
        ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
                        errmsg("ztype: cannot change the policy of column \"%s\" in place: %s depends on it",
                               colname, getObjectDescription(address, false)),
                        errhint("Drop it and recreate it afterwards, or use ALTER TABLE ... ALTER COLUMN ... TYPE, which rewrites.")));
    }
}

/* The three types, by the extension's schema; a foreign type never reaches the catalog write. */
static bool
zt_is_ztype(Oid typid)
{
    static const char *names[] = {"ztext", "zjsonb", "zbytea"};
    Oid ext = get_extension_oid("ztype", true);
    Oid nsp;
    int i;
    if (!OidIsValid(ext))
        return false;
    nsp = get_extension_schema(ext);
    for (i = 0; i < 3; i++)
        if (typid == GetSysCacheOid2(TYPENAMENSP, Anum_pg_type_oid, CStringGetDatum(names[i]), ObjectIdGetDatum(nsp)))
            return true;
    return false;
}

/* One relation of the inheritance set: checks, then the atttypmod write and the dependents. */
static void
zt_column_retypmod(Oid relid, const char *colname, int32 typmod, bool root, const char *op)
{
    Relation rel = table_open(relid, NoLock); /* locked by find_all_inheritors */
    Relation attrel;
    HeapTuple tup;
    Form_pg_attribute att;
    AttrNumber attnum;
    if (rel->rd_rel->relkind != RELKIND_RELATION && rel->rd_rel->relkind != RELKIND_PARTITIONED_TABLE)
        ereport(ERROR, (errcode(ERRCODE_WRONG_OBJECT_TYPE),
                        errmsg("ztype: \"%s\" is not a table", RelationGetRelationName(rel))));
    if (!object_ownercheck(RelationRelationId, relid, GetUserId()))
        aclcheck_error(ACLCHECK_NOT_OWNER, get_relkind_objtype(rel->rd_rel->relkind), RelationGetRelationName(rel));
    if (OidIsValid(rel->rd_rel->reloftype))
        ereport(ERROR, (errcode(ERRCODE_FEATURE_NOT_SUPPORTED),
                        errmsg("ztype: cannot change the policy of a column of typed table \"%s\"", RelationGetRelationName(rel))));
    CheckTableNotInUse(rel, op);
    attnum = get_attnum(relid, colname);
    if (attnum == InvalidAttrNumber || attnum < 1)
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_COLUMN),
                        errmsg("column \"%s\" of relation \"%s\" does not exist", colname, RelationGetRelationName(rel))));
    attrel = table_open(AttributeRelationId, RowExclusiveLock);
    tup = SearchSysCacheCopyAttNum(relid, attnum);
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "cache lookup failed for attribute %d of relation %u", attnum, relid);
    att = (Form_pg_attribute) GETSTRUCT(tup);
    if (!zt_is_ztype(att->atttypid))
        ereport(ERROR, (errcode(ERRCODE_DATATYPE_MISMATCH),
                        errmsg("ztype: column \"%s\" of relation \"%s\" is not a ztext, zjsonb or zbytea column",
                               colname, RelationGetRelationName(rel))));
    if (root && att->attinhcount > 0)
        ereport(ERROR, (errcode(ERRCODE_INVALID_TABLE_DEFINITION),
                        errmsg("ztype: cannot change the policy of inherited column \"%s\"", colname),
                        errhint("Run it on the parent table; partitions and children follow.")));
    if (att->atttypmod != typmod)
    {
        att->atttypmod = typmod;
        CatalogTupleUpdate(attrel, &tup->t_self, tup);
        InvokeObjectPostAlterHook(RelationRelationId, relid, attnum);
        zt_retypmod_dependents(relid, attnum, colname, typmod);
        CacheInvalidateRelcacheByRelid(relid);
    }
    heap_freetuple(tup);
    table_close(attrel, RowExclusiveLock);
    table_close(rel, NoLock);
}

/* The column's current modifier on the root relation, after the ownership and type checks. */
static int32
zt_column_typmod(Oid relid, const char *colname)
{
    AttrNumber attnum = get_attnum(relid, colname);
    HeapTuple tup;
    int32 typmod;
    if (attnum == InvalidAttrNumber || attnum < 1)
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_COLUMN),
                        errmsg("column \"%s\" of relation \"%s\" does not exist", colname, get_rel_name(relid))));
    tup = SearchSysCacheAttNum(relid, attnum);
    if (!HeapTupleIsValid(tup))
        elog(ERROR, "cache lookup failed for attribute %d of relation %u", attnum, relid);
    typmod = ((Form_pg_attribute) GETSTRUCT(tup))->atttypmod;
    ReleaseSysCache(tup);
    return typmod;
}

/* One row of one scalar SQL result under the caller's rights (SPI), or -1 for no row. */
static int64
zt_spi_scalar(const char *sql, Oid argtype, Datum arg)
{
    int64 result = -1;
    bool isnull;
    if (SPI_connect() != SPI_OK_CONNECT)
        elog(ERROR, "ztype: SPI_connect failed");
    if (SPI_execute_with_args(sql, argtype ? 1 : 0, &argtype, &arg, NULL, true, 1) != SPI_OK_SELECT)
        elog(ERROR, "ztype: query failed: %s", sql);
    if (SPI_processed == 1)
    {
        Datum d = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
        result = isnull ? -1 : DatumGetInt64(d);
    }
    SPI_finish();
    return result;
}

PG_FUNCTION_INFO_V1(ztype_set_column_policy);
Datum ztype_set_column_policy(PG_FUNCTION_ARGS)
{
    Oid relid;
    char *colname;
    int32 level, slot = 0, typmod;
    List *rels;
    ListCell *lc;
    if (PG_ARGISNULL(0) || PG_ARGISNULL(1) || PG_ARGISNULL(2))
        ereport(ERROR, (errcode(ERRCODE_NULL_VALUE_NOT_ALLOWED), errmsg("ztype: table, column and level are required")));
    relid = PG_GETARG_OID(0);
    colname = NameStr(*PG_GETARG_NAME(1));
    level = PG_GETARG_INT32(2);
    if (level < 1 || level > ZT_MAX_LEVEL)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: expected a level 1..%d", ZT_MAX_LEVEL)));
    if (!PG_ARGISNULL(3) && get_fn_expr_argtype(fcinfo->flinfo, 3) == INT4OID)
    {
        slot = PG_GETARG_INT32(3); /* the (regclass, name, integer, integer) overload */
        if (slot < 0 || slot > ZT_MAX_SLOT)
            ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                            errmsg("ztype: dictionary slot must be 0..%d", ZT_MAX_SLOT)));
    }
    else if (!PG_ARGISNULL(3))
    {
        char *dict = text_to_cstring(PG_GETARG_TEXT_PP(3));
        if (zt_all_digits(dict))
        {
            if (!zt_typmod_int(dict, &slot) || slot < 0 || slot > ZT_MAX_SLOT)
                ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                                errmsg("ztype: dictionary slot must be 0..%d", ZT_MAX_SLOT)));
        }
        else /* the SECURITY DEFINER name lookup, callable by any role; unregistered names are its error */
            slot = (int32) zt_spi_scalar("SELECT ztype.dictionary_slot($1)::pg_catalog.int8", TEXTOID, CStringGetTextDatum(dict));
    }
    typmod = level | (slot << 5) | ZT_TM_PENDING;
    rels = find_all_inheritors(relid, AccessExclusiveLock, NULL);
    foreach(lc, rels)
        zt_column_retypmod(lfirst_oid(lc), colname, typmod, lc == list_head(rels), "ztype.set_column_policy");
    CommandCounterIncrement();
    PG_RETURN_VOID();
}

PG_FUNCTION_INFO_V1(ztype_finish_column_policy);
Datum ztype_finish_column_policy(PG_FUNCTION_ARGS)
{
    Oid relid = PG_GETARG_OID(0);
    char *colname = NameStr(*PG_GETARG_NAME(1));
    int32 typmod;
    int level, slot;
    List *rels;
    ListCell *lc;
    int64 pending = 0;
    rels = find_all_inheritors(relid, AccessExclusiveLock, NULL);
    /* The checks of the write path first, so a non-owner never scans. */
    zt_column_retypmod(relid, colname, zt_column_typmod(relid, colname), true, "ztype.finish_column_policy");
    typmod = zt_column_typmod(relid, colname);
    if (typmod == -1 || !(typmod & ZT_TM_PENDING))
        PG_RETURN_VOID();
    zt_policy(typmod, &level, &slot);
    if (slot && zt_spi_scalar("SELECT 1::pg_catalog.int8 FROM ztype.dictionary_inventory WHERE slot = $1", INT4OID, Int32GetDatum(slot)) < 0)
        ereport(ERROR, (errcode(ERRCODE_UNDEFINED_OBJECT),
                        errmsg("ztype: dictionary slot %d is not available", slot),
                        errhint("Register the dictionary before finishing the column's policy.")));
    foreach(lc, rels)
    {
        Oid r = lfirst_oid(lc);
        StringInfoData sql;
        int64 n;
        if (get_rel_relkind(r) != RELKIND_RELATION)
            continue;
        initStringInfo(&sql);
        appendStringInfo(&sql, "SELECT pg_catalog.count(*) FROM ONLY %s WHERE %s IS NOT NULL AND NOT ztype.matches_policy(%s, %d, %d)",
                         quote_qualified_identifier(get_namespace_name(get_rel_namespace(r)), get_rel_name(r)),
                         quote_identifier(colname), quote_identifier(colname), level, slot);
        n = zt_spi_scalar(sql.data, InvalidOid, (Datum) 0);
        pfree(sql.data);
        if (n > 0)
            pending += n;
    }
    if (pending > 0)
        ereport(ERROR, (errcode(ERRCODE_OBJECT_IN_USE),
                        errmsg("ztype: %lld row%s of column \"%s\" still carry another policy", (long long) pending, pending == 1 ? "" : "s", colname),
                        errhint("Rewrite them first, for example UPDATE t SET col = col::%s WHERE NOT ztype.matches_policy(col, %d, %d), then finish again.",
                                slot ? psprintf("ztext(%d,%d)", level, slot) : psprintf("ztext(%d)", level), level, slot)));
    typmod &= ~ZT_TM_PENDING;
    foreach(lc, rels)
        zt_column_retypmod(lfirst_oid(lc), colname, typmod, lc == list_head(rels), "ztype.finish_column_policy");
    CommandCounterIncrement();
    PG_RETURN_VOID();
}

/* Integrity sweep. Decodes the value under a soft-error context and reports the first
 * problem as text instead of raising, so one statement can check a whole table and the
 * transaction survives every bad row:
 *
 *   SELECT id, v.msg FROM messages m, LATERAL ztype.validate(m.body) v(msg)
 *   WHERE v.msg IS NOT NULL;
 *
 * NULL means the value passed every check ztype makes: the envelope (magic, kind against
 * the SQL-declared type, level, payload format, allocation bounds), the exact stored
 * length of a raw payload, and for a compressed one exactly one zstd frame carrying a
 * content checksum, a dictionary that resolves in the registry, and a full decode of
 * exactly the declared length. ztext additionally decodes to valid text in the database
 * encoding. It never walks a jsonb container -- that is PostgreSQL's own representation,
 * and zjsonb only checks that the payload is large enough to hold one -- so a value whose
 * frame is intact but whose bytes are not a well-formed container validates clean. Only
 * out of memory can still raise.
 */
static Datum
zt_validate(FunctionCallInfo fcinfo, uint8 kind)
{
    ZtValue *v = (ZtValue *) PG_GETARG_VARLENA_P(0);
    ErrorSaveContext escontext = {T_ErrorSaveContext};
    escontext.details_wanted = true; /* the message is the result */
    zt_decompress(v, kind, (Node *) &escontext);
    if (!escontext.error_occurred)
        PG_RETURN_NULL();
    PG_RETURN_TEXT_P(cstring_to_text(escontext.error_data && escontext.error_data->message
                                     ? escontext.error_data->message
                                     : "ztype: value failed validation"));
}

PG_FUNCTION_INFO_V1(ztype_validate_text);
Datum ztype_validate_text(PG_FUNCTION_ARGS)
{
    return zt_validate(fcinfo, ZT_TEXT);
}

PG_FUNCTION_INFO_V1(ztype_validate_jsonb);
Datum ztype_validate_jsonb(PG_FUNCTION_ARGS)
{
    return zt_validate(fcinfo, ZT_JSONB);
}

PG_FUNCTION_INFO_V1(ztype_validate_bytea);
Datum ztype_validate_bytea(PG_FUNCTION_ARGS)
{
    return zt_validate(fcinfo, ZT_BYTEA);
}

/* Observability: what the envelope and frame header say about a stored value.
 * Reads the whole datum but decodes nothing, so it does not verify the content
 * checksum; a full cast does. Slot and name come from the registry (this runs
 * as the extension owner) and are NULL when the dictionary is not registered.
 */
/* Everything inspect reports sits in the envelope and the zstd frame header, and the
 * stored size is in the TOAST pointer, so an out-of-line value is fetched only up to
 * the frame header: partial_reads in the suite counts the buffers. The frame itself is
 * not validated here; that is what ztype.validate is for.
 */
PG_FUNCTION_INFO_V1(ztype_inspect);
Datum ztype_inspect(PG_FUNCTION_ARGS)
{
    Datum arg = PG_GETARG_DATUM(0);
    ZtValue *v = (ZtValue *) PG_DETOAST_DATUM_SLICE(arg, 0, ZT_HDR - VARHDRSZ + ZSTD_FRAMEHEADERSIZE_MAX);
    static const char *kinds[] = {NULL, "text", "jsonb", "bytea"};
    TupleDesc desc;
    Datum values[9] = {0};
    bool nulls[9] = {false, false, false, false, false, false, true, true, true};
    uint8 kind = zt_kind(v);
    unsigned id;

    if (get_call_result_type(fcinfo, NULL, &desc) != TYPEFUNC_COMPOSITE)
        elog(ERROR, "ztype: inspect must return a composite type");
    zt_header(v, kind, NULL);
    id = zt_frame_dict_id(v);
    values[0] = CStringGetTextDatum(kinds[kind]);
    values[1] = CStringGetTextDatum(v->codec == ZT_ZSTD ? "zstd" : "raw");
    values[2] = Int32GetDatum(v->level);
    values[3] = Int32GetDatum(v->format);
    values[4] = Int32GetDatum(v->rawlen);
    values[5] = Int32GetDatum((int32) (toast_raw_datum_size(arg) - VARHDRSZ));
    if (id)
    {
        Oid argtype = INT8OID;
        Datum dictid = Int64GetDatum((int64) id);
        bool pushed = !ActiveSnapshotSet();
        MemoryContext caller = CurrentMemoryContext;
        values[6] = dictid;
        nulls[6] = false;
        if (pushed)
            PushActiveSnapshot(GetTransactionSnapshot());
        if (SPI_connect() != SPI_OK_CONNECT)
            elog(ERROR, "ztype: SPI_connect failed");
        if (SPI_execute_plan(zt_plan(&zt_plan_inspect, "SELECT slot, name FROM ztype.dictionaries WHERE dict_id = $1", argtype),
                             &dictid, NULL, true, 1) != SPI_OK_SELECT)
            elog(ERROR, "ztype: dictionary lookup failed");
        if (SPI_processed == 1)
        {
            bool isnull;
            MemoryContext old;
            values[7] = SPI_getbinval(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 1, &isnull);
            nulls[7] = isnull;
            old = MemoryContextSwitchTo(caller);
            values[8] = CStringGetTextDatum(SPI_getvalue(SPI_tuptable->vals[0], SPI_tuptable->tupdesc, 2));
            MemoryContextSwitchTo(old);
            nulls[8] = false;
        }
        SPI_finish();
        if (pushed) PopActiveSnapshot();
    }
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(desc, values, nulls)));
}

/* What this library is, for packaging and mismatch questions: its own version (the
 * SQL script's is in pg_extension), the envelope magic and jsonb payload format it
 * writes, and libzstd at build time against the one actually loaded.
 */
PG_FUNCTION_INFO_V1(ztype_build_info);
Datum ztype_build_info(PG_FUNCTION_ARGS)
{
    TupleDesc desc;
    Datum values[5];
    bool nulls[5] = {false, false, false, false, false};
    char magic[16];

    if (get_call_result_type(fcinfo, NULL, &desc) != TYPEFUNC_COMPOSITE)
        elog(ERROR, "ztype: build_info must return a composite type");
    snprintf(magic, sizeof(magic), "0x%08x", ZT_MAGIC);
    values[0] = CStringGetTextDatum(ZT_VERSION);
    values[1] = CStringGetTextDatum(magic);
    values[2] = Int32GetDatum(ZT_JSONB_FORMAT);
    values[3] = CStringGetTextDatum(ZSTD_VERSION_STRING);
    values[4] = CStringGetTextDatum(ZSTD_versionString());
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(desc, values, nulls)));
}

PG_FUNCTION_INFO_V1(ztype_zstd_version);
Datum ztype_zstd_version(PG_FUNCTION_ARGS)
{
    PG_RETURN_TEXT_P(cstring_to_text(ZSTD_versionString()));
}

/* Admin validation also backs the dictionary table CHECK constraint. */
PG_FUNCTION_INFO_V1(zstd_dict_id);
Datum zstd_dict_id(PG_FUNCTION_ARGS)
{
    PG_RETURN_INT64(zt_dictionary_id(PG_GETARG_BYTEA_PP(0)));
}

/* Optional admin reset; correctness does not depend on manually calling this. */
PG_FUNCTION_INFO_V1(ztype_reload_dictionaries);
Datum ztype_reload_dictionaries(PG_FUNCTION_ARGS)
{
    while (dict_cache)
        zt_cache_free(dict_cache, NULL);
    if (miss_context)
        MemoryContextDelete(miss_context);
    PG_RETURN_VOID();
}

/* This backend's dictionary cache: live entries and bytes against the budget,
 * plus lifetime load and eviction counts. Backend-local, so not parallel safe.
 */
PG_FUNCTION_INFO_V1(ztype_dictionary_cache_stats);
Datum ztype_dictionary_cache_stats(PG_FUNCTION_ARGS)
{
    TupleDesc desc;
    Datum values[5];
    bool nulls[5] = {false, false, false, false, false};
    if (get_call_result_type(fcinfo, NULL, &desc) != TYPEFUNC_COMPOSITE)
        elog(ERROR, "ztype: dictionary_cache_stats must return a composite type");
    values[0] = Int32GetDatum(cache_entries);
    values[1] = Int64GetDatum((int64) cache_bytes);
    values[2] = Int64GetDatum((int64) zt_cache_kb * 1024);
    values[3] = Int64GetDatum(cache_loads);
    values[4] = Int64GetDatum(cache_evictions);
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(desc, values, nulls)));
}

/* Backend-local counters for the decode cache; the suite's oracle for "decoded once". */
PG_FUNCTION_INFO_V1(ztype_decode_cache_stats);
Datum ztype_decode_cache_stats(PG_FUNCTION_ARGS)
{
    TupleDesc desc;
    Datum values[3];
    bool nulls[3] = {false, false, false};
    if (get_call_result_type(fcinfo, NULL, &desc) != TYPEFUNC_COMPOSITE)
        elog(ERROR, "ztype: decode_cache_stats must return a composite type");
    values[0] = Int64GetDatum(decode_cache_hits);
    values[1] = Int64GetDatum(decode_cache_misses);
    {
        int64 bytes = 0;
        int i;
        for (i = 0; i < ZT_DECODE_CACHE_ENTRIES; i++)
            if (decode_cache[i].in)
                bytes += MAXALIGN(VARSIZE(decode_cache[i].in)) + VARSIZE(decode_cache[i].out) + 1;
        values[2] = Int64GetDatum(bytes);
    }
    PG_RETURN_DATUM(HeapTupleGetDatum(heap_form_tuple(desc, values, nulls)));
}

/* Train bounded samples through a cursor. The column may be text, bytea or jsonb:
 * each sample is the value's own stored bytes, so a jsonb column trains on the
 * binary form that zjsonb compresses, never on its text rendering. Other types,
 * including the compressed types themselves, are rejected.
 */
PG_FUNCTION_INFO_V1(zstd_train_dictionary);
Datum zstd_train_dictionary(PG_FUNCTION_ARGS)
{
    char *sql = text_to_cstring(PG_GETARG_TEXT_PP(0));
    int maxdict = PG_GETARG_INT32(1), sample_bytes = PG_GETARG_INT32(2);
    SPIPlanPtr plan;
    Portal portal;
    MemoryContext caller = CurrentMemoryContext;
    MemoryContext old;
    StringInfoData samples;
    size_t *sizes;
    unsigned count = 0;
    bytea *result;
    size_t dsize;
    bool done = false;

    if (maxdict < 1024 || maxdict > ZT_MAX_DICT || sample_bytes < 1 || sample_bytes > ZT_MAX_SAMPLE_BYTES)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE),
                        errmsg("ztype: dictionary size must be 1024..%d and sample bytes 1..%d", ZT_MAX_DICT, ZT_MAX_SAMPLE_BYTES)));
    initStringInfo(&samples);
    sizes = palloc(sizeof(*sizes) * ZT_MAX_SAMPLES);
    result = palloc(VARHDRSZ + maxdict);
    if (SPI_connect() != SPI_OK_CONNECT) elog(ERROR, "ztype: SPI_connect failed");
    plan = SPI_prepare(sql, 0, NULL);
    if (!plan || !SPI_is_cursor_plan(plan))
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: training query must be one SELECT")));
    portal = SPI_cursor_open(NULL, plan, NULL, NULL, true);
    if (!portal) elog(ERROR, "ztype: could not open training cursor");
    while (!done && count < ZT_MAX_SAMPLES)
    {
        uint64 i, rows;
        SPITupleTable *table;
        CHECK_FOR_INTERRUPTS();
        SPI_cursor_fetch(portal, true, ZT_BATCH);
        rows = SPI_processed;
        table = SPI_tuptable;
        if (rows == 0) { if (table) SPI_freetuptable(table); break; }
        if (table->tupdesc->natts != 1 ||
            (TupleDescAttr(table->tupdesc, 0)->atttypid != TEXTOID &&
             TupleDescAttr(table->tupdesc, 0)->atttypid != BYTEAOID &&
             TupleDescAttr(table->tupdesc, 0)->atttypid != JSONBOID))
            ereport(ERROR, (errcode(ERRCODE_DATATYPE_MISMATCH), errmsg("ztype: training query must return exactly one text, bytea or jsonb column")));
        for (i = 0; i < rows && count < ZT_MAX_SAMPLES; i++)
        {
            bool isnull;
            Datum val = SPI_getbinval(table->vals[i], table->tupdesc, 1, &isnull);
            text *t;
            Size len;
            if (isnull) continue;
            t = (text *) PG_DETOAST_DATUM_SLICE(val, 0, sample_bytes); /* any varlena: text, bytea or jsonb bytes */
            len = VARSIZE_ANY_EXHDR(t);
            if (len == 0) { pfree(t); continue; }
            if (len > ZT_TRAIN_BUDGET - samples.len) { pfree(t); done = true; break; }
            old = MemoryContextSwitchTo(caller);
            appendBinaryStringInfo(&samples, VARDATA_ANY(t), len);
            MemoryContextSwitchTo(old);
            pfree(t);
            sizes[count++] = len;
        }
        SPI_freetuptable(table);
    }
    SPI_cursor_close(portal);
    SPI_freeplan(plan);
    SPI_finish();
    if (count < 8)
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: at least 8 nonempty samples are required")));
    CHECK_FOR_INTERRUPTS();
    dsize = ZDICT_trainFromBuffer(VARDATA(result), maxdict, samples.data, sizes, count);
    CHECK_FOR_INTERRUPTS();
    if (ZDICT_isError(dsize))
        ereport(ERROR, (errcode(ERRCODE_INVALID_PARAMETER_VALUE), errmsg("ztype: training failed: %s", ZDICT_getErrorName(dsize))));
    SET_VARSIZE(result, VARHDRSZ + dsize);
    zt_dictionary_id(result);
    pfree(samples.data);
    pfree(sizes);
    PG_RETURN_BYTEA_P(result);
}
