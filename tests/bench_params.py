#!/usr/bin/env python3
"""Parameter inserts: what the double compression of an untyped parameter costs per row.

An application inserts with `INSERT ... VALUES ($1)`. The parameter arrives with no type
modifier, so the type input function compresses it at the default policy and the column's
coercion then decompresses and compresses it again with the column's level and dictionary.
Typing the parameter as the base type (`$1::jsonb`) routes it through the base-type cast,
which receives the column modifier and compresses once. This benchmark measures both shapes,
over libpq (tests/param_insert.c), into a plain jsonb column, a default zjsonb column (where
the untyped parameter needs no coercion) and a zjsonb column with a dictionary, using the
benchmark's small ERP documents. Pipelined runs overlap round trips and show the server's
cost per row; plain runs wait for each row, which is what an application without pipelining
pays, round trip included. Median of ZTYPE_BENCH_RUNS (default 5) after a warm-up, each run
into a fresh unlogged table, in one transaction. Runs in a disposable cluster; prints Markdown.
"""
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

ROWS = int(os.environ.get('ZTYPE_BENCH_PARAMS', 20000))
RUNS = int(os.environ.get('ZTYPE_BENCH_RUNS', 5))
COLUMNS = [('`jsonb`', 'jsonb'), ('`zjsonb(6)`', 'zjsonb(6)'), ("`zjsonb(6, 'erp')`", "zjsonb(6, 'erp')")]
SHAPES = [('untyped `$1`', 'INSERT INTO {table} VALUES ($1)'), ('typed `$1::jsonb`', 'INSERT INTO {table} VALUES ($1::jsonb)')]


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-params-') as tmp:
        work = Path(tmp)
        c = Cluster(Path(t.BIN), work, 'pg', 55474, share(work, 'share', t.library().with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '256MB'\nfsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            exe = work / 'param_insert'
            inc, lib = t.run([t.PG_CONFIG, '--includedir']), t.run([t.PG_CONFIG, '--libdir'])
            t.run(['cc', '-O2', '-o', exe, t.ROOT / 'tests' / 'param_insert.c', f'-I{inc}', f'-L{lib}', '-lpq'])
            s = c.session()
            s.query('SET statement_timeout = 0;')
            s.query(bench.SETUP)
            s.query(f"SELECT setseed(0.7); CREATE TABLE train AS SELECT erp_line(i) AS doc FROM generate_series(1, 20000) i;"
                    "SELECT ztype.add_dictionary('erp', ztype.train_dictionary('SELECT doc FROM train', 112640, 8192));")
            s.query(f"SELECT setseed(0.8); CREATE TABLE src AS SELECT erp_line(i) AS doc FROM generate_series(1, {ROWS}) i;")
            values = work / 'values.txt'
            t.run([t.BIN / 'psql', '-XqAt', '-c', 'SELECT doc::text FROM src', '-o', values], env=c.env)
            avg = int(s.query('SELECT avg(octet_length(doc::text))::int FROM src;'))
            results = {}
            for mode in ('pipelined', 'plain'):
                for col_label, coltype in COLUMNS:
                    for shape_label, sql in SHAPES:
                        secs = []
                        for i in range(RUNS + 1):
                            s.query(f'DROP TABLE IF EXISTS target; CREATE UNLOGGED TABLE target (doc {coltype});')
                            out = t.run([exe, sql.format(table='target'), values, mode], env=c.env)
                            rows, elapsed = out.split()
                            assert int(rows) == ROWS, out
                            if i:
                                secs.append(float(elapsed))
                        results[(mode, col_label, shape_label)] = statistics.median(secs) / ROWS * 1e6
                        s.equal('SELECT count(*) FROM target;', str(ROWS))
            ver = s.query('SELECT version();')
            s.close()
        finally:
            c.stop()
    print(f'\n{ver}; {bench.machine()}. {ROWS:,} rows of {avg} bytes per document, median of {RUNS} runs after a warm-up, µs per row.\n')
    for mode, note in (('pipelined', 'pipelined: server cost per row'), ('plain', 'one round trip per row over a Unix socket')):
        print(f'| {note} | ' + ' | '.join(label for label, _ in COLUMNS) + ' |')
        print('|---|' + '---:|' * len(COLUMNS))
        for shape_label, _ in SHAPES:
            print(f'| {shape_label} | ' + ' | '.join(f'{results[(mode, col, shape_label)]:.1f}' for col, _ in COLUMNS) + ' |')
        print()


if __name__ == '__main__':
    main()
