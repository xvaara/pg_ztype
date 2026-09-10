#!/usr/bin/env python3
"""Write tests/fixtures/storage-<magic>.json: the exact bytes this build stores, kept in the
repository so a later build has to keep decoding them.

`make test-cross` builds both majors from today's source, so nothing there notices a change to
the envelope, the frame options or the jsonb payload tagging. A committed fixture does: the
suite (test_ztype.fixtures) decodes these bytes, compares them against the logical values
recorded beside them, and rejects the retired magics.

Run it by hand after `make`, and only when the change to the storage format was deliberate:

    python3 tests/make_fixtures.py            # PG_CONFIG=... picks the installation

Everything here is synthetic - a dictionary trained on generated strings and sixteen generated
values. No corpus data goes into a fixture. Envelopes are native-endian, so a fixture is only
readable on a machine with the byte order recorded in it; the generator refuses to write one on
a big-endian machine rather than commit bytes the suite would skip everywhere.

When a fixture for the same magic already exists, every entry it has must come out byte-identical
(the dictionary too: training is deterministic for one libzstd version), otherwise the generator
refuses to overwrite it: bytes changing under an unchanged magic is the format drift the fixture
exists to catch. `--replace` overrides that after a deliberate decision (a new libzstd that encodes
differently is the expected reason; say so in the CHANGELOG). Adding entries never needs it.
"""
import argparse
import datetime
import json
from pathlib import Path
import struct
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import (BASE_TYPE, Cluster, FIXTURE_DIR, INSPECT_ROW, LIBRARY_MAGIC, RAW_CASTS, RAW_IN,  # noqa: E402
                        library, share, training)

# Overlaps training('fixture')'s samples, so the dictionary actually earns its place.
SAMPLE = ("(SELECT string_agg('fixture sample subject ' || i || md5(i::text), ' ') "
          'FROM generate_series(1,60) i)')
NESTED = (f"jsonb_build_object('body', {SAMPLE}, 'nested', jsonb_build_array(1, null, 'x'), "
          "'nul', null)")
# (id, type, typmod, logical SQL expression). Three kinds, raw and compressed, levels 1/6/19,
# with and without the dictionary, plus the edges a decoder can get wrong on its own: an empty
# value, multibyte text, a JSON null inside an array, and bytea with embedded zero bytes.
VALUES = [
    ('text-empty-raw', 'ztext', '', "''"),
    ('text-multibyte-raw', 'ztext', '', "'héllo \U0001f600'"),
    ('text-zstd-l6', 'ztext', '', SAMPLE),
    ('text-zstd-l1', 'ztext', '(1)', SAMPLE),
    ('text-zstd-l1-dict', 'ztext', '(1,1)', SAMPLE),
    ('text-zstd-l19-dict', 'ztext', '(19,1)', SAMPLE),
    ('text-multibyte-zstd', 'ztext', '', "repeat('aä\U0001f600 fixture ', 40)"),
    ('jsonb-raw', 'zjsonb', '', '\'{"a": 1}\'::jsonb'),
    ('jsonb-nested-zstd-l6', 'zjsonb', '', NESTED),
    ('jsonb-nested-dict-l19', 'zjsonb', '(19,1)', NESTED),
    ('bytea-zeros-raw', 'zbytea', '', "decode('0000ff00','hex')"),
    ('bytea-zeros-zstd', 'zbytea', '', "decode(repeat('0000ff00',40),'hex')"),
    ('bytea-utf8-dict-l1', 'zbytea', '(1,1)', f"convert_to({SAMPLE}, 'UTF8')"),
    # Above the TOAST threshold once compressed (md5 text compresses about 2:1), one per type, so the
    # suite also reads each type's bytes back out of line from a table.
    ('text-toast-zstd-l6', 'ztext', '', "(SELECT string_agg(md5(i::text), ' ') FROM generate_series(1,400) i)"),
    ('jsonb-toast-dict-l6', 'zjsonb', '(6,1)',
     "jsonb_build_object('body', (SELECT string_agg('fixture sample subject ' || i || md5(i::text), ' ') "
     "FROM generate_series(1,300) i), 'count', 300)"),
    ('bytea-toast-zstd-l1', 'zbytea', '(1)', "convert_to((SELECT string_agg(md5(i::text), '') FROM generate_series(1,300) i), 'UTF8')"),
]


def retired_envelope(magic):
    """A hand-built envelope under a magic ztype no longer writes: 16 bytes on disk (the four-byte
    varlena header encode() does not show, then magic, rawlen, format, level, kind, codec) and no
    payload. 0x5a540002 is real history - it is what the format looked like between dropping the
    outer CRC32C and splitting format into format + level, all on 2026-09-08 and all pre-release.
    Nothing but the magic has to be right: the magic is the first thing every read path checks."""
    return (struct.pack('<II', magic, 0) + bytes([0, 6, 1, 0])).hex()


def collect(s):
    """Register the fixture dictionary, then record each value's stored bytes and inspect row.
    Every entry is decoded here as well, so a regenerated fixture is known to be readable."""
    s.query('CREATE EXTENSION ztype;')
    s.equal(training('fixture'), '1')
    s.query(RAW_CASTS)
    dict_hex = s.query("SELECT encode(dict, 'hex') FROM ztype.dictionaries WHERE slot = 1;")
    dict_id = int(s.query('SELECT dict_id FROM ztype.dictionaries WHERE slot = 1;'))
    values = []
    for name, kind, tm, logical in VALUES:
        stored = f'({logical})::{kind}{tm}'
        hexval = s.query(f"SELECT encode(({stored})::bytea, 'hex');" if kind != 'zbytea'
                         else f"SELECT encode((({stored})::ztext)::bytea, 'hex');")
        raw = RAW_IN[kind].format(h=hexval)
        row = s.query(INSPECT_ROW.format(v=raw)).split('|')
        entry = {'id': name, 'type': kind, 'typmod': tm, 'sql': logical, 'hex': hexval,
                 'inspect': {'kind': row[0], 'codec': row[1], 'level': int(row[2]), 'format': int(row[3]),
                             'raw_length': int(row[4]), 'stored_bytes': int(row[5]),
                             'dict_id': int(row[6]) if row[6] else None}}
        assert entry['inspect']['stored_bytes'] == len(hexval) // 2, entry
        assert (entry['inspect']['dict_id'] == dict_id) == (',' in tm), entry
        # The same assertions the suite makes, so a fixture is never committed unreadable.
        s.equal(f'SELECT ({raw})::{BASE_TYPE[kind]} = ({logical})::{BASE_TYPE[kind]};')
        s.equal(f'SELECT ztype.validate({raw}) IS NULL;')
        if kind == 'ztext':
            s.equal(f"SELECT prefix({raw}, 7) = convert_from(substring(convert_to(({logical})::text,'UTF8')"
                    " from 1 for 7),'UTF8');")
        elif kind == 'zjsonb':
            # The suite compares `->>` over every top-level key, so a jsonb entry must be an object.
            s.equal(f"SELECT jsonb_typeof(({logical})::jsonb) = 'object';")
        values.append(entry)
    return dict_hex, dict_id, values


def unchanged(previous, data):
    """The entries the existing fixture already has, byte for byte; a list of what moved."""
    moved = []
    if previous['dictionary'] != data['dictionary']:
        moved.append('dictionary')
    new = {v['id']: v['hex'] for v in data['values']}
    moved += [v['id'] for v in previous['values'] if v['id'] not in new or new[v['id']] != v['hex']]
    return moved


def main():
    ap = argparse.ArgumentParser(description='write the storage fixture for the built library')
    ap.add_argument('--replace', action='store_true', help='overwrite entries whose bytes changed under the same magic')
    args = ap.parse_args()
    assert sys.byteorder == 'little', (
        f'refusing to write a {sys.byteorder}-endian fixture: envelopes are native-endian, so the '
        'suite would skip it on every ordinary machine')
    with tempfile.TemporaryDirectory(prefix='ztype-fixtures-') as tmp:
        work = Path(tmp)
        cluster = Cluster(t.BIN, work, 'pg', 55462, share(work, 'share', library().with_suffix('')))
        cluster.start()
        s = cluster.session()
        try:
            dict_hex, dict_id, values = collect(s)
            magic = int(s.query(LIBRARY_MAGIC))
            data = {
                'magic': f'0x{magic:08x}',
                'jsonb_format': int(s.query("SELECT get_byte(('{\"a\":1}'::zjsonb)::bytea, 8);")),
                'byte_order': sys.byteorder,
                'zstd_version': s.query('SELECT ztype.zstd_version();'),
                'generated': datetime.date.today().isoformat(),
                'note': 'synthetic data only; regenerate with tests/make_fixtures.py',
                'dictionary': {'name': 'fixture', 'slot': 1, 'dict_id': dict_id, 'hex': dict_hex},
                'values': values,
                # Bytes that must stay unreadable. On a deliberate magic bump, add the previous
                # magic here (and keep the previous file, which the suite reads the same way).
                'retired': [{'id': 'magic-5a540002', 'type': 'ztext', 'magic': '0x5a540002',
                             'hex': retired_envelope(0x5a540002)}],
            }
        finally:
            s.close()
            cluster.stop('immediate')
    FIXTURE_DIR.mkdir(parents=True, exist_ok=True)
    out = FIXTURE_DIR / f'storage-{magic:08x}.json'
    if out.exists():
        previous = json.loads(out.read_text())
        moved = unchanged(previous, data)
        if moved and not args.replace:
            sys.exit(f'{out} already records different bytes for {moved} under the same magic '
                     f'(written with zstd {previous["zstd_version"]}, this build has {data["zstd_version"]}): '
                     'bytes changing under an unchanged magic is what the fixture exists to catch. Bump ZT_MAGIC for a '
                     'format change, or rerun with --replace after a deliberate decision.')
        data['retired'] = previous.get('retired', data['retired'])  # history is kept, never regenerated
        print(f'{len(previous["values"])} existing entries unchanged' if not moved else f'replacing {moved}')
    out.write_text(json.dumps(data, indent=1, sort_keys=False) + '\n')
    print(f'wrote {out} ({len(values)} values, magic 0x{magic:08x}, zstd {data["zstd_version"]})')


if __name__ == '__main__':
    main()
