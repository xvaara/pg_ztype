#!/usr/bin/env python3
"""Native working memory: what one codec call or one training run costs a backend, beyond work_mem.

zstd's contexts, dictionary objects and training buffers are allocated outside PostgreSQL's memory
contexts, so `work_mem` does not bound them and `pg_backend_memory_contexts` does not show them.
This benchmark prints two things. First, libzstd's own accounting from tests/codec_memory.c (compiled
here): the compression context per level and pledged input size, with and without a dictionary, the
cached dictionary objects per dictionary size and level, and the decompression context per frame for
a full decode (stable output buffer, the datum is the window) and for `prefix` (streaming, zstd
allocates the frame's window). Second, a check of those numbers against a real backend: the peak
resident size of one fresh backend per statement, sampled while it compresses values of 16 MB and
128 MB at levels 1, 6, 19 and 22, decodes and prefix-reads frames, and trains a 110 kB and a 1 MB
dictionary. Resident memory counts touched pages, and a statement also touches what PostgreSQL holds
for it (shared-buffer pages, the detoasted input, the result), so compressions are compared as a
difference against the level-1 run of the same size, which touches the same datums. Level 22 on
128 MB takes minutes; ZTYPE_BENCH_MEMORY_QUICK=1 skips the 128 MB compressions above level 6, and
ZTYPE_BENCH_MEMORY_SMOKE=1 (the smoke mode of tests/bench_all.py) shrinks the values to 1 MB and 16 MB
and the training corpora to a tenth, which proves the run and measures nothing worth quoting.
Disposable cluster; prints Markdown.
"""
import os
from pathlib import Path
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, PeakRss, share  # noqa: E402
import bench_zjsonb as bench  # noqa: E402

QUICK = os.environ.get('ZTYPE_BENCH_MEMORY_QUICK') == '1'
SMOKE = os.environ.get('ZTYPE_BENCH_MEMORY_SMOKE') == '1'
MB = 1048576
# The two value sizes the backend check compresses, decodes and prefix-reads, in MB; both must be
# among the probe's pledged input sizes (tests/codec_memory.c) so the reserved context is known.
MID, BIG = (1, 16) if SMOKE else (16, 128)
SMALL_DOCS, LARGE_DOCS = (2000, 100) if SMOKE else (20000, 1600)


def mb(b):
    return f'{b / MB:,.0f} MB' if b >= 10 * MB else f'{b / MB:,.1f} MB'


def probe_raw(exe):
    """The probe's numbers as {(kind, level, size): bytes}."""
    out = {}
    for line in t.run([exe, 'raw']).splitlines():
        kind, level, size, size_bytes = line.split()
        out[(kind, int(level), int(size))] = int(size_bytes)
    return out


def main():
    zstd_cflags = t.run(['pkg-config', '--cflags', 'libzstd']).split() if os.environ.get('ZSTD_CFLAGS') is None else os.environ['ZSTD_CFLAGS'].split()
    zstd_libs = t.run(['pkg-config', '--libs', 'libzstd']).split() if os.environ.get('ZSTD_LIBS') is None else os.environ['ZSTD_LIBS'].split()
    with tempfile.TemporaryDirectory(prefix='ztype-memory-') as tmp:
        work = Path(tmp)
        probe = work / 'codec_memory'
        t.run(['cc', '-O2', *zstd_cflags, '-o', probe, t.ROOT / 'tests' / 'codec_memory.c', *zstd_libs])
        tables = t.run([probe])
        raw = probe_raw(probe)
        c = Cluster(Path(t.BIN), work, 'pg', 55477, share(work, 'share', t.library().with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '1GB'\nfsync = off\nsynchronous_commit = off\nautovacuum = off\n")
        c.start()
        try:
            s = c.session()
            s.query('SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0;')
            s.query(bench.SETUP)
            # md5 text: about 2:1 at level 6, so frames stay large and windows stay wide.
            for size in (MID, BIG):  # 32 bytes per md5
                s.query(f"CREATE TABLE v{size} AS SELECT (SELECT string_agg(md5(i::text), '') FROM generate_series(1, {size * MB // 32}) i) AS t;")
            for level in (6, 22):
                s.query(f'CREATE TABLE f{MID}_{level} AS SELECT t::ztext({level}) AS z FROM v{MID};')
            s.query(f'CREATE TABLE f{BIG}_6 AS SELECT t::ztext(6) AS z FROM v{BIG};')
            # Training corpora: 20,000 small documents (12 MB of samples at the 8 kB cut) and enough
            # large ones to fill the 64 MB sample budget at a 64 kB cut.
            s.query(f'SELECT setseed(0.4); CREATE TABLE small_train AS SELECT erp_line(i) AS doc FROM generate_series(1, {SMALL_DOCS}) i;')
            s.query(f'SELECT setseed(0.6); CREATE TABLE large_train AS SELECT erp_document(i) AS doc FROM generate_series(1, {LARGE_DOCS}) i;')
            small_bytes = int(s.query("SELECT sum(least(pg_column_size(doc), 8192)) FROM small_train;"))
            large_bytes = min(64 * MB, int(s.query("SELECT sum(least(pg_column_size(doc), 65536)) FROM large_train;")))
            # Every table is read once so its pages are in shared buffers before any backend is sampled.
            s.query(f'SELECT sum(octet_length(t)) FROM v{MID}; SELECT sum(octet_length(t)) FROM v{BIG};'
                    f'SELECT sum(pg_column_size(z)) FROM f{MID}_6; SELECT sum(pg_column_size(z)) FROM f{MID}_22; SELECT sum(pg_column_size(z)) FROM f{BIG}_6;')
            s.close()

            def sample(sql):
                """Peak RSS growth and wall time of one statement in a fresh backend, so nothing a previous
                statement allocated and freed can be reused silently: a new backend's resident size is its
                baseline, and everything the statement touches counts."""
                b = c.session()
                pid = int(b.query('SELECT pg_backend_pid();'))
                b.query('SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0; SELECT 1;')
                with PeakRss(pid) as peak:
                    started = time.perf_counter()
                    b.query(sql)
                    secs = time.perf_counter() - started
                b.close()
                return peak.growth_kb * 1024, secs

            compress = {}
            for size in (MID, BIG):
                for level in (1, 6, 19, 22):
                    if QUICK and size == BIG and level > 6:
                        continue
                    compress[(size, level)] = sample(f'SELECT pg_column_size(t::ztext({level})) FROM v{size};')
            reads = [(f'full decode of {MID} MB, level-6 frame', f'SELECT length(z::text) FROM f{MID}_6;', ('dctx_full', 6, MID)),
                     (f'full decode of {MID} MB, level-22 frame', f'SELECT length(z::text) FROM f{MID}_22;', ('dctx_full', 22, MID)),
                     (f'full decode of {BIG} MB, level-6 frame', f'SELECT length(z::text) FROM f{BIG}_6;', ('dctx_full', 6, BIG)),
                     (f'`prefix` of {MID} MB, level-6 frame', f'SELECT length(prefix(z, 100)) FROM f{MID}_6;', ('dctx_stream', 6, MID)),
                     (f'`prefix` of {MID} MB, level-22 frame', f'SELECT length(prefix(z, 100)) FROM f{MID}_22;', ('dctx_stream', 22, MID)),
                     (f'`prefix` of {BIG} MB, level-6 frame', f'SELECT length(prefix(z, 100)) FROM f{BIG}_6;', ('dctx_stream', 6, BIG))]
            read_rows = [(label, key, *sample(sql)) for label, sql, key in reads]
            train_rows = [(f'train 110 kB from {SMALL_DOCS:,} small documents', small_bytes,
                           *sample("SELECT octet_length(ztype.train_dictionary('SELECT doc FROM small_train', 112640, 8192));")),
                          (f'train 1 MB from {mb(large_bytes)} of large documents', large_bytes,
                           *sample("SELECT octet_length(ztype.train_dictionary('SELECT doc FROM large_train', 1048576, 65536));"))]
            v = c.session()
            ver = v.query('SELECT version();')
            v.close()
        finally:
            c.stop()

    print(f'\n{ver}; {bench.machine()}.\n')
    print(tables)
    print('\nBackend check. Each statement runs in a fresh backend and its peak resident size above the backend\'s '
          'starting size is sampled every 5 ms. Resident memory counts pages touched, not bytes reserved, and a '
          'statement also touches what PostgreSQL holds for it (the table\'s pages in shared buffers, the detoasted '
          'input, the result), so the comparison against the probe is a difference: the same statement at level 1 '
          'touches the same datums, and the growth beyond it is the context.\n')
    print('| compression | peak RSS growth | beyond the level-1 run | probe: context beyond level 1 | time |')
    print('|---|---:|---:|---:|---:|')
    for (size, level), (growth, secs) in sorted(compress.items()):
        base = compress[(size, 1)][0]
        expected = raw[('cctx', level, size * MB)] - raw[('cctx', 1, size * MB)]
        beyond = f'{mb(growth - base)}' if level > 1 else '—'
        print(f'| {size} MB at level {level} | {mb(growth)} | {beyond} | {mb(expected) if level > 1 else "—"} | {secs:.1f} s |')
    print('\n| read | peak RSS growth | probe: context reserved | time |')
    print('|---|---:|---:|---:|')
    for label, (kind, level, size), growth, secs in read_rows:
        print(f'| {label} | {mb(growth)} | {mb(raw[(kind, level, size * MB)])} | {secs:.1f} s |')
    print('\n| training | sample bytes | peak RSS growth | time |')
    print('|---|---:|---:|---:|')
    for label, samples, growth, secs in train_rows:
        print(f'| {label} | {mb(samples)} | {mb(growth)} | {secs:.1f} s |')
    print()


if __name__ == '__main__':
    main()
