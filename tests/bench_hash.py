#!/usr/bin/env python3
"""What the equality and hash operator classes cost per row, against the base type and the cast.

`=` and the hash support functions compare decoded values, so a GROUP BY, a hash join or an
equality filter on a compressed column decodes every row at least once, where the base type
compares bytes it already has. This benchmark puts numbers on that for the size benchmark's small
ERP documents, stored as `jsonb`, `zjsonb(6)`, `zjsonb(6)` with a dictionary and, as text, `text`,
`ztext(6)`, `ztext(6)` with a dictionary. Rows repeat each distinct document ZTYPE_BENCH_HASH_REPEAT
times in interleaved order, so hash aggregation runs the equality function on every row after the
first of its group. Per variant: GROUP BY on the column (and, for the compressed types, on the cast
to the base type, which was the only way before the operator classes), a hash join against a table
holding one row per distinct value, an equality filter against one constant, and ANALYZE, which
gathers distinct-value statistics through the equality operator and did nothing on these columns
before. The backend's decode-cache misses per row say how many decodes each row cost. Sorting is
disabled so every variant takes the hash path. Median of ZTYPE_BENCH_RUNS (default 5) after a
warm-up, unlogged tables, in a disposable cluster; prints Markdown.
"""
import os
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

ROWS = int(os.environ.get('ZTYPE_BENCH_HASH', 200000))
REPEAT = int(os.environ.get('ZTYPE_BENCH_HASH_REPEAT', 20))
FAMILIES = [('jsonb', 'jsonb', [('`jsonb`', 'jsonb'), ('`zjsonb(6)`', 'zjsonb(6)'), ("`zjsonb(6, 'erp')`", "zjsonb(6, 'erp')")]),
            ('text', 'text', [('`text`', 'text'), ('`ztext(6)`', 'ztext(6)'), ("`ztext(6, 'erp-text')`", "ztext(6, 'erp-text')")])]


def misses_per_row(s, sql):
    before = int(s.query('SELECT misses FROM ztype.decode_cache_stats();'))
    s.query(sql)
    return (int(s.query('SELECT misses FROM ztype.decode_cache_stats();')) - before) / ROWS


def main():
    distinct = max(ROWS // REPEAT, 1)
    with tempfile.TemporaryDirectory(prefix='ztype-bench-') as tmp:
        work = Path(tmp)
        lib = next(t.ROOT / f'ztype{x}' for x in ('.so', '.dylib') if (t.ROOT / f'ztype{x}').exists())
        c = Cluster(Path(t.BIN), work, 'pg', 55478, share(work, 'share', lib.with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '512MB'\nfsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            s = c.session()
            s.query('SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0; SET enable_sort = off;')
            s.query(bench.SETUP)
            s.query(f'SELECT setseed(0.5); CREATE TABLE keys_src AS SELECT i, erp_line(i) AS doc FROM generate_series(1, {distinct}) i;')
            s.query(f'CREATE TABLE src AS SELECT k.doc FROM generate_series(1, {ROWS}) r JOIN keys_src k ON k.i = 1 + r % {distinct} ORDER BY r;')
            s.query('SELECT setseed(0.9); CREATE TABLE train AS SELECT erp_line(i) AS doc FROM generate_series(1, 20000) i;')
            s.query("SELECT ztype.add_dictionary('erp', ztype.train_dictionary('SELECT doc FROM train', 112640, 8192));")
            s.query("SELECT ztype.add_dictionary('erp-text', ztype.train_dictionary('SELECT doc::text FROM train', 112640, 8192));")
            env = {'PostgreSQL': s.query('SELECT version();'), 'zstd': s.query('SELECT ztype.zstd_version();'),
                   'ztype': s.query("SELECT extversion FROM pg_extension WHERE extname = 'ztype';")}
            results = []
            for family, base, variants in FAMILIES:
                rows = []
                for label, decl in variants:
                    table = 'v' + str(len(results)) + '_' + str(len(rows))
                    s.query(f'CREATE UNLOGGED TABLE {table} (doc {decl}); INSERT INTO {table} SELECT doc::text::{base} FROM src; '
                            f'CREATE UNLOGGED TABLE {table}_keys (doc {decl}); INSERT INTO {table}_keys SELECT doc::text::{base} FROM keys_src;')
                    size = int(s.query(f"SELECT pg_total_relation_size('{table}');"))
                    group_sql = f'SELECT count(*) FROM (SELECT doc FROM {table} GROUP BY doc) q;'
                    cast_sql = f'SELECT count(*) FROM (SELECT doc::{base} FROM {table} GROUP BY doc::{base}) q;'
                    join_sql = f'SELECT count(*) FROM {table} v JOIN {table}_keys k ON k.doc = v.doc;'
                    filter_sql = f'SELECT count(*) FROM {table} WHERE doc = (SELECT doc FROM {table}_keys WHERE doc IS NOT NULL LIMIT 1);'
                    for sql, node in ((group_sql, 'HashAggregate'), (join_sql, 'Hash Join')):
                        plan = s.query('EXPLAIN (COSTS OFF) ' + sql)
                        assert node in plan, (label, plan)
                    assert s.query(group_sql) == str(distinct) and s.query(join_sql) == str(ROWS), label
                    group = bench.timed(s, group_sql)
                    cast = bench.timed(s, cast_sql) if decl != base else None
                    join = bench.timed(s, join_sql)
                    filt = bench.timed(s, filter_sql)
                    analyze = bench.timed(s, f'ANALYZE {table};')
                    stats = s.query(f"SELECT n_distinct::text || ' distinct, ' || coalesce(array_length(most_common_vals::text::text[], 1), 0) || ' MCVs' "
                                    f"FROM pg_stats WHERE tablename = '{table}' AND attname = 'doc';")
                    misses = (misses_per_row(s, group_sql), misses_per_row(s, join_sql), misses_per_row(s, filter_sql)) if decl != base else None
                    rows.append((label, size, group, cast, join, filt, analyze, stats, misses))
                results.append((family, rows))
            s.close()
        finally:
            c.stop()
    print(f"\n{env['PostgreSQL']}; libzstd {env['zstd']}; ztype {env['ztype']}; {bench.machine()}.")
    print(f'{ROWS:,} rows of the small ERP documents, {distinct:,} distinct values each repeated {REPEAT} times in interleaved order; '
          f'enable_sort = off, default_statistics_target = 100. Microseconds per row, median of {bench.RUNS} runs after a warm-up.\n')
    for family, rows in results:
        print(f'| {family} column | size | GROUP BY column | GROUP BY cast | hash join | `= constant` | ANALYZE | statistics gathered |')
        print('|---|---|---|---|---|---|---|---|')
        for label, size, group, cast, join, filt, analyze, stats, misses in rows:
            print(f'| {label} | {bench.mb(size)} | {bench.per_row(group, ROWS)} | {bench.per_row(cast, ROWS) if cast else "—"} | '
                  f'{bench.per_row(join, ROWS)} | {bench.per_row(filt, ROWS)} | {analyze[0]:.2f} s | {stats} |')
        print()
        print(f'Decodes per row (backend decode-cache misses) for {family}: ' + '; '.join(
            f'{label} {m[0]:.2f} in GROUP BY, {m[1]:.2f} in the join, {m[2]:.2f} in the filter' for label, *_, m in rows if m) + '.\n')


if __name__ == '__main__':
    main()
