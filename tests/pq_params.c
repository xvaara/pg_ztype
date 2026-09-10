/* Extended-protocol parameters for the ztype suite (tests/test_ztype.py, extended_protocol).
 * One executable, printing OK on success: PQexecParams with inferred and typed parameters in text
 * and binary format, NULL parameters, a bytea carrying embedded zero bytes, Unicode text, binary
 * payloads the receive functions must reject, one prepared statement executed eight times across
 * both formats, the same name deallocated and prepared again with typed parameters (and a second
 * PREPARE under a live name refused with 42P05), and a pipeline in which one statement fails:
 * the implicit transaction between two syncs takes the statements before the failure down with
 * it, the ones after it are aborted, and the next sync starts clean. Every row is read back with
 * binary results and every column compared byte for byte against the `expect` table below (jsonb
 * including its version byte); every rejection pins the SQLSTATE and leaves the connection
 * usable. The row ids and the NULL rows are what extended_protocol() asserts against
 * ztype.inspect, so keep the table and the suite in step.
 */
#include <libpq-fe.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <arpa/inet.h>

#define INT4OID 23
#define TEXTOID 25
#define BYTEAOID 17
#define JSONBOID 3802

static const char *sample = "first sample subject first sample subject first sample subject first sample subject ";
/* 'aä😀' first, then enough more UTF-8 to clear the 64-byte compression floor. */
static const char *uni_unit = "aä😀ünïcodé ";
static const char *insert_sql = "INSERT INTO pq VALUES ($1, $2, $3, $4)";
static const Oid base_types[4] = {INT4OID, TEXTOID, JSONBOID, BYTEAOID};

static char json_text[256];
static char jsonb_bin[256];
static int jsonb_bin_len;
static char sample_hex[256];
static char uni[256];
static char uni_hex[600];
static char zeros[256];
static int zeros_len;
static char zeros_hex[600];

static int fail(PGconn *c, const char *what)
{
    fprintf(stderr, "%s: %s\n", what, PQerrorMessage(c));
    return 1;
}

static int wrong(const char *what, int rowid)
{
    fprintf(stderr, "%s: row %d did not round trip\n", what, rowid);
    return 1;
}

/* A "\x..." literal, the text representation of a bytea parameter. */
static void hexlit(char *out, const char *src, int len)
{
    int i;
    memcpy(out, "\\x", 2);
    for (i = 0; i < len; i++)
        sprintf(out + 2 + 2 * i, "%02x", (unsigned char) src[i]);
}

/* One four-parameter INSERT in text format; a NULL pointer is a NULL parameter. types may be NULL
 * to let the server infer the ztype types from the target columns. */
static int ins_text(PGconn *c, const char *what, int rowid, const char *body,
                    const char *meta, const char *blob, const Oid *types)
{
    char id[16];
    const char *values[4];
    PGresult *r;

    snprintf(id, sizeof id, "%d", rowid);
    values[0] = id; values[1] = body; values[2] = meta; values[3] = blob;
    r = PQexecParams(c, insert_sql, 4, types, values, NULL, NULL, 0);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, what); }
    PQclear(r);
    return 0;
}

/* The same insert with every parameter in binary format, so the receive functions run. */
static int ins_bin(PGconn *c, const char *what, int rowid, const char *body, int body_len,
                   const char *meta, int meta_len, const char *blob, int blob_len, const Oid *types)
{
    int32_t netid = htonl((uint32_t) rowid);
    const char *values[4];
    int lengths[4], formats[4];
    PGresult *r;
    int i;

    values[0] = (const char *) &netid; lengths[0] = 4;
    values[1] = body; lengths[1] = body_len;
    values[2] = meta; lengths[2] = meta_len;
    values[3] = blob; lengths[3] = blob_len;
    for (i = 0; i < 4; i++) formats[i] = 1;
    r = PQexecParams(c, insert_sql, 4, types, values, lengths, formats, 0);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, what); }
    PQclear(r);
    return 0;
}

/* A binary payload the receive function must reject: the SQLSTATE is pinned, and because the
 * statement ran in autocommit the same connection must run the next one with no transaction to
 * roll back. */
static int rejected(PGconn *c, const char *what, const char *sql,
                    const char *value, int len, const char *sqlstate)
{
    const char *values[1];
    int lengths[1], formats[1];
    const char *state;
    PGresult *r;

    values[0] = value; lengths[0] = len; formats[0] = 1;
    r = PQexecParams(c, sql, 1, NULL, values, lengths, formats, 0);
    state = PQresultErrorField(r, PG_DIAG_SQLSTATE);
    if (PQresultStatus(r) != PGRES_FATAL_ERROR || !state || strcmp(state, sqlstate) != 0)
    {
        fprintf(stderr, "%s: expected SQLSTATE %s, got %s: %s\n", what, sqlstate,
                state ? state : "none", PQresultErrorMessage(r));
        PQclear(r);
        return 1;
    }
    PQclear(r);
    r = PQexec(c, "SELECT 1");
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1 ||
        strcmp(PQgetvalue(r, 0, 0), "1") != 0)
    {
        PQclear(r);
        return fail(c, what);
    }
    PQclear(r);
    if (PQtransactionStatus(c) != PQTRANS_IDLE)
    {
        fprintf(stderr, "%s: connection left a transaction open\n", what);
        return 1;
    }
    return 0;
}

/* The three payload columns of one row, in binary format: the logical values. */
static PGresult *binrow(PGconn *c, int rowid)
{
    char sql[80];
    PGresult *r;

    snprintf(sql, sizeof sql, "SELECT body, meta, blob FROM pq WHERE id = %d", rowid);
    r = PQexecParams(c, sql, 0, NULL, NULL, NULL, NULL, 1);
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1) { PQclear(r); return NULL; }
    return r;
}

static int same(PGresult *r, int col, const char *want, int len)
{
    return !PQgetisnull(r, 0, col) && PQgetlength(r, 0, col) == len &&
           memcmp(PQgetvalue(r, 0, col), want, (size_t) len) == 0;
}

/* What every inserted row must read back as. V_NULL marks the rows whose three payload columns
 * were all NULL parameters; every other row's meta is the one JSON document, whose binary output
 * is the version byte followed by json_text unchanged. This table and the counts asserted by
 * extended_protocol() in the suite are one contract; change them together. */
enum { V_NULL, V_SAMPLE, V_ZEROS, V_UNI };

static const struct { int id; int body; int blob; } expect[] = {
    {1, V_SAMPLE, V_SAMPLE},    /* inferred, text */
    {2, V_SAMPLE, V_SAMPLE},    /* typed, text */
    {3, V_SAMPLE, V_SAMPLE},    /* inferred, binary */
    {4, V_SAMPLE, V_SAMPLE},    /* typed, binary */
    {5, V_NULL, V_NULL},        /* NULLs, text */
    {6, V_NULL, V_NULL},        /* NULLs, binary */
    {7, V_SAMPLE, V_ZEROS},     /* embedded zero bytes, binary parameter */
    {8, V_SAMPLE, V_ZEROS},     /* embedded zero bytes, escaped literal */
    {9, V_UNI, V_UNI},          /* Unicode, text */
    {10, V_UNI, V_UNI},         /* Unicode, binary */
    {11, V_SAMPLE, V_SAMPLE},   /* 11-18: the prepared statement, text and binary alternating */
    {12, V_SAMPLE, V_SAMPLE},
    {13, V_NULL, V_NULL},
    {14, V_SAMPLE, V_SAMPLE},
    {15, V_SAMPLE, V_SAMPLE},
    {16, V_NULL, V_NULL},
    {17, V_SAMPLE, V_SAMPLE},
    {18, V_SAMPLE, V_SAMPLE},
    {19, V_SAMPLE, V_SAMPLE},   /* 19-20: the name deallocated and prepared again, typed; text then binary */
    {20, V_SAMPLE, V_SAMPLE},
    {24, V_SAMPLE, V_SAMPLE},   /* 24-25: the pipeline's second implicit transaction (21-23 rolled back) */
    {25, V_SAMPLE, V_SAMPLE},
};

/* Rows the pipeline must not have stored: 21 was applied before the failing 22 in the same
 * implicit transaction, 23 was aborted after it. */
static const int absent[] = {21, 22, 23};

/* One PQsendQueryPrepared of the four-column insert for the pipeline: text format, or binary
 * with a body of the caller's choosing (so one row can carry bytes the receive function rejects). */
static int send_row(PGconn *c, int rowid, int binary, const char *body, int body_len)
{
    char id[16];
    int32_t netid = htonl((uint32_t) rowid);
    const char *values[4];
    int lengths[4], formats[4], k;

    snprintf(id, sizeof id, "%d", rowid);
    values[0] = binary ? (const char *) &netid : id; lengths[0] = 4;
    values[1] = body; lengths[1] = body_len;
    values[2] = binary ? jsonb_bin : json_text; lengths[2] = jsonb_bin_len;
    values[3] = binary ? sample : sample_hex; lengths[3] = (int) strlen(sample);
    for (k = 0; k < 4; k++) formats[k] = binary;
    return PQsendQueryPrepared(c, "ins", 4, values, lengths, formats, 0) ? 0 : fail(c, "pipeline send");
}

/* The next result must have this status, followed by the NULL that ends one statement's results
 * (a sync result stands alone). For PGRES_FATAL_ERROR the SQLSTATE is pinned as well. */
static int next_result(PGconn *c, ExecStatusType status, const char *sqlstate, const char *what)
{
    PGresult *r = PQgetResult(c);
    if (!r || PQresultStatus(r) != status)
    {
        fprintf(stderr, "%s: expected %s, got %s: %s\n", what, PQresStatus(status),
                r ? PQresStatus(PQresultStatus(r)) : "no result", r ? PQresultErrorMessage(r) : "");
        PQclear(r);
        return 1;
    }
    if (sqlstate)
    {
        const char *state = PQresultErrorField(r, PG_DIAG_SQLSTATE);
        if (!state || strcmp(state, sqlstate) != 0)
        {
            fprintf(stderr, "%s: expected SQLSTATE %s, got %s\n", what, sqlstate, state ? state : "none");
            PQclear(r);
            return 1;
        }
    }
    PQclear(r);
    if (status != PGRES_PIPELINE_SYNC && PQgetResult(c) != NULL)
    {
        fprintf(stderr, "%s: extra result\n", what);
        return 1;
    }
    return 0;
}

/* The bytes a V_ kind stands for; the buffers are filled by main before the read-back runs. */
static const char *kind_bytes(int kind, int *len)
{
    switch (kind)
    {
        case V_SAMPLE: *len = (int) strlen(sample); return sample;
        case V_ZEROS: *len = zeros_len; return zeros;
        default: *len = (int) strlen(uni); return uni;
    }
}

int main(void)
{
    PGconn *c = PQconnectdb("");
    PGresult *r;
    char id[16];
    int sample_len, uni_len;
    int i;

    if (PQstatus(c) != CONNECTION_OK) return fail(c, "connect");
    sample_len = (int) strlen(sample);
    snprintf(json_text, sizeof json_text, "{\"body\": \"%s\"}", sample);
    /* binary jsonb is a version byte followed by the text */
    jsonb_bin[0] = 1;
    memcpy(jsonb_bin + 1, json_text, strlen(json_text));
    jsonb_bin_len = (int) strlen(json_text) + 1;
    hexlit(sample_hex, sample, sample_len);
    for (i = 0; i < 8; i++)
        memcpy(uni + i * strlen(uni_unit), uni_unit, strlen(uni_unit));
    uni_len = (int) strlen(uni);
    hexlit(uni_hex, uni, uni_len);
    for (i = 0; i < 8; i++)
        memcpy(zeros + i * 16, "zero\0byte\0chunk ", 16);
    zeros_len = 8 * 16;
    hexlit(zeros_hex, zeros, zeros_len);

    /* 1-4: the four happy-path shapes, inferred and typed, text and binary. */
    if (ins_text(c, "inferred text", 1, sample, json_text, sample_hex, NULL)) return 1;
    if (ins_text(c, "typed text", 2, sample, json_text, sample_hex, base_types)) return 1;
    if (ins_bin(c, "inferred binary", 3, sample, sample_len, jsonb_bin, jsonb_bin_len,
                sample, sample_len, NULL)) return 1;
    if (ins_bin(c, "typed binary", 4, sample, sample_len, jsonb_bin, jsonb_bin_len,
                sample, sample_len, base_types)) return 1;

    /* 5-6: NULL parameters, inferred in text format and typed in binary format. */
    if (ins_text(c, "inferred text NULLs", 5, NULL, NULL, NULL, NULL)) return 1;
    if (ins_bin(c, "typed binary NULLs", 6, NULL, 0, NULL, 0, NULL, 0, base_types)) return 1;

    /* 7-8: a bytea whose bytes include zeros, as a binary parameter and as an escaped literal. */
    if (ins_bin(c, "embedded zeros binary", 7, sample, sample_len, jsonb_bin, jsonb_bin_len,
                zeros, zeros_len, NULL)) return 1;
    if (ins_text(c, "embedded zeros text", 8, sample, json_text, zeros_hex, NULL)) return 1;

    /* 9-10: Unicode text and the same UTF-8 bytes as bytea, in both formats. */
    if (ins_text(c, "unicode text", 9, uni, json_text, uni_hex, NULL)) return 1;
    if (ins_bin(c, "unicode binary", 10, uni, uni_len, jsonb_bin, jsonb_bin_len,
                uni, uni_len, NULL)) return 1;

    /* 11-18: one prepared statement, eight executions alternating format, two of them all NULL,
     * so the plan cache switches from custom to generic plans while the formats keep changing. */
    r = PQprepare(c, "ins", insert_sql, 0, NULL);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, "prepare"); }
    PQclear(r);
    for (i = 0; i < 8; i++)
    {
        int binary = i % 2;
        int null_row = (i == 2 || i == 5);
        int32_t netid = htonl((uint32_t) (11 + i));
        const char *values[4];
        int lengths[4], formats[4], k;

        snprintf(id, sizeof id, "%d", 11 + i);
        values[0] = binary ? (const char *) &netid : id;
        lengths[0] = 4;
        values[1] = null_row ? NULL : sample;
        lengths[1] = sample_len;
        values[2] = null_row ? NULL : (binary ? jsonb_bin : json_text);
        lengths[2] = jsonb_bin_len;
        values[3] = null_row ? NULL : (binary ? sample : sample_hex);
        lengths[3] = sample_len;
        for (k = 0; k < 4; k++) formats[k] = binary;
        r = PQexecPrepared(c, "ins", 4, values, lengths, formats, 0);
        if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, "prepared insert"); }
        PQclear(r);
    }

    /* 19-20: the same name across DEALLOCATE and a second PQprepare, now with typed parameters,
     * executed in text and then binary format; and PREPARE under a name that is still live is
     * the server's 42P05, with the connection unaffected. */
    r = PQprepare(c, "ins", insert_sql, 4, base_types);
    if (PQresultStatus(r) != PGRES_FATAL_ERROR ||
        strcmp(PQresultErrorField(r, PG_DIAG_SQLSTATE) ? PQresultErrorField(r, PG_DIAG_SQLSTATE) : "", "42P05") != 0)
    { PQclear(r); return fail(c, "second prepare under a live name should be 42P05"); }
    PQclear(r);
    r = PQexec(c, "DEALLOCATE ins");
    if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, "deallocate"); }
    PQclear(r);
    r = PQprepare(c, "ins", insert_sql, 4, base_types);
    if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, "re-prepare typed"); }
    PQclear(r);
    for (i = 0; i < 2; i++)
    {
        int binary = i;
        int32_t netid = htonl((uint32_t) (19 + i));
        const char *values[4];
        int lengths[4], formats[4], k;

        snprintf(id, sizeof id, "%d", 19 + i);
        values[0] = binary ? (const char *) &netid : id; lengths[0] = 4;
        values[1] = sample; lengths[1] = sample_len;
        values[2] = binary ? jsonb_bin : json_text; lengths[2] = jsonb_bin_len;
        values[3] = binary ? sample : sample_hex; lengths[3] = sample_len;
        for (k = 0; k < 4; k++) formats[k] = binary;
        r = PQexecPrepared(c, "ins", 4, values, lengths, formats, 0);
        if (PQresultStatus(r) != PGRES_COMMAND_OK) { PQclear(r); return fail(c, "re-prepared insert"); }
        PQclear(r);
    }

    /* 21-25: pipeline mode with the re-prepared statement. Rows 21, 22 and 23 go before one sync,
     * and 22 carries a body the receive function rejects; 24 and 25 go before a second sync.
     * Without an explicit transaction, everything between two syncs is one implicit transaction:
     * 21 is rolled back with 22, 23 is reported aborted, and 24 and 25 land. */
    {
        static const char bad_utf8[] = {(char) 0xff, (char) 0xfe, 'a'};
        if (!PQenterPipelineMode(c)) return fail(c, "enter pipeline mode");
        if (send_row(c, 21, 0, sample, sample_len)) return 1;
        if (send_row(c, 22, 1, bad_utf8, (int) sizeof bad_utf8)) return 1;
        if (send_row(c, 23, 1, sample, sample_len)) return 1;
        if (!PQpipelineSync(c)) return fail(c, "sync 1");
        if (send_row(c, 24, 0, sample, sample_len)) return 1;
        if (send_row(c, 25, 1, sample, sample_len)) return 1;
        if (!PQpipelineSync(c)) return fail(c, "sync 2");
        if (next_result(c, PGRES_COMMAND_OK, NULL, "row 21")) return 1;
        if (next_result(c, PGRES_FATAL_ERROR, "22021", "row 22")) return 1;
        if (next_result(c, PGRES_PIPELINE_ABORTED, NULL, "row 23")) return 1;
        if (next_result(c, PGRES_PIPELINE_SYNC, NULL, "sync 1")) return 1;
        if (next_result(c, PGRES_COMMAND_OK, NULL, "row 24")) return 1;
        if (next_result(c, PGRES_COMMAND_OK, NULL, "row 25")) return 1;
        if (next_result(c, PGRES_PIPELINE_SYNC, NULL, "sync 2")) return 1;
        if (!PQexitPipelineMode(c)) return fail(c, "exit pipeline mode");
        if (PQtransactionStatus(c) != PQTRANS_IDLE) { fprintf(stderr, "pipeline left a transaction open\n"); return 1; }
    }
    for (i = 0; i < (int) (sizeof absent / sizeof absent[0]); i++)
    {
        if ((r = binrow(c, absent[i])) != NULL)
        { PQclear(r); fprintf(stderr, "row %d from the failed implicit transaction was stored\n", absent[i]); return 1; }
    }

    /* Binary payloads the base receive functions reject before anything is compressed. The
     * SQLSTATEs are the observed ones: an encoding failure for text, jsonb's own parse error, and
     * an internal error for a jsonb version byte the server does not know. */
    {
        static const char bad_utf8[] = {(char) 0xff, (char) 0xfe, 'a'};
        static const char nul_text[] = {'a', 'b', '\0', 'c', 'd'};
        static const char jsonb_v2[] = {2, '{', '}'};
        static const char jsonb_bad[] = {1, 'n', 'o', 't', ' ', 'j', 's', 'o', 'n'};
        const char *body_sql = "INSERT INTO pq (id, body) VALUES (99, $1)";
        const char *meta_sql = "INSERT INTO pq (id, meta) VALUES (99, $1)";

        if (rejected(c, "invalid utf-8 text", body_sql, bad_utf8, sizeof bad_utf8, "22021")) return 1;
        if (rejected(c, "text with embedded NUL", body_sql, nul_text, sizeof nul_text, "22021")) return 1;
        if (rejected(c, "jsonb version byte 2", meta_sql, jsonb_v2, sizeof jsonb_v2, "XX000")) return 1;
        if (rejected(c, "jsonb body is not json", meta_sql, jsonb_bad, sizeof jsonb_bad, "22P02")) return 1;
    }

    /* Binary results are the logical values: every row, every column, byte for byte. */
    for (i = 0; i < (int) (sizeof expect / sizeof expect[0]); i++)
    {
        const char *want;
        int want_len;

        if (!(r = binrow(c, expect[i].id))) return fail(c, "binary select");
        if (expect[i].body == V_NULL)
        {
            if (!PQgetisnull(r, 0, 0) || !PQgetisnull(r, 0, 1) || !PQgetisnull(r, 0, 2))
            { PQclear(r); return wrong("NULL parameter", expect[i].id); }
            PQclear(r);
            continue;
        }
        want = kind_bytes(expect[i].body, &want_len);
        if (!same(r, 0, want, want_len)) { PQclear(r); return wrong("binary text result", expect[i].id); }
        if (!same(r, 1, jsonb_bin, jsonb_bin_len))
        { PQclear(r); return wrong("binary jsonb result", expect[i].id); }
        want = kind_bytes(expect[i].blob, &want_len);
        if (!same(r, 2, want, want_len)) { PQclear(r); return wrong("binary bytea result", expect[i].id); }
        PQclear(r);
    }

    /* raw_length counts UTF-8 bytes, not characters. */
    r = PQexec(c, "SELECT raw_length(body), raw_length(blob) FROM pq WHERE id = 9");
    if (PQresultStatus(r) != PGRES_TUPLES_OK || PQntuples(r) != 1) { PQclear(r); return fail(c, "raw_length"); }
    snprintf(id, sizeof id, "%d", uni_len);
    if (strcmp(PQgetvalue(r, 0, 0), id) != 0 || strcmp(PQgetvalue(r, 0, 1), id) != 0)
    { PQclear(r); return wrong("unicode raw_length", 9); }
    PQclear(r);

    PQfinish(c);
    puts("OK");
    return 0;
}
