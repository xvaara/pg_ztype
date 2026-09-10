#!/usr/bin/env python3
"""What moving a table to a new compression policy costs, by strategy.

Every row of a `ztext`, `zjsonb` or `zbytea` column carries the column's policy, so changing the
policy (a new dictionary, a new level) or catching up rows that were restored without their
dictionary means rewriting every row that does not have it. This benchmark measures the four ways
to do that on one table, from the same starting state (rows compressed at level 6 without a
dictionary) to the same end state (level 6 with a 110 kB dictionary), in a disposable cluster:

- `ALTER TABLE ... ALTER COLUMN ... TYPE` to the new modifier: one transaction under an
  `ACCESS EXCLUSIVE` lock, a new heap and new indexes;
- one whole-table `UPDATE t SET doc = doc::jsonb` into a column that already declares the
  policy (the "table-only restore, then catch up" shape from the README): one transaction,
  `ROW EXCLUSIVE`, every row a new tuple version, old versions left for `VACUUM`;
- the same `UPDATE` in primary-key batches, one transaction each, with the README's resumable
  predicate that skips rows already on the policy;
- `CREATE TABLE ... AS SELECT` with the coercion, then indexes, then drop and rename.

Each strategy runs with the primary key alone and with a second, expression index on a JSON key
(`(doc ->> 'sku')`, the "index the logical value" advice), because an update that touches an
indexed column cannot be a heap-only tuple (HOT) update. Reported per strategy: wall time, WAL
bytes written (`pg_current_wal_insert_lsn` before and after, after a `CHECKPOINT`, so first-touch
full-page images are included), heap and TOAST size before, after and after a plain `VACUUM`,
index size after, and how many updates were HOT. A rerun of the batched predicate must select
nothing, and every strategy must leave every row on the target policy with its content intact;
both are asserted. Tables are logged (WAL is the point), the cluster runs with `fsync` and
`synchronous_commit` off and autovacuum disabled; each measurement starts from a fresh copy of a
template database, so nothing carries over. Median of ZTYPE_BENCH_RUNS (default 3). Prints Markdown.
"""
import os
from pathlib import Path
import statistics
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

RUNS = int(os.environ.get('ZTYPE_BENCH_RUNS', 3))
SHAPES = [  # name, generator, rows, training rows, sample bytes
    ('small', 'erp_line(i)', int(os.environ.get('ZTYPE_BENCH_REWRITE_SMALL', 200000)), 20000, 8192),
    ('large', 'erp_document(i)', int(os.environ.get('ZTYPE_BENCH_REWRITE_LARGE', 4000)), 1500, 65536),
]
BATCHES = 20
INDEXES = [('primary key only', ''), ('+ expression index', "CREATE INDEX t_sku ON t ((doc ->> 'sku'));")]
# The README recipe: level, then raw rows (too short, or incompressible) pass and a frame must name
# the slot's dictionary, so a rerun selects nothing. Envelope and frame header only.
PREDICATE = "NOT ztype.matches_policy(doc, 6, 1)"
STRATEGIES = ['alter', 'update', 'batched', 'ctas']
LABELS = {'alter': '`ALTER COLUMN TYPE`', 'update': 'whole-table `UPDATE`',
          'batched': f'batched `UPDATE`, {BATCHES} transactions', 'ctas': '`CREATE TABLE AS`, indexes, rename'}


def mb(b):
    return f'{b / 1048576:,.0f} MB' if b >= 10 * 1048576 else f'{b / 1048576:,.1f} MB'


def measure(cluster, shape, strategy, index_sql, rows):
    """One strategy on a fresh copy of the shape's template database. Returns a dict of numbers."""
    admin = cluster.session()
    admin.query(f'CREATE DATABASE run TEMPLATE tmpl_{shape};')
    admin.close()
    s = cluster.session('run')
    try:
        s.query('SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0; SET max_parallel_maintenance_workers = 0;')
        # 'alter' and 'ctas' start from a column declared without the dictionary; the UPDATE
        # strategies from a column that declares it over rows stored without it.
        source = 'plain' if strategy in ('alter', 'ctas') else 'pinned'
        s.query(f'ALTER TABLE {source} RENAME TO t; ALTER INDEX {source}_pkey RENAME TO t_pkey;')
        s.query(f'DROP TABLE {"pinned" if source == "plain" else "plain"};')
        if index_sql:
            s.query(index_sql)
        s.query('VACUUM ANALYZE t; CHECKPOINT;')
        before = s.query("SELECT pg_table_size('t'), pg_indexes_size('t'), pg_current_wal_insert_lsn();").split('|')
        started = time.perf_counter()
        if strategy == 'alter':
            s.query("ALTER TABLE t ALTER COLUMN doc TYPE zjsonb(6, 'erp');")
        elif strategy == 'update':
            s.query('UPDATE t SET doc = doc::jsonb;')
        elif strategy == 'batched':
            step = -(-rows // BATCHES)
            for lo in range(1, rows + 1, step):
                s.query(f'UPDATE t SET doc = doc::jsonb WHERE id >= {lo} AND id < {lo + step} AND {PREDICATE};')
        else:
            s.query("CREATE TABLE t_new AS SELECT id, created, doc::zjsonb(6, 'erp') AS doc FROM t;"
                    'ALTER TABLE t_new ADD PRIMARY KEY (id);'
                    + (index_sql.replace(' ON t ', ' ON t_new ').replace('t_sku', 't_new_sku') if index_sql else '')
                    + 'DROP TABLE t; ALTER TABLE t_new RENAME TO t;')
        elapsed = time.perf_counter() - started
        after = s.query("SELECT pg_table_size('t'), pg_indexes_size('t'), pg_current_wal_insert_lsn();").split('|')
        wal = int(s.query(f"SELECT pg_wal_lsn_diff('{after[2]}', '{before[2]}');"))
        s.query('SELECT pg_stat_force_next_flush();')
        stats = s.query("SELECT coalesce(sum(n_tup_upd), 0), coalesce(sum(n_tup_hot_upd), 0), coalesce(sum(n_dead_tup), 0)"
                        " FROM pg_stat_user_tables WHERE relname = 't';").split('|')
        # End state: every row on the target policy, content intact, and the rerun predicate empty.
        s.equal(f'SELECT count(*) FROM t WHERE {PREDICATE};', '0')
        s.equal("SELECT bool_and(i.level = 6 AND i.dict_slot = 1) FROM t, ztype.inspect(t.doc) i WHERE i.codec = 'zstd';")
        s.equal('SELECT bool_and(md5(t.doc::jsonb::text) = s.h) AND count(*) = (SELECT count(*) FROM src) FROM t JOIN src s USING (id);')
        s.query('VACUUM t;')
        settled = int(s.query("SELECT pg_table_size('t');"))
    finally:
        s.close()
    admin = cluster.session()
    admin.query('DROP DATABASE run;')
    admin.close()
    return {'secs': elapsed, 'wal': wal, 'heap_before': int(before[0]), 'heap_after': int(after[0]),
            'heap_settled': settled, 'index_after': int(after[1]), 'upd': int(stats[0]), 'hot': int(stats[1]),
            'dead': int(stats[2])}


def median(results, key):
    return statistics.median(r[key] for r in results)


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-rewrite-') as tmp:
        work = Path(tmp)
        c = Cluster(Path(t.BIN), work, 'pg', 55476, share(work, 'share', t.library().with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '512MB'\nmax_wal_size = '8GB'\ncheckpoint_timeout = '1h'\n"
                       "fsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            info = {}
            for shape, gen, rows, train_n, sample in SHAPES:
                admin = c.session()
                admin.query(f'CREATE DATABASE tmpl_{shape};')
                admin.close()
                s = c.session(f'tmpl_{shape}')
                s.query('SET statement_timeout = 0;')
                s.query(bench.SETUP)
                s.query(f'SELECT setseed(0.7); CREATE TABLE train AS SELECT {gen} AS doc FROM generate_series(1, {train_n}) i;')
                s.query(f"SELECT setseed(0.3); CREATE TABLE src AS SELECT i AS id, timestamptz '2024-01-01' + i * interval '1 minute' AS created,"
                        f' {gen} AS doc FROM generate_series(1, {rows}) i;')
                s.query('ALTER TABLE src ADD COLUMN h text; UPDATE src SET h = md5(doc::text); ALTER TABLE src ADD PRIMARY KEY (id);')
                # Both start tables hold identical (6, 0) frames. `pinned` declares slot 1 before it exists,
                # which is the table-only-restore state; the one WARNING per transaction is expected.
                s.query('CREATE TABLE plain (id integer PRIMARY KEY, created timestamptz, doc zjsonb(6));'
                        'INSERT INTO plain SELECT id, created, doc FROM src;')
                s.query('CREATE TABLE pinned (id integer PRIMARY KEY, created timestamptz, doc zjsonb(6, 1));'
                        'INSERT INTO pinned SELECT id, created, doc FROM src;')
                s.equal('SELECT count(*) FROM pinned p, ztype.inspect(p.doc) i WHERE i.dict_slot IS NOT NULL;', '0')
                s.equal('SELECT (SELECT sum(pg_column_size(doc)) FROM plain) = (SELECT sum(pg_column_size(doc)) FROM pinned);')
                s.query(f"SELECT ztype.import_dictionary(1, 'erp', ztype.train_dictionary('SELECT doc FROM train', 112640, {sample}));")
                s.query('DROP TABLE train; VACUUM ANALYZE;')
                info[shape] = {
                    'rows': rows, 'avg': int(s.query('SELECT avg(octet_length(doc::text))::int FROM src;')),
                    'heap_jsonb': int(s.query("SELECT pg_table_size('src');")),
                    'heap_plain': int(s.query("SELECT pg_table_size('plain');")),
                    'dict_bytes': int(s.query("SELECT octet_length(dict) FROM ztype.dictionaries WHERE slot = 1;")),
                    'raw_rows': int(s.query("SELECT count(*) FROM plain p, ztype.inspect(p.doc) i WHERE i.codec = 'raw';")),
                }
                s.close()
            results = {}
            for shape, _, rows, _, _ in SHAPES:
                for strategy in STRATEGIES:
                    for label, index_sql in INDEXES:
                        results[(shape, strategy, label)] = [measure(c, shape, strategy, index_sql, rows) for _ in range(RUNS)]
            s = c.session()
            ver = s.query('SELECT version();')
            s.close()
        finally:
            c.stop()

    print(f'\n{ver}; {bench.machine()}. Median of {RUNS} runs, each on a fresh copy of the table; logged tables, '
          '`full_page_writes` on, a `CHECKPOINT` before each measurement, autovacuum off, `fsync` and '
          '`synchronous_commit` off, 512 MB of `shared_buffers`. From `zjsonb(6)` to `zjsonb(6, \'erp\')`.\n')
    for shape, _, rows, _, _ in SHAPES:
        i = info[shape]
        print(f"**{shape}: {i['rows']:,} rows × {i['avg']:,} bytes of JSON text**, {mb(i['heap_plain'])} of heap and TOAST as "
              f"`zjsonb(6)` ({mb(i['heap_jsonb'])} as `jsonb`), {i['raw_rows']} rows stored raw, dictionary {i['dict_bytes'] // 1024} kB.\n")
        print('| strategy | indexes | time | WAL | heap+TOAST before → after → after `VACUUM` | indexes after | HOT updates |')
        print('|---|---|---:|---:|---|---:|---:|')
        for strategy in STRATEGIES:
            for label, _ in INDEXES:
                rs = results[(shape, strategy, label)]
                hot = f"{median(rs, 'hot'):,.0f} of {median(rs, 'upd'):,.0f}" if strategy in ('update', 'batched') else '—'
                print(f"| {LABELS[strategy]} | {label} | {median(rs, 'secs'):.1f} s | {mb(median(rs, 'wal'))} | "
                      f"{mb(median(rs, 'heap_before'))} → {mb(median(rs, 'heap_after'))} → {mb(median(rs, 'heap_settled'))} | "
                      f"{mb(median(rs, 'index_after'))} | {hot} |")
        print()


if __name__ == '__main__':
    main()
