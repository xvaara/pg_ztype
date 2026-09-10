#!/usr/bin/env python3
"""Size benchmark: jsonb (pglz and lz4 TOAST) against zjsonb with and without a dictionary.

Documents imitate ERP data: many keys, ISO timestamps, codes, short and long free text.
Three shapes: small (one order line, well under the 2 kB TOAST threshold, so plain jsonb
stores it uncompressed), large (an invoice with dozens of lines, history and notes) and
random (small objects whose keys and values are random strings, numbers and booleans: no
shared vocabulary at all, the worst case for a dictionary and for compression in general).
Dictionaries are trained on separately generated documents, never on the measured ones.
Besides sizes it times three operations per variant, each ZTYPE_BENCH_RUNS times (default
five) after one unmeasured warm-up run, and reports the median with the min-max spread and
the median cost per row in microseconds, which is the number to compare codec changes
with: the insert (INSERT ... SELECT from a source table, so it
includes serialising the source jsonb to text and parsing it back, the same for every
variant), a full-document read (containment through ::jsonb, which decompresses
everything) and a key lookup with the native ->> operator, and a three-key row filter (three ->> on the same row, OR-ed so every key
is evaluated for nearly every row). Writes go into a fresh unlogged
table each time, so no WAL or checkpoint lands inside a timing; reads are warm. Parallel
query and autovacuum are disabled so the timings compare codec cost under the same serial
plan with nothing else running in the cluster.
Table sizes are pg_total_relation_size, which excludes the shared dictionary registry;
dictionary sizes are printed separately.
Runs in a disposable cluster; prints Markdown tables and the environment they came from.
Env: ZTYPE_BENCH_SMALL, ZTYPE_BENCH_LARGE, ZTYPE_BENCH_RANDOM (row counts), ZTYPE_BENCH_RUNS.
"""
import os
from pathlib import Path
import platform
import statistics
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_cross_major import Cluster, share  # noqa: E402

PG_CONFIG = os.environ.get('PG_CONFIG', 'pg_config')
SMALL = int(os.environ.get('ZTYPE_BENCH_SMALL', 200000))
LARGE = int(os.environ.get('ZTYPE_BENCH_LARGE', 4000))
RANDOM = int(os.environ.get('ZTYPE_BENCH_RANDOM', 1000000))
RUNS = int(os.environ.get('ZTYPE_BENCH_RUNS', 5))

SETUP = r"""
CREATE EXTENSION ztype;
CREATE FUNCTION pick(choices text[], r float8) RETURNS text LANGUAGE sql IMMUTABLE
  AS $$ SELECT choices[1 + floor(r * array_length(choices, 1))::int] $$;
CREATE FUNCTION ts(r float8) RETURNS text LANGUAGE sql IMMUTABLE
  AS $$ SELECT to_char(timestamptz '2024-01-01 00:00:00+00' + r * interval '600 days', 'YYYY-MM-DD"T"HH24:MI:SS.MS"Z"') $$;
CREATE TABLE vocab AS SELECT
  ARRAY['Hex bolt M8x40 zinc plated','Hex nut M8 stainless A2','Flat washer M8','Cable tie 200x4.8 mm black',
        'Industrial ethernet cable Cat6A 5 m','Circuit breaker 3P 16A C curve','Contactor 25A 230V coil','LED panel 600x600 40W 4000K',
        'Pressure sensor 0-10 bar 4-20mA','Ball valve DN25 brass','Copper pipe 22 mm 3 m','PEX pipe 16 mm 100 m roll',
        'Safety glasses clear anti-fog','Nitrile gloves size L box of 100','Hearing protection ear muffs 30 dB','Work trousers navy size 52',
        'Cordless drill 18V 2x4Ah','Angle grinder 125 mm 1200W','Laser distance meter 50 m','Torque wrench 20-200 Nm',
        'On-site installation, hourly','Commissioning and testing','Remote support, 15 min block','Project management',
        'Freight, pallet','Freight, parcel','Return handling fee','Expedited processing',
        'Server rack 42U 800x1000','Patch panel 24 port Cat6','UPS 3000VA rack mount','Managed switch 48 port PoE+',
        'Office chair ergonomic black','Height adjustable desk 160x80','Monitor 27 inch 4K','Docking station USB-C',
        'Printer toner black 12k pages','Copy paper A4 80g 5x500','Whiteboard 120x90','Label printer 62 mm'] AS products,
  ARRAY['Customer requested delivery before noon.','Please leave the package at the loading dock.','Backordered; expected from supplier next week.',
        'Replaces the previously cancelled line.','Price agreed per framework contract 2024-17.','Invoice separately from the rest of the order.',
        'Site contact is on leave until Monday; call the switchboard.','Partial delivery accepted by the customer.','Packaging must be returnable pallets only.',
        'Serial numbers to be reported on the delivery note.','Approved by purchasing manager after price check.','Customer will collect from the warehouse.',
        'Delivery address differs from the billing address; verify before dispatch.','Recurring monthly order, do not change quantities without confirmation.',
        'Includes installation as agreed in the quotation.','Credit limit check passed on the order date.','Damaged in transit; replacement shipped free of charge.',
        'Warranty extended to 36 months per contract.','Hold shipment until payment is received.','Discount applied per campaign code SPRING24.',
        'Documentation and certificates attached to the invoice.','Customer reference required on all documents.','Remaining quantity will follow in the next batch.',
        'Order confirmed by phone; written confirmation sent.','Special handling: fragile, keep upright.'] AS sentences,
  ARRAY['draft','confirmed','confirmed','picked','shipped','shipped','invoiced','invoiced','paid','paid','paid','cancelled'] AS statuses,
  ARRAY['helsinki-01','vantaa-02','tampere-01','oulu-01','turku-03'] AS warehouses,
  ARRAY['pcs','pcs','pcs','pcs','kg','m','h','box','roll','pallet'] AS units,
  ARRAY['maria.koskinen','juha.virtanen','anna.nieminen','pekka.makinen','laura.heikkinen','system.import','api.webshop','tuomas.laine'] AS users,
  ARRAY['created','updated','confirmed','price_changed','quantity_changed','picked','shipped','invoiced','payment_received','comment','reopened','cancelled'] AS actions,
  ARRAY['priority','backorder','contract','webshop','export','project','warranty','recurring','sample','credit-hold'] AS tags,
  ARRAY['Pohjolan Rakennus','Suomen Sähköasennus','Lahden Konepaja','Turun Talotekniikka','Nordic Data Center','Kymen Kuljetus','Helsinki Harbour Logistics',
        'Oulu Offshore Services','Savon Sahat','Länsi-Suomen LVI','Tampere Automation','Vantaa Facility Services','Espoo Medical Supplies','Kuopio Kiinteistöt'] AS companies,
  ARRAY['Oy','Oy','Oy Ab','Ky','Oyj','Tmi'] AS suffixes,
  ARRAY['Teollisuuskatu','Rautatienkatu','Satamatie','Kauppakatu','Tehtaankatu','Asemakatu','Rantatie','Koulukatu','Puistokatu','Mannerheimintie'] AS streets,
  ARRAY['Helsinki','Espoo','Tampere','Vantaa','Oulu','Turku','Jyväskylä','Lahti','Kuopio','Pori'] AS cities;

CREATE FUNCTION erp_line(i bigint) RETURNS jsonb LANGUAGE sql VOLATILE AS $$
  SELECT jsonb_build_object(
    'id', md5('line' || i),
    'tenant_id', 'tn_' || (i % 37),
    'sales_order_id', 'SO-' || lpad((i / 8)::text, 8, '0'),
    'line_no', (i % 8) + 1,
    'sku', 'SKU-' || lpad(floor(random() * 5000)::text, 6, '0'),
    'description', pick(v.products, random()),
    'quantity', round((random() * 200)::numeric, 2),
    'unit', pick(v.units, random()),
    'unit_price', round((random() * 950 + 0.5)::numeric, 2),
    'discount_pct', (ARRAY[0,0,0,5,10,15])[1 + floor(random() * 6)::int],
    'vat_rate', (ARRAY[24,24,24,14,10,0])[1 + floor(random() * 6)::int],
    'currency', 'EUR',
    'status', pick(v.statuses, random()),
    'warehouse', pick(v.warehouses, random()),
    'cost_center', 'CC-' || (100 + floor(random() * 40)::int),
    'created_at', ts(random()),
    'updated_at', ts(random()),
    'delivery_date', to_char(date '2024-01-01' + floor(random() * 700)::int, 'YYYY-MM-DD'),
    'created_by', pick(v.users, random()),
    'notes', CASE WHEN random() < 0.3 THEN pick(v.sentences, random()) END,
    'tags', jsonb_build_array(pick(v.tags, random()), pick(v.tags, random())),
    'custom_fields', jsonb_build_object('project_code', 'PRJ-' || floor(random() * 300)::int, 'approved', random() < 0.7, 'batch', NULL))
  FROM vocab v $$;

-- Random keys and values with no shared vocabulary: base64 of random hashes, numbers, booleans.
CREATE FUNCTION rnd(n int) RETURNS text LANGUAGE sql VOLATILE
  AS $$ SELECT substr(encode(sha256(random()::text::bytea), 'base64'), 1, n) $$;
CREATE FUNCTION random_doc(i bigint) RETURNS jsonb LANGUAGE sql VOLATILE AS $$
  SELECT jsonb_object_agg(k, v) FROM (
    SELECT rnd(4 + floor(random() * 9)::int) AS k,
           CASE floor(random() * 4)::int
             WHEN 0 THEN to_jsonb(rnd(24 + floor(random() * 40)::int))
             WHEN 1 THEN to_jsonb(round((random() * 1000000)::numeric, 3))
             WHEN 2 THEN to_jsonb(random() < 0.5)
             ELSE to_jsonb(rnd(1 + floor(random() * 12)::int))
           END AS v
    FROM generate_series(1, 6 + floor(random() * 10)::int)) q $$;

CREATE FUNCTION erp_address(r float8) RETURNS jsonb LANGUAGE sql VOLATILE AS $$
  SELECT jsonb_build_object('street', pick(v.streets, random()) || ' ' || (1 + floor(random() * 120)::int),
    'postal_code', lpad(floor(random() * 99999)::text, 5, '0'), 'city', pick(v.cities, random()), 'country', 'FI')
  FROM vocab v $$;

CREATE FUNCTION erp_document(i bigint) RETURNS jsonb LANGUAGE sql VOLATILE AS $$
  SELECT jsonb_build_object(
    'id', md5('doc' || i),
    'type', 'sales_invoice',
    'number', 'INV-' || (2024 + i % 3) || '-' || lpad(i::text, 6, '0'),
    'tenant_id', 'tn_' || (i % 37),
    'status', pick(v.statuses, random()),
    'currency', 'EUR',
    'issued_at', ts(random()), 'due_date', to_char(date '2024-01-01' + floor(random() * 700)::int, 'YYYY-MM-DD'),
    'created_at', ts(random()), 'updated_at', ts(random()),
    'customer', jsonb_build_object('number', 'C-' || lpad(floor(random() * 20000)::text, 6, '0'),
      'name', pick(v.companies, random()) || ' ' || pick(v.suffixes, random()),
      'vat_id', 'FI' || lpad(floor(random() * 100000000)::text, 8, '0'),
      'email', 'laskutus@' || lower(replace(pick(v.companies, random()), ' ', '')) || '.fi',
      'billing_address', erp_address(random()), 'shipping_address', erp_address(random())),
    'lines', (SELECT jsonb_agg(erp_line(i * 1000 + n)) FROM generate_series(1, 20 + floor(random() * 100)::int) n),
    'totals', jsonb_build_object('net', round((random() * 90000)::numeric, 2), 'vat', round((random() * 21600)::numeric, 2),
      'gross', round((random() * 111600)::numeric, 2), 'paid', round((random() * 111600)::numeric, 2)),
    'history', (SELECT jsonb_agg(e) FROM (SELECT jsonb_build_object('at', ts(random()), 'user', pick(v.users, random()), 'action', pick(v.actions, random()),
      'comment', CASE WHEN random() < 0.5 THEN pick(v.sentences, random()) END) AS e FROM generate_series(1, 5 + floor(random() * 30)::int)) q),
    'notes', (SELECT string_agg(x, ' ') FROM (SELECT pick(v.sentences, random()) AS x FROM generate_series(1, 3 + floor(random() * 8)::int)) q),
    'attachments', (SELECT coalesce(jsonb_agg(jsonb_build_object('filename', 'scan_' || n || '.pdf', 'bytes', floor(random() * 2000000)::int,
      'sha256', md5(i::text || n) || md5(n::text || i))), '[]'::jsonb) FROM generate_series(1, floor(random() * 3)::int) n))
  FROM vocab v $$;
"""

VARIANTS = [('jsonb, pglz TOAST', 'jsonb COMPRESSION pglz'), ('jsonb, lz4 TOAST', 'jsonb COMPRESSION lz4'),
            ('zjsonb(6)', 'zjsonb(6)'), ('zjsonb(6) + dictionary', "zjsonb(6, '{dict}')")]


def timed(s, sql, before=None):
    """Median and spread of RUNS runs after one unmeasured warm-up, wall clock through one
    persistent psql (about a millisecond of overhead per query). `before` runs unmeasured
    ahead of every run, including the warm-up (a fresh table for writes)."""
    secs = []
    for i in range(RUNS + 1):
        if before:
            s.query(before)
        started = time.perf_counter()
        s.query(sql)
        if i > 0:
            secs.append(time.perf_counter() - started)
    return statistics.median(secs), min(secs), max(secs)


def cell(t):
    med, lo, hi = t
    return f'{med:.2f} s ({lo:.2f}–{hi:.2f})'


def per_row(t, n):
    us = t[0] / n * 1e6
    return f'{us:.1f}' if us < 100 else f'{us:.0f}'


def load(s, shape, n, generator, seed, train_n, sample_bytes):
    s.query(f"SELECT setseed({seed}); CREATE TABLE {shape}_src AS SELECT {generator} AS doc FROM generate_series(1, {n}) i;")
    s.query(f"SELECT setseed({seed + 0.4}); CREATE TABLE {shape}_train AS SELECT {generator} AS doc FROM generate_series(1, {train_n}) i;")
    started = time.time()
    s.query(f"SELECT ztype.add_dictionary('erp-{shape}', ztype.train_dictionary('SELECT doc FROM {shape}_train', 112640, {sample_bytes}));")
    train_secs = time.time() - started
    raw = int(s.query(f"SELECT sum(octet_length(doc::text)) FROM {shape}_src;"))
    dict_bytes = int(s.query(f"SELECT octet_length(dict) FROM ztype.dictionaries WHERE name = 'erp-{shape}';"))
    sizes = []
    for label, coltype in VARIANTS:
        table = f"{shape}_{len(sizes)}"
        create = f"DROP TABLE IF EXISTS {table}; CREATE UNLOGGED TABLE {table} (doc {coltype.format(dict='erp-' + shape)});"
        # Re-materialise each datum: a compressed datum copied from another table keeps its
        # original TOAST method, which would make the pglz and lz4 columns identical.
        write = timed(s, f"INSERT INTO {table} SELECT doc::text::jsonb FROM {shape}_src;", before=create)
        s.query(f"VACUUM {table};")
        if coltype.startswith('jsonb'):
            methods = s.query(f"SELECT string_agg(DISTINCT coalesce(pg_column_compression(doc), 'none'), ',') FROM {table};")
            assert methods in (coltype.split()[-1], 'none'), (label, methods)
        size = int(s.query(f"SELECT pg_total_relation_size('{table}');"))
        read_sql = f"SELECT count(*) FROM {table} WHERE doc::jsonb @> '{{\"currency\": \"EUR\"}}';"
        key_sql = f"SELECT count(*) FROM {table} WHERE doc ->> 'status' = 'paid';"
        keys_sql = (f"SELECT count(*) FROM {table} WHERE doc ->> 'status' = 'paid' OR doc ->> 'currency' = 'XXX'"
                    f" OR doc ->> 'tenant_id' = 'none';")
        for sql in (read_sql, key_sql, keys_sql):
            plan = s.query('EXPLAIN (COSTS OFF) ' + sql)
            assert 'Seq Scan' in plan and 'Parallel' not in plan, (label, plan)
        read = timed(s, read_sql)
        key = timed(s, key_sql)
        keys = timed(s, keys_sql)
        sizes.append((label, size, write, read, key, keys))
    return raw, dict_bytes, train_secs, sizes


def mb(b):
    return f'{b / 1048576:,.1f} MB'


def machine():
    """CPU and OS for the environment line; best effort, stdlib only."""
    cpu = platform.processor() or platform.machine()
    try:
        if sys.platform == 'darwin':
            cpu = subprocess.check_output(['sysctl', '-n', 'machdep.cpu.brand_string'], text=True).strip()
        elif sys.platform.startswith('linux'):
            with open('/proc/cpuinfo') as f:
                cpu = next(line.split(':', 1)[1].strip() for line in f if line.startswith('model name'))
    except (OSError, StopIteration, subprocess.CalledProcessError):
        pass
    return f'{cpu}, {platform.system()} {platform.release()} {platform.machine()}'


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-bench-') as tmp:
        work = Path(tmp)
        lib = next(t.ROOT / f'ztype{s}' for s in ('.so', '.dylib') if (t.ROOT / f'ztype{s}').exists())
        c = Cluster(Path(t.BIN), work, 'pg', 55460, share(work, 'share', lib.with_suffix('')))
        with (c.data / 'postgresql.conf').open('a') as conf:
            conf.write("shared_buffers = '512MB'\nmax_wal_size = '4GB'\nfsync = off\nsynchronous_commit = off\n"
                       "autovacuum = off\ncheckpoint_timeout = '1h'\n")
        c.start()
        try:
            s = c.session()
            s.query("SET statement_timeout = 0; SET max_parallel_workers_per_gather = 0;")
            s.query(SETUP)
            env = {'PostgreSQL': s.query('SELECT version();'), 'zstd': s.query('SELECT ztype.zstd_version();'),
                   'ztype': s.query("SELECT extversion FROM pg_extension WHERE extname = 'ztype';"),
                   'settings': s.query("SELECT string_agg(name || '=' || setting, ', ' ORDER BY name) FROM pg_settings WHERE name IN "
                                       "('shared_buffers', 'fsync', 'synchronous_commit', 'max_parallel_workers_per_gather', 'default_toast_compression', 'autovacuum');")}
            results = []
            for shape, n, gen, seed, train_n, sample in [('small', SMALL, 'erp_line(i)', 0.1, 20000, 8192),
                                                          ('large', LARGE, 'erp_document(i)', 0.2, 1500, 65536),
                                                          ('random', RANDOM, 'random_doc(i)', 0.3, 20000, 8192)]:
                raw, dict_bytes, train_secs, sizes = load(s, shape, n, gen, seed, train_n, sample)
                results.append((shape, n, raw, dict_bytes, train_secs, sizes))
            s.close()
        finally:
            c.stop()
    print(f"\n{env['PostgreSQL']}; libzstd {env['zstd']}; ztype {env['ztype']}; {machine()}.")
    print(f"Server settings: {env['settings']}. zstd level 6, dictionaries trained on separate documents.\n")
    print('| documents | raw JSON text | ' + ' | '.join(label for label, _ in VARIANTS) + ' |')
    print('|---|---:|' + '---:|' * len(VARIANTS))
    for shape, n, raw, dict_bytes, train_secs, sizes in results:
        base = sizes[0][1]
        avg = raw / n
        cells = [f'{mb(size)} ({size / base:.0%})' for _, size, *_ in sizes]
        print(f'| {shape}: {n:,} × {avg / 1024:.1f} kB | {mb(raw)} | ' + ' | '.join(cells) + ' |')
    print('\nOn-disk table size including TOAST, relative to jsonb with pglz.\n')
    print('| documents | operation | ' + ' | '.join(label for label, _ in VARIANTS) + ' |')
    print('|---|---|' + '---:|' * len(VARIANTS))
    ops = [('write {n:,} rows (INSERT … SELECT)', 2), ('read all documents (`::jsonb @>`)', 3), ("key access (`->> 'status'`)", 4),
           ("three keys (`->>` × 3)", 5)]
    for shape, n, raw, dict_bytes, train_secs, sizes in results:
        for op, idx in ops:
            print(f'| {shape} | {op.format(n=n)} | ' + ' | '.join(cell(row[idx]) for row in sizes) + ' |')
    print(f'\nMedian of {RUNS} runs after one warm-up (min–max in parentheses) over the whole table, serial plans,'
          ' writes into a fresh unlogged table each run, reads warm.\n')
    print('| documents | operation | ' + ' | '.join(label for label, _ in VARIANTS) + ' |')
    print('|---|---|' + '---:|' * len(VARIANTS))
    for shape, n, raw, dict_bytes, train_secs, sizes in results:
        for op, idx in ops:
            print(f'| {shape} | {op.format(n=n)} | ' + ' | '.join(per_row(row[idx], n) for row in sizes) + ' |')
    print('\nMedian microseconds per row: the figure to compare codec changes with. Dictionaries:\n')
    for shape, n, raw, dict_bytes, train_secs, sizes in results:
        print(f'- {shape}: {dict_bytes / 1024:.0f} kB trained in {train_secs:.1f} s')


if __name__ == '__main__':
    main()
