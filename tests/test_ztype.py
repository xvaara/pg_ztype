#!/usr/bin/env python3
"""Exercise the real extension in an isolated PostgreSQL 18+ cluster, using only stdlib."""
import concurrent.futures
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
PG_CONFIG = os.environ.get('PG_CONFIG', 'pg_config')
BIN = Path(subprocess.check_output([PG_CONFIG, '--bindir'], text=True).strip())
# A sanitizer runtime to load into the server before anything else (make test-asan sets it).
SANITIZER_RUNTIME = os.environ.get('ZTYPE_SANITIZER_RUNTIME')
RAW = object()  # Session.query(error=RAW): return output without asserting on ERROR/FATAL


def run(args, **kwargs):
    """Fail with captured diagnostics rather than hiding the first SQL error."""
    result = subprocess.run([str(x) for x in args], text=True, capture_output=True, **kwargs)
    if result.returncode:
        raise AssertionError(f'{args}\n{result.stdout}\n{result.stderr}')
    return result.stdout.strip()


def library():
    """The library under test: ZTYPE_LIBRARY (a sanitizer build elsewhere), else the tree's own."""
    if os.environ.get('ZTYPE_LIBRARY'):
        lib = Path(os.environ['ZTYPE_LIBRARY']).resolve()
        assert lib.exists(), f'ZTYPE_LIBRARY does not exist: {lib}'
        return lib
    lib = next((ROOT / f'ztype{suffix}' for suffix in ('.so', '.dylib') if (ROOT / f'ztype{suffix}').exists()), None)
    assert lib, 'run make first'
    return lib


class Session:
    """Keep one backend alive for transaction, cache, and role-switch tests. A lost connection
    fails with the server log tail and any sanitizer report, which is where the cause is."""
    def __init__(self, env, bin=None, cluster=None):
        self.cluster = cluster
        # errors='replace': the mutation harness prints corrupt values whose bytes are not valid in
        # the client encoding; a decode error on the pipe must not abort the run.
        self.proc = subprocess.Popen(
            [str((bin or BIN) / 'psql'), '-XqAt', '-v', 'ON_ERROR_STOP=0'], env=env,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors='replace')
        self.query("SET statement_timeout = '15s'; SET client_min_messages = warning;")

    def query(self, sql, error=None):
        self.proc.stdin.write(sql + "\n\\echo ZTYPE_TEST_DONE\n")
        self.proc.stdin.flush()
        output = []
        while True:
            line = self.proc.stdout.readline()
            if not line:
                detail = self.cluster.diagnostics() if self.cluster else ''
                raise AssertionError('backend exited: ' + '\n'.join(output) + detail)
            line = line.rstrip('\n')
            if line == 'ZTYPE_TEST_DONE':
                break
            output.append(line)
        result = '\n'.join(output)
        if error is RAW:
            pass
        elif error:
            assert 'ERROR:' in result and error in result, (sql, result, error)
        else:
            assert 'ERROR:' not in result and 'FATAL:' not in result, (sql, result)
        return result

    def equal(self, sql, expected='t'):
        actual = self.query(sql)
        assert actual == expected, (sql, actual, expected)

    def raw(self, sql):
        """Output with no expectation asserted; a lost connection still raises (the mutation
        harness classifies the outcome itself)."""
        return self.query(sql, error=RAW)

    def sqlstate(self, sql):
        """The SQLSTATE of a statement that must fail. Privilege tests pin the code, not the
        wording: under psql's sqlstate verbosity the error line carries nothing else."""
        out = self.query('\\set VERBOSITY sqlstate\n' + sql.rstrip().rstrip(';') + ';\n\\set VERBOSITY default', error=RAW)
        codes = [line.split()[-1] for line in out.splitlines() if line.startswith('ERROR:')]
        assert len(codes) == 1, (sql, out)
        return codes[0]

    def close(self):
        self.proc.stdin.close()
        self.proc.wait(timeout=10)


def training(label):
    """Distinct deterministic training sets yield distinct dictionary IDs."""
    return (f"SELECT ztype.train_and_add('{label}', $$SELECT '{label} sample subject ' || i || "
            "repeat(md5(i::text),4) FROM generate_series(1,1000) i$$, 2048);")


# Committed storage fixtures (tests/make_fixtures.py writes them, fixtures() below reads them).
FIXTURE_DIR = ROOT / 'tests' / 'fixtures'
BASE_TYPE = {'ztext': 'text', 'zjsonb': 'jsonb', 'zbytea': 'bytea'}
# Stored bytes enter a value only through casts with no function, as in test_cross_major.CASTS.
RAW_CASTS = ('CREATE CAST (ztext AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS ztext) WITHOUT FUNCTION;'
             'CREATE CAST (zjsonb AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS zjsonb) WITHOUT FUNCTION;'
             'CREATE CAST (zbytea AS ztext) WITHOUT FUNCTION; CREATE CAST (ztext AS zbytea) WITHOUT FUNCTION;')
RAW_IN = {'ztext': "decode('{h}', 'hex')::ztext", 'zjsonb': "decode('{h}', 'hex')::zjsonb",
          'zbytea': "(decode('{h}', 'hex')::ztext)::zbytea"}
INSPECT_ROW = ('SELECT i.kind, i.codec, i.level, i.format, i.raw_length, i.stored_bytes, '
               "coalesce(i.dict_id::text,'') FROM ztype.inspect({v}) i;")
# The library's own envelope magic, from the first four bytes of a fresh value. The envelope is
# native-endian, so the bytes are assembled in this machine's order: the magic is then the same
# number everywhere, and a big-endian machine skips the fixture for the byte order it records
# instead of claiming a magic bump that never happened.
MAGIC_BYTES = (3, 2, 1, 0) if sys.byteorder == 'little' else (0, 1, 2, 3)
LIBRARY_MAGIC = ('SELECT (get_byte(b,{0})<<24)|(get_byte(b,{1})<<16)|(get_byte(b,{2})<<8)|get_byte(b,{3}) '
                 "FROM (SELECT ('x'::ztext)::bytea b) q;").format(*MAGIC_BYTES)


def check(cluster, work):
    """Cover failure boundaries and PostgreSQL integration, not implementation mirrors."""
    a, b, env = cluster.session(), cluster.session(), cluster.env
    try:
        a.query('CREATE EXTENSION ztype;')
        a.equal("SELECT 'héllo'::ztext::text = 'héllo', raw_length('héllo'::ztext) = 6;", 't|t')
        a.equal("SELECT prefix('ä'::ztext,2) = 'ä', prefix('aä'::ztext,3) = 'aä', prefix('😀'::ztext,4) = '😀';", 't|t|t')
        a.equal("SELECT prefix(repeat('aä😀',1000)::ztext,6) = 'aä', prefix(repeat('aä😀',1000)::ztext,7) = 'aä😀';", 't|t')
        a.equal("SELECT prefix('abc'::ztext,-1) = '', prefix(''::ztext,10) = '';", 't|t')
        a.equal("SELECT decode('0000ff','hex')::zbytea::bytea = decode('0000ff','hex');")
        a.equal("SELECT '{\"a\":null}'::zjsonb -> 'a' = 'null'::jsonb, ('{}'::zjsonb ->> 'missing') IS NULL, ('{\"a\":null}'::zjsonb ->> 'a') IS NULL;", 't|t|t')
        a.equal("SELECT ('42'::zjsonb)::jsonb = '42'::jsonb, ('[1,2]'::zjsonb)::jsonb = '[1,2]'::jsonb;", 't|t')
        a.query('CREATE TABLE corpus(id integer, source text, body ztext, meta zjsonb, blob zbytea);')
        a.query("INSERT INTO corpus SELECT i, s, s, jsonb_build_object('body',s), convert_to(s,'UTF8') FROM (SELECT i, repeat(md5(i::text)||'ä😀', i*50) s FROM generate_series(0,40) i) q;")
        a.equal("SELECT bool_and(body::text = source AND (meta ->> 'body') = source AND blob::bytea = convert_to(source,'UTF8') AND raw_length(body) = octet_length(source)) FROM corpus;")
        a.equal("SELECT pg_column_size('x'::ztext) = 17;")  # 16-byte envelope plus one raw byte
        a.equal("SELECT i.kind, i.codec, i.level, i.format, i.raw_length, i.stored_bytes, i.dict_id FROM ztype.inspect('x'::ztext) i;", 'text|raw|6|0|1|13|')
        a.equal("SELECT i.kind, i.codec, i.level, i.format, i.raw_length > 0, i.dict_id FROM ztype.inspect(jsonb_build_object('k', repeat('v', 100))::zjsonb(3)) i;", 'jsonb|zstd|3|1|t|')
        a.equal("SELECT ztype.zstd_version() ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$';")
        # build_info is the packaging check: the library's version against the installed script,
        # the magic against the committed fixture (asserted in fixtures()), zstd built against loaded.
        a.equal("SELECT b.library = e.extversion, b.magic ~ '^0x[0-9a-f]{8}$', b.jsonb_format, "
                "b.zstd_runtime = ztype.zstd_version(), b.zstd_compiled ~ '^[0-9]+\\.[0-9]+\\.[0-9]+$' "
                "FROM ztype.build_info() b, pg_extension e WHERE e.extname = 'ztype';", 't|t|1|t|t')
        a.query('CREATE INDEX corpus_json ON corpus USING gin ((meta::jsonb));')
        a.equal(training('first'), '1')
        a.query('CREATE TABLE dict_values(id integer, body ztext(6,1));')
        literal = 'first sample subject ' * 10
        a.query(f"CREATE TABLE default_values(body ztext(6,1) DEFAULT '{literal}'::ztext(6,1)); INSERT INTO default_values DEFAULT VALUES;")
        a.query("INSERT INTO dict_values SELECT 1, repeat('first sample subject ',1000);")
        # Dictionaries are addressable by registered name; only the slot reaches the catalog.
        a.query("CREATE TABLE named_values(body ztext(6,'first'), alt ztext(6, first));")
        a.equal("SELECT string_agg(format_type(atttypid, atttypmod), ',' ORDER BY attnum) FROM pg_attribute WHERE attrelid = 'named_values'::regclass AND attnum > 0;", 'ztext(6,1),ztext(6,1)')
        a.equal("SELECT pg_column_size(repeat('first sample subject ',100)::ztext(6,'first')) = pg_column_size(repeat('first sample subject ',100)::ztext(6,1));")
        a.equal("SELECT ztype.dictionary_slot('first');", '1')
        a.query("SELECT ztype.dictionary_slot('missing');", error='not registered')
        a.query("SELECT 'x'::ztext(6,'missing');", error='not registered')
        a.query("SELECT 'x'::ztext(six,1);", error='expected')
        a.equal("SELECT pg_typeof('x'::ztext(6,'1'))::text;", 'ztext')
        a.equal("SELECT slot, name, dict_bytes > 0, dict_id > 0 FROM ztype.dictionary_inventory;", '1|first|t|t')
        a.query("INSERT INTO ztype.dictionaries SELECT 200,dict_id,'42',dict,NULL,now() FROM ztype.dictionaries LIMIT 1;", error='check constraint')
        a.query("INSERT INTO ztype.dictionaries SELECT 200,dict_id,'',dict,NULL,now() FROM ztype.dictionaries LIMIT 1;", error='check constraint')
        a.query("SELECT ztype.add_dictionary('first', ztype.train_dictionary($$SELECT 'duplicate name sample ' || i || repeat(md5(i::text),4) FROM generate_series(1,1000) i$$, 2048));", error='dictionaries_name_key')
        b.equal("SELECT body::text = repeat('first sample subject ',1000) FROM dict_values;")
        # Real ordinary-role access, without exposing dictionary bytes or training queries.
        a.query('CREATE ROLE ztype_reader; GRANT SELECT ON dict_values TO ztype_reader;')
        b.query('SET ROLE ztype_reader;')
        b.equal('SELECT length(body::text) > 0 FROM dict_values;')
        b.equal("SELECT ztype.dictionary_slot('first') = 1 AND length((repeat('first sample subject ',100)::ztext(6,'first'))::text) > 0;")
        b.query('SELECT dict FROM ztype.dictionaries;', error='permission denied')
        b.equal("SELECT name FROM ztype.dictionary_inventory;", 'first')
        b.equal("SELECT (ztype.inspect(body)).dict_name FROM dict_values;", 'first')
        b.query("SELECT ztype.train_dictionary('SELECT 1');", error='permission denied')
        b.query("SET ztype.dict_path='/nonexistent';", error='invalid configuration parameter')
        b.query("SET ztype.restore_mode=on;", error='invalid configuration parameter')
        b.query('RESET ROLE;')
        delegation(cluster, a)
        # Misses refresh under READ COMMITTED even inside an already warm transaction.
        a.query('BEGIN;')
        a.equal('SELECT length(body::text) > 0 FROM dict_values;')
        b.equal(training('second'), '2')
        b.query('CREATE TABLE newer(body ztext(6,2));')
        b.query("INSERT INTO newer SELECT repeat('second sample subject ',1000);")
        a.equal('SELECT length(body::text) > 0 FROM newer;')
        a.query('COMMIT;')
        # Full rollback and savepoint rollback must discard uncommitted dictionaries.
        a.query('BEGIN;')
        a.equal(training('aborted'), '3')
        a.query("SELECT length((repeat('aborted sample subject ',100)::ztext(6,'aborted'))::text);")
        a.query("CREATE TABLE aborted_values(body ztext(6,'aborted'));")
        a.query('ROLLBACK;')
        out = a.query("SELECT length((repeat('aborted',100)::ztext(6,3))::text);")
        assert 'WARNING:  ztype: dictionary slot 3 is not available' in out, out
        a.query("SELECT ztype.recompress(repeat('aborted',100)::ztext, 6, 3);", error='not available')
        a.query("SELECT repeat('aborted',100)::ztext(6,'aborted');", error='not registered')
        a.query('BEGIN; SAVEPOINT s;')
        a.equal(training('savepoint'), '3')
        a.query("SELECT repeat('savepoint sample subject ',100)::ztext(6,3);")
        a.query('ROLLBACK TO s;')
        a.query("SELECT ztype.recompress(repeat('savepoint',100)::ztext, 6, 3);", error='not available')
        a.query('ROLLBACK;')
        # Registration is serialized; both writers receive a distinct slot.
        with concurrent.futures.ThreadPoolExecutor(2) as pool:
            results = list(pool.map(lambda pair: pair[0].query(training(pair[1])), [(a,'third'),(b,'fourth')]))
        assert set(results) == {'3','4'}, results
        # A logical write whose slot is not visible falls back to dictionary-free compression and
        # warns once per slot per transaction; decoding and recompress stay strict. Typed column and
        # domain defaults hit the same path at DDL time, which is what a restore does.
        a.query("CREATE DOMAIN early_body AS ztext(6,5) DEFAULT 'domain default'::ztext(6,5);")
        a.query("CREATE TABLE early(id integer, body ztext(6,5) DEFAULT 'column default'::ztext(6,5), dom early_body);")
        sample = "(SELECT string_agg('fifth sample subject ' || i || md5(i::text), ' ') FROM generate_series(1,50) i)"
        out = a.query(f"BEGIN; INSERT INTO early(id, body) SELECT 1, {sample}; INSERT INTO early(id, body) SELECT 2, {sample}; INSERT INTO early(id) VALUES (3); COMMIT;")
        assert out.count('WARNING:') == 1 and 'slot 5 is not available' in out, out
        a.equal(f"SELECT body::text = {sample} AND pg_column_size(body) = pg_column_size(body::text::ztext(6,0)) FROM early WHERE id = 1;")
        a.equal("SELECT body::text = 'column default' AND dom::text = 'domain default' FROM early WHERE id = 3;")
        a.query("SELECT ztype.recompress(body, 6, 5) FROM early WHERE id = 1;", error='not available')
        a.query('BEGIN;')
        a.query(f"INSERT INTO early(id, body) SELECT 4, {sample};")
        a.equal(training('fifth'), '5')
        a.query(f"INSERT INTO early(id, body) SELECT 5, {sample};")
        a.query('COMMIT;')
        a.equal("SELECT (SELECT pg_column_size(body) FROM early WHERE id = 5) < (SELECT pg_column_size(body) FROM early WHERE id = 4);")
        a.equal("SELECT bool_and(ztype.recompress(body, 6, 5)::text = body::text) FROM early WHERE body IS NOT NULL;")
        # Size comparisons only for compressed values: short raw values differ by varlena header width alone.
        a.equal("SELECT bool_and(pg_column_size(ztype.recompress(body, 6, 5)) <= pg_column_size(body)) FROM early WHERE raw_length(body) >= 64;")
        # jsonb and bytea training columns sample stored bytes: a jsonb dictionary helps zjsonb values.
        a.equal("SELECT ztype.add_dictionary('json', ztype.train_dictionary($$SELECT jsonb_build_object('body', 'sample ' || i, 'n', i, 'key', md5(i::text)) FROM generate_series(1,1000) i$$, 4096, 8192));", '6')
        a.equal("SELECT pg_column_size(jsonb_build_object('body', 'sample 7', 'n', 7, 'key', md5('7'))::zjsonb(6,'json')) < pg_column_size(jsonb_build_object('body', 'sample 7', 'n', 7, 'key', md5('7'))::zjsonb(6));")
        a.equal("SELECT ztype.dict_id(ztype.train_dictionary($$SELECT convert_to('sample ' || md5(i::text), 'UTF8') FROM generate_series(1,1000) i$$, 2048)) > 0;")
        a.query("SELECT ztype.add_dictionary('raw',convert_to(repeat('abc',100),'UTF8'));", error='trained dictionary')
        a.query('UPDATE ztype.dictionaries SET name = name;', error='append-only')
        a.query('DELETE FROM ztype.dictionaries;', error='append-only')
        a.query('TRUNCATE ztype.dictionaries;', error='append-only')
        a.query('SET session_replication_role = replica;')
        a.query('DELETE FROM ztype.dictionaries;', error='append-only')
        a.query('RESET session_replication_role;')
        a.query("INSERT INTO ztype.dictionaries SELECT 33554432,dict_id,'bad',dict,NULL,now() FROM ztype.dictionaries LIMIT 1;", error='check constraint')
        a.query("SELECT 'x'::ztext(6,33554432);", error='slot must be 0..33554431')
        a.query('CREATE TABLE wide(body ztext(3,33554431));')
        a.equal("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'wide'::regclass AND attname = 'body';", 'ztext(3,33554431)')
        out = a.query("INSERT INTO wide SELECT repeat('wide slot ', 20); SELECT (ztype.inspect(body)).level FROM wide;")
        assert 'slot 33554431 is not available' in out and out.endswith('3'), out
        # The widest modifier the encoding can hold: top level, top slot and the pending bit
        # (bit 30). It must stay a positive int32 and print back exactly.
        a.query('CREATE TABLE widest(body ztext(22,33554431,pending));')
        a.equal("SELECT atttypmod > 0, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'widest'::regclass AND attname = 'body';", 't|ztext(22,33554431,pending)')
        a.query("SELECT 'x'::ztext(0);", error='expected')
        a.query("SELECT 'x'::ztext(6,1,2);", error='expected')
        a.query("SELECT text_to_ztext('x',8192,false);", error='invalid type modifier')
        a.query("SELECT ztype.train_dictionary('SELECT 1',2048,0);", error='sample bytes')
        a.query("SELECT ztype.train_dictionary('SELECT 1',2048,10);", error='text, bytea or jsonb column')
        a.query("SELECT ztype.train_dictionary('SELECT meta FROM corpus',2048,10);", error='text, bytea or jsonb column')
        a.query("SELECT ztype.train_dictionary('SELECT null::text FROM generate_series(1,10)',2048,10);", error='8 nonempty')
        a.query("SELECT ztype.train_dictionary('SELECT 1; SELECT 2',2048,10);", error='one SELECT')
        # Per-level compression differs for a deliberately varied corpus.
        a.query('CREATE TABLE levels(a ztext(1,1), b ztext(22,1));')
        a.query("INSERT INTO levels SELECT string_agg(md5(i::text)||repeat('sample subject ',i%15),''), string_agg(md5(i::text)||repeat('sample subject ',i%15),'') FROM generate_series(1,500) i;")
        a.equal('SELECT a::text = b::text AND pg_column_size(a) <> pg_column_size(b) FROM levels;')
        a.equal('SELECT ztype.recompress(a,22,1)::text = a::text FROM levels;')
        a.equal('SELECT bool_and(ztype.recompress(meta,6,1)::jsonb = meta::jsonb AND ztype.recompress(blob,6,1)::bytea = blob::bytea) FROM corpus;')
        a.query('CREATE DOMAIN message_body AS ztext(6,1); CREATE TABLE domains(body message_body);')
        a.query("PREPARE ins(text) AS INSERT INTO domains VALUES($1); EXECUTE ins('hello');")
        a.equal("SELECT body::text = 'hello' FROM domains;")
        # ALTER COLUMN TYPE with a new modifier rewrites, like any type change, and every row ends up
        # with the column's policy; the same modifier is a no-op.
        rel = a.query("SELECT pg_relation_filenode('dict_values');")
        a.query('ALTER TABLE dict_values ALTER COLUMN body TYPE ztext(12,2);')
        assert a.query("SELECT pg_relation_filenode('dict_values');") != rel
        a.equal("SELECT i.level, i.dict_slot, body::text = repeat('first sample subject ',1000) FROM dict_values d, ztype.inspect(d.body) i;", '12|2|t')
        rel = a.query("SELECT pg_relation_filenode('dict_values');")
        a.query('ALTER TABLE dict_values ALTER COLUMN body TYPE ztext(12,2);')
        a.equal("SELECT pg_relation_filenode('dict_values');", rel)
        policy_paths(a, work, env)
        bare_column(a)
        incompressible(a)
        partial_reads(a)
        column_policies(a)
        deferred_policy(a, work, env)
        # A miss is forgotten as soon as a later snapshot could see the registration, even when
        # only read-only statements ran in between; under REPEATABLE READ it lasts the transaction.
        a.query('BEGIN;')
        out = a.query("SELECT (ztype.inspect(repeat('seventh sample subject ',20)::ztext(6,7))).dict_slot IS NULL;")
        assert 'slot 7 is not available' in out and out.endswith('t'), out
        b.equal(training('seventh'), '7')
        a.equal("SELECT (ztype.inspect(repeat('seventh sample subject ',20)::ztext(6,7))).dict_slot;", '7')
        a.query('COMMIT;')
        a.query('BEGIN ISOLATION LEVEL REPEATABLE READ;')
        out = a.query("SELECT (ztype.inspect(repeat('eighth sample subject ',20)::ztext(6,8))).dict_slot IS NULL;")
        assert 'slot 8 is not available' in out and out.endswith('t'), out
        b.equal(training('eighth'), '8')
        a.equal("SELECT (ztype.inspect(repeat('eighth sample subject ',20)::ztext(6,8))).dict_slot IS NULL;", 't')  # no second warning
        a.query('COMMIT;')
        a.equal("SELECT (ztype.inspect(repeat('eighth sample subject ',20)::ztext(6,8))).dict_slot;", '8')
        # Codec functions are parallel safe: plans stay parallel and a worker can load a dictionary.
        a.query('SET max_parallel_workers_per_gather = 2; SET parallel_setup_cost = 0; SET parallel_tuple_cost = 0; SET min_parallel_table_scan_size = 0;')
        plan = a.query("EXPLAIN (COSTS OFF) SELECT count(*) FROM corpus WHERE (meta ->> 'body') = 'x' AND raw_length(body) > 0 AND prefix(body, 3) <> 'zz';")
        assert 'Parallel Seq Scan' in plan, plan
        plan = a.query("EXPLAIN (COSTS OFF) SELECT count(*) FROM dict_values WHERE body::text <> '' AND ztype.recompress(body, 6, 1)::text <> '';")
        assert 'Parallel Seq Scan' in plan, plan
        a.query('SET debug_parallel_query = on;')
        a.equal("SELECT bool_and(body::text = repeat('first sample subject ',1000) AND (ztype.inspect(body)).dict_name = 'second') FROM dict_values;")
        a.equal("SELECT bool_and((meta ->> 'body') = source AND prefix(body, 5) = left(source, 5)) FROM corpus;")
        out = a.query("SELECT (ztype.inspect(repeat('x', 100)::ztext(6,9))).dict_slot IS NULL;")
        assert 'slot 9 is not available' in out and out.endswith('t'), out
        a.query('RESET debug_parallel_query; RESET max_parallel_workers_per_gather; RESET parallel_setup_cost; RESET parallel_tuple_cost; RESET min_parallel_table_scan_size;')
        # Expose raw datums ONLY in this superuser test database to inject corruption.
        # Offsets as seen by set_byte/get_byte (the bytea value excludes the 4-byte varlena
        # header): 0 magic, 4 rawlen, 8 format, 9 level, 10 kind, 11 codec, 12 payload.
        a.query('CREATE CAST (ztext AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS ztext) WITHOUT FUNCTION;')
        a.query('CREATE CAST (zjsonb AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS zjsonb) WITHOUT FUNCTION;')
        ghost = cache_bounds(a)
        decode_cache(a)
        equality(a)
        jsonb_operators(a)
        dict_report(cluster)
        parameter_shapes(a)
        input_hygiene(a)
        plain_reads(a)
        out = a.query("SELECT (decode('00','hex')::ztext)::text;", error='storage format')
        assert 'spike' not in out, out  # the hint names no project-internal history
        a.query("SELECT raw_length(decode('00','hex')::ztext);", error='storage format')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,0,0)::ztext)::text;", error='storage format')
        # Metadata corruption is caught structurally: declared length, format, kind and codec.
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,4,1)::ztext)::text;", error='invalid frame header')
        a.query("SELECT (set_byte(('short raw value'::ztext)::bytea,4,1)::ztext)::text;", error='invalid raw value length')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,8,1)::ztext)::text;", error='invalid value header')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,9,0)::ztext)::text;", error='invalid value header')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,9,23)::ztext)::text;", error='invalid value header')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,10,3)::ztext)::text;", error='invalid value header')
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,11,2)::ztext)::text;", error='invalid value header')
        a.query("SELECT (set_byte(('short raw value'::ztext)::bytea,11,1)::ztext)::text;", error='invalid frame header')
        # Payload corruption is caught by zstd: the frame's content checksum or block decoding.
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,octet_length((repeat('a',1000)::ztext)::bytea)-1,0)::ztext)::text;", error='checksum')
        a.query("SELECT (set_byte(v,20,get_byte(v,20)#255)::ztext)::text FROM (SELECT (string_agg(md5(i::text),' ')::ztext)::bytea v FROM generate_series(1,200) i) q;", error='ztype:')
        # As documented, a partial prefix stops before the frame checksum and cannot detect this.
        a.equal("SELECT prefix(set_byte((repeat('a',1000)::ztext)::bytea,octet_length((repeat('a',1000)::ztext)::bytea)-1,0)::ztext,5);", 'aaaaa')
        # The JSON payload-format marker is ztype's own constant, never the server major.
        a.equal("SELECT get_byte(('{\"a\":1}'::zjsonb)::bytea,8) = 1 AND get_byte(('{\"a\":1}'::zjsonb)::bytea,9) = 6;")
        a.equal("SELECT get_byte(('{\"a\":1}'::zjsonb)::bytea,8) <> current_setting('server_version_num')::int / 10000;")
        a.equal("SELECT get_byte(('x'::ztext)::bytea,8) = 0;")
        a.query("SELECT (set_byte(('{\"a\":1}'::zjsonb)::bytea,8,2)::zjsonb)::jsonb;", error='unsupported JSON payload format 2')
        a.query("SELECT (set_byte(('{\"a\":1}'::zjsonb)::bytea,8,current_setting('server_version_num')::int / 10000)::zjsonb)::jsonb;", error='unsupported JSON payload format')
        a.query("SELECT (set_byte(('{\"a\":1}'::zjsonb)::bytea,8,0)::zjsonb)::jsonb;", error='unsupported JSON payload format 0')
        a.query("SELECT set_byte(('{\"a\":1}'::zjsonb)::bytea,8,2)::zjsonb ->> 'a';", error='unsupported JSON payload format 2')
        # Boundaries the mutation harness (tests/mutate_ztype.py) exercises in bulk, pinned here.
        # A kind byte changed to another type of the same payload-format family (ztext<->zbytea, both
        # format 0): the base-type output cast rejects the mismatch, while recompress and the
        # polymorphic coercion honour the stored byte (they cannot see the SQL-declared type), so a
        # ztext relabelled to zbytea recompresses and coerces as bytea without error.
        a.query("SELECT (set_byte((repeat('a',1000)::ztext)::bytea,10,3)::ztext)::text;", error='invalid value header')
        a.equal("SELECT raw_length(ztype.recompress(set_byte((repeat('a',1000)::ztext)::bytea,10,3)::ztext, 6, 0)) = 1000;")
        a.equal("SELECT (ztype.inspect(set_byte((repeat('a',1000)::ztext)::bytea,10,3)::ztext)).kind;", 'bytea')
        # Trailing bytes after a complete frame are rejected on every full-decode path.
        a.query("SELECT ((repeat('a',1000)::ztext)::bytea || '\\x00'::bytea)::ztext::text;", error='trailing frame data')
        validate_sweep(a, ghost)
        cancel_secs, decode_cancel, full_decode, prefix_secs, growth_kb = memory(a)
        a.query('DROP CAST (ztext AS bytea); DROP CAST (bytea AS ztext); DROP CAST (zjsonb AS bytea); DROP CAST (bytea AS zjsonb);')
        print('PASS: codec, Unicode, JSON, binary, TOAST, roles, delegated administration, named dictionaries, rollback, concurrent dictionaries, missing-dictionary fallback, '
              'levels, policy paths, bare column, incompressible values, partial reads, miss cache, parallel, binary COPY, cache bounds, input hygiene, corruption, '
              f'JSON payload format, validate, memory (compress cancel {cancel_secs:.2f} s, decode cancel {decode_cancel:.3f} s and prefix {prefix_secs:.3f} s of {full_decode:.3f} s, RSS {growth_kb:+d} kB)', flush=True)
    finally:
        a.close()
        b.close()


def delegation(cluster, a):
    """Delegating dictionary administration to a non-superuser role. Registration
    (ztype.add_dictionary, ztype.import_dictionary) is SECURITY DEFINER, so EXECUTE is the whole
    grant and the role never holds any privilege on ztype.dictionaries; training keeps caller
    privileges, so the training query cannot read what the role cannot. This pins the grant list an
    administrator has to write, that each function in it is load-bearing, and that the role still
    cannot read dictionary bytes afterwards. Runs in its own database so the slots it hands out do
    not renumber the rest of the suite."""
    a.query('CREATE DATABASE ztype_delegation;')
    s, d = cluster.session('ztype_delegation'), cluster.session('ztype_delegation')
    try:
        s.query('CREATE EXTENSION ztype;')
        s.query("CREATE TABLE public.source(body text); "
                "INSERT INTO public.source SELECT 'delegated sample subject ' || i || repeat(md5(i::text),4) "
                'FROM generate_series(1,1000) i;')
        s.query("CREATE TABLE public.private(body text); INSERT INTO public.private VALUES ('training data');")
        s.equal(training('house'), '1')  # one superuser registration first, so 'next slot' means 2
        s.query('CREATE ROLE ztype_admin; '
                'GRANT CREATE ON SCHEMA public TO ztype_admin; '
                'GRANT SELECT ON public.source TO ztype_admin;')
        d.query('SET ROLE ztype_admin;')
        register = "SELECT ztype.train_and_add('delegated', $$SELECT body FROM public.source$$, 2048);"
        functions = {'train': 'ztype.train_dictionary(text,integer,integer)', 'add': 'ztype.add_dictionary(text,bytea,text)',
                     'wrap': 'ztype.train_and_add(text,text,integer)'}
        # Each function in the grant is load-bearing: train_and_add runs with caller privileges and
        # calls the other two as the caller. Nothing reaches the registry until all three are granted.
        for grants in ((), ('wrap',), ('wrap', 'train'), ('wrap', 'add'), ('train', 'add')):
            s.query('REVOKE ALL ON FUNCTION ' + ', '.join(functions.values()) + ' FROM ztype_admin;'
                    + (' GRANT EXECUTE ON FUNCTION ' + ', '.join(functions[g] for g in grants) + ' TO ztype_admin;' if grants else ''))
            assert d.sqlstate(register) == '42501', grants
            s.equal('SELECT count(*) = 1 FROM ztype.dictionaries;', 't')
        # The documented grant list: EXECUTE on the three functions, no table privilege, not even
        # on ztype.dict_id (the registry's CHECK constraint runs as the definer).
        s.query('GRANT EXECUTE ON FUNCTION ' + ', '.join(functions.values()) + ' TO ztype_admin;')
        d.equal(register, '2')
        d.query("CREATE TABLE public.delegated_values(body ztext(6,'delegated'));")
        d.query("INSERT INTO public.delegated_values SELECT repeat('delegated sample subject ', 100);")
        d.equal("SELECT i.dict_slot, i.dict_name, v.body::text = repeat('delegated sample subject ', 100) "
                'FROM public.delegated_values v, ztype.inspect(v.body) i;', '2|delegated|t')
        # Registration by bytes, and import with a preserved slot, are the same grant shape.
        d.equal("SELECT ztype.add_dictionary('by-bytes', ztype.train_dictionary($$SELECT body FROM public.source$$, 4096));", '3')
        assert d.sqlstate("SELECT ztype.import_dictionary(9, 'imported', ztype.train_dictionary($$SELECT body FROM public.source$$, 3072));") == '42501'
        s.query('GRANT EXECUTE ON FUNCTION ztype.import_dictionary(integer,text,bytea,text) TO ztype_admin;')
        d.equal("SELECT ztype.import_dictionary(9, 'imported', ztype.train_dictionary($$SELECT body FROM public.source$$, 3072));", '9')
        assert d.sqlstate("SELECT ztype.import_dictionary(9, 'other', ztype.train_dictionary($$SELECT body FROM public.source$$, 5120));") == '42710'
        assert d.sqlstate("SELECT ztype.add_dictionary('garbage', 'not a dictionary'::bytea);") == '22023'
        # What the role does not get: the bytes, the row count, and any write to the registry.
        assert d.sqlstate('SELECT dict FROM ztype.dictionaries;') == '42501'
        assert d.sqlstate('SELECT count(*) FROM ztype.dictionaries;') == '42501'
        assert d.sqlstate("INSERT INTO ztype.dictionaries SELECT 20, dict_id, 'direct', dict FROM ztype.dictionaries;") == '42501'
        assert d.sqlstate('UPDATE ztype.dictionaries SET name = name;') == '42501'
        assert d.sqlstate('DELETE FROM ztype.dictionaries;') == '42501'
        assert d.sqlstate('TRUNCATE ztype.dictionaries;') == '42501'
        d.equal("SELECT string_agg(slot::text || ':' || name, ',' ORDER BY slot) FROM ztype.dictionary_inventory;",
                '1:house,2:delegated,3:by-bytes,9:imported')
        # The training query runs with the caller's rights: a table the role cannot read never trains.
        assert d.sqlstate("SELECT ztype.train_and_add('leak', $$SELECT body FROM public.private$$, 2048);") == '42501'
        s.equal('SELECT count(*) = 4 FROM ztype.dictionaries;', 't')
        # It is a grant to one role, not a widening: an ordinary reader is where it was.
        d.query('RESET ROLE; SET ROLE ztype_reader;')
        d.query('SELECT dict FROM ztype.dictionaries;', error='permission denied')
        d.query("SELECT ztype.train_and_add('reader', $$SELECT body FROM public.source$$, 2048);",
                error='permission denied')
        d.query("SELECT ztype.add_dictionary('reader', '\\x37a430ec'::bytea);", error='permission denied')
        d.query("SELECT ztype.import_dictionary(10, 'reader', '\\x37a430ec'::bytea);", error='permission denied')
        d.equal("SELECT count(*) = 4 FROM ztype.dictionary_inventory;", 't')
        d.query('RESET ROLE;')
    finally:
        s.close()
        d.close()


def plain_reads(a):
    """The README's first example: a column selected as it is prints the decoded value and compares
    against a literal without a cast, and the assignment casts move it into a base-type column; the
    cast is what a base-type function or operator needs, and the result column's type otherwise
    stays the compressed one."""
    a.query("CREATE TABLE plain_reads(body ztext(6), meta zjsonb(6), payload zbytea(6), copy_of text);")
    a.query("""INSERT INTO plain_reads VALUES ('hello world', '{"source": "email"}', '\\x0000ff', NULL);""")
    a.equal("SELECT body, meta, payload FROM plain_reads;", 'hello world|{"source": "email"}|\\x0000ff')
    a.equal("SELECT pg_typeof(body), pg_typeof(body::text), pg_typeof(meta::jsonb), pg_typeof(payload::bytea) FROM plain_reads;",
            'ztext|text|jsonb|bytea')
    a.equal("""SELECT body = 'hello world', meta = '{"source":"email"}', payload = '\\x0000ff', meta ->> 'source' FROM plain_reads;""",
            't|t|t|email')
    a.query("UPDATE plain_reads SET copy_of = body;")  # assignment cast, no ::text needed
    a.equal("SELECT copy_of = body::text FROM plain_reads;")
    a.equal("SELECT length(body::text), body::text LIKE 'hello%', jsonb_typeof(meta::jsonb), octet_length(payload::bytea) FROM plain_reads;",
            '11|t|object|3')
    for sql, missing in (("SELECT length(body) FROM plain_reads;", 'function length(ztext) does not exist'),
                         ("SELECT body LIKE 'hello%' FROM plain_reads;", 'operator does not exist: ztext ~~ unknown'),
                         ("SELECT jsonb_typeof(meta) FROM plain_reads;", 'function jsonb_typeof(zjsonb) does not exist'),
                         ("SELECT octet_length(payload) FROM plain_reads;", 'function octet_length(zbytea) does not exist')):
        a.query(sql, error=missing)
    a.query('DROP TABLE plain_reads;')
    print('PASS: plain reads and literal comparisons need no cast; base-type functions do', flush=True)


def input_hygiene(a):
    """Malformed jsonb and bytea text input is reported through the caller's error context, so
    pg_input_is_valid() and COPY ... ON_ERROR behave as for the base types and nothing is stored;
    an unrepresentable type modifier is still ztype's own error, not the integer parser's."""
    a.equal("SELECT pg_input_is_valid('{', 'zjsonb'), pg_input_is_valid('\\xzz', 'zbytea');", 'f|f')
    a.equal("SELECT pg_input_is_valid('{\"a\":1}', 'zjsonb'), pg_input_is_valid('{\"a\":1}', 'zjsonb(6,1)'), "
            "pg_input_is_valid('\\xdeadbeef', 'zbytea(9)'), pg_input_is_valid('plain', 'ztext(6,''first'')');", 't|t|t|t')
    # The reported problem is the base type's, verbatim: ztype adds no wrapper of its own.
    a.equal("SELECT message = (SELECT message FROM pg_input_error_info('{', 'jsonb')) FROM pg_input_error_info('{', 'zjsonb');")
    a.equal("SELECT message = (SELECT message FROM pg_input_error_info('\\xzz', 'bytea')) FROM pg_input_error_info('\\xzz', 'zbytea');")
    # A soft failure is a report, not a value: the hard path still raises.
    a.query("SELECT '{'::zjsonb;", error='invalid input syntax for type json')
    a.query("SELECT '\\xzz'::zbytea;", error='invalid hexadecimal digit')

    # The base-type parse runs before zt_compress, so a rejected value never touches the registry.
    a.query('BEGIN;')
    before = a.query('SELECT entries, bytes, loads FROM ztype.dictionary_cache_stats();')
    a.equal("SELECT pg_input_is_valid('{', 'zjsonb(6,1)'), pg_input_is_valid('\\xzz', 'zbytea(6,1)');", 'f|f')
    a.equal('SELECT entries, bytes, loads FROM ztype.dictionary_cache_stats();', before)
    a.query('COMMIT;')

    body = 'first sample subject ' * 20
    a.query("CREATE TABLE soft_copy(id integer, meta zjsonb(6,'first'));")
    a.query('COPY soft_copy FROM STDIN WITH (ON_ERROR ignore);\n'
            f'1\t{{"body":"{body}"}}\n'
            '2\t{\n'
            f'3\t{{"body":"{body}"}}\n'
            '\\.')
    # The malformed row is skipped; the rows that loaded carry the column's policy.
    a.equal("SELECT string_agg(id::text, ',' ORDER BY id) FROM soft_copy;", '1,3')
    a.equal("SELECT bool_and(i.level = 6 AND i.codec = 'zstd' AND i.dict_slot = 1 AND (meta ->> 'body') = %s) "
            "FROM soft_copy, ztype.inspect(meta) i;" % ("'" + body + "'"))
    a.equal('SELECT entries >= 1 FROM ztype.dictionary_cache_stats();')  # the column's dictionary outlives the COPY
    a.query('DROP TABLE soft_copy;')

    # A digit-only modifier that does not fit in int32 is rejected by ztype, with ztype's message.
    for bad in ("ztext(99999999999)", "ztext(6,99999999999)", "ztext(4294967302)", "zjsonb(99999999999)"):
        out = a.query(f"SELECT 'x'::{bad};", error='ztype:')
        assert 'out of range' not in out, (bad, out)
    a.equal("SELECT pg_typeof('x'::ztext(00006))::text, format_type(atttypid, atttypmod) FROM pg_attribute "
            "WHERE attrelid = 'named_values'::regclass AND attnum = 1;", 'ztext|ztext(6,1)')
    # Name resolution reads the registry, so the typmod input function is STABLE, never IMMUTABLE.
    a.equal("SELECT provolatile FROM pg_proc WHERE proname = 'ztext_typmod_in';", 's')


def policy_paths(a, work, env):
    """Every way a logical value enters a column gets the column's policy; stored values move unchanged."""
    lit = 'first sample subject ' * 8
    a.query("CREATE TABLE policy(id integer, body ztext(9,'first'));")
    a.query(f"INSERT INTO policy VALUES (1, '{lit}');")                         # literal
    a.query(f"PREPARE untyped AS INSERT INTO policy VALUES (2, $1); EXECUTE untyped('{lit}');")  # inferred parameter
    a.query(f"PREPARE typed(text) AS INSERT INTO policy VALUES (3, $1); EXECUTE typed('{lit}');")  # text parameter
    a.query(f"INSERT INTO policy VALUES (4, '{lit}'::ztext);")                  # explicit cast without modifier
    a.query("INSERT INTO policy SELECT 5, repeat('first sample subject ', 8);")   # text expression
    a.query("INSERT INTO policy SELECT 6, CASE WHEN id = 1 THEN body END FROM dict_values;")  # computed from a ztext column
    a.query(f"ALTER TABLE policy ALTER COLUMN body SET DEFAULT '{lit}'; INSERT INTO policy(id) VALUES (7);")  # default
    a.query(f"COPY policy(id, body) FROM STDIN;\n8\t{lit}\n\\.")               # COPY text
    a.query("CREATE DOMAIN policy_body AS ztext(9,'first'); CREATE TABLE policy_dom(id integer, body policy_body);")
    a.query(f"INSERT INTO policy_dom VALUES (1, '{lit}');")                       # domain
    a.query("CREATE TABLE policy_arr(id integer, bodies ztext(9,'first')[]);")
    a.query(f"INSERT INTO policy_arr VALUES (1, ARRAY['{lit}', '{lit}']);")       # array element
    a.equal("SELECT count(*), bool_and(i.level = 9 AND i.dict_slot = 1 AND i.codec = 'zstd'), "
            "bool_and(body::text = repeat('first sample subject ', CASE WHEN id = 6 THEN 1000 ELSE 8 END)) FROM policy p, ztype.inspect(p.body) i;", '8|t|t')
    a.equal("SELECT i.level, i.dict_slot FROM policy_dom d, ztype.inspect(d.body) i;", '9|1')
    a.equal("SELECT i.level, i.dict_slot FROM policy_arr d, ztype.inspect(d.bodies[2]) i;", '9|1')
    a.equal("SELECT i.level, i.dict_slot FROM ztype.inspect(repeat('a', 100)::ztext(12)) i;", '12|')
    # Values too short to compress still get the level and the dictionary validation of the policy.
    out = a.query("SELECT i.codec, i.level FROM ztype.inspect('hi'::ztext(9,65000)) i;")
    assert 'slot 65000 is not available' in out and out.endswith('raw|9'), out
    out = a.query("SELECT i.codec, i.level FROM ztype.inspect(('hi'::text)::ztext(9,65000)) i;")
    assert 'slot 65000 is not available' in out and out.endswith('raw|9'), out
    # Query shape does not matter: column references, CTEs, CASE and explicit casts all land on the policy.
    a.query('INSERT INTO policy SELECT 20, body FROM dict_values;')                                  # column from a (12,2) column
    a.query('INSERT INTO policy SELECT 21, body::text FROM dict_values;')
    a.query('INSERT INTO policy WITH v AS NOT MATERIALIZED (SELECT ztype.recompress(body, 3, 0) AS b FROM dict_values) SELECT 22, b FROM v;')
    a.query('INSERT INTO policy WITH v AS MATERIALIZED (SELECT ztype.recompress(body, 3, 0) AS b FROM dict_values) SELECT 23, b FROM v;')
    a.query('INSERT INTO policy SELECT 24, CASE WHEN true THEN body ELSE NULL END FROM dict_values;')
    a.query('INSERT INTO policy SELECT 25, body::ztext(9,1) FROM dict_values;')
    a.query('INSERT INTO policy SELECT 26, ztype.recompress(body, 3, 0) FROM dict_values;')
    a.equal("SELECT count(*), bool_and(i.level = 9 AND i.dict_slot = 1 AND body::text = repeat('first sample subject ',1000)) FROM policy p, ztype.inspect(p.body) i WHERE id >= 20;", '7|t')
    # The column's policy wins on assignment; the catch-up recipe after a restore is a plain self-assignment.
    a.query('UPDATE policy SET body = ztype.recompress(body, 3, 0) WHERE id = 1;')
    a.equal("SELECT i.level, i.dict_slot FROM policy p, ztype.inspect(p.body) i WHERE id = 1;", '9|1')
    a.query("ALTER TABLE policy ALTER COLUMN body TYPE ztext(9,'json');")
    a.equal("SELECT bool_and(i.level = 9 AND i.dict_slot = 6) FROM policy p, ztype.inspect(p.body) i;")
    extended_protocol(a, env)
    # A value that already carries the policy is returned as is, not recompressed.
    a.equal("SELECT pg_column_size(v::ztext(9,1)) = pg_column_size(v) AND pg_column_size(v::ztext(9,0)) > pg_column_size(v) FROM (SELECT repeat('first sample subject ', 8)::ztext(9,1) v) q;")
    # Binary protocol: send/receive carry logical values, and receive applies the column policy.
    a.query("CREATE TABLE binary_src(id integer, body ztext(9,'first'), meta zjsonb(9,'first'), blob zbytea(9,'first'));")
    a.query("CREATE TABLE binary_dst(id integer, body ztext(4,'json'), meta zjsonb(4,'json'), blob zbytea(4));")
    a.query(f"INSERT INTO binary_src SELECT i, '{lit}' || i, jsonb_build_object('body', '{lit}', 'i', i), convert_to('{lit}' || i, 'UTF8') FROM generate_series(1, 5) i;")
    a.query(f"COPY binary_src TO '{work}/binary.copy' (FORMAT binary); COPY binary_dst FROM '{work}/binary.copy' (FORMAT binary);")
    a.equal("SELECT bool_and(s.body::text = d.body::text AND s.meta::jsonb = d.meta::jsonb AND s.blob::bytea = d.blob::bytea) FROM binary_src s JOIN binary_dst d USING (id);")
    a.equal("SELECT i.level, i.dict_slot, j.level, j.dict_slot, k.level, k.dict_slot FROM binary_dst d, ztype.inspect(d.body) i, ztype.inspect(d.meta) j, ztype.inspect(d.blob) k WHERE id = 1;", '4|6|4|6|4|')


def bare_column(a):
    """A target with no modifier is the one place the policy contract does not reach: PostgreSQL
    applies no length coercion towards typmod -1, so a bare ztext column compresses logical input at
    (6,0) through the input function or the base-type cast but stores a value moved from another
    ztype column exactly as it arrives, dictionary and all. ztext(6) is how a column enforces (6,0),
    for rows already stored as well. README: "When the modifier applies"."""
    a.query('CREATE TABLE plain(id integer, body ztext); CREATE TABLE six(id integer, body ztext(6));')
    a.query('INSERT INTO plain SELECT 1, body FROM dict_values;')   # a (12,2) column at this point
    a.query('INSERT INTO six SELECT 1, body FROM dict_values;')
    a.equal("SELECT i.level, i.dict_slot FROM plain p, ztype.inspect(p.body) i;", '12|2')
    a.equal("SELECT i.level, i.dict_slot FROM six s, ztype.inspect(s.body) i;", '6|')
    a.query("INSERT INTO plain SELECT 2, repeat('first sample subject ',1000);")   # logical input
    a.equal("SELECT i.level, i.dict_slot FROM plain p, ztype.inspect(p.body) i WHERE id = 2;", '6|')
    rel = a.query("SELECT pg_relation_filenode('plain');")
    a.query('ALTER TABLE plain ALTER COLUMN body TYPE ztext(6);')
    assert a.query("SELECT pg_relation_filenode('plain');") != rel
    a.equal("SELECT count(*), bool_and(i.level = 6 AND i.dict_slot IS NULL AND body::text = repeat('first sample subject ',1000)) "
            "FROM plain p, ztype.inspect(p.body) i;", '2|t')


def incompressible(a):
    """A value zstd cannot shrink stays raw at the requested level on every path that writes one:
    the base-type cast, a cast with a modifier, recompress and the same-type coercion into a column.
    Random bytes are the only reliable way there (the frame zstd would emit is longer than the
    input), and the round trip must still be exact. README: "Compression policy"."""
    a.query("CREATE TABLE noise AS SELECT (SELECT string_agg(decode(replace(gen_random_uuid()::text,'-',''),'hex'),'') "
            "FROM generate_series(1,192)) AS b;")   # 3 kB of random bytes, core functions only
    a.equal("SELECT octet_length(b) FROM noise;", '3072')
    a.equal("SELECT i.codec, i.level, i.raw_length, i.stored_bytes = i.raw_length + 12, b::zbytea::bytea = b "
            "FROM noise, ztype.inspect(b::zbytea) i;", 'raw|6|3072|t|t')
    # A dictionary cannot help either, and asking for one on a value that stays raw is not a miss.
    out = a.query("SELECT i.codec, i.level, i.dict_id IS NULL FROM noise, ztype.inspect(b::zbytea(6,1)) i;")
    assert out == 'raw|6|t' and 'WARNING' not in out, out
    a.equal("SELECT i.codec, i.level, ztype.recompress(b::zbytea, 22, 1)::bytea = b "
            "FROM noise, ztype.inspect(ztype.recompress(b::zbytea, 22, 1)) i;", 'raw|22|t')
    # The coercion runs (level 6 is not the column's) and falls back to raw at the column's level.
    a.query('CREATE TABLE noise_col(id integer, v zbytea(9,2));')
    a.query('INSERT INTO noise_col SELECT 1, b::zbytea FROM noise;')
    a.equal("SELECT i.codec, i.level, i.dict_id IS NULL, c.v::bytea = n.b FROM noise_col c, noise n, ztype.inspect(c.v) i;", 'raw|9|t|t')


def partial_reads(a):
    """raw_length reads only the envelope from TOAST: on a 1 MB out-of-line value it touches an
    order of magnitude fewer buffers than the decoding cast, which fetches every chunk. prefix is
    the other partial read; its decode is timed against the 128 MB frame in memory()."""
    a.query('CREATE TABLE toasted(v zbytea); ALTER TABLE toasted ALTER COLUMN v SET STORAGE external;')
    a.query("INSERT INTO toasted SELECT string_agg(decode(replace(gen_random_uuid()::text,'-',''),'hex'),'') "
            "FROM generate_series(1,65536);")   # incompressible, so TOAST stores it whole and out of line
    a.equal("SELECT raw_length(v) = 1048576 AND pg_column_size(v) > 1000000 FROM toasted;")

    def buffers(sql):
        plan = json.loads(a.query(f'EXPLAIN (ANALYZE, BUFFERS, TIMING OFF, COSTS OFF, FORMAT JSON) {sql}'))[0]['Plan']
        return plan['Shared Hit Blocks'] + plan['Shared Read Blocks']

    envelope = buffers('SELECT raw_length(v) FROM toasted;')
    whole = buffers('SELECT length(v::bytea) FROM toasted;')
    assert envelope < 10 and whole > 100, (envelope, whole)
    # inspect reads the envelope and the frame header only, and takes the stored size from the
    # TOAST pointer, so it costs what raw_length costs and still reports the whole value's size.
    inspected = buffers('SELECT (ztype.inspect(v)).stored_bytes FROM toasted;')
    assert inspected < 10, (inspected, envelope)
    a.equal("SELECT i.stored_bytes = 1048576 + 12 AND i.raw_length = 1048576 AND i.codec = 'raw' FROM toasted, ztype.inspect(v) i;")
    # A frame out of line (3.3 MB of hex halves): the dictionary ID is in the frame header, so the
    # read stops there too, and stored_bytes is the whole frame although only its start was fetched.
    a.query("UPDATE toasted SET v = (SELECT convert_to(string_agg(md5(i::text),' '),'UTF8')::zbytea FROM generate_series(1,100000) i);")
    inspected = buffers('SELECT (ztype.inspect(v)).dict_id FROM toasted;')
    assert inspected < 10, inspected
    a.equal("SELECT i.codec = 'zstd' AND i.raw_length = 3299999 AND i.stored_bytes = pg_column_size(v) "
            "AND i.stored_bytes BETWEEN 1000000 AND 2000000 FROM toasted, ztype.inspect(v) i;")
    a.query('DROP TABLE toasted;')


def column_policies(a):
    """ztype.column_policies lists every stored column of the three types with its decoded
    modifier and the dictionary the slot resolves to here; a slot that resolves to nothing is what
    a table-only restore or a lagging subscriber leaves behind, and the view is how to find it.
    Bare columns show the (6, 0) they apply to logical input, flagged as having no modifier."""
    a.query('CREATE SCHEMA pol; CREATE TABLE pol.t(id integer, plain text, bare ztext, named zjsonb(9,1), '
            'numeric_slot zbytea(3,1), unregistered ztext(6,999), pending ztext(9,1,pending), dropped ztext(6,1)); '
            'ALTER TABLE pol.t DROP COLUMN dropped; CREATE VIEW pol.v AS SELECT bare FROM pol.t; '
            'CREATE MATERIALIZED VIEW pol.m AS SELECT named FROM pol.t;')
    a.equal("SELECT table_name, column_name, type_name, has_modifier, level, slot, pending, coalesce(dict_name,'-'), coalesce(dict_id::text,'-') "
            "FROM ztype.column_policies WHERE schema_name = 'pol' ORDER BY table_name, column_name;",
            'm|named|zjsonb|t|9|1|f|first|' + a.query('SELECT dict_id FROM ztype.dictionary_inventory WHERE slot = 1;') + '\n'
            't|bare|ztext|f|6|0|f|-|-\n'
            't|named|zjsonb|t|9|1|f|first|' + a.query('SELECT dict_id FROM ztype.dictionary_inventory WHERE slot = 1;') + '\n'
            't|numeric_slot|zbytea|t|3|1|f|first|' + a.query('SELECT dict_id FROM ztype.dictionary_inventory WHERE slot = 1;') + '\n'
            't|pending|ztext|t|9|1|t|first|' + a.query('SELECT dict_id FROM ztype.dictionary_inventory WHERE slot = 1;') + '\n'
            't|unregistered|ztext|t|6|999|f|-|-')
    # The README's recipe: dictionary columns whose slot this database cannot resolve.
    a.equal("SELECT table_name || '.' || column_name FROM ztype.column_policies "
            "WHERE schema_name = 'pol' AND slot > 0 AND dict_name IS NULL;", 't.unregistered')
    a.query('SET ROLE ztype_reader;')   # readable by every role, like the inventory it joins
    a.equal("SELECT count(*) FROM ztype.column_policies WHERE schema_name = 'pol';", '6')
    a.query('RESET ROLE; DROP SCHEMA pol CASCADE;')


def deferred_policy(a, work, env):
    """ztype.set_column_policy re-points a column at a new policy without a rewrite: the catalog
    modifier changes, the filenode does not, stored rows keep the policy they were written with and
    new writes get the new one. The modifier carries `pending` until every row is caught up and
    ztype.finish_column_policy has checked that under its lock. What makes the interval sound is
    that a pending modifier equals no plain one, so PostgreSQL coerces every move out of the column
    (the leak a truthful-looking modifier would open); expression indexes, CHECK constraints and
    trigger conditions are rewritten in place so their Vars keep matching the column; views and
    extended statistics are refused. README: "Changing the policy without a rewrite"."""
    lit = 'first sample subject ' * 20
    pending92 = 9 | (2 << 5) | (1 << 30)
    a.query(f"CREATE TABLE deferred(id integer PRIMARY KEY, body ztext(6,'first') DEFAULT '{lit}', n integer, CHECK (raw_length(body) < 100000));")
    a.query("CREATE FUNCTION deferred_trg() RETURNS trigger LANGUAGE plpgsql AS 'BEGIN NEW.n := 1; RETURN NEW; END';")
    a.query('CREATE TRIGGER deferred_t BEFORE INSERT OR UPDATE OF body ON deferred FOR EACH ROW WHEN (raw_length(NEW.body) > 0) EXECUTE FUNCTION deferred_trg();')
    a.query('INSERT INTO deferred(id) SELECT i FROM generate_series(1, 200) i; CREATE INDEX deferred_body ON deferred ((body::text)); ANALYZE deferred;')
    a.query('PREPARE deferred_ins AS INSERT INTO deferred(id) VALUES ($1); EXECUTE deferred_ins(0);')
    rel = a.query("SELECT pg_relation_filenode('deferred');")

    def plan():
        return a.query("SET enable_seqscan = off; EXPLAIN (COSTS OFF) SELECT id FROM deferred WHERE body::text = 'x'; RESET enable_seqscan;")

    assert 'Index Scan using deferred_body' in plan(), plan()
    a.query("SELECT ztype.set_column_policy('deferred', 'body', 9, 'second');")
    a.equal("SELECT pg_relation_filenode('deferred') = %s, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'deferred'::regclass AND attname = 'body';" % rel, 't|ztext(9,2,pending)')
    a.equal("SELECT level, slot, pending, dict_name FROM ztype.column_policies WHERE table_name = 'deferred';", '9|2|t|second')
    # Stored rows are untouched; a default, a prepared statement planned before the change and a
    # plain insert all write the new policy, and the trigger still fires.
    a.equal("SELECT i.level, i.dict_slot, count(*), bool_and(n = 1) FROM deferred d, ztype.inspect(d.body) i GROUP BY 1, 2 ORDER BY 1;", '6|1|201|t')
    a.query("EXECUTE deferred_ins(301); INSERT INTO deferred(id) VALUES (302); INSERT INTO deferred(id, body) VALUES (303, repeat('second sample subject ', 20));")
    a.equal("SELECT count(*), bool_and(i.level = 9 AND i.dict_slot = 2 AND n = 1) FROM deferred d, ztype.inspect(d.body) i WHERE id > 300;", '3|t')
    # The index expression, the CHECK constraint and the trigger condition now name the new modifier.
    assert 'Index Scan using deferred_body' in plan(), plan()
    a.equal(f"SELECT bool_and(t ~ ':vartypmod {pending92} ') AND bool_and(t !~ ':vartypmod 38 ') FROM (SELECT indexprs::text AS t FROM pg_index WHERE indexrelid = 'deferred_body'::regclass "
            "UNION ALL SELECT conbin::text FROM pg_constraint WHERE conrelid = 'deferred'::regclass AND contype = 'c' UNION ALL SELECT tgqual::text FROM pg_trigger WHERE tgrelid = 'deferred'::regclass) q;")
    # The leak the pending bit closes: a plain (9,2) column coerces what it takes from a pending
    # (9,2) column, because the modifiers differ; another pending (9,2) column does not, and its
    # own finish then sees the rows.
    a.query('CREATE TABLE deferred_plain(id integer, body ztext(9,2)); INSERT INTO deferred_plain SELECT id, body FROM deferred;')
    a.equal("SELECT count(*), bool_and(i.level = 9 AND i.dict_slot = 2) FROM deferred_plain d, ztype.inspect(d.body) i;", '204|t')
    a.query('CREATE TABLE deferred_twin(id integer, body ztext(9,2,pending)); INSERT INTO deferred_twin SELECT id, body FROM deferred;')
    a.equal("SELECT count(*) FROM deferred_twin WHERE NOT ztype.matches_policy(body, 9, 2);", '201')
    a.query("SELECT ztype.finish_column_policy('deferred_twin', 'body');", error='201 rows of column "body" still carry another policy')
    a.query('DROP TABLE deferred_twin;')
    # finish refuses while rows are off the policy; the batched catch-up is the README recipe,
    # its predicate selects nothing on a rerun, and finish then clears the bit without a rewrite.
    a.query("SELECT ztype.finish_column_policy('deferred', 'body');", error='201 rows of column "body" still carry another policy')
    a.equal("SELECT count(*) FROM deferred WHERE NOT ztype.matches_policy(body, 9, 2);", '201')
    for lo, hi in [(0, 100), (100, 400)]:
        a.query(f'UPDATE deferred SET body = body::ztext(9,2) WHERE id >= {lo} AND id < {hi} AND NOT ztype.matches_policy(body, 9, 2);')
    a.equal("SELECT count(*) FROM deferred WHERE NOT ztype.matches_policy(body, 9, 2);", '0')
    a.query("SELECT ztype.finish_column_policy('deferred', 'body');")
    a.equal("SELECT pg_relation_filenode('deferred') = %s, format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'deferred'::regclass AND attname = 'body';" % rel, 't|ztext(9,2)')
    a.equal("SELECT count(*), bool_and(i.level = 9 AND i.dict_slot = 2), bool_and(body::text = CASE WHEN id = 303 THEN repeat('second sample subject ', 20) ELSE '%s' END) FROM deferred d, ztype.inspect(d.body) i;" % lit, '204|t|t')
    a.equal("SELECT pending FROM ztype.column_policies WHERE table_name = 'deferred';", 'f')
    assert 'Index Scan using deferred_body' in plan(), plan()
    a.query("SELECT ztype.finish_column_policy('deferred', 'body');")   # nothing pending: a no-op
    # Refusals: the caller must own the table, the column must be one of the three types, a view
    # or extended statistics on the column block the change as they block ALTER TABLE, and a
    # partition is changed through its parent.
    a.query("SET ROLE ztype_reader; SELECT ztype.set_column_policy('deferred', 'body', 3);", error='must be owner')
    a.query("RESET ROLE; SELECT ztype.set_column_policy('deferred', 'id', 3);", error='not a ztext, zjsonb or zbytea column')
    a.query("SELECT ztype.set_column_policy('deferred', 'missing', 3);", error='does not exist')
    a.query("SELECT ztype.set_column_policy('deferred', 'body', 3, 'missing');", error='not registered')
    a.query("SELECT ztype.set_column_policy('deferred', 'body', 0);", error='level 1..22')
    a.query("SELECT ztype.set_column_policy('deferred', 'body', 3, 33554432);", error='slot must be 0..33554431')
    a.query("CREATE VIEW deferred_v AS SELECT body FROM deferred; SELECT ztype.set_column_policy('deferred', 'body', 3);", error='view public.deferred_v depends on it')
    a.query("DROP VIEW deferred_v; CREATE STATISTICS deferred_s ON (body::text), id FROM deferred; SELECT ztype.set_column_policy('deferred', 'body', 3);", error='statistics object public.deferred_s depends on it')
    a.query('DROP STATISTICS deferred_s;')
    a.query("BEGIN; DECLARE deferred_c CURSOR FOR SELECT * FROM deferred; SELECT ztype.set_column_policy('deferred', 'body', 3);", error='being used by active queries')
    a.query('ROLLBACK;')
    # A partitioned table recurses, and finish counts every partition.
    a.query('CREATE TABLE deferred_part(id integer, body ztext(6,1)) PARTITION BY RANGE (id); '
            'CREATE TABLE deferred_p1 PARTITION OF deferred_part FOR VALUES FROM (0) TO (100); CREATE TABLE deferred_p2 PARTITION OF deferred_part FOR VALUES FROM (100) TO (200); '
            f"INSERT INTO deferred_part SELECT i, '{lit}' FROM generate_series(1, 199) i; CREATE INDEX deferred_part_body ON deferred_part ((body::text));")
    a.query("SELECT ztype.set_column_policy('deferred_p1', 'body', 9, 2);", error='inherited column')
    a.query("SELECT ztype.set_column_policy('deferred_part', 'body', 9, 2);")
    a.equal("SELECT string_agg(c.relname || ' ' || format_type(atttypid, atttypmod), ', ' ORDER BY c.relname) FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE attname = 'body' AND c.relname ~ '^deferred_p(art|[12])$';",
            'deferred_p1 ztext(9,2,pending), deferred_p2 ztext(9,2,pending), deferred_part ztext(9,2,pending)')
    a.query("SELECT ztype.finish_column_policy('deferred_part', 'body');", error='199 rows')
    a.query('UPDATE deferred_part SET body = body::ztext(9,2) WHERE NOT ztype.matches_policy(body, 9, 2);')
    a.query("SELECT ztype.finish_column_policy('deferred_part', 'body');")
    a.equal("SELECT string_agg(format_type(atttypid, atttypmod), ', ') FROM pg_attribute a JOIN pg_class c ON c.oid = a.attrelid WHERE attname = 'body' AND c.relname ~ '^deferred_p(art|[12])$';", 'ztext(9,2), ztext(9,2), ztext(9,2)')
    plan_part = a.query("SET enable_seqscan = off; EXPLAIN (COSTS OFF) SELECT id FROM deferred_part WHERE body::text = 'x'; RESET enable_seqscan;")
    assert plan_part.count('Index Scan') == 2, plan_part
    # ALTER COLUMN TYPE to a plain modifier is the other way out: it rewrites and clears the bit
    # (and refuses a trigger on the column, which set_column_policy handles in place).
    a.query("SELECT ztype.set_column_policy('deferred', 'body', 3, 'json'); DROP TRIGGER deferred_t ON deferred;")
    rel = a.query("SELECT pg_relation_filenode('deferred');")
    a.query('ALTER TABLE deferred ALTER COLUMN body TYPE ztext(3,6);')
    assert a.query("SELECT pg_relation_filenode('deferred');") != rel
    a.equal("SELECT format_type(atttypid, atttypmod), (SELECT bool_and(i.level = 3 AND i.dict_slot = 6) FROM deferred d, ztype.inspect(d.body) i) FROM pg_attribute WHERE attrelid = 'deferred'::regclass AND attname = 'body';", 'ztext(3,6)|t')
    # The modifier syntax round-trips the state (dumps, pg_upgrade, LIKE); anything else in third place is rejected.
    a.equal("SELECT pg_typeof('x'::ztext(6,1,pending))::text, 'x'::ztext(6,0,PENDING)::text, (ztype.inspect(jsonb_build_object('k', repeat('a', 100))::zjsonb(3,1,pending))).level;", 'ztext|x|3')
    a.query("SELECT 'x'::ztext(6,1,\"Pending\");", error='expected')
    a.query('CREATE TABLE deferred_like (LIKE deferred_plain); INSERT INTO deferred_like SELECT * FROM deferred_plain;')
    a.query("SELECT ztype.set_column_policy('deferred_like', 'body', 9, 2); CREATE TABLE deferred_like2 (LIKE deferred_like);")
    a.equal("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'deferred_like2'::regclass AND attname = 'body';", 'ztext(9,2,pending)')
    a.query("SELECT ztype.finish_column_policy('deferred_like2', 'body'); DROP TABLE deferred_like2, deferred_plain;")   # empty: a scan of nothing
    # deferred_like stays pending with rows already on its policy: backups() restores it and finishes it.
    # A bare column can be given a policy this way; the rows that keep a dictionary (bare_column) are what finish then reports.
    a.query("CREATE TABLE deferred_bare(body ztext); INSERT INTO deferred_bare SELECT repeat('first sample subject ', 20)::ztext(6,1); SELECT ztype.set_column_policy('deferred_bare', 'body', 6);")
    a.query("SELECT ztype.finish_column_policy('deferred_bare', 'body');", error='1 row of column "body" still carry another policy')
    a.query("SELECT ztype.set_column_policy('deferred_bare', 'body', 6, 999); SELECT ztype.finish_column_policy('deferred_bare', 'body');", error='slot 999 is not available')
    a.query('DROP TABLE deferred_bare;')
    # matches_policy is the predicate's contract: level, then raw passes, a frame must name the slot.
    a.equal("SELECT ztype.matches_policy('x'::ztext, 6, 0), ztype.matches_policy('x'::ztext, 9, 0), ztype.matches_policy(repeat('a',100)::ztext(6,1), 6, 1), "
            "ztype.matches_policy(repeat('a',100)::ztext(6,1), 6, 0), ztype.matches_policy(repeat('a',100)::ztext(6,0), 6, 1), ztype.matches_policy('{}'::zjsonb, 6, 0), ztype.matches_policy('\\x00'::zbytea, 6, 0);", 't|f|t|f|f|t|t')
    a.equal("SELECT ztype.matches_policy(b::zbytea(6,1), 6, 1) FROM noise;")   # raw because incompressible: on the policy at its level
    a.query("SELECT ztype.matches_policy(repeat('a',100)::ztext(6,1), 6, 999);", error='not available')
    a.query("SELECT ztype.matches_policy(repeat('a',100)::ztext, 23, 0);", error='invalid policy')


def extended_protocol(a, env):
    """Extended-protocol parameters through libpq (tests/pq_params.c): inferred and typed, text and
    binary format, NULLs, a bytea with embedded zero bytes, Unicode text, and one prepared
    statement reused across formats until the plan cache goes generic, then deallocated and
    prepared again typed, and a pipeline in which one statement fails and takes its implicit
    transaction with it. Parameters arrive with typmod -1 and are then coerced to the column
    policy; binary results are the logical values, which the C program compares byte for byte,
    along with the SQLSTATE of every binary payload the receive functions reject and the usability
    of the connection afterwards. Here the oracle is ztype.inspect: 22 rows, four of them NULL in
    all three columns, every stored value at the column's level 9 and dictionary slot 1, and none
    of the three ids the failed pipeline transaction covered."""
    exe = Path(env['PGHOST']) / 'pq_params'
    inc, lib = run([PG_CONFIG, '--includedir']), run([PG_CONFIG, '--libdir'])
    run(['cc', '-o', exe, ROOT / 'tests' / 'pq_params.c', f'-I{inc}', f'-L{lib}', '-lpq'])
    a.query("CREATE TABLE pq(id integer, body ztext(9,'first'), meta zjsonb(9,'first'), blob zbytea(9,'first'));")
    assert run([exe], env=env) == 'OK'
    # Rejected parameters stored nothing; a NULL parameter is NULL in every column of its row.
    a.equal("SELECT count(*), count(body), count(meta), count(blob) FROM pq;", '22|18|18|18')
    a.equal("SELECT count(*) FROM pq WHERE id BETWEEN 21 AND 23;", '0')
    a.equal("SELECT count(*) FROM pq WHERE (body IS NULL) <> (meta IS NULL) OR (body IS NULL) <> (blob IS NULL);", '0')
    # IS TRUE, because bool_and would ignore the NULL a value stored raw (no dictionary) produces.
    a.equal("SELECT bool_and(((ztype.inspect(body)).level = 9 AND (ztype.inspect(body)).dict_slot = 1 "
            "AND (ztype.inspect(meta)).level = 9 AND (ztype.inspect(meta)).dict_slot = 1 "
            "AND (ztype.inspect(blob)).level = 9 AND (ztype.inspect(blob)).dict_slot = 1) IS TRUE) "
            "FROM pq WHERE body IS NOT NULL;")


def parameter_shapes(a):
    """An untyped parameter or an unknown-type literal into a modified column is compressed by the
    input function at the default policy and again by the coercion, which decodes it in between; a
    parameter or literal typed as the base type goes through the base-type cast with the column
    modifier and is compressed once. The decode counters are the oracle; the stored policy must be
    the column's either way. Documented in README under "When the modifier applies"."""
    def decodes(sql):
        before = int(a.query('SELECT hits + misses FROM ztype.decode_cache_stats();'))
        a.query(sql)
        return int(a.query('SELECT hits + misses FROM ztype.decode_cache_stats();')) - before

    a.query("CREATE TEMP TABLE ps_default (doc zjsonb); CREATE TEMP TABLE ps_dict (doc zjsonb(6, 'json')); CREATE TEMP TABLE ps_text (body ztext(9, 1));")
    doc = '{"k": "' + 'v' * 200 + '"}'
    for table, untyped, typed in (('ps_default', 0, 0), ('ps_dict', 1, 0)):
        assert decodes(f"INSERT INTO {table} VALUES ($1) \\bind '{doc}' \\g") == untyped, table
        assert decodes(f"INSERT INTO {table} VALUES ($1::jsonb) \\bind '{doc}' \\g") == typed, table
        assert decodes(f"INSERT INTO {table} VALUES ('{doc}');") == untyped, table
        assert decodes(f"INSERT INTO {table} VALUES ('{doc}'::jsonb);") == typed, table
    assert decodes(f"INSERT INTO ps_text VALUES ($1) \\bind '{'w' * 200}' \\g") == 1
    assert decodes(f"INSERT INTO ps_text VALUES ($1::text) \\bind '{'w' * 200}' \\g") == 0
    a.equal("SELECT bool_and((ztype.inspect(doc)).dict_name = 'json' AND (ztype.inspect(doc)).level = 6) AND count(*) = 4 FROM ps_dict;")
    a.equal("SELECT bool_and((ztype.inspect(body)).dict_slot = 1 AND (ztype.inspect(body)).level = 9) AND count(*) = 2 FROM ps_text;")
    a.query('DROP TABLE ps_default; DROP TABLE ps_dict; DROP TABLE ps_text;')


def dict_report(cluster):
    """tests/dict_report.py fetches one column read-only from a source database (this cluster), evaluates
    a dictionary on it in its own disposable cluster, and rejects a query with the wrong shape."""
    script = ROOT / 'tests' / 'dict_report.py'
    source = f"host={cluster.env['PGHOST']} port={cluster.env['PGPORT']} dbname=postgres"
    out = run([sys.executable, script, '--source', source, '--query', 'SELECT source FROM corpus, generate_series(1, 12)',
               '--levels', '1,6', '--runs', '1', '--sample-bytes', '1024', '--port', '55472'], env=cluster.env)
    assert 'text values: 492 rows' in out and '| 1 |' in out and '| 6 |' in out and '| value size |' in out, out
    s = cluster.session()
    try:
        s.equal("SELECT count(*) FROM ztype.dictionaries WHERE name = 'eval';", '0')  # nothing registered in the source
    finally:
        s.close()
    bad = subprocess.run([sys.executable, script, '--source', source, '--query', 'SELECT id, source FROM corpus'],
                         env=cluster.env, text=True, capture_output=True)
    assert bad.returncode and 'exactly one column' in bad.stderr, (bad.returncode, bad.stderr)


def decode_cache(a):
    """Repeated references to one stored value decode once: three keys of a row, a filter and a
    projection on the same column, and a prefix after a full decode. The cache holds two entries,
    only compressed values up to 256 kB, and nothing that failed to decode."""
    def stats():
        return [int(x) for x in a.query('SELECT hits, misses, bytes FROM ztype.decode_cache_stats();').split('|')]

    def delta(sql, expected=None):
        before = stats()
        result = a.query(sql)
        if expected is not None:
            assert result == expected, (sql, result, expected)
        after = stats()
        return after[0] - before[0], after[1] - before[1], after[2]

    a.query("CREATE TEMP TABLE dc AS SELECT i, jsonb_build_object('a', i, 'b', repeat('x', 100), 'c', 'k' || i)::zjsonb AS doc, "
            "(repeat('row ' || i || ' ', 40))::ztext AS body, (repeat('other ' || i || ' ', 30))::ztext AS other FROM generate_series(1, 3) i;")
    a.equal("SELECT bool_and((ztype.inspect(doc)).codec = 'zstd' AND (ztype.inspect(body)).codec = 'zstd') FROM dc;")
    hits, misses, _ = delta("SELECT string_agg((doc ->> 'a') || (doc ->> 'c') || length(doc ->> 'b'), ',' ORDER BY i) FROM dc;", '1k1100,2k2100,3k3100')
    assert (hits, misses) == (6, 3), (hits, misses)
    hits, misses, _ = delta("SELECT count(*) FROM dc WHERE body::text LIKE 'row %' AND length(body::text) > 10 AND prefix(body, 3) = 'row';", '3')
    assert (hits, misses) == (6, 3), (hits, misses)
    # Two entries: the operands of a binary operator both stay cached, a third value evicts the
    # least recently used; recompressing the same logical value yields the same bytes (zstd is
    # deterministic), so it hits.
    hits, misses, _ = delta("SELECT bool_and(doc::text <> body::text AND doc::text <> '') FROM dc;", 't')
    assert (hits, misses) == (3, 6), (hits, misses)
    hits, misses, _ = delta("SELECT bool_and(length(doc::text) > 0 AND length(body::text) > 0 AND length(other::text) > 0 AND length(doc::text) > 0) FROM dc;", 't')
    assert (hits, misses) == (0, 12), (hits, misses)
    hits, misses, _ = delta("SELECT bool_and(doc::text = (doc::jsonb::zjsonb)::text) FROM dc;", 't')
    assert (hits, misses) == (6, 3), (hits, misses)  # doc::text misses, doc::jsonb and the recompressed copy hit
    # Short raw values never touch it; values above the size limit are decoded but not kept.
    hits, misses, _ = delta("SELECT ('{\"a\": 1}'::zjsonb ->> 'a') || ('{\"a\": 1}'::zjsonb ->> 'a');", '11')
    assert (hits, misses) == (0, 0), (hits, misses)
    a.query("CREATE TEMP TABLE dcbig AS SELECT repeat('b', 300000)::ztext AS body;")
    hits, misses, kept = delta("SELECT length(body::text) + length(body::text) FROM dcbig;", '600000')
    assert (hits, misses) == (0, 2) and kept < 300000, (hits, misses, kept)
    # A corrupt value is never remembered: the intact value it was made from still misses afterwards.
    a.query("SELECT (set_byte(body::bytea, octet_length(body::bytea) - 1, 0)::ztext)::text FROM dc WHERE i = 1;", error='checksum')
    hits, misses, _ = delta("SELECT body::text = body::text FROM dc WHERE i = 1;", 't')
    assert (hits, misses) == (1, 1), (hits, misses)
    a.query('DROP TABLE dc; DROP TABLE dcbig;')


def equality(a):
    """`=`, `<>` and the default hash operator classes compare decoded values, whatever policy stored
    them, and decide from the envelope alone when the stored bytes are identical or, for ztext and
    zbytea, the declared lengths differ. They give the planner hash aggregation, hash joins, IN
    lists, hash partitioning, hash indexes and statistics; nothing orders. Runs after the raw casts
    exist (corrupt frames) and after slots 1 and 2 are registered."""
    def stats():
        return [int(x) for x in a.query('SELECT hits, misses FROM ztype.decode_cache_stats();').split('|')]

    def decodes(sql, expected):
        before = stats()
        a.equal(sql, expected)
        after = stats()
        return after[1] - before[1]

    body = "'the same logical value, long enough to compress: ' || repeat('lorem ipsum ', 12)"
    doc = "jsonb_build_object('k', repeat('v', 120), 'n', 1, 'list', jsonb_build_array(1, 2, 3))"
    # One logical value under five policies (levels 1 and 9, slot 1, slot 2 and the default): every
    # pair is equal and hashes alike, for both hash support functions, though the stored bytes differ.
    a.query(f"CREATE TEMP TABLE eqv AS SELECT ({body})::ztext(1,0) t1, ({body})::ztext(9,1) t2, ({body})::ztext(6,2) t3, "
            f"({body})::ztext t4, ({doc})::zjsonb(1,0) j1, ({doc})::zjsonb(9,1) j2, ({doc})::zjsonb(3,2) j3, "
            f"convert_to({body}, 'UTF8')::zbytea(1,0) b1, convert_to({body}, 'UTF8')::zbytea(9,2) b2;")
    a.equal("SELECT t1::bytea <> t2::bytea AND t2::bytea <> t3::bytea AND j1::bytea <> j2::bytea FROM eqv;")
    a.equal("SELECT t1 = t2 AND t2 = t3 AND t3 = t4 AND t1 = t4 AND NOT t1 <> t2 AND j1 = j2 AND j2 = j3 AND NOT j1 <> j3 AND b1 = b2 AND NOT b1 <> b2 FROM eqv;")
    a.equal("SELECT ztext_hash(t1) = ztext_hash(t2) AND ztext_hash(t2) = ztext_hash(t4) AND zjsonb_hash(j1) = zjsonb_hash(j3) AND zbytea_hash(b1) = zbytea_hash(b2) FROM eqv;")
    a.equal("SELECT ztext_hash_extended(t1, 7) = ztext_hash_extended(t3, 7) AND ztext_hash_extended(t1, 7) <> ztext_hash_extended(t1, 8) "
            "AND zjsonb_hash_extended(j1, 7) = zjsonb_hash_extended(j2, 7) AND zbytea_hash_extended(b1, 7) = zbytea_hash_extended(b2, 7) FROM eqv;")
    # The hash is the base type's: text under the C collation, jsonb's own, so the values agree with
    # what a plain column of the base type hashes to (hash partitioning and joins can rely on it).
    a.equal("SELECT ztext_hash(t1) = hashtext(t1::text COLLATE \"C\") AND zjsonb_hash(j1) = jsonb_hash(j1::jsonb) "
            "AND ztext_hash_extended(t1, 3) = hashtextextended(t1::text COLLATE \"C\", 3) AND zjsonb_hash_extended(j1, 3) = jsonb_hash_extended(j1::jsonb, 3) FROM eqv;")
    # Base-type semantics: ztext and zbytea are bytewise, zjsonb structural (key order, numeric scale).
    a.equal("SELECT 'a'::ztext <> 'A'::ztext, 'x'::ztext = 'x'::ztext, '\\x00'::bytea::zbytea <> '\\x0000'::bytea::zbytea, "
            "'{\"b\": 1.0, \"a\": [1]}'::zjsonb = '{\"a\": [1], \"b\": 1.00}'::zjsonb, '[1]'::zjsonb <> '[1, 1]'::zjsonb;", 't|t|t|t|t')
    # Envelope decisions: identical stored bytes and, for ztext and zbytea, different declared lengths
    # decode nothing; equal-length compressed values of different bytes decode both sides.
    assert decodes("SELECT t2 = t2, t2 <> t2 FROM eqv;", 't|f') == 0
    assert decodes(f"SELECT t2 = ({body} || 'x')::ztext(9,1), b1 = convert_to({body} || 'x', 'UTF8')::zbytea(1,0) FROM eqv;", 'f|f') == 0
    assert decodes(f"SELECT t2 = translate({body}, 'lorem', 'merol')::ztext(9,1) FROM eqv;", 'f') == 2
    assert decodes("SELECT t1 = t2 FROM eqv;", 't') == 2
    assert decodes("SELECT j1 = ('{\"n\": 2}'::zjsonb) FROM eqv;", 'f') == 1  # the short literal is raw: one frame decoded
    # A corrupt frame: identical bytes compare equal and a length mismatch compares unequal with no
    # verification, as documented; anything that decodes raises what the cast raises.
    a.query("CREATE TEMP TABLE eqc AS SELECT set_byte(t2::bytea, octet_length(t2::bytea) - 1, 0)::ztext bad, t2 good FROM eqv;")
    a.equal("SELECT bad = bad, bad <> bad, bad = (good::text || 'x')::ztext FROM eqc;", 't|f|f')
    a.query("SELECT bad = good FROM eqc;", error='checksum')
    a.query("SELECT ztext_hash(bad) FROM eqc;", error='checksum')
    a.query("SELECT (decode('00', 'hex')::ztext) = (decode('00', 'hex')::ztext);", error='storage format')
    # An ordinary role compares dictionary frames it could not load itself.
    a.query('SET ROLE ztype_reader;')
    a.equal(f"SELECT ({body})::ztext(6,1) = ({body})::ztext(6,2), ztext_hash(({body})::ztext(6,1)) = ztext_hash(({body})::ztext);", 't|t')
    a.query('RESET ROLE;')
    # What the planner can now do: hash aggregation for GROUP BY, DISTINCT and UNION, a hash join, an
    # IN list and = ANY, all on the compressed column and without a cast; nothing sort-based.
    a.query("CREATE TEMP TABLE eqg AS SELECT i, ('group ' || (i % 5) || ' ' || repeat('payload ', 20))::ztext(6,1) body, "
            "jsonb_build_object('g', i % 5, 'pad', repeat('p', 100))::zjsonb doc FROM generate_series(1, 200) i;")
    a.query("CREATE TEMP TABLE eqh AS SELECT i, ('group ' || (i % 5) || ' ' || repeat('payload ', 20))::ztext(9,2) body FROM generate_series(1, 5) i;")
    a.query('SET enable_sort = off;')  # a sort would need a btree opclass; hashing must carry every plan below
    a.equal("SELECT count(*), sum(n) FROM (SELECT body, doc, count(*) n FROM eqg GROUP BY body, doc) q;", '5|200')
    a.equal("SELECT count(*) FROM (SELECT DISTINCT body FROM eqg UNION SELECT body FROM eqh) q;", '5')
    a.equal("SELECT count(*) FROM (SELECT body FROM eqg INTERSECT SELECT body FROM eqh) q;", '5')
    a.equal("SELECT count(*), count(DISTINCT h.i) FROM eqg g JOIN eqh h ON g.body = h.body;", '200|5')
    a.equal("SELECT count(*) FROM eqg WHERE body IN (SELECT body FROM eqh WHERE i IN (1, 2));", '80')
    a.equal("SELECT count(*) FROM eqg WHERE body = ANY (ARRAY(SELECT body FROM eqh WHERE i = 3)) OR doc = '{\"g\": 4, \"pad\": \"" + 'p' * 100 + "\"}';", '80')
    plan = a.query("EXPLAIN (COSTS OFF) SELECT body, count(*) FROM eqg GROUP BY body;")
    assert 'HashAggregate' in plan, plan
    plan = a.query("EXPLAIN (COSTS OFF) SELECT count(*) FROM eqg g JOIN eqh h ON g.body = h.body;")
    assert 'Hash Join' in plan and 'Hash Cond: (h.body = g.body)' in plan, plan
    a.query('RESET enable_sort;')
    a.query('SELECT body FROM eqg ORDER BY body LIMIT 1;', error='could not identify an ordering operator')
    a.query('SELECT count(DISTINCT body) FROM eqg;', error='could not identify an ordering operator')  # aggregate DISTINCT sorts
    a.query('SELECT max(body) FROM eqg;', error='function max(ztext) does not exist')
    # ANALYZE gathers distinct-value and most-common-value statistics through the equality operator,
    # and the estimate for an equality filter comes from them.
    # ANALYZE decodes each sample value once and runs the base type's analyzer over the decoded
    # values: distinct count and most-common values as for the base type, the latter stored
    # re-encoded under the column's policy, no histogram or correlation (nothing orders), and the
    # estimate for an equality filter comes from them. Values declared wider than 1 kB are never
    # decoded and count as distinct, like the standard analyzer's too-wide values.
    misses = decodes('ANALYZE eqg; SELECT 1;', '1')
    assert misses == 400, misses  # 200 rows, two columns, one decode each
    a.equal("SELECT n_distinct, null_frac, array_length(most_common_vals::text::text[], 1), histogram_bounds IS NULL, correlation IS NULL "
            "FROM pg_stats WHERE tablename = 'eqg' AND attname = 'body';", '5|0|5|t|t')
    a.equal("SELECT n_distinct, array_length(most_common_vals::text::text[], 1), sum(f) FROM pg_stats, unnest(most_common_freqs) f "
            "WHERE tablename = 'eqg' AND attname = 'doc' GROUP BY 1, 2;", '5|5|1')
    # The slot names the type's own equality, and the values are the column's datums: the five
    # most-common values take exactly the bytes the five distinct stored values take.
    a.equal("SELECT stakind1, staop1::regoperator, stacoll1, stakind2 FROM pg_statistic WHERE starelid = 'eqg'::regclass AND staattnum = 2;", '1|=(ztext,ztext)|0|0')
    a.equal("SELECT (SELECT pg_column_size(stavalues1) FROM pg_statistic WHERE starelid = 'eqg'::regclass AND staattnum = 2) "
            "= (SELECT pg_column_size(array_agg(body)) FROM (SELECT body FROM eqg GROUP BY body) q);")
    a.equal("SELECT count(*) FROM pg_stats, unnest(most_common_vals::text::ztext[]) v WHERE tablename = 'eqg' AND attname = 'body' "
            "AND v = ('group 3 ' || repeat('payload ', 20))::ztext;", '1')
    a.equal("SELECT avg_width BETWEEN 40 AND 120 FROM pg_stats WHERE tablename = 'eqg' AND attname = 'body';")
    plan = a.query("EXPLAIN SELECT * FROM eqg WHERE body = (SELECT body FROM eqh WHERE i = 1);")
    assert 'rows=40 ' in plan, plan
    a.query("CREATE TEMP TABLE eqw AS SELECT CASE WHEN i <= 50 THEN ('narrow ' || i % 5 || repeat(' pad', 20))::ztext WHEN i <= 80 THEN "
            "(i || repeat(' wide value', 200))::ztext END body, repeat('b', 100000)::zbytea AS blob FROM generate_series(1, 100) i;")
    a.equal("SELECT count(*) FILTER (WHERE raw_length(body) > 1024), count(*) FILTER (WHERE body IS NULL) FROM eqw;", '30|20')
    assert decodes('ANALYZE eqw; SELECT 1;', '1') == 50  # the narrow rows only; nothing wide is decoded
    a.equal("SELECT attname, n_distinct, null_frac, coalesce(array_length(most_common_vals::text::text[], 1), 0) FROM pg_stats "
            "WHERE tablename = 'eqw' ORDER BY attname;", 'blob|-1|0|0\nbody|-0.35|0.2|5')  # 5 narrow values + 30 wide, each its own
    a.query('DROP TABLE eqw;')
    # A hash index and hash partitioning both key on the decoded value: a probe with a value stored
    # under another policy finds the rows, and the row lands in the same partition as its base type.
    a.query('CREATE INDEX eqg_body ON eqg USING hash (body); SET enable_seqscan = off;')
    plan = a.query("EXPLAIN (COSTS OFF) SELECT count(*) FROM eqg WHERE body = (SELECT body FROM eqh WHERE i = 2);")
    assert 'Index Scan using eqg_body' in plan or 'Bitmap Index Scan on eqg_body' in plan, plan
    a.equal("SELECT count(*) FROM eqg WHERE body = (SELECT body FROM eqh WHERE i = 2);", '40')
    a.query('RESET enable_seqscan;')
    a.query('CREATE TEMP TABLE eqp (body ztext(3,1), t text) PARTITION BY HASH (body); '
            'CREATE TEMP TABLE eqp0 PARTITION OF eqp FOR VALUES WITH (MODULUS 4, REMAINDER 0); '
            'CREATE TEMP TABLE eqp1 PARTITION OF eqp FOR VALUES WITH (MODULUS 4, REMAINDER 1); '
            'CREATE TEMP TABLE eqp2 PARTITION OF eqp FOR VALUES WITH (MODULUS 4, REMAINDER 2); '
            'CREATE TEMP TABLE eqp3 PARTITION OF eqp FOR VALUES WITH (MODULUS 4, REMAINDER 3);')
    a.query("INSERT INTO eqp SELECT body, body::text FROM eqg;")
    a.equal("SELECT count(*) FROM eqp;", '200')
    a.equal("SELECT bool_and(satisfies_hash_partition('eqp'::regclass, 4, substr(tableoid::regclass::text, 4)::int, body)) FROM eqp;")
    a.equal("SELECT count(DISTINCT tableoid) > 1 FROM eqp;")
    plan = a.query("EXPLAIN (COSTS OFF) SELECT * FROM eqp WHERE body = ('group 1 ' || repeat('payload ', 20))::ztext;")
    assert plan.count('Seq Scan') == 1, plan  # pruned to one partition
    a.query('DROP TABLE eqv, eqc, eqg, eqh, eqp;')
    print('PASS: equality and hash operator classes', flush=True)


def jsonb_operators(a):
    """Every jsonb operator that reads is native on zjsonb: the object-key accessors in C, the rest
    as SQL functions the planner inlines into the cast, so an index on `(doc::jsonb)` or on an
    accessor expression is matched from the operator form and the cost is the cast's one decode."""
    doc = "'{\"a\": {\"b\": [10, 20, {\"c\": \"deep\"}]}, \"tags\": [\"x\", \"y\"], \"n\": 1}'"
    a.equal(f"SELECT d -> 1, d ->> 1, d -> -1 IS NULL FROM (SELECT '[\"p\", \"q\"]'::zjsonb d) q;", '"q"|q|f')
    a.equal(f"SELECT d #> '{{a,b,2,c}}', d #>> '{{a,b,2,c}}', d #> '{{a,nope}}' IS NULL FROM (SELECT {doc}::zjsonb d) q;", '"deep"|deep|t')
    a.equal(f"SELECT d ? 'tags', d ? 'zz', d ?| array['zz', 'n'], d ?& array['a', 'n'], d ?& array['a', 'zz'] FROM (SELECT {doc}::zjsonb d) q;", 't|f|t|t|f')
    a.equal(f"SELECT d @> '{{\"n\": 1}}', d @> '{{\"n\": 2}}', '{{\"n\": 1}}' <@ d, d <@ '{{\"n\": 1}}' FROM (SELECT {doc}::zjsonb d) q;", 't|f|t|f')
    a.equal(f"SELECT d @? '$.a.b[*] ? (@ > 15)', d @? '$.a.b[*] ? (@ > 25)', d @@ '$.n == 1', d @@ '$.n == 2' FROM (SELECT {doc}::zjsonb d) q;", 't|f|t|f')
    # The same answers the base type gives, on a document long enough to be compressed, and strict.
    a.query(f"CREATE TEMP TABLE jo AS SELECT i, jsonb_build_object('id', i, 'tags', jsonb_build_array('t' || i % 3, 'all'), 'pad', repeat('p', 200), "
            "'nest', jsonb_build_object('k', i % 7))::zjsonb doc FROM generate_series(1, 300) i;")
    a.equal("SELECT bool_and((ztype.inspect(doc)).codec = 'zstd') FROM jo;")
    a.equal("SELECT bool_and(doc @> '{\"tags\": [\"all\"]}' = doc::jsonb @> '{\"tags\": [\"all\"]}' AND doc ? 'pad' = doc::jsonb ? 'pad' AND "
            "doc #>> '{nest,k}' = doc::jsonb #>> '{nest,k}' AND doc -> 'tags' -> 0 = doc::jsonb -> 'tags' -> 0 AND doc @? '$.nest.k ? (@ > 3)' = doc::jsonb @? '$.nest.k ? (@ > 3)') FROM jo;")
    a.equal("SELECT count(*) FILTER (WHERE doc @> '{\"tags\": [\"t1\"]}'), count(*) FILTER (WHERE doc @@ '$.nest.k == 0'), count(*) FILTER (WHERE doc ->> 'id' = '7') FROM jo;", '100|42|1')
    a.equal("SELECT NULL::zjsonb @> '{}' IS NULL, '{}'::zjsonb @> NULL IS NULL, NULL::zjsonb #>> '{a}' IS NULL;", 't|t|t')
    # Inlined: the plan shows the base operator over the cast, and a GIN index on the cast is used
    # from the operator form; an index on an accessor expression matches the same expression.
    plan = a.query("EXPLAIN (VERBOSE, COSTS OFF) SELECT doc @> '{\"id\": 1}', doc #>> '{nest,k}' FROM jo;")
    assert "((doc)::jsonb @> '{\"id\": 1}'::jsonb)" in plan and "((doc)::jsonb #>> '{nest,k}'::text[])" in plan, plan
    a.query("CREATE INDEX jo_gin ON jo USING gin ((doc::jsonb)); CREATE INDEX jo_k ON jo ((doc #>> '{nest,k}')); "
            "CREATE INDEX jo_path ON jo USING gin ((doc::jsonb) jsonb_path_ops); SET enable_seqscan = off;")
    for sql in ("SELECT count(*) FROM jo WHERE doc @> '{\"tags\": [\"t2\"]}';", "SELECT count(*) FROM jo WHERE doc ? 'pad';",
                "SELECT count(*) FROM jo WHERE doc @? '$.tags[*] ? (@ == \"t2\")';"):
        plan = a.query('EXPLAIN (COSTS OFF) ' + sql)
        assert 'Bitmap Index Scan on jo_' in plan, (sql, plan)
    a.equal("SELECT count(*) FROM jo WHERE doc @> '{\"tags\": [\"t2\"]}';", '100')
    plan = a.query("EXPLAIN (COSTS OFF) SELECT count(*) FROM jo WHERE doc #>> '{nest,k}' = '3';")
    assert 'jo_k' in plan, plan
    a.equal("SELECT count(*) FROM jo WHERE doc #>> '{nest,k}' = '3';", '43')
    a.query('RESET enable_seqscan;')
    # The decode is the cast's: a corrupt frame raises what the cast raises, and an ordinary role
    # reads dictionary frames through it.
    a.query("SELECT set_byte(doc::bytea, octet_length(doc::bytea) - 1, 0)::zjsonb @> '{}' FROM jo WHERE i = 1;", error='checksum')
    a.query('SET ROLE ztype_reader;')
    a.equal("SELECT jsonb_build_object('k', repeat('v', 100))::zjsonb(6,1) @> '{\"k\": \"" + 'v' * 100 + "\"}';")
    a.query('RESET ROLE; DROP TABLE jo;')
    print('PASS: native zjsonb operators', flush=True)


def cache_bounds(a):
    """The backend's cache stays within ztype.dictionary_cache_size: least recently used dictionaries
    are evicted and reloaded transparently, accounting is exact across compression-object swaps, errors
    and rollbacks, and a budget below one entry still round-trips values. Entries outlive the
    transaction that loaded them once it commits; an abort, a savepoint rollback or PREPARE TRANSACTION
    drops what that transaction loaded, so a rolled-back registration never lingers. Needs the raw casts.
    Returns the ghost value: a frame whose dictionary was rolled back, reused by validate_sweep."""
    def stats():
        return [int(x) for x in a.query('SELECT entries, bytes, budget_bytes, loads, evictions FROM ztype.dictionary_cache_stats();').split('|')]

    def touch(*slots):  # write with each dictionary, then read it back: one decompression and one compression object each
        return a.query('SELECT ' + ', '.join(f"length((repeat('sample subject ', 30)::ztext(6,{s}))::text)" for s in slots) + ';')

    a.query('SELECT ztype.reload_dictionaries();')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    a.equal("SHOW ztype.dictionary_cache_size;", '64MB')
    a.query('BEGIN;'); touch(1)
    entries, one, budget, loads, evictions = stats()
    assert entries == 1 and 0 < one <= budget == 64 * 1024 * 1024, (entries, one, budget)
    touch(2, 3)
    entries, size = stats()[:2]
    assert entries == 3 and one < size <= budget, (entries, size, one, budget)
    before = stats()
    a.query('COMMIT;')
    assert stats()[:2] == before[:2], (before, stats())  # commit keeps the cache
    touch(1, 2, 3)  # autocommit statements hit it too
    assert stats()[3] == before[3], (before, stats())
    a.query('BEGIN;'); touch(1); a.query('ROLLBACK;')
    assert stats()[:2] == before[:2] and stats()[3] == before[3]  # a hit is not provisional: the abort keeps it
    a.query('SELECT ztype.reload_dictionaries();')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    a.query('BEGIN;'); touch(1); a.query('ROLLBACK;')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')  # a load of an aborted transaction goes
    # A registration of this transaction: cached while it runs, dropped with the rollback, promoted by a commit.
    a.query('BEGIN;'); a.equal(training('twophase'), '9')
    a.query("SELECT length((repeat('twophase sample subject ', 30)::ztext(6,9))::text);")
    assert stats()[0] == 1
    a.query('ROLLBACK;')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); touch(1); a.query('SAVEPOINT s;'); touch(2); a.query('ROLLBACK TO s;')
    assert stats()[0] == 1  # the savepoint's load goes, the transaction's stays
    a.query('SAVEPOINT t;'); touch(3); a.query('RELEASE t;'); a.query('SAVEPOINT u;'); touch(2); a.query('ROLLBACK TO u;')
    assert stats()[0] == 2  # released into the transaction: kept; rolled back: gone
    a.query('COMMIT;')
    assert stats()[0] == 2
    # PREPARE TRANSACTION drops the transaction's loads: its outcome is not known, and a dictionary
    # registered inside it must not be usable by a later write unless it commits.
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); a.equal(training('twophase'), '9')
    a.query("SELECT length((repeat('twophase sample subject ', 30)::ztext(6,9))::text);")
    a.query("PREPARE TRANSACTION 'zt';")
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    out = a.query("SELECT length((repeat('twophase sample subject ', 30)::ztext(6,9))::text);")
    assert 'slot 9 is not available' in out, out
    a.query("ROLLBACK PREPARED 'zt';")
    a.query('BEGIN;'); touch(1); before = stats(); a.query("PREPARE TRANSACTION 'zt';")
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    a.query("COMMIT PREPARED 'zt';"); touch(1)
    assert stats()[0] == 1 and stats()[3] == before[3] + 1  # reloaded after the commit, not kept through it
    a.query('SELECT ztype.reload_dictionaries();')
    # Budget below one entry: every switch reloads, the dictionary in use is kept, values stay exact.
    a.query('SET ztype.dictionary_cache_size = 0;')
    a.query('BEGIN;')
    before = stats()
    assert touch(1, 2, 3) == '450|450|450'
    entries, size, budget, loads, evictions = stats()
    assert entries == 1 and size > budget == 0 and loads == before[3] + 3 and evictions == before[4] + 2, (entries, size, loads, evictions)
    touch(1)
    assert stats()[3] == before[3] + 4  # 1 was evicted, so it loads again
    a.equal("SELECT body::text = repeat('second sample subject ',1000) FROM newer;")
    a.query('COMMIT;')
    # Budget for one entry and a half: the second dictionary pushes the first out.
    a.query(f"SET ztype.dictionary_cache_size = '{one * 3 // 2 // 1024}kB';")
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); touch(1); before = stats(); touch(2)
    entries, size, budget, loads, evictions = stats()
    assert entries == 1 and size <= budget and loads == before[3] + 1 and evictions == before[4] + 1, (entries, size, budget, loads, evictions)
    a.query('COMMIT;')
    # Least recently used goes first: with room for two, a hit on 1 keeps it and 3 evicts 2.
    a.query(f"SET ztype.dictionary_cache_size = '{one * 5 // 2 // 1024}kB';")
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); touch(1, 2); touch(1); before = stats(); touch(3)
    assert stats()[0] == 2 and stats()[4] == before[4] + 1
    touch(1)
    assert stats()[3] == before[3] + 1   # still cached: no load beyond the one for 3
    touch(2)
    assert stats()[3] == before[3] + 2   # 2 was the victim
    a.query('COMMIT;')
    a.query('RESET ztype.dictionary_cache_size;')
    a.query('SELECT ztype.reload_dictionaries();')
    # Swapping the compression object for another level is accounted exactly, and the entry in use may
    # exceed a budget on its own.
    a.query('BEGIN;')
    a.query("SELECT pg_column_size(repeat('sample subject ', 30)::ztext(1,1));"); low = stats()[1]
    a.query("SELECT pg_column_size(repeat('sample subject ', 30)::ztext(19,1));"); high = stats()[1]
    a.query("SELECT pg_column_size(repeat('sample subject ', 30)::ztext(1,1));")
    assert high > low and stats()[1] == low, (low, high, stats())
    a.query('SET LOCAL ztype.dictionary_cache_size = 0;')
    a.query("SELECT pg_column_size(repeat('sample subject ', 30)::ztext(19,1));")
    assert stats()[:3] == [1, high, 0]
    a.query('COMMIT;')
    # Lowering the budget mid-transaction trims a populated cache on the next hit, keeping the entry in use.
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); touch(1, 2, 3)
    assert stats()[0] == 3
    a.query('SET LOCAL ztype.dictionary_cache_size = 0;'); before = stats(); touch(2)
    assert stats()[0] == 1 and stats()[3] == before[3] and stats()[4] == before[4] + 2, (before, stats())
    a.query('COMMIT;')
    # Errors: a failed load leaves the accounting untouched, a savepoint rollback drops only what the
    # savepoint loaded, and the transaction's abort drops the rest. The ghost frame references a
    # dictionary that was rolled back.
    a.query('SELECT ztype.reload_dictionaries();')
    a.query('BEGIN;'); a.equal(training('ghost'), '9')
    ghost = a.query("SELECT encode((repeat('ghost sample subject ', 30)::ztext(6,9))::bytea, 'hex');")
    a.query('ROLLBACK;')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    a.query('BEGIN;'); touch(1); before = stats()
    a.query('SAVEPOINT s;')
    a.query(f"SELECT (decode('{ghost}', 'hex')::ztext)::text;", error='dictionary ID')
    a.query('ROLLBACK TO s;')
    assert stats()[:2] == before[:2], (before, stats())
    touch(1)
    assert stats()[0] == 1 and stats()[3] == before[3]
    a.query(f"SELECT (decode('{ghost}', 'hex')::ztext)::text;", error='dictionary ID')
    a.query('ROLLBACK;')
    a.query(f"SELECT (decode('{ghost}', 'hex')::ztext)::text;", error='dictionary ID')
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    touch(1, 2)
    a.query('SET ztype.dictionary_cache_sizes = 1;', error='invalid configuration parameter')
    return ghost


def validate_sweep(a, ghost):
    """ztype.validate() is the decoder without the longjmp. For every corrupted value the suite
    pins it returns exactly the text the base-type cast raises -- except an encoding failure, which
    it words itself -- for every stored value NULL, and it never raises: the sweeping transaction
    stays usable and the dictionary cache is untouched by a failed lookup. It bypasses the decode cache in both directions, and it stops at the envelope and
    the frame: a payload that is not a well-formed jsonb container still validates clean.
    Needs the raw casts created by the caller."""
    thousand = "(repeat('a',1000)::ztext)::bytea"
    short = "('short raw value'::ztext)::bytea"
    doc = """('{"a":1}'::zjsonb)::bytea"""
    corrupt = [
        ("decode('00','hex')::ztext", 'text'),                                   # storage format
        (f"set_byte({thousand},0,0)::ztext", 'text'),                            # storage format
        (f"set_byte({thousand},4,1)::ztext", 'text'),                            # invalid frame header
        (f"set_byte({short},4,1)::ztext", 'text'),                               # invalid raw value length
        (f"set_byte({thousand},8,1)::ztext", 'text'),                            # invalid value header
        (f"set_byte({thousand},9,0)::ztext", 'text'),
        (f"set_byte({thousand},9,23)::ztext", 'text'),
        (f"set_byte({thousand},10,3)::ztext", 'text'),                           # kind relabelled
        (f"set_byte({thousand},11,2)::ztext", 'text'),
        (f"set_byte({short},11,1)::ztext", 'text'),                              # raw claimed as a frame
        (f"set_byte({thousand},octet_length({thousand})-1,0)::ztext", 'text'),   # frame checksum
        ("(SELECT set_byte(v,20,get_byte(v,20)#255)::ztext FROM "
         "(SELECT (string_agg(md5(i::text),' ')::ztext)::bytea v FROM generate_series(1,200) i) q)", 'text'),
        (f"set_byte({doc},8,2)::zjsonb", 'jsonb'),                               # unsupported JSON payload format 2
        (f"set_byte({doc},8,0)::zjsonb", 'jsonb'),                               # ... and 0
        ('(' + thousand + " || '\\x00'::bytea)::ztext", 'text'),                 # trailing frame data
    ]

    def raised(sql):
        """The cast's message as psql prints it, without the level prefix and any HINT line."""
        out = a.query(sql, error=RAW)
        errors = [line for line in out.splitlines() if line.startswith('ERROR:')]
        assert len(errors) == 1, (sql, out)
        return errors[0][len('ERROR:'):].strip()

    for expr, base in corrupt:
        message = raised(f'SELECT ({expr})::{base};')
        assert message.startswith('ztype: '), (expr, message)
        a.equal(f'SELECT ztype.validate({expr});', message)
    # The one verdict that is ztype's own rather than the cast's: a payload that survives every
    # structural check but does not decode to valid text in the database encoding (byte 16 is the
    # fifth payload byte of the raw value). The cast lets PostgreSQL name the offending byte and
    # raises with no `ztype:` prefix; the soft path asks pg_verify_mbstr not to raise, so validate
    # reports the failure in ztype's own words. Both reject the value; only the wording differs.
    bad_text = f"set_byte({short},16,255)::ztext"
    cast = raised(f'SELECT ({bad_text})::text;')
    assert cast.startswith('invalid byte sequence for encoding'), cast
    a.equal(f'SELECT ztype.validate({bad_text});', 'ztype: decoded text is not valid in encoding "UTF8"')
    # Only ztext is checked for encoding validity: the same bytes labelled bytea validate clean.
    # (bytea AS zbytea) is the real compressing cast, so the relabelled bytes go in through ztext.
    a.query('CREATE CAST (ztext AS zbytea) WITHOUT FUNCTION;')
    a.equal(f"SELECT ztype.validate(set_byte(set_byte({short},16,255),10,3)::ztext::zbytea) IS NULL;")
    a.query('DROP CAST (ztext AS zbytea);')
    # Everything the suite stored validates clean, whatever its policy, kind or size.
    a.equal('SELECT bool_and(ztype.validate(body) IS NULL AND ztype.validate(meta) IS NULL '
            'AND ztype.validate(blob) IS NULL) FROM corpus;')
    for table in ('dict_values', 'early', 'default_values', 'newer'):
        a.equal(f'SELECT bool_and(ztype.validate(body) IS NULL) FROM {table};')
    a.equal("SELECT ztype.validate('hi'::ztext) IS NULL, ztype.validate('{\"a\":1}'::zjsonb) IS NULL, "
            "ztype.validate(decode('0000ff','hex')::zbytea) IS NULL;", 't|t|t')
    # A missing dictionary is reported, not raised: the transaction survives and the cache is untouched.
    a.query('BEGIN;')
    a.query("SELECT length((repeat('sample subject ', 30)::ztext(6,1))::text);")
    before = a.query('SELECT entries, bytes FROM ztype.dictionary_cache_stats();')
    message = a.query(f"SELECT ztype.validate(decode('{ghost}', 'hex')::ztext);")
    assert message.startswith('ztype: dictionary ID ') and message.endswith('is not available'), message
    a.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', before)
    a.equal('SELECT count(*) > 0 FROM corpus;')  # the transaction is still usable
    a.query('COMMIT;')
    # The one-entry decode cache is neither read nor written by a sweep.
    a.query("CREATE TEMP TABLE vsweep AS SELECT repeat('validate sweep ', 40)::ztext AS body, "
            "repeat('other sweep ', 40)::ztext AS other;")
    a.equal("SELECT (ztype.inspect(body)).codec, (ztype.inspect(other)).codec FROM vsweep;", 'zstd|zstd')

    def counters():
        return [int(x) for x in a.query('SELECT hits, misses FROM ztype.decode_cache_stats();').split('|')]

    a.query('SELECT length(other::text) FROM vsweep;')  # the cache now holds `other`
    before = counters()
    a.equal('SELECT ztype.validate(body) IS NULL AND ztype.validate(other) IS NULL FROM vsweep;')
    assert counters() == before, (counters(), before)
    a.query('SELECT length(body::text) FROM vsweep;')   # a miss: validate seeded nothing
    assert counters() == [before[0], before[1] + 1], (counters(), before)
    a.query('DROP TABLE vsweep;')
    # The documented limitation, pinned: content that is not a jsonb container behind an intact
    # frame validates clean, because validate stops at the frame and never walks the container.
    # Casting it to jsonb is what hands those bytes to PostgreSQL's reader; the suite does not.
    a.equal("SELECT ztype.validate(set_byte(set_byte((repeat('not a container ',20)::ztext)::bytea,10,2),8,1)::zjsonb) IS NULL;")
    # Parallel safe: a worker validates the same rows as the leader.
    a.query('SET debug_parallel_query = on;')
    a.equal('SELECT bool_and(ztype.validate(body) IS NULL AND ztype.validate(meta) IS NULL '
            'AND ztype.validate(blob) IS NULL) FROM corpus;')
    a.query('RESET debug_parallel_query;')


def rss_kb(pid):
    return int(run(['ps', '-o', 'rss=', '-p', str(pid)]))


class PeakRss:
    """Peak resident size of one process while the block runs, sampled from a thread every few
    milliseconds: what a backend's native allocations (zstd contexts, training buffers) cost at
    their high-water mark, which no PostgreSQL memory context sees. `growth_kb` is the peak above
    the size at entry."""
    def __init__(self, pid, interval=0.005):
        self.pid, self.interval = pid, interval
        self.start_kb = self.peak_kb = 0

    def __enter__(self):
        import threading
        self.start_kb = self.peak_kb = rss_kb(self.pid)
        self.stop = threading.Event()

        def sample():
            while not self.stop.is_set():
                try:
                    self.peak_kb = max(self.peak_kb, rss_kb(self.pid))
                except AssertionError:  # the process is gone
                    return
                self.stop.wait(self.interval)
        self.thread = threading.Thread(target=sample, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.stop.set()
        self.thread.join()
        self.peak_kb = max(self.peak_kb, rss_kb(self.pid))

    @property
    def growth_kb(self):
        return self.peak_kb - self.start_kb


def timed_query(a, sql, error=None):
    started = time.perf_counter()
    a.query(sql, error=error)
    return time.perf_counter() - started


def memory(a):
    """Native zstd objects live outside PostgreSQL's memory accounting, so watch the backend's RSS
    across commits, aborts, savepoint rollbacks, codec errors, alternating levels, several
    dictionaries and a cancelled compression. Requires the raw casts created by the caller."""
    pid = int(a.query('SELECT pg_backend_pid();'))
    corrupt = "SELECT (set_byte((repeat('a',1000)::ztext)::bytea,octet_length((repeat('a',1000)::ztext)::bytea)-1,0)::ztext)::text;"
    # Inputs are materialised first so the timeout can only fire inside the codec: 8 MB of md5 text
    # for a level-22 compression, and a 128 MB frame for decompression.
    # Materialising them is not what is measured, and under a sanitizer runtime it outlasts the
    # session's 15 s statement timeout (29 s on Debian 13, PG 18, clang 19), so it runs without one.
    # The timing bounds below stay live under a sanitizer: measured there at 0.37 s for the compress
    # cancel, 0.04 s for the decode cancel and 0.09 s for the prefix against a 0.85 s full decode.
    a.query("SET statement_timeout = 0;")
    setup_secs = timed_query(a, "CREATE TABLE big AS SELECT (SELECT string_agg(md5(i::text), '') FROM generate_series(1, 250000) i) AS t;")
    setup_secs += timed_query(a, "CREATE TABLE bigz AS SELECT (SELECT string_agg(md5(i::text), '') FROM generate_series(1, 4000000) i)::ztext(1) AS z;")
    a.query("SET statement_timeout = '15s';")
    big = "SELECT raw_length(t::ztext(22)) FROM big;"
    bigz = "SELECT length(z::text) FROM bigz;"
    full = timed_query(a, bigz)
    # A partial read decodes only what it returns: the first 100 bytes of a 128 MB frame, exactly.
    started = time.perf_counter()
    a.equal("SELECT prefix(z, 100) = (SELECT left(string_agg(md5(i::text), ''), 100) FROM generate_series(1, 4) i) FROM bigz;")
    prefix_secs = time.perf_counter() - started
    assert prefix_secs < full / 2, (prefix_secs, full)

    def cycle():
        a.query("SELECT ztype.reload_dictionaries(); SELECT length(body::text) FROM dict_values; SELECT length(body::text) FROM newer;")
        a.query("BEGIN; SELECT pg_column_size(repeat('first sample subject ', 50)::ztext(1,1)); SELECT pg_column_size(repeat('first sample subject ', 50)::ztext(9,1)); "
                "SELECT pg_column_size(repeat('fifth sample subject ', 50)::ztext(9,5)); SELECT pg_column_size(jsonb_build_object('body', repeat('sample', 30))::zjsonb(6,'json')); ROLLBACK;")
        a.query("BEGIN; SAVEPOINT s; SELECT length((repeat('second sample subject ', 50)::ztext(6,2))::text); ROLLBACK TO s; SELECT length((repeat('second sample subject ', 50)::ztext(6,2))::text); COMMIT;")
        # Eviction under a zero budget: every dictionary switch frees and rebuilds native objects.
        a.query("BEGIN; SET LOCAL ztype.dictionary_cache_size = 0; SELECT length((repeat('first sample subject ', 50)::ztext(19,1))::text), "
                "length((repeat('second sample subject ', 50)::ztext(6,2))::text), length((repeat('fifth sample subject ', 50)::ztext(6,5))::text); COMMIT;")
        a.query(corrupt, error='checksum')
        a.query("BEGIN; SELECT length((repeat('first sample subject ', 50)::ztext(6,1))::text); SELECT (set_byte((repeat('a',1000)::ztext)::bytea,10,3)::ztext)::text; ROLLBACK;", error='invalid value header')

    for _ in range(10):
        cycle()
    a.query("SET statement_timeout = '100ms';")
    cancel_secs = timed_query(a, big, error='statement timeout')  # a level-22 compression of 8 MB, cancelled mid-frame
    a.query("SET statement_timeout = '10ms';")
    decode_cancel = timed_query(a, bigz, error='statement timeout')  # a 128 MB decompression, cancelled mid-frame
    a.query("SET statement_timeout = '15s';")
    assert cancel_secs < 1, cancel_secs
    assert decode_cancel < full / 2, (decode_cancel, full)
    a.query('DROP TABLE bigz;')
    # Baseline after the allocator has seen the largest working set the loop will use.
    a.query("SET statement_timeout = '100ms';")
    a.query(big, error='statement timeout')
    a.query("SET statement_timeout = '15s';")
    cycle()

    def window():
        """RSS growth over 120 dictionary and codec cycles."""
        start = rss_kb(pid)
        for _ in range(120):
            cycle()
        return rss_kb(pid) - start

    # The first window absorbs allocator warm-up; a leak grows just as much in the second window,
    # which is the one asserted. Cancelled level-22 compressions are measured apart: their contexts
    # are around 100 MB each, so freeing them on cancel shows against a bound that ignores the tens
    # of MB the allocator may keep or return around such allocations.
    first, second = window(), window()
    start = rss_kb(pid)
    for _ in range(4):
        a.query("SET statement_timeout = '100ms';")
        a.query(big, error='statement timeout')
        a.query("SET statement_timeout = '15s';")
    cancel_growth = rss_kb(pid) - start
    # Under a sanitizer runtime the allocator is ASan's: freed blocks sit in its quarantine and every
    # allocation carries redzones, so resident size says nothing about ztype. The cycles above still
    # ran (that is the coverage), the bounds are what a sanitized run cannot judge.
    if SANITIZER_RUNTIME:
        print(f'SKIP: RSS bounds under a sanitizer runtime (setup {setup_secs:.1f} s, windows {first:+d}/{second:+d} kB, '
              f'cancels {cancel_growth:+d} kB)', flush=True)
    else:
        assert second < 16384, (first, second)
        assert cancel_growth < 131072, cancel_growth
    return cancel_secs, decode_cancel, full, prefix_secs, second


def backups(cluster, work):
    """Restore real extension config data and dictionary-using columns into fresh DBs."""
    env = cluster.env
    dump = work / 'backup.dump'
    plain = work / 'backup.sql'
    run([BIN/'pg_dump', '-Fc', '-f', dump], env=env)
    run([BIN/'pg_dump', '-f', plain], env=env)
    for name, args in [('plain_restore', None), ('parallel_restore', ['-j','4'])]:
        run([BIN/'createdb', name], env=env)
        target = dict(env, PGDATABASE=name)  # no PGOPTIONS: restores need no settings
        if args is None:
            run([BIN/'psql','-X','-v','ON_ERROR_STOP=1','-f',plain], env=target)
        else:
            run([BIN/'pg_restore','--exit-on-error','-d',name,*args,dump], env=target)
        s = cluster.session(name)
        try:
            s.equal('SELECT length(body::text) > 0 FROM dict_values;')
            s.equal("SELECT body::text = repeat('first sample subject ',10) FROM default_values;")
            s.equal('SELECT bool_and(body::text = source AND (meta ->> \'body\') = source) FROM corpus;')
            s.equal('SELECT count(*) FROM ztype.dictionaries;', '8')
            s.equal("SELECT body::text = 'column default' AND dom::text = 'domain default' FROM early WHERE id = 3;")
            # Rows restored before their dictionary arrived are exact; recompress catches them up.
            s.equal("SELECT bool_and(ztype.recompress(body, 6, 1)::text = body::text AND pg_column_size(ztype.recompress(body, 6, 1)) <= pg_column_size(body)) FROM dict_values WHERE raw_length(body) >= 64;")
            s.equal("SELECT bool_and(ztype.recompress(body, 6, 5)::text = body::text) FROM early WHERE body IS NOT NULL;")
            # A pending column restores as pending. The restore wrote every row under the column's
            # policy, minus the dictionary where the registry came later (as for any restored column),
            # so the ordinary catch-up applies and finish then clears the mark.
            s.equal("SELECT format_type(atttypid, atttypmod) FROM pg_attribute WHERE attrelid = 'deferred_like'::regclass AND attname = 'body';", 'ztext(9,2,pending)')
            s.query("UPDATE deferred_like SET body = body::ztext(9,2) WHERE NOT ztype.matches_policy(body, 9, 2);")
            s.query("SELECT ztype.finish_column_policy('deferred_like', 'body');")
            s.equal("SELECT format_type(atttypid, atttypmod), (SELECT count(*) FROM deferred_like d, ztype.inspect(d.body) i WHERE i.level = 9 AND i.dict_slot = 2) FROM pg_attribute WHERE attrelid = 'deferred_like'::regclass AND attname = 'body';", 'ztext(9,2)|204')
        finally:
            s.close()
    print('PASS: plain and parallel dump/restore without settings', flush=True)



# Every ztext/zjsonb/zbytea column whose modifier names a slot this database does not have.
# atttypmod & 31 is the level and (atttypmod >> 5) & 33554431 the slot (bit 30 is the pending
# flag); -1 means (6,0). README: "Recovery".
UNREGISTERED_SLOTS = """
SELECT c.relname, a.attname, a.atttypmod & 31 AS level, (a.atttypmod >> 5) & 33554431 AS slot
  FROM pg_attribute a
  JOIN pg_class c ON c.oid = a.attrelid
  JOIN pg_type t ON t.oid = a.atttypid
  LEFT JOIN ztype.dictionary_inventory d ON d.slot = (a.atttypmod >> 5) & 33554431
 WHERE t.typname IN ('ztext','zjsonb','zbytea') AND a.attnum > 0 AND NOT a.attisdropped
   AND c.relkind IN ('r','p','m') AND a.atttypmod <> -1 AND (a.atttypmod >> 5) & 33554431 > 0 AND d.slot IS NULL
 ORDER BY 1, 2"""


def stage_frames(s, work, expected_rows='1'):
    """Put the exported stored bytes into a bare ztext column, unchanged. This is what a *physical*
    restore preserves; no logical dump can, because output and send both decode. A bare column is
    the only target that stores them verbatim (no coercion towards typmod -1, see bare_column)."""
    s.query('CREATE CAST (bytea AS ztext) WITHOUT FUNCTION;')
    s.query('CREATE TABLE staged(id integer, b bytea); CREATE TABLE restored(id integer, body ztext);')
    s.query(f"COPY staged FROM '{work}/frames.copy';")
    s.query('INSERT INTO restored SELECT id, b::ztext FROM staged;')
    s.equal('SELECT count(*) FROM restored;', expected_rows)
    s.query('DROP TABLE staged; DROP CAST (bytea AS ztext);')


def recovery(cluster, work):
    """Operational recovery: moving dictionaries and dictionary-using tables between databases.
    Three boundaries decide every recipe in README's "Recovery" subsection.

    A *logical* dump never carries a frame: output and send decode, so a restore re-encodes every
    value under the target column's policy. A missing dictionary therefore degrades to
    dictionary-free with a warning, and a *different* dictionary in the same slot is applied
    silently - that is the slot-mismatch pitfall, and its fix is to re-point the column. Frames
    survive only a physical restore (base backup, pg_upgrade, a physical replica), which
    stage_frames stands in for; those are the values that fail strictly until their dictionary is
    registered again.

    A frame names its dictionary by zstd ID, never by slot, so registering the exported bytes under
    any free slot makes it readable. And the registry is append-only: an import fills unused slots,
    every collision is a named constraint violation, and nothing existing is ever rewritten.

    Two smaller boundaries the recipes rest on: a role importing with COPY needs EXECUTE on
    ztype.dict_id as well as INSERT, because the registry's CHECK constraint calls it as the inserting
    role, while ztype.import_dictionary is SECURITY DEFINER and needs EXECUTE alone; and the batched
    catch-up predicate has to be restricted to frames, because a raw value has no dictionary ID and
    an unrestricted predicate therefore rewrites every raw row on every run."""
    env, first = cluster.env, "repeat('first sample subject ',1000)"
    sessions = []
    try:
        src = cluster.session()
        sessions.append(src)
        # (1) Export. The registry is ordinary table data; COPY preserves slot, dict_id and name,
        # which is the whole reason an import is readable on the other side.
        src.query(f"COPY ztype.dictionaries TO '{work}/dicts.copy';")
        src.query(f"COPY (SELECT * FROM ztype.dictionaries WHERE slot IN (2,5)) TO '{work}/subset.copy';")
        frame_id = src.query('SELECT (ztype.inspect(body)).dict_id FROM dict_values;')
        src.query('CREATE CAST (ztext AS bytea) WITHOUT FUNCTION;')
        src.query(f"COPY (SELECT id, body::bytea FROM dict_values) TO '{work}/frames.copy';")
        src.query('DROP CAST (ztext AS bytea);')
        run([BIN/'pg_dump', '-t', 'dict_values', '-t', 'corpus', '-f', work/'tables.sql'], env=env)
        # pg_dump -t on the registry does emit the extension config table's rows, so a partial
        # backup can carry the dictionaries in a file of its own. README says so.
        dumped = run([BIN/'pg_dump', '-t', 'ztype.dictionaries', '--data-only'], env=env)
        rows = dumped.split('FROM stdin;\n', 1)[1].split('\n\\.', 1)[0].splitlines()
        assert sorted(int(line.split('\t')[0]) for line in rows) == list(range(1, 9)), rows[:1]

        # (2) A table-only restore into a database with no registry.
        run([BIN/'createdb', 'recovery_b'], env=env)
        b = cluster.session('recovery_b')
        sessions.append(b)
        b.query('CREATE EXTENSION ztype;')
        restore = subprocess.run([str(BIN/'psql'), '-X', '-v', 'ON_ERROR_STOP=1', '-f', str(work/'tables.sql')],
                                 env=dict(env, PGDATABASE='recovery_b'), capture_output=True, text=True)
        assert restore.returncode == 0, (restore.stdout, restore.stderr)
        assert 'ztype: dictionary slot 2 is not available' in restore.stderr, restore.stderr
        # Raw and dictionary-free columns restore exactly; the dictionary column was re-encoded on
        # the way in, so it reads, and only its size differs.
        b.equal("SELECT bool_and(body::text = source AND (meta ->> 'body') = source "
                "AND blob::bytea = convert_to(source,'UTF8')) FROM corpus;")
        b.equal(f"SELECT i.level, i.codec, i.dict_id IS NULL, ztype.validate(body) IS NULL, body::text = {first} "
                'FROM dict_values d, ztype.inspect(d.body) i;', '12|zstd|t|t|t')
        # The catalog query README gives finds the column that points at a slot this database lacks.
        b.equal(UNREGISTERED_SLOTS + ';', 'dict_values|body|12|2')
        # Bytes preserved by a physical restore are a different matter: strictly unreadable, and
        # validate names the missing ID rather than raising.
        stage_frames(b, work)
        b.query('SELECT body::text FROM restored;', error=f'dictionary ID {frame_id} is not available')
        b.equal('SELECT ztype.validate(body) FROM restored;', f'ztype: dictionary ID {frame_id} is not available')
        b.equal('SELECT i.dict_id, i.dict_slot IS NULL, i.dict_name IS NULL FROM restored r, ztype.inspect(r.body) i;',
                f'{frame_id}|t|t')
        # A logical write warns once and lands dictionary-free. These rows are the catch-up work.
        out = b.query(f"INSERT INTO dict_values SELECT 1000 + i, {first} || i FROM generate_series(0,19) i;")
        assert out.count('WARNING:') == 1 and 'slot 2 is not available' in out, out
        b.equal('SELECT count(*), bool_and(i.dict_id IS NULL AND i.level = 12) '
                'FROM dict_values d, ztype.inspect(d.body) i WHERE id >= 1000;', '20|t')

        # (3) Import with the original slots. Every frame is readable and name-resolvable again.
        b.query(f"COPY ztype.dictionaries FROM '{work}/dicts.copy';")
        b.equal('SELECT count(*) FROM ztype.dictionaries;', '8')
        b.equal(f'SELECT body::text = {first}, ztype.validate(body) IS NULL, i.dict_slot, i.dict_name '
                'FROM restored r, ztype.inspect(r.body) i;', 't|t|2|second')
        b.equal(UNREGISTERED_SLOTS + ';', '')
        # (4) Collisions. Each is the constraint that owns that column, and each leaves the registry
        # exactly as it was: an existing row is never altered, it is the thing to point at instead.
        b.query(f"COPY ztype.dictionaries FROM '{work}/dicts.copy';", error='dictionaries_pkey')
        b.query("INSERT INTO ztype.dictionaries SELECT 100, dict_id, 'copy-of-second', dict, NULL, now() "
                'FROM ztype.dictionaries WHERE slot = 2;', error='dictionaries_dict_id_key')
        b.query("SELECT ztype.add_dictionary('second', ztype.train_dictionary($$SELECT 'rival sample subject ' || i "
                '|| repeat(md5(i::text),4) FROM generate_series(1,1000) i$$, 2048));', error='dictionaries_name_key')
        b.equal('SELECT count(*) FROM ztype.dictionaries;', '8')
        b.equal('SELECT ztype.validate(body) IS NULL FROM restored;', 't')
        # Registration continues above the imported slots; slots are never reused.
        b.equal(training('ninth'), '9')

        # (5) A subset import: gaps are allowed, and the next slot is still max(slot)+1. It also
        # pins what an importing role needs. INSERT alone is not enough: the registry carries
        # CHECK (dict_id = ztype.dict_id(dict)), a CHECK expression runs as the inserting role, and
        # EXECUTE on ztype.dict_id is revoked from PUBLIC - so the COPY fails with 42501 and
        # imports nothing until that one grant is added. README says so under "Recovery".
        run([BIN/'createdb', 'recovery_subset'], env=env)
        sub = cluster.session('recovery_subset')
        sessions.append(sub)
        sub.query('CREATE EXTENSION ztype;')
        sub.query('CREATE ROLE ztype_importer; GRANT pg_read_server_files TO ztype_importer; '
                  'GRANT INSERT ON ztype.dictionaries TO ztype_importer;')
        imp = cluster.session('recovery_subset')
        sessions.append(imp)
        imp.query('SET ROLE ztype_importer;')
        assert imp.sqlstate(f"COPY ztype.dictionaries FROM '{work}/subset.copy';") == '42501'
        sub.equal('SELECT count(*) FROM ztype.dictionaries;', '0')
        sub.query('GRANT EXECUTE ON FUNCTION ztype.dict_id(bytea) TO ztype_importer;')
        imp.query(f"COPY ztype.dictionaries FROM '{work}/subset.copy';")
        sub.equal('SELECT string_agg(slot::text, \',\' ORDER BY slot) FROM ztype.dictionaries;', '2,5')
        stage_frames(sub, work)
        sub.equal(f'SELECT body::text = {first}, i.dict_slot, i.dict_name FROM restored r, ztype.inspect(r.body) i;',
                  't|2|second')
        sub.equal(training('sixth'), '6')

        # (6) The pitfall: a database whose slot 2 is a different dictionary.
        run([BIN/'createdb', 'recovery_c'], env=env)
        c = cluster.session('recovery_c')
        sessions.append(c)
        c.query('CREATE EXTENSION ztype;')
        c.equal(training('local-a'), '1')
        c.equal(training('local-b'), '2')
        restore = subprocess.run([str(BIN/'psql'), '-X', '-v', 'ON_ERROR_STOP=1', '-f', str(work/'tables.sql')],
                                 env=dict(env, PGDATABASE='recovery_c'), capture_output=True, text=True)
        assert restore.returncode == 0, (restore.stdout, restore.stderr)
        # Silently re-encoded against the wrong dictionary: no error, no warning, correct value.
        assert 'WARNING' not in restore.stderr, restore.stderr
        c.equal(f'SELECT body::text = {first}, i.dict_slot, i.dict_name FROM dict_values d, ztype.inspect(d.body) i;',
                't|2|local-b')
        c.equal(UNREGISTERED_SLOTS + ';', '')  # the catalog query cannot see it: slot 2 exists here
        # Preserved bytes are unreadable until the exported dictionary is registered - under a free
        # slot, because slots 1 and 2 are taken and the registry is append-only.
        stage_frames(c, work)
        c.query('SELECT body::text FROM restored;', error=f'dictionary ID {frame_id} is not available')
        c.query(f"COPY ztype.dictionaries FROM '{work}/dicts.copy';", error='dictionaries_pkey')
        c.query('CREATE TEMP TABLE incoming (LIKE ztype.dictionaries);')
        c.query(f"COPY incoming FROM '{work}/dicts.copy';")
        c.equal("SELECT ztype.add_dictionary('imported-second', dict) FROM incoming WHERE slot = 2;", '3')
        c.equal(f'SELECT body::text = {first}, ztype.validate(body) IS NULL, i.dict_slot, i.dict_name '
                'FROM restored r, ztype.inspect(r.body) i;', 't|t|3|imported-second')
        # (6b) ztype.import_dictionary is the same import one row at a time, with the slot preserved:
        # it is idempotent on an identical row and refuses each of the three collisions with its own
        # 42710 naming what the target already has, registry untouched, where COPY leaves the
        # constraint name. The slot-2 pitfall from above is the first refusal.
        assert c.sqlstate('SELECT ztype.import_dictionary(slot, name, dict) FROM incoming WHERE slot = 2;') == '42710'
        c.query('SELECT ztype.import_dictionary(slot, name, dict) FROM incoming WHERE slot = 2;',
                error='slot 2 is taken by dictionary "local-b"')
        c.equal('SELECT ztype.import_dictionary(slot, name, dict, trained_from) FROM incoming WHERE slot = 4;', '4')
        c.equal('SELECT ztype.import_dictionary(slot, name, dict, trained_from) FROM incoming WHERE slot = 4;', '4')
        c.equal("SELECT string_agg(slot::text || ':' || name, ',' ORDER BY slot) FROM ztype.dictionary_inventory;",
                '1:local-a,2:local-b,3:imported-second,4:' + c.query('SELECT name FROM incoming WHERE slot = 4;'))
        c.equal('SELECT i.dict_id = s.dict_id AND i.name = s.name FROM ztype.dictionary_inventory i JOIN incoming s USING (slot) WHERE slot = 4;')
        c.query("SELECT ztype.import_dictionary(9, 'second-again', dict) FROM incoming WHERE slot = 2;",
                error='is already registered as slot 3 ("imported-second")')
        c.query("SELECT ztype.import_dictionary(10, 'imported-second', dict) FROM incoming WHERE slot = 5;",
                error='dictionary name "imported-second" is already registered as slot 3')
        assert c.sqlstate("SELECT ztype.import_dictionary(NULL, 'x', dict) FROM incoming WHERE slot = 5;") == '22004'
        assert c.sqlstate("SELECT ztype.import_dictionary(0, 'x', dict) FROM incoming WHERE slot = 5;") == '22023'
        assert c.sqlstate("SELECT ztype.import_dictionary(11, '123', dict) FROM incoming WHERE slot = 5;") == '22023'
        assert c.sqlstate("SELECT ztype.import_dictionary(11, 'x', 'not a dictionary'::bytea);") == '22023'
        c.equal('SELECT count(*) FROM ztype.dictionaries;', '4')
        # Registration continues above the imported slot, so the imported slot is never handed out.
        c.equal(training('local-c'), '5')
        # The function import is SECURITY DEFINER: EXECUTE on it is the whole grant, with no
        # privilege on the registry at all, and the role still cannot read the bytes or the rows.
        # A fresh role, so the COPY grants above (INSERT, dict_id) are not what makes it work.
        sub.query('REVOKE INSERT ON ztype.dictionaries FROM ztype_importer; '
                  'REVOKE EXECUTE ON FUNCTION ztype.dict_id(bytea) FROM ztype_importer;')
        imp.query('CREATE TEMP TABLE incoming (slot integer, dict_id bigint, name text, dict bytea, '
                  'trained_from text, created_at timestamptz);')   # LIKE the registry would need SELECT on it
        imp.query(f"COPY incoming FROM '{work}/dicts.copy';")
        assert imp.sqlstate('SELECT ztype.import_dictionary(slot, name, dict) FROM incoming WHERE slot = 7;') == '42501'
        sub.query('GRANT EXECUTE ON FUNCTION ztype.import_dictionary(integer, text, bytea, text) TO ztype_importer;')
        imp.equal('SELECT ztype.import_dictionary(slot, name, dict) FROM incoming WHERE slot = 7;', '7')
        assert imp.sqlstate('SELECT count(*) FROM ztype.dictionaries;') == '42501'
        assert imp.sqlstate('SELECT dict FROM ztype.dictionaries;') == '42501'
        assert imp.sqlstate('INSERT INTO ztype.dictionaries SELECT * FROM incoming WHERE slot = 8;') == '42501'
        imp.equal('SELECT ztype.import_dictionary(slot, name, dict) FROM incoming WHERE slot = 2;', '2')  # identical: no-op
        assert imp.sqlstate("SELECT ztype.import_dictionary(8, 'fifth-again', dict) FROM incoming WHERE slot = 5;") == '42710'
        sub.equal("SELECT string_agg(slot::text, ',' ORDER BY slot) FROM ztype.dictionaries;", '2,5,6,7')
        # New writes still use the column's slot, which is the local dictionary, not the imported one.
        out = c.query(f"INSERT INTO dict_values VALUES (2, {first});")
        assert 'WARNING' not in out, out
        c.equal("SELECT bool_and((ztype.inspect(body)).dict_name = 'local-b') FROM dict_values;")
        # Re-pointing the column is an ALTER COLUMN TYPE, so it rewrites, like any modifier change.
        rel = c.query("SELECT pg_relation_filenode('dict_values');")
        c.query("ALTER TABLE dict_values ALTER COLUMN body TYPE ztext(12,'imported-second');")
        assert c.query("SELECT pg_relation_filenode('dict_values');") != rel
        c.equal(f'SELECT count(*), bool_and(i.dict_slot = 3 AND i.level = 12 AND body::text = {first}) '
                'FROM dict_values d, ztype.inspect(d.body) i;', '2|t')

        # (7) The resumable catch-up recipe, in B, after the import: primary-key ranges, one commit
        # per batch, and a predicate restricted to frames so that a rerun is free.
        # Two rows the column's own policy stores raw are why that restriction is load-bearing:
        # a value the codec keeps raw carries no frame, so dict_slot is NULL for it however often
        # it is rewritten, and `naive` below - the same predicate without the codec test - selects
        # exactly those two rows forever. These two are raw because they are under ZT_MIN_COMPRESS
        # (63 is one byte under it); incompressible() pins the other way a stored value stays raw.
        b.query("INSERT INTO dict_values VALUES (1020, 'short'), (1021, repeat('x', 63));")
        b.equal("SELECT string_agg(id::text, ',' ORDER BY id) FROM dict_values d, ztype.inspect(d.body) i "
                "WHERE i.codec = 'raw';", '1020,1021')
        stale = ("((ztype.inspect(body)).level <> 12 OR ((ztype.inspect(body)).codec = 'zstd' "
                 'AND (ztype.inspect(body)).dict_slot IS DISTINCT FROM 2))')
        naive = '((ztype.inspect(body)).dict_slot IS DISTINCT FROM 2 OR (ztype.inspect(body)).level <> 12)'
        b.query('CREATE TEMP TABLE catchup_before AS SELECT id, md5(body::text) AS h FROM dict_values;')
        b.equal('SELECT count(*) FROM catchup_before;', '23')
        lsn, size = b.query('SELECT pg_current_wal_insert_lsn();'), int(b.query("SELECT pg_total_relation_size('dict_values');"))
        for lo, hi in ((0, 1009), (1010, 1021)):
            b.query(f'BEGIN; UPDATE dict_values SET body = body::text WHERE id BETWEEN {lo} AND {hi} AND {stale}; COMMIT;')
        wal = int(b.query(f"SELECT pg_current_wal_insert_lsn() - '{lsn}'::pg_lsn;"))
        grew = int(b.query("SELECT pg_total_relation_size('dict_values');")) - size
        b.equal('SELECT count(*), bool_and(i.level = 12) FROM dict_values d, ztype.inspect(d.body) i;', '23|t')
        b.equal('SELECT count(*), bool_and(i.dict_slot = 2) FROM dict_values d, ztype.inspect(d.body) i '
                "WHERE i.codec = 'zstd';", '21|t')
        b.equal("SELECT string_agg(id::text, ',' ORDER BY id) FROM dict_values d, ztype.inspect(d.body) i "
                "WHERE i.codec = 'raw';", '1020,1021')
        b.equal('SELECT bool_and(md5(d.body::text) = c.h) FROM dict_values d JOIN catchup_before c USING (id);')
        b.equal(f'WITH u AS (UPDATE dict_values SET body = body::text WHERE {stale} RETURNING 1) SELECT count(*) FROM u;', '0')
        b.equal(f'WITH u AS (UPDATE dict_values SET body = body::text WHERE {naive} RETURNING 1) SELECT count(*) FROM u;', '2')
        print('PASS: recovery - registry export/import with preserved slots and the grants an importer needs, '
              'import_dictionary (idempotent, three collisions refused), '
              'table-only restore, collisions, slot mismatch, batched catch-up '
              f'({wal} WAL bytes, {grew:+d} bytes of table growth for 23 rows)', flush=True)
    finally:
        for s in sessions:
            s.close()


def fixture_current(loaded, magic):
    """The one fixture written under the library's own magic. Every other file in the directory is
    history: its bytes must be rejected, never decoded. Failing here is the wanted signal after a
    deliberate ZT_MAGIC bump - regenerate, and keep the old file as a must-reject entry."""
    current = [f for f in loaded if int(f['magic'], 16) == magic]
    assert len(current) == 1, (
        f"no single storage fixture matches the library magic 0x{magic:08x} (fixtures carry "
        f"{[f['magic'] for f in loaded]}): if the bump was deliberate, regenerate with "
        'python3 tests/make_fixtures.py and keep the old file as a must-reject entry')
    return current[0]


def fixtures(cluster):
    """Bytes stored by an earlier build must still decode, and retired magics must still be refused.
    test-cross builds both sides from today's source, so nothing else here would notice an
    accidental change to the envelope, the frame options or the jsonb payload tagging: only bytes
    that were written once and committed can. The fixture records each value's logical SQL, which
    is evaluated live and compared, so the check is on the meaning of the bytes, not on a string.

    Envelopes are native-endian, so the value entries are readable only where the byte order
    matches; the fixture records it and the run says so rather than failing. The magic is read in
    this machine's byte order (LIBRARY_MAGIC), so it still compares equal there and the skip below
    is what a big-endian machine hits, not the regenerate instruction."""
    loaded = [json.loads(path.read_text()) for path in sorted(FIXTURE_DIR.glob('storage-*.json'))]
    assert loaded, f'no storage fixtures in {FIXTURE_DIR}'
    run([BIN/'createdb', 'fixtures'], env=cluster.env)
    s = cluster.session('fixtures')
    try:
        s.query('CREATE EXTENSION ztype;')
        s.query(RAW_CASTS)
        magic = int(s.query(LIBRARY_MAGIC))
        data = fixture_current(loaded, magic)
        # What the library says it writes must be what the bytes say, and what the fixture recorded.
        s.equal('SELECT magic, jsonb_format FROM ztype.build_info();', f"0x{magic:08x}|{data['jsonb_format']}")
        # A mismatched magic must say what to do about it; assert that on a copy, not on the file.
        rejected = ''
        try:
            fixture_current([dict(data, magic='0x5a540002')], magic)
        except AssertionError as exc:
            rejected = str(exc)
        assert 'regenerate' in rejected, rejected
        if sys.byteorder != 'little' or data['byte_order'] != 'little':
            print(f"SKIP: storage fixtures - envelopes are native-endian (fixture {data['byte_order']}-endian, "
                  f'this machine {sys.byteorder}-endian)', flush=True)
            return
        s.equal("SELECT get_byte(('{\"a\":1}'::zjsonb)::bytea, 8);", str(data['jsonb_format']))
        d = data['dictionary']
        s.equal(f"SELECT ztype.add_dictionary('{d['name']}', decode('{d['hex']}', 'hex'));", str(d['slot']))
        s.equal(f"SELECT dict_id FROM ztype.dictionaries WHERE slot = {d['slot']};", str(d['dict_id']))
        # Bare columns store a value's bytes unchanged (no modifier, so no coercion), which makes a
        # table the other read path for the same bytes: through the heap and, above the TOAST
        # threshold, out of line. The fixture carries entries on both sides of it.
        for kind in BASE_TYPE:
            s.query(f'CREATE TABLE fx_{kind} (id text PRIMARY KEY, v {kind});')
        toasted = set()
        for v in data['values']:
            raw, i, base = RAW_IN[v['type']].format(h=v['hex']), v['inspect'], BASE_TYPE[v['type']]
            assert i['stored_bytes'] == len(v['hex']) // 2, v['id']
            s.equal(f"SELECT ({raw})::{base} = ({v['sql']})::{base};")
            s.query(f"INSERT INTO fx_{v['type']} VALUES ('{v['id']}', {raw});")
            s.equal(f"SELECT v::{base} = ({v['sql']})::{base} AND (v::bytea = decode('{v['hex']}', 'hex')) "
                    f"FROM fx_{v['type']} WHERE id = '{v['id']}';" if v['type'] != 'zbytea' else
                    f"SELECT v::{base} = ({v['sql']})::{base} AND ((v::ztext)::bytea = decode('{v['hex']}', 'hex')) "
                    f"FROM fx_{v['type']} WHERE id = '{v['id']}';")
            if i['stored_bytes'] > 4096:
                toasted.add(v['type'])
            s.equal(f'SELECT ztype.validate({raw}) IS NULL;')
            s.equal(INSPECT_ROW.format(v=raw), '|'.join(str(x) for x in (
                i['kind'], i['codec'], i['level'], i['format'], i['raw_length'], i['stored_bytes'],
                '' if i['dict_id'] is None else i['dict_id'])))
            if i['dict_id'] is not None:  # a frame names its dictionary by ID; the slot is local
                s.equal(f'SELECT i.dict_slot, i.dict_name FROM ztype.inspect({raw}) i;', f"{d['slot']}|{d['name']}")
            if v['type'] == 'ztext':
                # 7 bytes never splits a character in these samples; make_fixtures asserts that too.
                s.equal(f"SELECT prefix({raw}, 7) = convert_from(substring(convert_to(({v['sql']})::text,'UTF8') "
                        f"from 1 for 7),'UTF8') AND raw_length({raw}) = {i['raw_length']};")
            elif v['type'] == 'zjsonb':
                # Every top-level key this entry actually has: one fixed name would be NULL on both
                # sides for the entries without it, which any bytes would satisfy. The fixture's
                # jsonb values are objects, and make_fixtures asserts that before writing one.
                s.equal(f"SELECT count(*) > 0 AND bool_and(({raw} ->> k) IS NOT DISTINCT FROM "
                        f"(({v['sql']})::jsonb ->> k)) FROM jsonb_object_keys(({v['sql']})::jsonb) k;")
            else:
                s.equal(f"SELECT raw_length({raw}) = {i['raw_length']};")
        assert toasted == set(BASE_TYPE), f'the fixture needs an out-of-line entry for every type, has {sorted(toasted)}'
        for kind in toasted:
            s.equal(f"SELECT pg_relation_size(reltoastrelid) > 0 FROM pg_class WHERE relname = 'fx_{kind}';")
        # Retired magics: the entries recorded in any fixture, plus every value of an older file.
        retired = [(f['magic'], e) for f in loaded for e in f.get('retired', [])]
        retired += [(f['magic'], v) for f in loaded if f is not data for v in f['values']]
        assert retired, 'a fixture must carry at least one retired-magic entry'
        for magic_hex, e in retired:
            raw, base = RAW_IN[e['type']].format(h=e['hex']), BASE_TYPE[e['type']]
            s.query(f'SELECT ({raw})::{base};', error='storage format')
            if e['type'] != 'zjsonb':  # raw_length is declared for ztext and zbytea only
                s.query(f'SELECT raw_length({raw});', error='storage format')
            out = s.query(f'SELECT ztype.validate({raw});')
            assert 'storage format' in out, (magic_hex, e['id'], out)
        print(f"PASS: storage fixtures - {len(data['values'])} values under magic {data['magic']} "
              f"(written with zstd {data['zstd_version']} on {data['generated']}) decode and validate, in a table too, "
              f"{len(retired)} envelope{'' if len(retired) == 1 else 's'} under a retired magic rejected",
              flush=True)
    finally:
        s.close()


def share(work, name, lib_base):
    """An extension directory for extension_control_path naming one built library (path sans suffix,
    or a bare module name resolved through dynamic_library_path)."""
    ext = work / name / 'extension'
    ext.mkdir(parents=True)
    (ext / 'ztype.control').write_text((ROOT / 'ztype.control').read_text().replace('$libdir/ztype', str(lib_base)))
    shutil.copy(ROOT / 'ztype--0.9.sql', ext)
    return work / name


def sanitizer_env(work):
    """Variables that load the sanitizer runtime into the server before its own libraries, so a
    sanitized module can be dlopen'ed into an ordinary PostgreSQL build. Leak detection is off
    (server processes never free everything at exit) and a report ends the offending process
    with exit status 1 after writing work/sanitizer.<pid>, which the run then fails on."""
    if not SANITIZER_RUNTIME:
        return {}
    key = 'DYLD_INSERT_LIBRARIES' if sys.platform == 'darwin' else 'LD_PRELOAD'
    options = f'detect_leaks=0:abort_on_error=0:exitcode=1:symbolize=1:log_path={work}/sanitizer'
    # PostgreSQL raises errors with siglongjmp out of instrumented frames. On macOS the runtime's
    # unpoison-on-no-return walks the signal alternate stack, and with the runtime only DYLD-inserted
    # into an uninstrumented server that CHECK-fails on the second error; use_sigaltstack=0 avoids it.
    # ZTYPE_ASAN_EXTRA can append or override for debugging.
    extra = os.environ.get('ZTYPE_ASAN_EXTRA', 'use_sigaltstack=0')
    options = f'{options}:{extra}' if extra else options
    return {key: SANITIZER_RUNTIME, 'ASAN_OPTIONS': options, 'UBSAN_OPTIONS': f'print_stacktrace=1:halt_on_error=1:{options}'}


class Cluster:
    """One disposable socket-only cluster from one installation. The postmaster is launched directly
    rather than with pg_ctl start: pg_ctl goes through /bin/sh, and macOS SIP strips DYLD_* variables
    from that exec, which would silently leave a sanitizer run without its runtime.

    adopt=True takes an existing data directory (a pg_basebackup copy) instead of running initdb;
    the connection settings are appended after the primary's own file, so they win, and extra adds
    further lines (wal_level on a publisher, hot_standby on a replica)."""
    def __init__(self, bin, work, name, port, sharedir, libdir=None, adopt=False, extra=''):
        self.bin, self.data, self.work = Path(bin), work / name, work
        self.env = dict(os.environ, PGHOST=str(work), PGPORT=str(port), PGDATABASE='postgres', PGCLIENTENCODING='UTF8')
        # Never inherit a user's connection service, credentials, or server overrides.
        for key in ('PGSERVICE', 'PGSERVICEFILE', 'PGUSER', 'PGPASSWORD', 'PGOPTIONS', 'PGHOSTADDR'):
            self.env.pop(key, None)
        # Without a valid locale, macOS PostgreSQL aborts startup as 'postmaster became multithreaded'.
        self.env.setdefault('LC_ALL', 'C')
        self.proc = self.log = None
        if not adopt:
            run([self.bin / 'initdb', '-D', self.data, '-A', 'trust', '--no-locale', '-E', 'UTF8'], env=self.env)
        with (self.data / 'postgresql.conf').open('a') as conf:
            conf.write(f"\nlisten_addresses = ''\nunix_socket_directories = '{work}'\nport = {port}\n"
                       f"extension_control_path = '{sharedir}:$system'\n")
            if libdir:  # bare module name in the control file, resolved per cluster like a real install
                conf.write(f"dynamic_library_path = '{libdir}:$libdir'\n")
            if extra:
                conf.write(extra.rstrip('\n') + '\n')

    def start(self):
        self.log = open(self.work / f'{self.data.name}.log', 'ab')
        self.proc = subprocess.Popen([str(self.bin / 'postgres'), '-D', str(self.data)],
                                     env=dict(self.env, **sanitizer_env(self.work)), stdin=subprocess.DEVNULL,
                                     stdout=self.log, stderr=subprocess.STDOUT, start_new_session=True)
        self.wait_ready()

    def wait_ready(self, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError('postmaster exited' + self.diagnostics())
            if subprocess.run([self.bin / 'pg_isready'], env=self.env, capture_output=True).returncode == 0:
                return
            time.sleep(0.1)
        raise AssertionError('server did not become ready' + self.diagnostics())

    def stop(self, mode='fast'):
        if self.proc and self.proc.poll() is None:
            run([self.bin / 'pg_ctl', '-D', self.data, 'stop', '-m', mode], env=self.env)
            self.proc.wait(timeout=60)
        if self.log:
            self.log.close()

    def session(self, db='postgres'):
        return Session(dict(self.env, PGDATABASE=db), bin=self.bin, cluster=self)

    def reports(self):
        """Sanitizer report files written by server processes."""
        return sorted(self.work.glob('sanitizer.*'))

    def diagnostics(self):
        """Server log tail plus every sanitizer report: what to read when a backend disappears."""
        text = '\n--- server log tail:\n' + (self.work / f'{self.data.name}.log').read_text(errors='replace')[-6000:]
        for report in self.reports():
            text += f'\n--- {report.name}:\n' + report.read_text(errors='replace')
        return text

    def check_reports(self):
        """A report is a failure even when every query behaved: the bug was reached, just not felt."""
        if self.reports():
            raise AssertionError('sanitizer reports were written' + self.diagnostics())


def sanitizer_self_check(cluster):
    """Positive control for sanitizer runs: a module with a deliberate heap overflow must produce a
    report. Without it, a run whose runtime never reached the server would pass vacuously."""
    probe = cluster.work / 'probe'  # built through PGXS so platform flags and linking match the server
    probe.mkdir()
    shutil.copy(ROOT / 'tests' / 'asan_probe.c', probe)
    (probe / 'Makefile').write_text('MODULES = asan_probe\nPG_CONFIG ?= pg_config\nPGXS := $(shell $(PG_CONFIG) --pgxs)\ninclude $(PGXS)\n')
    run(['make', '-C', probe, f'PG_CONFIG={PG_CONFIG}', f"CC={os.environ.get('ZTYPE_CC', 'cc')}",
         'PG_CFLAGS=-fsanitize=address -g', 'PG_LDFLAGS=-fsanitize=address'])
    out = next(probe / f'asan_probe{suffix}' for suffix in ('.so', '.dylib') if (probe / f'asan_probe{suffix}').exists())
    s = cluster.session()
    s.query(f"CREATE FUNCTION asan_probe() RETURNS void AS '{out}' LANGUAGE C;")
    try:
        s.query('SELECT asan_probe();')
        survived = True
    except AssertionError:
        survived = False
    reports = cluster.reports()
    if survived or not any('heap-buffer-overflow' in r.read_text(errors='replace') for r in reports):
        raise AssertionError('sanitizer runtime is not active in the server: the probe overflow went unreported'
                             + cluster.diagnostics())
    for report in reports:
        report.rename(report.with_name('selfcheck-' + report.name))
    s.close()
    cluster.wait_ready()
    s = cluster.session()
    s.query('DROP FUNCTION asan_probe();')
    s.close()
    print('PASS: sanitizer runtime active (probe overflow reported)', flush=True)


def saved_plans(cluster):
    """The registry lookups run as saved generic plans, so a backend must survive whatever invalidates
    them: the registry altered, and the extension dropped and re-created in the same session (new table
    OID, same query text). Every lookup shape is exercised after each: by name (the modifier), by slot
    (a logical write), by dictionary ID (a decode) and inspect's dict_id join. Own database, so the
    drop cascades to nothing the other checks rely on."""
    run([BIN/'createdb', 'plans'], env=cluster.env)
    s = cluster.session('plans')
    try:
        def round_trip(expect_slot):
            s.query(training('plan'))
            s.equal(f"SELECT slot FROM ztype.dictionary_inventory WHERE name = 'plan';", str(expect_slot))
            s.query("CREATE TABLE IF NOT EXISTS planned (body ztext(6,'plan'));")
            s.query("INSERT INTO planned SELECT repeat('plan sample subject ', 30);")
            s.equal("SELECT body::text = repeat('plan sample subject ', 30) FROM planned;")
            s.equal(f"SELECT dict_slot, dict_name FROM planned, ztype.inspect(body);", f'{expect_slot}|plan')
            s.equal("SELECT count(*) FROM planned, ztype.inspect(body) WHERE dict_id IS NOT NULL;", '1')
        s.query('CREATE EXTENSION ztype;')
        round_trip(1)
        # The registry's row layout changes under a saved plan that selects named columns from it.
        s.query('ALTER TABLE ztype.dictionaries ADD COLUMN plan_note text;')
        s.query("SELECT ztype.reload_dictionaries();")
        s.equal("SELECT body::text = repeat('plan sample subject ', 30) FROM planned;")
        s.equal("SELECT dict_slot FROM planned, ztype.inspect(body);", '1')
        s.query("INSERT INTO planned SELECT repeat('plan sample subject ', 30);")
        s.query('ALTER TABLE ztype.dictionaries DROP COLUMN plan_note;')
        s.equal("SELECT count(*) FROM planned WHERE body::text = repeat('plan sample subject ', 30);", '2')
        # The table the plans were built against disappears and a new one takes its place.
        s.query('DROP TABLE planned; DROP EXTENSION ztype CASCADE;')
        s.query('CREATE EXTENSION ztype;')
        round_trip(1)
        s.query("SELECT 'x'::ztext(6,'missing');", error='is not registered')
    finally:
        s.close()
    print('PASS: saved registry plans survive ALTER TABLE and DROP/CREATE EXTENSION', flush=True)


def main():
    """Use PostgreSQL 18 extension search paths to avoid a system-wide installation."""
    with tempfile.TemporaryDirectory(prefix='ztype-test-') as tmp:
        work = Path(tmp)
        cluster = Cluster(BIN, work, 'pg', 55439, share(work, 'share', library().with_suffix('')),
                          extra='max_prepared_transactions = 2')
        cluster.start()
        try:
            if SANITIZER_RUNTIME:
                sanitizer_self_check(cluster)
            check(cluster, work)
            backups(cluster, work)
            recovery(cluster, work)
            fixtures(cluster)
            saved_plans(cluster)
            run([BIN/'createdb','--template=template0','--encoding=LATIN1','latin1'], env=cluster.env)
            latin = cluster.session('latin1')
            try:
                latin.query('CREATE EXTENSION ztype;')
                latin.equal("SELECT prefix(repeat('ä',100)::ztext,1) = 'ä', raw_length('ä'::ztext) = 1;", 't|t')
            finally:
                latin.close()
            print('PASS: non-UTF8 database encoding',flush=True)
            cluster.check_reports()
        except BaseException:
            print(cluster.diagnostics())
            raise
        finally:
            cluster.stop('immediate')


if __name__ == '__main__':
    main()
