/* Per-statement latency over libpq for tests/bench_latency.py: one prepared statement with one text
 * parameter per line of a file, executed synchronously so every statement pays its round trip, the
 * way an application without pipelining does. Modes: autocommit runs each statement as its own
 * transaction on one connection; batched wraps the whole file in one transaction; reconnect opens a
 * new connection for every statement and runs it unprepared, the shape of an application with no
 * pool. Results are consumed and discarded. After the loop the same connection reports how many
 * dictionaries the backend loaded, so a per-transaction reload shows up as a count, not a guess
 * (-1 in reconnect mode, where every statement had a fresh backend).
 * usage: latency_probe <autocommit|batched|reconnect> <sql with $1> <values file>   (connection from PG*)
 * prints: rows, elapsed seconds, dictionary loads.
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

static PGconn *connect_or_die(void)
{
    PGconn *c = PQconnectdb("");
    if (PQstatus(c) != CONNECTION_OK) { fprintf(stderr, "connect: %s\n", PQerrorMessage(c)); exit(1); }
    return c;
}

static void ok(PGresult *r, const char *what)
{
    ExecStatusType s = PQresultStatus(r);
    if (s != PGRES_COMMAND_OK && s != PGRES_TUPLES_OK) { fprintf(stderr, "%s: %s\n", what, PQresultErrorMessage(r)); exit(1); }
    PQclear(r);
}

int main(int argc, char **argv)
{
    PGconn *c = NULL;
    FILE *f;
    char *line = NULL;
    size_t cap = 0;
    ssize_t len;
    long rows = 0, loads = -1;
    int batched, reconnect;
    double t0;
    if (argc != 4) { fprintf(stderr, "usage: latency_probe <autocommit|batched|reconnect> <sql> <file>\n"); return 2; }
    batched = strcmp(argv[1], "batched") == 0;
    reconnect = strcmp(argv[1], "reconnect") == 0;
    f = fopen(argv[3], "r");
    if (!f) { perror(argv[3]); return 1; }
    if (!reconnect)
    {
        c = connect_or_die();
        ok(PQprepare(c, "st", argv[2], 1, NULL), "prepare");
        if (batched) ok(PQexec(c, "BEGIN"), "begin");
    }
    t0 = now();
    while ((len = getline(&line, &cap, f)) > 0)
    {
        const char *values[1];
        int lengths[1];
        if (line[len - 1] == '\n') len--;
        values[0] = line;
        lengths[0] = (int) len;
        if (reconnect)
        {
            c = connect_or_die();
            ok(PQexecParams(c, argv[2], 1, NULL, values, lengths, NULL, 0), "statement");
            PQfinish(c);
        }
        else
            ok(PQexecPrepared(c, "st", 1, values, lengths, NULL, 0), "statement");
        rows++;
    }
    if (batched) ok(PQexec(c, "COMMIT"), "commit");
    t0 = now() - t0;
    if (!reconnect)
    {
        PGresult *r = PQexec(c, "SELECT loads FROM ztype.dictionary_cache_stats()");
        if (PQresultStatus(r) == PGRES_TUPLES_OK && PQntuples(r) == 1) loads = atol(PQgetvalue(r, 0, 0));
        PQclear(r);
        PQfinish(c);
    }
    printf("%ld %.6f %ld\n", rows, t0, loads);
    return 0;
}
