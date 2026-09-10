#!/usr/bin/env python3
"""Dictionary evaluation report: would a trained dictionary pay off on your data?

Takes one column of text, jsonb or bytea, either from a query against any database (`--source`
is a libpq connection string or URI, `--query` yields exactly one column) or from the benchmark's
synthetic corpora (`--synthetic small|large|random`), copies the sample into a disposable cluster
built from this tree, splits it into a training part and a held-out part (interleaved by row, or
`--split tail` to hold out the end of the query's order, which is how a dictionary ages when the
query is ordered by time), trains a dictionary on the training part only, and measures the
held-out part:

- per level, with and without the dictionary: stored bytes, ratio to raw, compression and
  decompression cost per value in microseconds (statement time minus a scan of the same rows,
  median of `--runs`);
- per value-size bucket at `--level`: values, raw bytes, stored bytes and ratio with and without
  the dictionary, since a dictionary earns its keep on small values and little on large ones;
- the dictionary's own cost: bytes, training time and the training backend's peak resident
  memory, per-backend cache bytes at `--level`, and the number of held-out-sized values after
  which its bytes are paid back.

Training rows are fed in a deterministic pseudo-random order across the training part by default
(`--train-order spread`), because training stops at 20,000 samples or 64 MB and a time-ordered
query would otherwise train on its oldest rows only; `--train-order source` keeps the query's order.

Nothing is registered in the source database; the source connection runs one read-only query.
Rows above `--limit` are not fetched. The report is Markdown on stdout. Requires `make`.
"""
import argparse
import os
from pathlib import Path
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, PeakRss, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

ZTYPE = {'text': 'ztext', 'jsonb': 'zjsonb', 'bytea': 'zbytea'}
DECODE = {'text': 'octet_length(z::text)', 'bytea': 'octet_length(z::bytea)', 'jsonb': 'pg_column_size(z::jsonb)'}
RAW = {'text': 'raw_length(v::ztext(1))', 'bytea': 'raw_length(v::zbytea(1))', 'jsonb': '(ztype.inspect(v::zjsonb(1))).raw_length'}
BUCKETS = [(0, 256), (256, 1024), (1024, 4096), (4096, 16384), (16384, 65536), (65536, None)]
SYNTHETIC = {'small': ('erp_line(i)', 20000), 'large': ('erp_document(i)', 1500), 'random': ('random_doc(i)', 20000)}


def fetch(source, query, limit, out):
    """One read-only psql session against the source: the column type through a temporary view,
    then the rows in COPY text form. Returns the base type name."""
    script = (f"CREATE TEMP VIEW zt_eval AS ({query});\n"
              "SELECT count(*) || ' ' || string_agg(format_type(atttypid, atttypmod), ',') FROM pg_attribute"
              " WHERE attrelid = 'zt_eval'::regclass AND attnum > 0 AND NOT attisdropped;\n"
              f"\\copy (SELECT * FROM zt_eval LIMIT {limit}) TO '{out}'\n")
    args = [str(Path(t.BIN) / 'psql'), '-XqAt', '-v', 'ON_ERROR_STOP=1']
    if source:
        args.append(source)
    result = subprocess.run(args, input=script, text=True, capture_output=True)
    if result.returncode:
        sys.exit(f'source query failed:\n{result.stderr.strip()}')
    count, types = result.stdout.strip().split(' ', 1)
    if count != '1':
        sys.exit(f'--query must yield exactly one column, got {count} ({types})')
    if types not in ZTYPE:
        sys.exit(f'column type {types} is not text, jsonb or bytea; cast it in the query')
    return types


def timed(s, sql, runs):
    s.query(sql)  # warm-up
    secs = []
    for _ in range(runs):
        started = time.perf_counter()
        s.query(sql)
        secs.append(time.perf_counter() - started)
    return statistics.median(secs)


def kb(b):
    return f'{b / 1024:,.0f} kB' if b < 10 * 1048576 else f'{b / 1048576:,.1f} MB'


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument('--query', help='SQL yielding one text, jsonb or bytea column, in the order the split should use')
    src.add_argument('--synthetic', choices=SYNTHETIC, help="one of the benchmark's generated corpora")
    ap.add_argument('--source', help='libpq connection string or URI for --query (default: PG* environment)')
    ap.add_argument('--rows', type=int, help='rows to generate for --synthetic (default per corpus)')
    ap.add_argument('--limit', type=int, default=50000, help='rows to fetch for --query (default 50000)')
    ap.add_argument('--holdout', type=float, default=0.25, help='held-out fraction (default 0.25)')
    ap.add_argument('--split', choices=['interleaved', 'tail'], default='interleaved')
    ap.add_argument('--train-order', choices=['spread', 'source'], default='spread',
                    help="feed training rows in a deterministic pseudo-random order across the training part (default), "
                         "or in the query's order, which with a time-ordered query and more rows than the sample budget "
                         "trains on the oldest rows only")
    ap.add_argument('--levels', default='1,3,6,9,19', help='zstd levels to report (default 1,3,6,9,19)')
    ap.add_argument('--level', type=int, default=6, help='level for the bucket table and cache cost (default 6)')
    ap.add_argument('--dict-bytes', type=int, default=112640)
    ap.add_argument('--sample-bytes', type=int, default=8192)
    ap.add_argument('--runs', type=int, default=3, help='timed repetitions per measurement (default 3)')
    ap.add_argument('--port', type=int, default=55470)
    args = ap.parse_args()
    levels = sorted({int(x) for x in args.levels.split(',')} | {args.level})

    with tempfile.TemporaryDirectory(prefix='ztype-dict-') as tmp:
        work = Path(tmp)
        base = None
        if args.query:
            base = fetch(args.source, args.query, args.limit, work / 'corpus.copy')
        lib = t.library()
        c = Cluster(Path(t.BIN), work, 'pg', args.port, share(work, 'share', lib.with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '512MB'\nfsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            s = c.session()
            s.query("SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0;")
            if args.query:
                s.query(f"CREATE EXTENSION ztype; CREATE UNLOGGED TABLE corpus (id serial, v {base});")
                s.query(f"\\copy corpus (v) FROM '{work / 'corpus.copy'}'")
            else:
                gen, default_rows = SYNTHETIC[args.synthetic]
                base = 'jsonb'
                s.query(bench.SETUP)
                s.query(f"SELECT setseed(0.5); CREATE UNLOGGED TABLE corpus AS SELECT i AS id, {gen} AS v"
                        f" FROM generate_series(1, {args.rows or default_rows}) i;")
            zt = ZTYPE[base]
            total = int(s.query('SELECT count(*) FROM corpus;'))
            if total < 20:
                sys.exit(f'only {total} rows; nothing to evaluate')
            if args.split == 'interleaved':
                k = max(2, round(1 / args.holdout))
                held = f'id % {k} = 0'
            else:
                held = f'id > {int(total * (1 - args.holdout))}'
            # Training reads at most 20,000 samples or 64 MB, whichever comes first, in the order the
            # query yields them: spread them across the training part (README, "Building a good
            # dictionary", point 2) unless the source order is asked for.
            order = 'ORDER BY md5(id::text)' if args.train_order == 'spread' else 'ORDER BY id'
            s.query(f"CREATE UNLOGGED TABLE train AS SELECT v FROM corpus WHERE NOT ({held}) {order};"
                    f"CREATE UNLOGGED TABLE holdout AS SELECT id, v, {RAW[base]} AS raw FROM corpus WHERE {held};")
            n_train = int(s.query('SELECT count(*) FROM train;'))
            n = int(s.query('SELECT count(*) FROM holdout;'))
            raw_total = int(s.query('SELECT sum(raw) FROM holdout;'))
            pid = int(s.query('SELECT pg_backend_pid();'))
            # Training samples and zstd's training buffers are native memory outside work_mem: the
            # backend's peak resident size while training is the only place that cost shows.
            with PeakRss(pid) as peak:
                started = time.perf_counter()
                s.query(f"SELECT ztype.add_dictionary('eval', ztype.train_dictionary('SELECT v FROM train', {args.dict_bytes}, {args.sample_bytes}));")
                train_secs = time.perf_counter() - started
            train_rss = peak.growth_kb * 1024
            dict_bytes = int(s.query("SELECT octet_length(dict) FROM ztype.dictionaries WHERE name = 'eval';"))

            scan_v = timed(s, 'SELECT sum(pg_column_size(v)) FROM holdout;', args.runs)
            rows = []
            for level in levels:
                for label, mod in (('plain', f'{level}'), ('dict', f"{level}, 'eval'")):
                    expr = f'v::{zt}({mod})'
                    stored = int(s.query(f'SELECT sum(pg_column_size({expr})) FROM holdout;'))
                    comp = timed(s, f'SELECT sum(pg_column_size({expr})) FROM holdout;', args.runs) - scan_v
                    s.query(f'DROP TABLE IF EXISTS h; CREATE UNLOGGED TABLE h AS SELECT {expr} AS z FROM holdout;')
                    scan_z = timed(s, 'SELECT sum(pg_column_size(z)) FROM h;', args.runs)
                    dec = timed(s, f'SELECT sum({DECODE[base]}) FROM h;', args.runs) - scan_z
                    rows.append((level, label, stored, max(comp, 0) / n * 1e6, max(dec, 0) / n * 1e6))
            case = ' '.join(f'WHEN raw < {hi} THEN {i}' for i, (lo, hi) in enumerate(BUCKETS) if hi)
            buckets = s.query(
                f"SELECT b, count(*), sum(raw), sum(pg_column_size(v::{zt}({args.level}))), sum(pg_column_size(v::{zt}({args.level}, 'eval')))"
                f" FROM (SELECT v, raw, CASE {case} ELSE {len(BUCKETS) - 1} END AS b FROM holdout) q GROUP BY b ORDER BY b;")
            cache_bytes = int(s.query(f"BEGIN; SELECT sum(pg_column_size(v::{zt}({args.level}, 'eval'))) FROM (SELECT v FROM holdout LIMIT 1) q;"
                                      " SELECT bytes FROM ztype.dictionary_cache_stats(); ROLLBACK;").split('\n')[-1])
            s.close()
        finally:
            c.stop()

    what = f"`{args.query}`" if args.query else f'synthetic {args.synthetic} corpus'
    print(f'# Dictionary evaluation: {what}\n')
    print(f'{base} values: {total:,} rows, {n_train:,} for training and {n:,} held out ({args.split} split), '
          f'held-out raw bytes {kb(raw_total)}, {raw_total / n:,.0f} bytes per value on average. '
          f'Dictionary: {kb(dict_bytes)} (requested {kb(args.dict_bytes)}), trained in {train_secs:.1f} s from '
          f'{args.sample_bytes:,}-byte samples; the training backend grew by {kb(train_rss)} of resident memory at its '
          f'peak (samples and training buffers, outside work_mem). Per-backend cache cost at level {args.level}: '
          f'{kb(cache_bytes)} (dictionary bytes plus zstd objects, for the rest of the session).\n')
    print('| level | stored, no dictionary | stored, dictionary | compress µs/value (plain → dict) | decompress µs/value (plain → dict) |')
    print('|---:|---:|---:|---:|---:|')
    by_level = {}
    for level, label, stored, comp, dec in rows:
        by_level.setdefault(level, {})[label] = (stored, comp, dec)
    for level in levels:
        p, d = by_level[level]['plain'], by_level[level]['dict']
        print(f'| {level} | {kb(p[0])} ({p[0] / raw_total:.0%}) | {kb(d[0])} ({d[0] / raw_total:.0%}) | '
              f'{p[1]:.1f} → {d[1]:.1f} | {p[2]:.1f} → {d[2]:.1f} |')
    p, d = by_level[args.level]['plain'], by_level[args.level]['dict']
    saved = p[0] - d[0]
    print()
    if saved > 0:
        print(f'At level {args.level} the dictionary saves {kb(saved)} on the held-out rows, {saved / n:,.0f} bytes per value; '
              f'its own {kb(dict_bytes)} are paid back after {dict_bytes / (saved / n):,.0f} such values.\n')
    else:
        print(f'At level {args.level} the dictionary saves nothing on the held-out rows.\n')
    print(f'| value size | values | raw | stored, no dictionary (level {args.level}) | stored, dictionary |')
    print('|---|---:|---:|---:|---:|')
    for line in buckets.split('\n'):
        b, count, raw, plain, dct = (int(x) for x in line.split('|'))
        lo, hi = BUCKETS[b]
        name = f'{lo:,}–{hi - 1:,} B' if hi else f'≥ {lo:,} B'
        ratio = (lambda x: f' ({x / raw:.0%})') if raw else (lambda x: '')
        print(f'| {name} | {int(count):,} | {kb(raw)} | {kb(plain)}{ratio(plain)} | {kb(dct)}{ratio(dct)} |')
    print('\nStored bytes are `pg_column_size` of the compressed value, in memory, before TOAST; ratios are to raw. '
          f'Codec costs are statement time minus a scan of the same rows, median of {args.runs}; the dictionary '
          'variant includes loading the dictionary once per statement.')


if __name__ == '__main__':
    main()
