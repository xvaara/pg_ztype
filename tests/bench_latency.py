#!/usr/bin/env python3
"""Request latency: what one short statement costs, and what a dictionary adds to it per transaction.

The size benchmark (`make bench`) runs whole tables in one statement; applications mostly run one
short statement per transaction over a pooled connection. Two things differ there. Every statement
pays a round trip, and the dictionary cache is transaction-local, so a transaction that touches a
dictionary column loads the dictionary from the registry again: one lookup plus building the zstd
decompression object (and, for a write, the compression object). This benchmark measures single
statements over libpq (tests/latency_probe.c): a typed-parameter insert, a point read of the whole
value and a point read of one key, each as its own transaction on a persistent connection and, for
comparison, batched inside one transaction where the cache is warm. Columns are plain `jsonb`,
`zjsonb(6)`, `zjsonb(6)` with a 110 kB dictionary and with an 8 kB one, using the benchmark's small
ERP documents; a second table holds four dictionary columns so the per-dictionary cost shows as a
multiple. A last table opens a new connection for every statement, the shape of an application
without a pool. The probe reports the backend's dictionary load count, so the per-transaction reload
is a measured count. Median of ZTYPE_BENCH_RUNS (default 5) after a warm-up, unlogged tables, in a
disposable cluster; prints Markdown.
"""
import os
from pathlib import Path
import random
import statistics
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

ROWS = int(os.environ.get('ZTYPE_BENCH_LATENCY', 10000))
RECONNECTS = int(os.environ.get('ZTYPE_BENCH_RECONNECTS', 200))
RUNS = int(os.environ.get('ZTYPE_BENCH_RUNS', 5))
COLUMNS = [('`jsonb`', 'jsonb'), ('`zjsonb(6)`', 'zjsonb(6)'),
           ("`zjsonb(6, 'erp')`, 110 kB", "zjsonb(6, 'erp')"), ("`zjsonb(6, 'erp8')`, 8 kB", "zjsonb(6, 'erp8')")]
WIDE = [('`jsonb` ×4', ['jsonb'] * 4), ('`zjsonb(6)` ×4', ['zjsonb(6)'] * 4),
        ('four dictionaries', [f"zjsonb(6, 'd{i}')" for i in range(1, 5)])]
MODES = [('autocommit', 'one statement per transaction'), ('batched', 'one transaction, cache warm')]


def median_us(runs, n):
    return statistics.median(runs) / n * 1e6


def measure(exe, env, mode, sql, values, n, reset=None):
    secs, loads = [], None
    for i in range(RUNS + 1):
        if reset:
            reset()
        out = t.run([exe, mode, sql, values], env=env)
        rows, elapsed, loads = out.split()
        assert int(rows) == n, out
        if i:
            secs.append(float(elapsed))
    return median_us(secs, n), int(loads)


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-latency-') as tmp:
        work = Path(tmp)
        c = Cluster(Path(t.BIN), work, 'pg', 55475, share(work, 'share', t.library().with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '256MB'\nfsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            exe = work / 'latency_probe'
            inc, lib = t.run([t.PG_CONFIG, '--includedir']), t.run([t.PG_CONFIG, '--libdir'])
            t.run(['cc', '-O2', '-o', exe, t.ROOT / 'tests' / 'latency_probe.c', f'-I{inc}', f'-L{lib}', '-lpq'])
            s = c.session()
            s.query('SET statement_timeout = 0;')
            s.query(bench.SETUP)
            # Six dictionaries from disjoint training sets: erp and d1..d4 at 110 kB, erp8 at 8 kB.
            for i, (name, size) in enumerate([('erp', 112640), ('erp8', 8192), ('d1', 112640), ('d2', 112640), ('d3', 112640), ('d4', 112640)]):
                s.query(f"SELECT setseed({0.11 + i / 20}); CREATE TABLE train_{name} AS SELECT erp_line(i) AS doc FROM generate_series(1, 20000) i;"
                        f"SELECT ztype.add_dictionary('{name}', ztype.train_dictionary('SELECT doc FROM train_{name}', {size}, 8192));")
            dict_sizes = s.query("SELECT string_agg(name || '=' || octet_length(dict), ', ' ORDER BY slot) FROM ztype.dictionaries;")
            s.query(f"SELECT setseed(0.8); CREATE TABLE src AS SELECT erp_line(i) AS doc FROM generate_series(1, {ROWS}) i;")
            values = work / 'values.txt'
            t.run([t.BIN / 'psql', '-XqAt', '-c', 'SELECT doc::text FROM src', '-o', values], env=c.env)
            avg = int(s.query('SELECT avg(octet_length(doc::text))::int FROM src;'))
            ids = list(range(1, ROWS + 1))
            random.Random(3).shuffle(ids)
            id_file = work / 'ids.txt'
            id_file.write_text(''.join(f'{i}\n' for i in ids))
            few = work / 'few.txt'
            few.write_text(''.join(f'{i}\n' for i in ids[:RECONNECTS]))
            # Read tables are filled once through the typed path; the same rows for every column type.
            for k, (_, coltype) in enumerate(COLUMNS):
                s.query(f'CREATE UNLOGGED TABLE read_{k} (id integer PRIMARY KEY, doc {coltype});'
                        f'INSERT INTO read_{k} SELECT row_number() OVER (), doc::text::jsonb FROM src;')
            for k, (_, cols) in enumerate(WIDE):
                decl = ', '.join(f'c{j} {ct}' for j, ct in enumerate(cols))
                s.query(f'CREATE UNLOGGED TABLE wide_read_{k} (id integer PRIMARY KEY, {decl});'
                        f"INSERT INTO wide_read_{k} SELECT row_number() OVER (), {', '.join(['doc::text::jsonb'] * 4)} FROM src;")

            def fresh(table, decl):
                return lambda: s.query(f'DROP TABLE IF EXISTS {table}; CREATE UNLOGGED TABLE {table} ({decl});')

            results = {}
            for mode, _ in MODES:
                for k, (label, coltype) in enumerate(COLUMNS):
                    results[(mode, label, 'insert')] = measure(exe, c.env, mode, 'INSERT INTO target VALUES ($1::jsonb)', values, ROWS,
                                                               reset=fresh('target', f'doc {coltype}'))
                    results[(mode, label, 'read')] = measure(exe, c.env, mode, f'SELECT doc::jsonb FROM read_{k} WHERE id = $1', id_file, ROWS)
                    results[(mode, label, 'key')] = measure(exe, c.env, mode, f"SELECT doc ->> 'sku' FROM read_{k} WHERE id = $1", id_file, ROWS)
                for k, (label, cols) in enumerate(WIDE):
                    decl = ', '.join(f'c{j} {ct}' for j, ct in enumerate(cols))
                    results[(mode, label, 'insert')] = measure(exe, c.env, mode, f"INSERT INTO wide VALUES ({', '.join(['$1::jsonb'] * 4)})", values, ROWS,
                                                               reset=fresh('wide', decl))
                    results[(mode, label, 'read')] = measure(exe, c.env, mode, f'SELECT c0::jsonb, c1::jsonb, c2::jsonb, c3::jsonb FROM wide_read_{k} WHERE id = $1', id_file, ROWS)
            for label, coltype in (COLUMNS[0], COLUMNS[2]):
                results[('reconnect', label, 'insert')] = measure(exe, c.env, 'reconnect', 'INSERT INTO target VALUES ($1::jsonb)', few, RECONNECTS,
                                                                  reset=fresh('target', f'doc {coltype}'))
                results[('reconnect', label, 'read')] = measure(exe, c.env, 'reconnect', f'SELECT doc::jsonb FROM read_{COLUMNS.index((label, coltype))} WHERE id = $1', few, RECONNECTS)
            ver = s.query('SELECT version();')
            s.close()
        finally:
            c.stop()

    print(f'\n{ver}; {bench.machine()}. {ROWS:,} statements of one {avg}-byte document each, prepared statement over a Unix socket, '
          f'median of {RUNS} runs after a warm-up, µs per statement. Dictionaries: {dict_sizes} bytes.\n')
    ops = [('insert, typed `$1::jsonb`', 'insert'), ('point read, whole value', 'read'), ("point read, one key `->>`", 'key')]
    for mode, note in MODES:
        print(f'| {note} | ' + ' | '.join(label for label, _ in COLUMNS) + ' |')
        print('|---|' + '---:|' * len(COLUMNS))
        for op_label, op in ops:
            print(f'| {op_label} | ' + ' | '.join(f'{results[(mode, col, op)][0]:.1f}' for col, _ in COLUMNS) + ' |')
        print()
    print('Dictionary loads per statement (from `ztype.dictionary_cache_stats()` in the probe\'s backend):\n')
    print('| | ' + ' | '.join(label for label, _ in COLUMNS[1:]) + ' |')
    print('|---|' + '---:|' * (len(COLUMNS) - 1))
    for mode, note in MODES:
        for op_label, op in ops:
            print(f'| {note}, {op_label} | ' + ' | '.join(f'{results[(mode, col, op)][1] / ROWS:.2f}' for col, _ in COLUMNS[1:]) + ' |')
    print()
    print('| four columns per row | ' + ' | '.join(label for label, _ in WIDE) + ' |')
    print('|---|' + '---:|' * len(WIDE))
    for mode, note in MODES:
        for op_label, op in (('insert', 'insert'), ('point read, four values', 'read')):
            print(f'| {note}, {op_label} | ' + ' | '.join(f'{results[(mode, col, op)][0]:.1f}' for col, _ in WIDE) + ' |')
    print()
    print(f'| new connection per statement ({RECONNECTS:,} statements) | ' + ' | '.join(label for label, _ in (COLUMNS[0], COLUMNS[2])) + ' |')
    print('|---|---:|---:|')
    for op_label, op in (('insert', 'insert'), ('point read, whole value', 'read')):
        print(f'| {op_label} | ' + ' | '.join(f'{results[("reconnect", col, op)][0]:.0f}' for col, _ in (COLUMNS[0], COLUMNS[2])) + ' |')
    print()


if __name__ == '__main__':
    main()
