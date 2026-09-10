/* Parameterised INSERT throughput over libpq for tests/bench_params.py: one prepared statement,
 * one text parameter per row read from a file (one value per line), in a single transaction.
 * Pipelined mode overlaps round trips so the time is the server's work; plain mode waits for
 * each row, which is what an application without pipelining sees.
 * usage: param_insert <sql with $1> <values file> <pipelined|plain>   (connection from PG*)
 * prints: rows, elapsed seconds.
 */
#include <libpq-fe.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static double now(void)
{
    struct timespec t;
    clock_gettime(CLOCK_MONOTONIC, &t);
    return t.tv_sec + t.tv_nsec * 1e-9;
}

static void fail(PGconn *c, const char *what)
{
    fprintf(stderr, "%s: %s\n", what, PQerrorMessage(c));
    exit(1);
}

static void expect(PGconn *c, ExecStatusType status, const char *what)
{
    PGresult *r = PQgetResult(c);
    if (!r || PQresultStatus(r) != status)
    {
        fprintf(stderr, "%s: %s\n", what, r ? PQresultErrorMessage(r) : "no result");
        exit(1);
    }
    PQclear(r);
}

int main(int argc, char **argv)
{
    PGconn *c;
    FILE *f;
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    long rows = 0;
    int pipelined;
    double t0;
    if (argc != 4) { fprintf(stderr, "usage: param_insert <sql> <file> <pipelined|plain>\n"); return 2; }
    pipelined = strcmp(argv[3], "pipelined") == 0;
    c = PQconnectdb("");
    if (PQstatus(c) != CONNECTION_OK) fail(c, "connect");
    f = fopen(argv[2], "r");
    if (!f) { perror(argv[2]); return 1; }
    {
        PGresult *r = PQprepare(c, "ins", argv[1], 1, NULL);
        if (PQresultStatus(r) != PGRES_COMMAND_OK) { fprintf(stderr, "prepare: %s\n", PQresultErrorMessage(r)); return 1; }
        PQclear(r);
        r = PQexec(c, "BEGIN");
        PQclear(r);
    }
    t0 = now();
    if (pipelined && !PQenterPipelineMode(c)) fail(c, "pipeline");
    while ((len = getline(&line, &cap, f)) > 0)
    {
        const char *values[1];
        int lengths[1];
        if (line[len - 1] == '\n') len--;
        values[0] = line;
        lengths[0] = (int) len;
        if (!PQsendQueryPrepared(c, "ins", 1, values, lengths, NULL, 0)) fail(c, "send");
        rows++;
        if (pipelined)
        {
            if (rows % 256 == 0)
            {
                long i;
                if (!PQpipelineSync(c)) fail(c, "sync");
                for (i = 0; i < 256; i++) { expect(c, PGRES_COMMAND_OK, "insert"); if (PQgetResult(c) != NULL) { fprintf(stderr, "extra result\n"); return 1; } }
                expect(c, PGRES_PIPELINE_SYNC, "sync");
            }
        }
        else
        {
            expect(c, PGRES_COMMAND_OK, "insert");
            if (PQgetResult(c) != NULL) { fprintf(stderr, "extra result\n"); return 1; }
        }
    }
    if (pipelined)
    {
        long i, rest = rows % 256;
        if (!PQpipelineSync(c)) fail(c, "sync");
        for (i = 0; i < rest; i++) { expect(c, PGRES_COMMAND_OK, "insert"); if (PQgetResult(c) != NULL) { fprintf(stderr, "extra result\n"); return 1; } }
        expect(c, PGRES_PIPELINE_SYNC, "sync");
        if (!PQexitPipelineMode(c)) fail(c, "exit pipeline");
    }
    {
        PGresult *r = PQexec(c, "COMMIT");
        if (PQresultStatus(r) != PGRES_COMMAND_OK) { fprintf(stderr, "commit: %s\n", PQresultErrorMessage(r)); return 1; }
        PQclear(r);
    }
    printf("%ld %.6f\n", rows, now() - t0);
    PQfinish(c);
    return 0;
}
