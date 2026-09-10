#!/usr/bin/env python3
"""Prove stored values survive a PostgreSQL major upgrade, using two real installations.

Set ZTYPE_OLD_PG_CONFIG and ZTYPE_NEW_PG_CONFIG (paths to pg_config). The script builds
the extension against each, then checks three things between disposable clusters:
raw stored bytes decode in either direction, a logical dump from old restores into new
with no settings, and pg_upgrade carries dictionaries, values and typed defaults across.
"""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402  (run, training)
from test_ztype import Cluster, share  # noqa: E402,F401  (bench imports them from here too)

ROOT = Path(__file__).resolve().parents[1]
OLD = os.environ.get('ZTYPE_OLD_PG_CONFIG', 'pg_config')
NEW = os.environ.get('ZTYPE_NEW_PG_CONFIG', '')  # required to run; optional to import
SAMPLE = "(SELECT string_agg('first sample subject ' || i || md5(i::text), ' ') FROM generate_series(1,80) i)"
# zbytea already has real casts to and from bytea, so its raw bytes travel through ztext.
CASTS = ('CREATE CAST (ztext AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS ztext) WITHOUT FUNCTION;'
         'CREATE CAST (zjsonb AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS zjsonb) WITHOUT FUNCTION;'
         'CREATE CAST (zbytea AS ztext) WITHOUT FUNCTION; CREATE CAST (ztext AS zbytea) WITHOUT FUNCTION;')
RAW_OUT = {'ztext': '({v})::bytea', 'zjsonb': '({v})::bytea', 'zbytea': '(({v})::ztext)::bytea'}
RAW_IN = {'ztext': "decode('{h}', 'hex')::ztext", 'zjsonb': "decode('{h}', 'hex')::zjsonb", 'zbytea': "(decode('{h}', 'hex')::ztext)::zbytea"}
# (type, logical SQL expression, typmod) written on one side and decoded on the other.
VALUES = [('ztext', "'héllo 😀'", ''), ('ztext', SAMPLE, ''), ('ztext', SAMPLE, '(6,1)'), ('ztext', SAMPLE, '(19,1)'),
          ('zjsonb', f"jsonb_build_object('body', {SAMPLE}, 'n', 1, 'nested', jsonb_build_array(1, null, 'x'))", ''),
          ('zjsonb', f"jsonb_build_object('body', {SAMPLE})", '(6,1)'),
          ('zbytea', f"convert_to({SAMPLE}, 'UTF8')", ''), ('zbytea', f"convert_to({SAMPLE}, 'UTF8')", '(6,1)'),
          ('zbytea', "decode('0000ff00', 'hex')", '')]


def bindir(pg_config):
    return Path(subprocess.check_output([pg_config, '--bindir'], text=True).strip())


def build(pg_config, work, name):
    """Build a private copy of the extension against one installation; return the library path sans suffix."""
    src = work / name
    shutil.copytree(ROOT, src, ignore=shutil.ignore_patterns('*.o', '*.dylib', '*.so', 'tests'))
    t.run(['make', '-C', src, f'PG_CONFIG={pg_config}'])
    lib = next(src / f'ztype{s}' for s in ('.so', '.dylib') if (src / f'ztype{s}').exists())
    return lib


def write_side(s):
    """Register the shared dictionary, expose raw bytes, and emit (type, typmod, hex, logical-check SQL)."""
    s.query('CREATE EXTENSION ztype;')
    s.equal(t.training('first'), '1')
    s.query(CASTS)
    dict_hex = s.query("SELECT encode(dict, 'hex') FROM ztype.dictionaries WHERE slot = 1;")
    rows = []
    for kind, logical, tm in VALUES:
        rows.append((kind, tm, s.query(f"SELECT encode({RAW_OUT[kind].format(v=f'{logical}::{kind}{tm}')}, 'hex');"), logical))
    return dict_hex, rows


def read_side(s, dict_hex, rows, label):
    """Decode bytes written by the other major and compare with freshly computed logical values."""
    s.query('CREATE EXTENSION ztype;')
    s.equal(f"SELECT ztype.add_dictionary('first', decode('{dict_hex}', 'hex'));", '1')
    s.query(CASTS)
    base = {'ztext': 'text', 'zjsonb': 'jsonb', 'zbytea': 'bytea'}
    for kind, tm, hexval, logical in rows:
        raw = RAW_IN[kind].format(h=hexval)
        s.equal(f"SELECT ({raw})::{base[kind]} = {logical}::{base[kind]};")
        if kind == 'ztext':
            # prefix() takes a byte budget; 7 bytes never splits a character in these samples.
            s.equal(f"SELECT raw_length({raw}) = octet_length({logical}::text) AND prefix({raw}, 7) = convert_from(substring(convert_to({logical}::text, 'UTF8') from 1 for 7), 'UTF8');")
        if kind == 'zjsonb':
            s.equal(f"SELECT ({raw} ->> 'body') IS NOT DISTINCT FROM ({logical}::jsonb ->> 'body');")
        s.equal(f"SELECT ztype.recompress({raw}, 6, 1)::{base[kind]} = {logical}::{base[kind]};")
    s.equal("SELECT get_byte(('{\"a\":1}'::zjsonb)::bytea, 8) = 1;")
    print(f'PASS: raw values written by {label} decode here', flush=True)


def seed(s):
    """Data for the dump/restore and pg_upgrade legs: dictionary column, typed and domain defaults, JSON."""
    s.query('CREATE EXTENSION ztype;')
    s.equal(t.training('first'), '1')
    s.query("CREATE DOMAIN message_body AS ztext(6,'first') DEFAULT 'domain default'::ztext(6,'first');")
    s.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first') DEFAULT 'column default'::ztext(6,1), "
            "dom message_body, meta zjsonb(6,1), blob zbytea);")
    s.query(f"INSERT INTO messages SELECT i, {SAMPLE} || i, {SAMPLE}, jsonb_build_object('i', i, 'body', {SAMPLE}), "
            f"convert_to({SAMPLE}, 'UTF8') FROM generate_series(1, 30) i;")
    s.query('INSERT INTO messages(id) VALUES (31);')
    s.query("CREATE INDEX messages_meta ON messages USING gin ((meta::jsonb));")


def verify(s, label, new_id):
    """new_id must differ per call: rows written here travel with the data into the next leg."""
    s.equal(f"SELECT bool_and(body::text = {SAMPLE} || id AND dom::text = {SAMPLE} AND (meta ->> 'body') = {SAMPLE} "
            f"AND blob::bytea = convert_to({SAMPLE}, 'UTF8')) FROM messages WHERE id <= 30;")
    s.equal("SELECT body::text = 'column default' AND dom::text = 'domain default' AND meta IS NULL FROM messages WHERE id = 31;")
    s.equal("SELECT count(*) FROM ztype.dictionaries;", '1')
    s.equal("SELECT bool_and(ztype.recompress(body, 6, ztype.dictionary_slot('first'))::text = body::text) FROM messages;")
    s.equal("SELECT id FROM messages WHERE meta::jsonb @> '{\"i\": 7}' ORDER BY id;", '7')
    s.query(f"INSERT INTO messages(id, body, meta) SELECT {new_id}, {SAMPLE}, jsonb_build_object('body', {SAMPLE});")
    s.equal(f"SELECT body::text = {SAMPLE} AND (meta ->> 'body') = {SAMPLE} FROM messages WHERE id = {new_id};")
    print(f'PASS: {label}', flush=True)


def main():
    assert NEW, 'set ZTYPE_NEW_PG_CONFIG to the newer installation\'s pg_config'
    old_bin, new_bin = bindir(OLD), bindir(NEW)
    with tempfile.TemporaryDirectory(prefix='ztype-xm-') as tmp:
        work = Path(tmp)
        old_lib, new_lib = build(OLD, work, 'build_old'), build(NEW, work, 'build_new')
        old_share = share(work, 'share_old', old_lib.with_suffix(''))
        new_share = share(work, 'share_new', new_lib.with_suffix(''))
        old = Cluster(old_bin, work, 'old', 55451, old_share)
        new = Cluster(new_bin, work, 'new', 55452, new_share)
        old.start(); new.start()
        try:
            # 1. Raw stored bytes in both directions.
            a, b = old.session(), new.session()
            dict_hex, rows = write_side(a)
            read_side(b, dict_hex, rows, 'old major')
            a.close(); b.close()
            t.run([old_bin / 'createdb', 'reverse'], env=old.env)
            t.run([new_bin / 'createdb', 'reverse'], env=new.env)
            b, a = new.session('reverse'), old.session('reverse')
            dict_hex, rows = write_side(b)
            read_side(a, dict_hex, rows, 'new major')
            a.close(); b.close()
            # 2. Logical dump from old, restored into new with the new major's tools and no settings.
            t.run([old_bin / 'createdb', 'seeded'], env=old.env)
            s = old.session('seeded'); seed(s); verify(s, 'seed on old major', 41); s.close()
            dump = work / 'seeded.dump'
            t.run([new_bin / 'pg_dump', '-Fc', '-d', 'seeded', '-f', dump], env=old.env)
            t.run([new_bin / 'createdb', 'restored'], env=new.env)
            t.run([new_bin / 'pg_restore', '--exit-on-error', '-j', '4', '-d', 'restored', dump], env=new.env)
            s = new.session('restored'); verify(s, 'logical dump/restore old -> new', 42); s.close()
        finally:
            old.stop(); new.stop()
        # 3. pg_upgrade: as in a real installation, each major loads its own build of the library
        # under the same module name, so the control file names the module bare and each cluster's
        # dynamic_library_path points at its build.
        upg_share = share(work, 'share_upg', 'ztype')
        old = Cluster(old_bin, work, 'upg_old', 55453, upg_share, old_lib.parent)
        old.start()
        try:
            s = old.session(); seed(s); verify(s, 'seed before pg_upgrade', 41); s.close()
        finally:
            old.stop()
        new = Cluster(new_bin, work, 'upg_new', 55454, upg_share, new_lib.parent)
        t.run([new_bin / 'pg_upgrade', '-b', old_bin, '-B', new_bin, '-d', old.data, '-D', new.data, '-s', work, '-p', '55453', '-P', '55454'],
              env=dict(new.env, PGHOST=str(work)), cwd=work)
        new.start()
        try:
            s = new.session(); verify(s, 'pg_upgrade old -> new', 42); s.close()
        finally:
            new.stop()


if __name__ == '__main__':
    main()
