#!/usr/bin/env python3
"""Bounded, seed-reproducible mutation harness against the real extension, stdlib only.

Two questions, kept apart because they have different answers:

* Client-input safety: anything a client can send (text literals, type modifiers, binary
  COPY fields) must end in a clean error or a stored value, never a crash.
* Stored-corruption robustness: bytes that reach the decoder only through disk corruption
  or a hostile superuser (there is no client path for compressed bytes). Envelope fields,
  frame headers, dictionary IDs, truncation, trailing data and byte flips must fail with
  an error. Two documented exceptions may pass silently: raw payloads (no checksum) and a
  level byte inside 1..22 (informational). A third class is reported on its own: logical
  content that decodes correctly but is not what the base type expects. Text is verified
  against the database encoding, bytea accepts anything, and jsonb containers are read by
  PostgreSQL's native code, which trusts them; a corrupt container may error, succeed or
  crash the backend, and that is a limitation of the base type, not something this
  extension can detect short of validating every container.

Raw bytes enter only through WITHOUT FUNCTION casts created in a disposable superuser
database inside a disposable cluster. Each case derives from (seed, index), so a failure
is replayed with --seed S --only I; failing cases are also written to results/mutation/.
A crash reconnects after recovery and, for multi-flip mutations, is minimised to a single
flip. Sanitizer reports (make test-asan) fail the run even when every query behaved.
"""
import argparse
import json
import random
import struct
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, share  # noqa: E402

HDR = 12  # bytes visible through the bytea cast: magic 0..3, rawlen 4..7, format 8, level 9, kind 10, codec 11
END = '<' if sys.byteorder == 'little' else '>'
BASE = {'ztext': 'text', 'zjsonb': 'jsonb', 'zbytea': 'bytea'}
KIND = {'ztext': 1, 'zjsonb': 2, 'zbytea': 3}
FORMAT = {'ztext': 0, 'zjsonb': 1, 'zbytea': 0}
MAX_ALLOC = 0x3fffffff
CASTS = ('CREATE CAST (ztext AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS ztext) WITHOUT FUNCTION;'
         'CREATE CAST (zjsonb AS bytea) WITHOUT FUNCTION; CREATE CAST (bytea AS zjsonb) WITHOUT FUNCTION;'
         'CREATE CAST (zbytea AS ztext) WITHOUT FUNCTION; CREATE CAST (ztext AS zbytea) WITHOUT FUNCTION;')
RAW_OUT = {'ztext': '({v})::bytea', 'zjsonb': '({v})::bytea', 'zbytea': '(({v})::ztext)::bytea'}
RAW_IN = {'ztext': "decode('{h}', 'hex')::ztext", 'zjsonb': "decode('{h}', 'hex')::zjsonb", 'zbytea': "(decode('{h}', 'hex')::ztext)::zbytea"}
DICTS = ['alpha', 'beta', 'gamma']  # slots 1..3, distinct training sets
# Operations that always decompress the whole payload, so they must reject a corrupt frame. The
# rest (inspect, raw_length read only the envelope; prefix stops early; coerce_same is a no-op when
# the policy already matches) may legitimately succeed on a valid header over a corrupt payload.
FULL_OPS = {'decode', 'output', 'send', 'recompress', 'coerce_other', 'arrow', 'arrow_text', 'hash', 'eq_other'}


def op_expected(case_expect, opname):
    """What this operation must do given the case's corruption class (see mutate())."""
    full = opname in FULL_OPS
    if opname == 'validate':
        return 'report'         # never raises; its text is the verdict, checked in judge()
    if case_expect == 'error':
        return 'error'          # invalid header: even a metadata read must reject
    if case_expect == 'jsonb':
        return 'jsonb' if full else 'any'
    if case_expect == 'decode':
        return 'error' if full else 'any'
    if case_expect == 'kindmismatch':
        # Only fixed-kind decoders must reject; recompress and the coercion honour the stored byte.
        return 'error' if (opname in ('decode', 'output', 'send', 'arrow', 'arrow_text') or opname.startswith('prefix')) else 'any'
    return 'any'                # 'raw' and 'any': no checksum or no guarantee


class Item:
    """One corpus value: its logical origin and the stored bytes plus what inspect says about them."""
    def __init__(self, kind, sql, tm, raw, level, slot, dict_id, codec):
        self.kind, self.sql, self.tm, self.raw = kind, sql, tm, raw
        self.level, self.slot, self.dict_id, self.codec = level, slot, dict_id, codec
        self.rawlen = struct.unpack_from(END + 'I', raw, 4)[0]

    def describe(self):
        return f'{self.kind}{self.tm} {self.codec} rawlen={self.rawlen} stored={len(self.raw)} slot={self.slot}'


def logical_values(rng):
    """Deterministic base values across sizes that matter: below and at the 64-byte compression
    threshold, ordinary, larger than a TOAST chunk and than one codec step (256 kB), incompressible
    bytes, and JSON shapes with nested containers. Random bytes come from the seeded generator."""
    text = "'hi'", "repeat('a', 63)", "repeat('a', 64)", "'aä😀' || repeat('alpha sample subject ', 20)"
    text += ("(SELECT string_agg('beta sample subject ' || i || md5(i::text), ' ') FROM generate_series(1, 120) i)",
             "(SELECT string_agg(md5(i::text) || 'ä', '') FROM generate_series(1, 9000) i)",
             "(SELECT string_agg(md5(i::text), '') FROM generate_series(1, 40000) i)")
    js = ("'{\"a\":1}'", "'[1, null, \"x\", {\"k\": [true, false]}]'", "'\"scalar\"'",
          "jsonb_build_object('k', repeat('v', 100), 'n', 1, 'nested', jsonb_build_array(1, null, 'x', jsonb_build_object('d', 2.5)))",
          "(SELECT jsonb_object_agg('key' || i, jsonb_build_object('body', 'gamma sample subject ' || md5(i::text), 'i', i)) FROM generate_series(1, 400) i)",
          "(SELECT jsonb_agg(jsonb_build_object('i', i, 's', md5(i::text))) FROM generate_series(1, 9000) i)")
    rnd = lambda n: "decode('" + bytes(rng.getrandbits(8) for _ in range(n)).hex() + "', 'hex')"
    by = ("decode('0000ff00', 'hex')", rnd(63), rnd(64), rnd(300), rnd(9000),
          "convert_to(repeat('alpha sample subject ', 40), 'UTF8')",
          "(SELECT string_agg(decode(md5(i::text), 'hex'), '') FROM generate_series(1, 20000) i)")
    return [('ztext', v) for v in text] + [('zjsonb', v) for v in js] + [('zbytea', v) for v in by]


def build_corpus(s, rng):
    """Register three dictionaries, store each base value under a few policies and read the bytes back."""
    for name in DICTS:
        s.query(t.training(name))
    s.query(CASTS)
    items = []
    for kind, sql in logical_values(rng):
        policies = ['', f'({rng.choice([1, 3, 6, 9, 19])},{rng.randint(1, 3)})']
        if rng.random() < 0.5:
            policies.append(f'({rng.choice([1, 22])})')
        for tm in policies:
            expr = f'({sql})::{kind}{tm}'
            row = s.query(f"SELECT encode({RAW_OUT[kind].format(v=expr)}, 'hex'), i.level, coalesce(i.dict_slot, 0), coalesce(i.dict_id, 0), i.codec "
                          f"FROM ztype.inspect({expr}) i;").split('|')
            items.append(Item(kind, sql, tm, bytes.fromhex(row[0]), int(row[1]), int(row[2]), int(row[3]), row[4]))
    return items


def frame_fields(payload):
    """Offsets of the variable frame-header fields after the magic and the descriptor byte."""
    fhd = payload[4]
    single = (fhd >> 5) & 1
    did_size = (0, 1, 2, 4)[fhd & 3]
    fcs_size = ((1 if single else 0), 2, 4, 8)[fhd >> 6]
    pos = 5 + (0 if single else 1)
    return {'fhd': 4, 'did': pos, 'did_size': did_size, 'fcs': pos + did_size, 'fcs_size': fcs_size, 'end': pos + did_size + fcs_size}


def put(b, off, value, size):
    return b[:off] + value.to_bytes(size, 'little') + b[off + size:]


class Case:
    """One mutated value, how it was made and what may legitimately happen to it. Flip cases carry
    their (offset, mask) list and a rebuild(session, subset) so a crash can be minimised."""
    def __init__(self, category, mutator, label, kind, raw, expect, flips=None, rebuild=None):
        self.category, self.mutator, self.label, self.kind = category, mutator, label, kind
        self.raw, self.expect, self.flips, self.rebuild = raw, expect, flips or [], rebuild


def apply_flips(data, flips):
    out = bytearray(data)
    for pos, mask in flips:
        out[pos] ^= mask
    return bytes(out)


def mutate(rng, item, corpus, dict_ids):
    """Stored-corruption mutators. Each Case carries an expectation class read per operation by the
    runner (op_expected): 'error' every operation (including metadata reads) must reject, because the
    envelope header is invalid; 'decode' full-decoding operations must reject but metadata reads
    (inspect, raw_length) and partial prefix may legitimately succeed, since a valid header with a
    corrupt payload only surfaces when the payload is actually decompressed; 'raw' anything (raw
    payloads carry no checksum, text is only verified against the encoding); 'jsonb' full decode
    reaches PostgreSQL's native container reader (error, success or a backend crash, reported as a
    limitation, none a ztype defect); 'any' error or success. Envelope offsets are the bytea view;
    payload offsets add HDR."""
    raw, kind = item.raw, item.kind
    zstd = item.codec == 'zstd'
    payload_len = len(raw) - HDR
    choice = rng.choice(['magic', 'rawlen', 'format', 'level', 'kind', 'codec', 'truncate', 'trailing',
                         'flip', 'flip', 'dict_id', 'fhd', 'splice', 'relabel', 'below_header'])
    if choice == 'magic':
        value = rng.choice([0, 0x5a540002, 0x5a540004, rng.getrandbits(32)])
        return Case('stored', choice, f'magic={value:#x}', kind, put(raw, 0, value, 4), 'error')
    if choice == 'rawlen':
        n = item.rawlen
        value = rng.choice([0, 1, max(n - 1, 0), n + 1, n ^ (1 << rng.randrange(32)), MAX_ALLOC - 16, MAX_ALLOC - 15, MAX_ALLOC, 0x7fffffff, 0xffffffff])
        if value == n:
            value = n + 1
        # An out-of-bounds length is rejected by the header; an in-bounds wrong one only at decode.
        header_fatal = value > MAX_ALLOC - HDR or (kind == 'zjsonb' and value < 4)
        return Case('stored', choice, f'rawlen={value}', kind, put(raw, 4, value, 4), 'error' if header_fatal else 'decode')
    if choice == 'format':
        value = rng.choice([v for v in (0, 1, 2, 255, rng.randrange(256)) if v != FORMAT[kind]])
        return Case('stored', choice, f'format={value}', kind, put(raw, 8, value, 1), 'error')
    if choice == 'level':
        value = rng.choice([0, 23, 31, 255, rng.randint(1, 22)])
        if value == item.level:
            value = 0
        return Case('stored', choice, f'level={value}', kind, put(raw, 9, value, 1), 'any' if 1 <= value <= 22 else 'error')
    if choice == 'kind':  # a different kind byte behind the same cast
        value = rng.choice([v for v in (0, 1, 2, 3, 4, 255) if v != KIND[kind]])
        # A valid but wrong kind is caught by decoders that expect a fixed kind (the base-type output
        # cast, prefix, the JSON operators); recompress and the coercion are polymorphic C functions
        # that trust the stored kind byte, so they process the value as the byte says. Out-of-range
        # kinds fail the header everywhere.
        return Case('stored', choice, f'kind={value}', kind, put(raw, 10, value, 1), 'kindmismatch' if 1 <= value <= 3 else 'error')
    if choice == 'codec':
        value = rng.choice([1 - (1 if zstd else 0), 2, 255])
        # codec 0/1 swap is caught at decode (raw-length or frame check); codec > 1 by the header.
        return Case('stored', choice, f'codec={value}', kind, put(raw, 11, value, 1), 'decode' if value <= 1 else 'error')
    if choice == 'below_header':
        cut = rng.randint(0, HDR - 1)
        return Case('stored', choice, f'cut={cut}', kind, raw[:cut], 'error')
    if choice == 'truncate':
        if payload_len == 0:
            return Case('stored', choice, 'cut=payload-empty', kind, raw[:HDR], 'decode')
        hot = [HDR, HDR + 4, HDR + 5, HDR + 6, HDR + 8, len(raw) - 4, len(raw) - 1]
        cut = rng.choice(hot + [rng.randrange(HDR, len(raw))])
        cut = min(max(cut, HDR), len(raw) - 1)
        return Case('stored', choice, f'cut={cut}', kind, raw[:cut], 'decode')
    if choice == 'trailing':
        tail = rng.choice([b'\0', bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 64))),
                           rng.choice(corpus).raw[HDR:] or b'x'])
        return Case('stored', choice, f'trailing={len(tail)}', kind, raw + tail, 'decode')
    if choice == 'flip':
        if payload_len == 0:
            return Case('stored', choice, 'flip=none', kind, raw, 'any')
        n = rng.choice([1, 1, 1, 2, 4])
        flips = sorted({(rng.randrange(HDR, len(raw)), 1 << rng.randrange(8)) for _ in range(n)})
        # A compressed payload is covered by the frame checksum: every flip must be caught, except
        # bit 4 of the frame header descriptor (offset HDR + 4), which the zstd format reserves as
        # unused and decoders must ignore; the fhd mutator covers it deliberately. A raw payload has
        # no checksum; text is only verified against the encoding.
        unused_only = all(f == (HDR + 4, 0x10) for f in flips)
        expect = ('any' if unused_only else 'decode') if zstd else ('jsonb' if kind == 'zjsonb' else 'raw')
        return Case('stored', choice, f'flip@{flips}', kind, apply_flips(raw, flips), expect, flips,
                    lambda s, subset: apply_flips(raw, subset))
    if choice in ('dict_id', 'fhd'):
        if not zstd:
            return Case('stored', choice, 'not-a-frame', kind, raw, 'any')
        f = frame_fields(raw[HDR:])
        if choice == 'fhd':
            bit = rng.choice([2, 3, 4, 5, 6, 7, 0, 1])
            out = bytearray(raw)
            out[HDR + 4] ^= 1 << bit
            # Bit 4 is unused and ignored by zstd; every other bit changes what the header means.
            return Case('stored', choice, f'fhd^bit{bit}', kind, bytes(out), 'any' if bit == 4 else 'decode')
        # A nonzero dictionary ID that is not registered is a guaranteed strict-lookup failure. A
        # real ID (the frame's own or another's) or zero may still decode: a real dictionary supplies
        # compatible tables and the content checksum passes, which is a valid value, not corruption.
        # The expectation is read from the ID actually stored, after masking to the field width.
        registered = set(dict_ids)
        unreg = next(v for v in range(0xC0FFEE01, 0xFFFFFFFF) if v not in registered)
        value = rng.choice([unreg, 0, (rng.choice(list(registered)) if registered else unreg), item.dict_id ^ 1])
        if f['did_size'] == 0:  # no dictionary field: declare a 4-byte one and insert it
            eff = value
            out = bytearray(raw)
            out[HDR + 4] |= 3
            out[HDR + f['did']:HDR + f['did']] = eff.to_bytes(4, 'little')
            label = f'dict_id+={eff}'
        else:
            eff = value & ((1 << (8 * f['did_size'])) - 1)
            out = put(raw, HDR + f['did'], eff, f['did_size'])
            label = f'dict_id={eff}'
        expect = 'decode' if eff != 0 and eff not in registered else 'any'
        return Case('stored', choice, label, kind, bytes(out), expect)
    if choice == 'splice':
        other = rng.choice(corpus)
        out = raw[:HDR] + other.raw[HDR:]
        # A different declared length or codec than the donor payload guarantees a decode error; a
        # matching frame is self-consistent and decodes (to the donor's content), so only no-crash is
        # required (a native jsonb container may still crash: the documented limitation).
        inconsistent = other.rawlen != item.rawlen or other.codec != item.codec
        expect = 'decode' if inconsistent else ('jsonb' if kind == 'zjsonb' else 'any')
        return Case('stored', choice, f'payload<-{other.describe()}', kind, out, expect)
    if choice == 'relabel':  # consistent kind+format for another base type, injected through that cast
        target = rng.choice([k for k in BASE if k != kind])
        out = put(put(raw, 10, KIND[target], 1), 8, FORMAT[target], 1)
        expect = {'ztext': 'any', 'zbytea': 'any', 'zjsonb': 'jsonb'}[target]
        return Case('stored', choice, f'{kind}->{target}', target, out, expect)
    raise AssertionError(choice)


def native_content(q, rng, item, corpus):
    """Logical content that is not what the base type expects, behind a valid frame: the decoded
    bytes of a corpus value with flips (or a text/bytea payload verbatim), compressed as zbytea
    and relabelled. Reaches the native jsonb readers and the encoding check with valid checksums.
    `q` is a query callable that reconnects on a crash; these build steps only ever decode as bytea
    or compress, so they do not themselves crash a backend."""
    source = rng.choice([i for i in corpus if i.kind == 'zjsonb']) if rng.random() < 0.7 else rng.choice(corpus)
    relabel = put(put(source.raw, 10, KIND['zbytea'], 1), 8, 0, 1)  # any kind decodes as bytea
    origin = bytes.fromhex(q(f"SELECT encode(({RAW_IN['zbytea'].format(h=relabel.hex())})::bytea, 'hex');"))
    flips, cut = [], len(origin)
    if origin and (source.kind == 'zjsonb' or rng.random() < 0.5):
        n = rng.choice([1, 1, 2, 3, 8])
        flips = sorted({(rng.randrange(len(origin)), rng.choice([1 << rng.randrange(8), 0xff])) for _ in range(n)})
        if rng.random() < 0.2:
            cut = rng.randint(0, len(origin))
    target = rng.choice(['zjsonb', 'zjsonb', 'ztext'])
    if source.kind == 'zjsonb' and rng.random() < 0.8:
        target = 'zjsonb'
    tm = rng.choice(['', '(3,1)', '(6,2)'])

    def rebuild(q, subset):
        content = apply_flips(origin, subset)[:cut]
        packed = bytes.fromhex(q(f"SELECT encode(((decode('{content.hex()}', 'hex')::zbytea{tm})::ztext)::bytea, 'hex');"))
        return put(put(packed, 10, KIND[target], 1), 8, FORMAT[target], 1)

    label = f'{source.describe()} content flips@{flips} cut={cut} -> {target}{tm}'
    return Case('native', 'content', label, target, rebuild(q, flips), 'jsonb' if target == 'zjsonb' else 'any', flips, rebuild)


def client_cases(rng, work, index):
    """Ordinary client input: text literals, type modifiers and binary COPY fields. Everything is
    an error or a stored value; nothing may crash."""
    junk = lambda n: ''.join(rng.choice('{}[]",:\\ 0123456789.eE+-tfnrulaäö😀\t\n') for _ in range(n))
    kind = rng.choice(['ztext', 'zjsonb', 'zbytea'])
    if rng.random() < 0.5:
        lit = junk(rng.randint(0, 80)).replace('$', '')
        tm = rng.choice(['', f'({rng.randint(-1, 25)})', f'({rng.randint(1, 22)},{rng.randint(-1, 70000)})',
                         f"({rng.randint(1, 22)},'{rng.choice(DICTS + ['nope', '1', ''])}')", '(6,1,2)', '(x)'])
        return Case('client', 'text_input', f'{kind}{tm} literal', kind, None, 'any'), f'SELECT pg_column_size($z${lit}$z$::{kind}{tm});'
    # Binary COPY file: PGCOPY header, one tuple of three fields, trailer. Field bytes are what
    # the receive functions see; the jsonb version byte and the encoding are the interesting parts.
    def field(data):
        return struct.pack('!i', len(data)) + data if data is not None else struct.pack('!i', -1)
    text = rng.choice([b'plain text', 'aä😀'.encode(), b'\xff\xfe bad utf8', b'nul\x00byte', b'', bytes(rng.getrandbits(8) for _ in range(rng.randint(1, 200)))])
    version = rng.choice([1, 1, 0, 2, 255])
    body = rng.choice([b'{"k": 1}', b'[1, 2', b'', b'{"k": "\xff"}', junk(rng.randint(1, 60)).encode(), b'{"k": "' + b'v' * 5000 + b'"}'])
    blob = rng.choice([b'', b'\x00' * 10, bytes(rng.getrandbits(8) for _ in range(rng.randint(0, 300)))])
    fields = [field(text), field(bytes([version]) + body), field(blob)]
    if rng.random() < 0.15:  # a length that overruns or overstates the data
        fields[rng.randrange(3)] = struct.pack('!i', rng.choice([5, 1 << 20, 0x7fffffff, -2])) + b'x'
    data = b'PGCOPY\n\xff\r\n\x00' + struct.pack('!ii', 0, 0) + struct.pack('!h', 3) + b''.join(fields) + struct.pack('!h', -1)
    path = work / f'copy-{index}.bin'
    path.write_bytes(data)
    return Case('client', 'binary_copy', f'text={text[:12]!r} jsonb v{version} body={body[:12]!r}', 'copy', None, 'any'), f"COPY copy_in FROM '{path}' (FORMAT binary);"


def ops_for(case, rng, work, item_level, item_slot):
    """Operations on the injected value, written with a {V} placeholder for the value expression so
    the runner can either read a temp-table column (batched mode) or inline the decode (one op per
    backend, which macOS ASan needs). Covers the base cast, the output function, prefix at byte
    boundaries, raw_length, inspect, the same-type coercion (no-op and rewrite), recompress, binary
    send, the JSON operators, equality and the hash."""
    kind, base = case.kind, BASE[case.kind]
    rawlen = struct.unpack_from(END + 'I', case.raw, 4)[0] if len(case.raw) >= 8 else 0
    # 'output' runs the type's own output function: psql renders the selected value through it.
    # validate first, so it runs even in the cases where the decode after it crashes the backend.
    ops = [('validate', 'SELECT ztype.validate(({V}));'),
           ('decode', f'SELECT length(({{V}})::{base}::text);'), ('output', 'SELECT ({V});')]
    if kind == 'ztext':
        for n in rng.sample([1, 5, 63, 64, 100, max(rawlen - 1, 0), rawlen, rawlen + 1, 2 ** 31 - 1], 3):
            ops.append((f'prefix({n})', f'SELECT octet_length(prefix(({{V}}), {n}));'))
    if kind in ('ztext', 'zbytea'):
        ops.append(('raw_length', 'SELECT raw_length(({V}));'))
    ops.append(('inspect', 'SELECT i.* FROM ztype.inspect(({V})) i;'))
    ops.append(('coerce_same', f'SELECT pg_column_size(({{V}})::{kind}({item_level},{item_slot}));'))
    level, slot = rng.choice([l for l in (1, 3, 6, 9, 19) if l != item_level]), rng.choice([s for s in (0, 1, 2, 3) if s != item_slot])
    ops.append(('coerce_other', f'SELECT pg_column_size(({{V}})::{kind}({level},{slot}));'))
    ops.append(('recompress', f'SELECT pg_column_size(ztype.recompress(({{V}}), {rng.choice([1, 6, 22])}, {rng.choice([0, 1, 2, 3])}));'))
    ops.append(('send', f"COPY (SELECT ({{V}})) TO '{work}/send.bin' (FORMAT binary);"))
    if kind == 'zjsonb':
        ops += [('arrow', "SELECT (({V}) -> 'k')::text;"), ('arrow_text', "SELECT ({V}) ->> 'key1';")]
    # Equality against itself decides on identical bytes after the header check alone; against the
    # same bytes with the level byte toggled (same declared length, different bytes) it must decode
    # both sides, like the hash, so both of those are full operations.
    alt = bytearray(case.raw)
    if len(alt) > 9:
        alt[9] ^= 1
    other = RAW_IN[kind].format(h=alt.hex())
    ops += [('hash', f'SELECT {kind}_hash(({{V}}));'), ('eq_self', 'SELECT ({V}) = ({V});'),
            ('eq_other', f'SELECT ({{V}}) = {other};')]
    keep = ops[:2] + rng.sample(ops[2:], min(5, len(ops) - 2))
    return keep


class Runner:
    """Drives one cluster: injects cases, classifies outcomes, stresses the dictionary cache
    around each operation, reconnects after crashes and records failures.

    isolate mode runs each operation in its own backend and transaction. It is required under
    AddressSanitizer on macOS, where the runtime CHECK-fails when a second ereport raises through
    an instrumented frame in the same forked backend (see README testing notes); it is a runtime
    artefact, not a ztype defect, and one longjmp per backend sidesteps it. Off, operations share
    one long session, which is faster and keeps the dictionary cache warm across a whole case."""
    def __init__(self, cluster, work, results, strict_jsonb, isolate):
        self.cluster, self.work, self.results, self.strict_jsonb, self.isolate = cluster, work, results, strict_jsonb, isolate
        self.s = cluster.session('mutation')
        self.counts = {}
        self.failures = []
        self.limitations = []
        self.other_errors = {}
        self.last_ops = []
        self.artifacts = 0  # macOS longjmp CHECK occurrences (platform, not ztype); should be zero

    def touch(self, rng, k=2):
        """Write with, then read through, a few dictionaries: loads and evictions around the case."""
        slots = [rng.randint(1, 3) for _ in range(k)]
        return 'SELECT ' + ', '.join(f"length((repeat('{DICTS[s - 1]} sample subject ', 30)::ztext(6,{s}))::text)" for s in slots) + ';'

    def classify(self, out):
        # A successful op keeps its (truncated) output: ztype.validate reports in the result,
        # not in an error, and judge() reads it there.
        if 'ERROR:' not in out:
            return 'ok', out[:200]
        message = next((line for line in out.splitlines() if line.startswith('ERROR:')), out)
        if 'statement timeout' in message:
            return 'timeout', message
        if 'ztype:' in message:
            return 'ztype_error', message
        return 'other_error', message

    def query_or_crash(self, session, sql):
        """Run sql; distinguish a clean SQL error from the backend disappearing (a real crash)."""
        try:
            return self.classify(session.raw(sql))
        except AssertionError as e:
            if not str(e).startswith('backend exited'):
                raise
            return 'crash', str(e).split('\n', 1)[0]

    def run_op_shared(self, sql):
        """Batched mode: one operation under a savepoint in the shared session."""
        outcome, message = self.query_or_crash(self.s, 'SAVEPOINT o; ' + sql)
        if outcome == 'crash':
            self.recover()
        else:
            self.s.query('ROLLBACK TO o;' if outcome != 'ok' else 'RELEASE o;')
        return outcome, message

    def run_op_isolated(self, value_sql, op, rng):
        """isolate mode: a fresh backend runs one operation in its own transaction, with a cache
        budget and dictionary traffic around it; when the operation succeeded the transaction commits,
        the cache must be within budget still, and a flush must release everything, accounting
        included. At most one ereport-longjmp reaches this backend, which is what macOS ASan requires,
        and a failed operation therefore gets no further query: its backend exits with the session,
        which frees whatever the error left behind."""
        s = self.cluster.session('mutation')
        stats_ok = True
        try:
            s.query(f'BEGIN; SET LOCAL ztype.dictionary_cache_size = {self.budget(rng)};')
            s.query(self.touch(rng))
            outcome, message = self.query_or_crash(s, op.format(V=value_sql))
            # A failed op aborts the transaction; a further query would raise a second error, and
            # under macOS ASan a second ereport-longjmp in one backend trips the runtime CHECK. Read
            # the cache only when the op succeeded.
            if outcome == 'ok':
                entries, size, budget = [int(x) for x in s.query('SELECT entries, bytes, budget_bytes FROM ztype.dictionary_cache_stats();').split('|')]
                stats_ok = entries >= 0 and (size <= budget or entries <= 1)
                s.query('COMMIT;')
                after = [int(x) for x in s.query('SELECT entries, bytes FROM ztype.dictionary_cache_stats();').split('|')]
                stats_ok = stats_ok and after[0] <= entries and after[1] <= size  # the commit keeps at most what was there
                s.query('SELECT ztype.reload_dictionaries();')
                stats_ok = stats_ok and s.query('SELECT entries, bytes FROM ztype.dictionary_cache_stats();') == '0|0'
        finally:
            try:
                s.close()
            except Exception:
                pass
        self.cluster.wait_ready()
        return outcome, message, stats_ok

    def s_query(self, sql):
        """The shared observer session, reconnected if a prior crash took it down. When the crash
        happens between two observer statements, psql learns of it on the next one: the backend's
        "terminating connection because of crash of another server process" notice arrives, the
        connection drops and psql may reset it and carry on, so the statement never ran and the notice
        would come back as its result. Treat that as the lost backend it is and run the statement
        again on a fresh session."""
        try:
            out = self.s.query(sql)
            if not any(k in out for k in ('Attempting reset', 'server closed the connection', 'connection to server')):
                return out
            print(f'observer connection was reset; rerunning the statement: {out!r}', file=sys.stderr)
        except AssertionError:
            pass
        self.recover()
        return self.s.query(sql)

    def recover(self):
        try:
            self.s.close()
        except Exception:
            pass
        self.cluster.wait_ready()
        self.s = self.cluster.session('mutation')

    def budget(self, rng):
        return rng.choice(['DEFAULT', '0', "'64kB'", "'192kB'", "'1MB'"])

    def run_case(self, index, case, sql, ops, rng):
        """Inject, run the ops under a cache budget with dictionary traffic around them, check the
        cache accounting afterwards, then classify against the case's expectation."""
        stats_ok = True
        outcomes = []
        self.last_ops = [sql] if ops is None else [op for _, op in ops]
        if ops is None:  # a client-input case: a single self-contained statement, no {V}
            if self.isolate:  # its own backend, so an error's longjmp is the only one there
                s = self.cluster.session('mutation')
                try:
                    s.query(f'SET ztype.dictionary_cache_size = {self.budget(rng)};')
                    outcome, message = self.query_or_crash(s, sql)
                finally:
                    try:
                        s.close()
                    except Exception:
                        pass
                if outcome == 'crash':
                    self.cluster.wait_ready()
                outcomes.append(('inject', outcome, message))
            else:
                self.s_query(f'BEGIN; SET LOCAL ztype.dictionary_cache_size = {self.budget(rng)};')
                outcome, message = self.run_op_shared(sql)
                outcomes.append(('inject', outcome, message))
                if outcome != 'crash':
                    self.s.query('ROLLBACK;')
        elif self.isolate:
            value_sql = RAW_IN[case.kind].format(h=case.raw.hex())
            for name, op in ops:
                outcome, message, op_stats = self.run_op_isolated(value_sql, op, rng)
                stats_ok = stats_ok and op_stats
                outcomes.append((name, outcome, message))
                if outcome == 'crash':
                    break
        else:
            self.s_query(f'BEGIN; SET LOCAL ztype.dictionary_cache_size = {self.budget(rng)};')
            self.s.query(self.touch(rng))
            value_sql = '(SELECT v FROM m)'
            self.s.query('DROP TABLE IF EXISTS m; CREATE TEMP TABLE m AS SELECT ' + RAW_IN[case.kind].format(h=case.raw.hex()) + ' AS v;')
            for name, op in ops:
                outcome, message = self.run_op_shared(op.format(V=value_sql))
                outcomes.append((name, outcome, message))
                if outcome == 'crash':
                    break
                self.s.query(self.touch(rng, 1))
            if outcomes[-1][1] != 'crash':
                entries, size, budget = [int(x) for x in self.s.query('SELECT entries, bytes, budget_bytes FROM ztype.dictionary_cache_stats();').split('|')]
                stats_ok = entries >= 0 and (size <= budget or entries <= 1)
                # The cache outlives the transaction; what must hold is that an abort leaves no more than
                # was there before it and that a manual flush releases everything, accounting included.
                self.s.query('ROLLBACK;' if rng.random() < 0.5 else 'COMMIT;')
                after = [int(x) for x in self.s.query('SELECT entries, bytes FROM ztype.dictionary_cache_stats();').split('|')]
                stats_ok = stats_ok and after[0] <= entries and after[1] <= size
                self.s.query('SELECT ztype.reload_dictionaries();')
                stats_ok = stats_ok and self.s.query('SELECT entries, bytes FROM ztype.dictionary_cache_stats();') == '0|0'
        reports = self.cluster.reports()
        report_texts = [r.read_text(errors='replace') for r in reports]
        verdict = self.judge(case, outcomes, report_texts, stats_ok)
        for name, outcome, message in outcomes:
            key = (case.category, case.mutator, outcome)
            self.counts[key] = self.counts.get(key, 0) + 1
            if outcome == 'other_error':
                self.other_errors[message] = self.other_errors.get(message, 0) + 1
        record = {'index': index, 'category': case.category, 'mutator': case.mutator, 'label': case.label, 'kind': case.kind,
                  'hex': case.raw.hex() if case.raw is not None else None, 'sql': sql, 'outcomes': outcomes, 'verdict': verdict,
                  'reports': [t[:4000] for t in report_texts], 'cache_stats_ok': stats_ok}
        if verdict in ('limitation', 'artifact'):
            self.limitations.append(record)
        elif verdict != 'pass':
            record['minimized'] = self.minimize(case, outcomes)
            self.failures.append(record)
        for r in reports:
            r.rename(r.with_name(f'case{index}-' + r.name))
        return record

    def jsonb_case(self, case):
        """A case whose full decode reaches PostgreSQL's native jsonb container reader."""
        return case.expect == 'jsonb'

    @staticmethod
    def is_macos_artifact(text):
        """The macOS ASan runtime CHECK-fails when a second ereport raises through an instrumented
        frame in one forked backend (PlatformUnpoisonStacks on the longjmp). It is a platform
        artifact, never a ztype defect; per-operation isolation should keep it from occurring at all.
        Its stack routes through ztype's own recv/input frames, so it must be excluded explicitly."""
        return 'asan_poisoning.cpp' in text and 'PlatformUnpoisonStacks' in text

    @staticmethod
    def implicates_ztype(report_texts):
        """True when the fault is in ztype's own code or in libzstd driven by it: the top two stack
        frames (#0 the faulting instruction, #1 its caller) name ztype or ZSTD. A crash whose top
        frames are PostgreSQL's own code — typically its native jsonb reader walking a corrupt
        container, with a zjsonb accessor only further down the stack — is the documented base-type
        limitation, not a defect this extension can prevent."""
        ours = ('ztype.c', 'ztype.dylib', 'ztype.so', ' zt_', 'ZSTD_', 'ZDICT_', 'libzstd')
        for text in report_texts:
            frames = [ln for ln in text.splitlines() if ln.lstrip().startswith('#')]
            if any(marker in frame for frame in frames[:2] for marker in ours):
                return True
        return False

    def judge(self, case, outcomes, report_texts, stats_ok):
        """Discount the macOS longjmp artifact first. A remaining sanitizer report that implicates
        ztype's own code, or broken cache accounting, fails outright; one only in PostgreSQL's code on
        a native-jsonb case is that documented limitation. Otherwise each operation is judged against
        what its corruption class allows: a crash is a failure unless the class is the native-jsonb
        one, a full-decode success on data that had to fail is a silent_success, timeout always fails."""
        artifacts = [t for t in report_texts if self.is_macos_artifact(t)]
        real = [t for t in report_texts if not self.is_macos_artifact(t)]
        if artifacts:
            self.artifacts += len(artifacts)
        if real:
            if self.implicates_ztype(real):
                return 'sanitizer'
            if self.jsonb_case(case) and not self.strict_jsonb:
                return 'limitation'  # PostgreSQL's jsonb reader on a corrupt container: the limitation
            return 'sanitizer'       # a report in PostgreSQL code on a non-jsonb case is unexpected
        if not stats_ok:
            return 'cache_accounting'
        # A crash whose only report is the macOS artifact (no real report) is the platform issue, not
        # a ztype crash; isolation should prevent it, but discount it here rather than fail on it.
        artifact_only = bool(artifacts) and not real
        decode_ok = next((o == 'ok' for n, o, m in outcomes if n == 'decode'), None)
        verdict = 'pass'
        for name, outcome, message in outcomes:
            expected = op_expected(case.expect, name)
            if outcome == 'crash':
                if expected == 'jsonb' and not self.strict_jsonb:
                    verdict = 'limitation' if verdict == 'pass' else verdict
                elif artifact_only:
                    verdict = 'artifact' if verdict == 'pass' else verdict
                else:
                    return 'crash'
            elif outcome == 'timeout':
                return 'timeout'
            elif expected == 'report':
                # ztype.validate is the decoder without the longjmp: it must always answer (an
                # error, a crash or a timeout from it is a defect), and its answer must agree with
                # the decode operation. On the native-jsonb class the base cast additionally walks
                # a container that validate deliberately never walks, so there the cast may fail
                # where validate is silent; validate reporting a value the cast decodes is always
                # a defect.
                if outcome != 'ok':
                    return 'validate_error'
                reported = message != ''
                if decode_ok is not None and reported == decode_ok and \
                        not (self.jsonb_case(case) and not decode_ok):
                    return 'validate_mismatch'
            elif outcome == 'ok' and expected == 'error':
                return 'silent_success'
        return verdict

    def minimize(self, case, outcomes):
        """For multi-flip cases whose first bad outcome was a crash or timeout, find one flip that
        reproduces it on the same operation, in a fresh backend each try. Bounded by the flip count."""
        bad = next(((n, o, op) for (n, o, m), op in zip(outcomes, self.last_ops) if o in ('crash', 'timeout')), None)
        if not bad or len(case.flips) < 2 or not case.rebuild:
            return None
        name, outcome, op = bad
        self.s_query('SELECT 1;')  # ensure the observer session is alive after any crash
        for flip in case.flips:
            raw = case.rebuild(self.s_query, [flip])
            s = self.cluster.session('mutation')
            try:
                got, _ = self.query_or_crash(s, "SET statement_timeout = '5s'; " + op.format(V=RAW_IN[case.kind].format(h=raw.hex())))
            finally:
                try:
                    s.close()
                except Exception:
                    pass
            self.cluster.wait_ready()
            for r in self.cluster.reports():
                r.rename(r.with_name('minimize-' + r.name))
            if got == outcome:
                return {'flip': flip, 'op': name, 'hex': raw.hex()}
        return None

    def summary(self):
        lines = ['', 'category      mutator        outcome        count']
        for (category, mutator, outcome), n in sorted(self.counts.items()):
            lines.append(f'{category:<13} {mutator:<14} {outcome:<14} {n}')
        if self.other_errors:
            lines.append('\nnon-ztype error messages (data errors from PostgreSQL itself):')
            for message, n in sorted(self.other_errors.items(), key=lambda kv: -kv[1])[:12]:
                lines.append(f'  {n:>4}  {message[:150]}')
        if self.artifacts:
            lines.append(f'\nmacOS ASan longjmp artifacts discounted (platform, not ztype): {self.artifacts}')
        return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    parser.add_argument('--seed', type=int, default=1)
    parser.add_argument('--iterations', type=int, default=400)
    parser.add_argument('--only', type=int, help='run one case index (replay)')
    parser.add_argument('--strict-jsonb', action='store_true', help='count native jsonb container crashes as failures')
    parser.add_argument('--isolate', dest='isolate', action='store_true', default=None,
                        help='one backend per operation (default: on under a sanitizer runtime, off otherwise)')
    parser.add_argument('--no-isolate', dest='isolate', action='store_false')
    parser.add_argument('--results', default=str(t.ROOT / 'results' / 'mutation'))
    args = parser.parse_args()
    isolate = bool(t.SANITIZER_RUNTIME) if args.isolate is None else args.isolate
    results = Path(args.results)
    started = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix='ztype-mutate-') as tmp:
        work = Path(tmp)
        cluster = Cluster(t.BIN, work, 'pg', 55441, share(work, 'share', t.library().with_suffix('')))
        cluster.start()
        try:
            if t.SANITIZER_RUNTIME:
                t.sanitizer_self_check(cluster)  # prove the runtime is live before trusting a clean run
            t.run([t.BIN / 'createdb', 'mutation'], env=cluster.env)
            s = cluster.session('mutation')
            s.query('CREATE EXTENSION ztype;')
            corpus = build_corpus(s, random.Random(f'{args.seed}:corpus'))
            s.query('CREATE TABLE copy_in(body ztext(6,1), meta zjsonb(6,2), blob zbytea);')
            dict_ids = sorted({i.dict_id for i in corpus if i.dict_id})
            s.close()
            runner = Runner(cluster, work, results, args.strict_jsonb, isolate)
            indexes = [args.only] if args.only is not None else range(args.iterations)
            for index in indexes:
                rng = random.Random(f'{args.seed}:{index}')
                roll = rng.random()
                if roll < 0.15:
                    case, sql = client_cases(rng, work, index)
                    record = runner.run_case(index, case, sql, None, rng)
                else:
                    item = rng.choice(corpus)
                    if roll < 0.30:
                        case = native_content(runner.s_query, rng, item, corpus)
                    else:
                        case = mutate(rng, item, corpus, dict_ids)
                    record = runner.run_case(index, case, None, ops_for(case, rng, work, item.level, item.slot), rng)
                if record['verdict'] != 'pass' or args.only is not None:
                    print(f"[{index}] {record['verdict']}: {record['category']}/{record['mutator']} {record['label']} -> "
                          + '; '.join(f'{n}={o}' + (f' ({m[:80]})' if m else '') for n, o, m in record['outcomes']), flush=True)
            cluster.check_reports()
            print(runner.summary())
            elapsed = time.perf_counter() - started
            for record in runner.failures + runner.limitations:
                results.mkdir(parents=True, exist_ok=True)
                (results / f'seed{args.seed}-case{record["index"]}-{record["verdict"]}.json').write_text(json.dumps(record, indent=1, default=str))
            if runner.limitations:
                print(f'\nnative jsonb container limitation: {len(runner.limitations)} case(s) crashed the backend on a corrupt '
                      f'container behind a valid frame (recorded under {results}; --strict-jsonb makes them failures):')
                for record in runner.limitations[:8]:
                    print(f"  [{record['index']}] {record['label'][:120]}")
            if runner.failures:
                print(f'\nFAIL: {len(runner.failures)} case(s); replay each with --seed {args.seed} --only <index>; records in {results}')
                for record in runner.failures:
                    print(f"  [{record['index']}] {record['verdict']}: {record['mutator']} {record['label'][:120]}")
                sys.exit(1)
            print(f'\nPASS: mutation seed {args.seed}, {len(list(indexes))} cases, {sum(runner.counts.values())} operations, '
                  f'{len(runner.limitations)} native jsonb limitation(s), {elapsed:.0f} s', flush=True)
        except BaseException:
            print(cluster.diagnostics()[-8000:])
            raise
        finally:
            cluster.stop('immediate')


if __name__ == '__main__':
    main()
