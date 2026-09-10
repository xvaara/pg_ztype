# pg_ztype

**zstd-compressed column types for PostgreSQL: `ztext`, `zjsonb`, `zbytea`.**

PostgreSQL compresses large values in TOAST with pglz or lz4, and that is the
whole menu: the compression method is not extensible, and values that fit in a
row, roughly anything under 2 kB, are never compressed at all. pg_ztype takes
a different route. It adds three types that compress their own bytes with
[zstd](https://facebook.github.io/zstd/), optionally with a trained dictionary,
and otherwise behave like `text`, `jsonb` and `bytea`.

```sql
CREATE EXTENSION ztype;

CREATE TABLE messages (
  body     ztext(6),        -- zstd level 6
  metadata zjsonb(6),
  payload  zbytea(6)
);

INSERT INTO messages VALUES ('hello', '{"source": "email"}', '\x0000ff');
SELECT body::text, metadata ->> 'source', payload::bytea FROM messages;
```

Selecting a column as it is prints the decoded value too, and `body = 'hello'`
compares against a literal without a cast: the cast is what gives a client a
`text`, `jsonb` or `bytea` result type instead of `ztext`, and what the base
type's functions and operators (`length`, `LIKE`, `jsonb_typeof`, `ORDER BY`)
need, since the compressed types carry none of them
(see [What is native, what needs a cast](#what-is-native-what-needs-a-cast)).

On a mix of small ERP-style JSON documents, `zjsonb` with a dictionary stores
**a quarter of what `jsonb` needs**. Measurements, and the cases where it is a
bad trade, are below.

## Status

Pre-release, extension version `0.9`. The extension is complete and tested
(see [Testing](#testing)), but the storage format and the type-modifier
encoding are versioned, not frozen: they may still change before a tagged
1.0, and there are no `ALTER EXTENSION ... UPDATE` scripts yet. If you store
data with a development build, keep the ability to dump it logically. Changes
are listed in [CHANGELOG.md](CHANGELOG.md); report security problems as
described in [SECURITY.md](SECURITY.md).

Requires PostgreSQL 18 or newer and libzstd 1.5 or newer. Tested on PostgreSQL
18.6 and 19beta3 with libzstd 1.5.7 on macOS (arm64); the CI workflow in
`.github/workflows/ci.yml` builds and tests on Linux against 18 and 19 and on
macOS against 18, including the staged-installation check (`make
test-install`), an undefined-behaviour sanitizer build, the functional suite
under AddressSanitizer on Linux, a smoke run of every benchmark and the
cross-major suite (`make test-cross`) from 18 to 19. The C code may build on older releases
but that is untested; the test harness relies on `extension_control_path`,
which is new in 18.

## Installation

```sh
git clone https://github.com/xvaara/pg_ztype
cd pg_ztype
make            # needs pg_config and pkg-config libzstd on PATH
make install    # into pg_config's PostgreSQL
```

`make install` puts the module and the SQL script where the server looks and
`ztype-sync`, the registry transport for replication and restores (see
[Distributing dictionaries](#distributing-dictionaries)), next to `psql`.

Then, in each database, as a superuser:

```sql
CREATE EXTENSION ztype;
```

Override `PG_CONFIG`, `ZSTD_CFLAGS` and `ZSTD_LIBS` on the `make` command line
when the defaults do not find your installation. The extension is not
relocatable: the types live in `public`, dictionary administration in the
`ztype` schema.

## The types

| Type | Logical value | Stored payload |
|---|---|---|
| `ztext` | `text` | the text bytes in the database encoding |
| `zjsonb` | `jsonb` | PostgreSQL's native binary jsonb, tagged with a pg_ztype payload-format version |
| `zbytea` | `bytea` | arbitrary bytes |

Each value is one zstd frame with a content checksum. Values under 64 bytes,
and values that do not shrink, are stored raw. All three types are declared
`STORAGE EXTERNAL`, so TOAST never wastes a second compression pass on them,
while still moving large values off-page.

Malformed `zjsonb` and `zbytea` text input is reported softly, exactly as
`jsonb` and `bytea` report it, so `pg_input_is_valid()` returns false instead of
raising and `COPY ... WITH (ON_ERROR ignore)` skips the bad row and loads the
rest with the column's policy. The binary receive functions raise as the base
types' do: PostgreSQL has no soft receive path, and binary `COPY` accepts no
`ON_ERROR` mode but `stop` (checked on 18.6 and 19beta3), so there is nothing
that could ask for one.

Assignment casts exist in both directions, so plain `INSERT` and `SELECT
col::text` work as expected, and the binary protocol (`COPY ... (FORMAT
binary)`, drivers that request binary results) carries the logical `text`,
`jsonb` or `bytea` value, never the compressed bytes. All codec functions,
casts and accessors are `PARALLEL SAFE`, so a query over these columns keeps
its parallel plan. Each type has `=` and `<>` and a default **hash** operator
class, comparing the decoded values with the base type's own equality
(bytewise for `ztext`, which is not collatable, and `zbytea`; jsonb's
structural equality for `zjsonb`), so `GROUP BY`, `DISTINCT`, `UNION`, `IN`,
`= ANY`, hash joins, hash partitioning and hash indexes work on the column
itself, and ANALYZE gathers distinct-value and most-common-value statistics
for it. There is no ordering operator and no btree operator class, by
decision: a sort on the compressed type would decode both sides of every
comparison, where a sort on the cast decodes each row once, so `ORDER BY
body::text` is the fast form and `ORDER BY body` an error rather than a
slow plan. The extension adds no implicit conversions that could change
operator resolution; everything else is indexed on the **logical** value:

```sql
CREATE INDEX messages_metadata ON messages USING gin ((metadata::jsonb));
```

The planner uses that index for `metadata::jsonb @> '{...}'`. What the
operator classes cost per row is measured under
[Equality, grouping and joins](#equality-grouping-and-joins).

### What is native, what needs a cast

- `zjsonb`: every jsonb operator that reads is native: `->` and `->>` by key
  and by index, `#>` and `#>>`, `?`, `?|`, `?&`, `@>` and `<@` (a `jsonb`
  literal may stand on either side), `@?` and `@@`.
  The key accessors are C functions; the rest are SQL functions the planner
  inlines into the cast, so `doc @> '{...}'` plans as `(doc::jsonb) @> '{...}'`
  and a GIN index on `((doc::jsonb))` is used from the operator form. The
  `jsonb_*` functions and the operators that build a new document (`||`, `-`,
  `#-`) still need `::jsonb`. Each of these decompresses once, but a backend
  keeps its last two decoded values, so five keys of one row selected
  separately decode the row once and copy it four times (see
  [The registry](#the-registry) for the cache).
- All three: `=` and `<>` on two values of the same type. Identical stored
  bytes compare equal and, for `ztext` and `zbytea`, different declared
  lengths compare unequal, both from the envelope alone; any other pair
  decodes both sides. Comparing a column against a value of the base type
  needs the cast on one side (`body = $1::ztext` or `body::text = $1`).
- `ztext`: `raw_length(body)` returns the uncompressed length in **bytes**,
  reading only the 16-byte envelope from TOAST. `prefix(body, n)` returns the
  first `n` **bytes** clipped to a whole character, decompressing only that
  much of the frame. It validates the envelope and frame header but, being
  partial, cannot verify the frame's content checksum; use a full cast when
  you want stored data validated.
- `zbytea`: `raw_length` as above.
- All three: `ztype.validate(value)` returns NULL when the stored value passes
  every check ztype makes, and otherwise the message the decoding cast would
  raise — except for a `ztext` whose payload is not valid in the database
  encoding, which it reports as `ztype: decoded text is not valid in encoding
  "UTF8"` where the cast lets PostgreSQL name the offending byte. It uses soft
  errors, so a bad row reports instead of aborting the statement and one query
  sweeps a whole table:

  ```sql
  SELECT id, v.problem
  FROM messages m, LATERAL ztype.validate(m.body) v(problem)
  WHERE v.problem IS NOT NULL;
  ```

  It decompresses each value, so it costs a full read of the column. What it
  checks, and what it deliberately does not, is under
  [Storage format](#storage-format).
- All three: `ztype.inspect(value)` reports what is stored, without decoding
  and without fetching the value: `kind`, `codec` (`raw` or `zstd`), `level`,
  `format`, `raw_length`, `stored_bytes`, and for dictionary frames `dict_id`,
  `dict_slot` and `dict_name` (the last two are NULL when the dictionary is
  not registered here). Everything it reports sits in the 12-byte envelope,
  the zstd frame header and the TOAST pointer, so on an out-of-line value it
  reads the first TOAST chunk only, like `raw_length`. It is how you check
  that a column really uses its dictionary:

  ```sql
  SELECT i.codec, i.level, i.dict_name, count(*)
  FROM messages m, ztype.inspect(m.body) i GROUP BY 1, 2, 3;
  ```

## What it saves, and what it costs

Measured with `make bench` on PostgreSQL 18.6 (Homebrew, libzstd 1.5.7,
pg_ztype 0.9) on an Apple M1 Pro laptop, zstd level 6, on generated ERP-style
documents: many keys, ISO timestamps, product codes, short notes and long free
text. The small shape is one order line; the large shape is a sales invoice
with 20–120 lines, a history log, notes and attachments. The random shape is
the worst case: small objects whose keys and values are random strings,
numbers and booleans, with no vocabulary shared between documents.
Dictionaries were trained on separately generated documents, never on the
measured rows. Sizes are `pg_total_relation_size`, on-disk table size
including TOAST, relative to `jsonb` with pglz; they exclude the shared
dictionary registry, which holds one 110 kB dictionary per shape here.

| documents | raw JSON text | `jsonb`, pglz | `jsonb`, lz4 | `zjsonb(6)` | `zjsonb(6)` + dictionary |
|---|---:|---:|---:|---:|---:|
| small: 200,000 × 0.6 kB | 123 MB | 146 MB (100%) | 146 MB (100%) | 109 MB (75%) | **36 MB (24%)** |
| large: 4,000 × 47 kB | 183 MB | 67 MB (100%) | 65 MB (97%) | 44 MB (66%) | **34 MB (51%)** |
| random: 1,000,000 × 0.3 kB | 300 MB | 348 MB (100%) | 348 MB (100%) | 331 MB (95%) | **273 MB (78%)** |

Time for the whole table: the median of three runs, with the min–max spread
in parentheses. Write is an `INSERT … SELECT` from a source table into a fresh
table each run; it includes serialising each source document to text and
parsing it back, identically for every variant. Read decompresses every
document for a containment check through `::jsonb`; key access uses the
native `->>`, and the three-key row filters on three `->>` of the same row,
OR-ed so nearly every row evaluates all three. Each operation ran five times after one unmeasured warm-up;
the cells are the median with the spread in parentheses. Reads are warm.
Parallel query and autovacuum were disabled so every variant runs the same
serial sequential scan with nothing else in the cluster, and the difference
is codec cost, not plan shape; the tables are unlogged so no WAL or
checkpoint lands inside a timing, and the cluster ran with `fsync` and
`synchronous_commit` off and 512 MB of `shared_buffers`.

| documents | operation | `jsonb`, pglz | `jsonb`, lz4 | `zjsonb(6)` | `zjsonb(6)` + dictionary |
|---|---|---:|---:|---:|---:|
| small | write 200,000 | 1.44 s (1.35–1.57) | 1.64 s (1.39–1.76) | 3.50 s (3.42–3.98) | 3.13 s (3.06–3.23) |
| small | read all | 0.04 s (0.04–0.04) | 0.04 s (0.04–0.04) | 0.58 s (0.57–0.62) | 0.21 s (0.21–0.21) |
| small | key access | 0.04 s (0.04–0.04) | 0.04 s (0.04–0.04) | 0.56 s (0.55–0.57) | 0.20 s (0.20–0.23) |
| small | three keys | 0.05 s (0.05–0.05) | 0.05 s (0.05–0.05) | 0.63 s (0.63–0.66) | 0.26 s (0.26–0.27) |
| large | write 4,000 | 3.88 s (3.83–4.24) | 2.27 s (2.13–2.32) | 3.60 s (3.52–4.08) | 3.81 s (3.71–3.90) |
| large | read all | 0.25 s (0.23–0.30) | 0.06 s (0.05–0.06) | 0.18 s (0.17–0.19) | 0.19 s (0.19–0.20) |
| large | key access | 0.21 s (0.21–0.22) | 0.06 s (0.06–0.06) | 0.18 s (0.17–0.18) | 0.19 s (0.19–0.20) |
| large | three keys | 0.50 s (0.50–0.59) | 0.14 s (0.13–0.15) | 0.20 s (0.19–0.24) | 0.22 s (0.22–0.25) |
| random | write 1,000,000 | 3.95 s (3.77–4.58) | 4.00 s (3.85–4.11) | 12.05 s (11.97–12.55) | 13.00 s (12.86–13.54) |
| random | read all | 0.12 s (0.12–0.13) | 0.12 s (0.12–0.12) | 1.76 s (1.74–1.86) | 1.44 s (1.43–1.55) |
| random | key access | 0.10 s (0.10–0.10) | 0.10 s (0.09–0.10) | 1.73 s (1.69–1.75) | 1.40 s (1.37–1.65) |
| random | three keys | 0.18 s (0.17–0.21) | 0.17 s (0.17–0.18) | 2.12 s (2.06–2.26) | 1.70 s (1.70–1.73) |

The same medians as microseconds per row, the figure to compare a codec
change against:

| documents | operation | `jsonb`, pglz | `jsonb`, lz4 | `zjsonb(6)` | `zjsonb(6)` + dictionary |
|---|---|---:|---:|---:|---:|
| small | write | 7.2 | 8.2 | 17.5 | 15.6 |
| small | read all | 0.2 | 0.2 | 2.9 | 1.0 |
| small | key access | 0.2 | 0.2 | 2.8 | 1.0 |
| small | three keys | 0.2 | 0.2 | 3.2 | 1.3 |
| large | write | 969 | 568 | 899 | 953 |
| large | read all | 62.2 | 14.6 | 44.0 | 48.4 |
| large | key access | 53.1 | 14.4 | 44.9 | 47.6 |
| large | three keys | 126 | 34.4 | 50.6 | 54.9 |
| random | write | 4.0 | 4.0 | 12.1 | 13.0 |
| random | read all | 0.1 | 0.1 | 1.8 | 1.4 |
| random | key access | 0.1 | 0.1 | 1.7 | 1.4 |
| random | three keys | 0.2 | 0.2 | 2.1 | 1.7 |

What decides the outcome:

- **Plain `jsonb` leaves these small documents uncompressed.** TOAST
  compression only runs when a row exceeds about 2 kB, a decision made per
  row, not per value; in this benchmark the 0.6 kB rows never reach it, so
  pglz and lz4 are identical: nothing was compressed (the benchmark asserts
  this through `pg_column_compression`), and nothing has to be decompressed
  on read either, which is why those reads are nearly free.
  `zjsonb` compresses every value of 64 bytes or more, and the dictionary is
  what makes small values compress well: the shared substrings (key names,
  timestamps, codes) live in the dictionary instead of in every value.
- **Small values pay per read.** Every access to a `zjsonb` value decompresses
  it, about 3 µs each here, and one row-at-a-time `->>` costs the same as a
  full read. Several references to the same row cost one decode: the backend
  keeps the last decoded value and serves repeats from it by comparing the
  stored bytes. The three-key row costs 3.2 µs per small row with that cache
  and 6.8 µs without it (measured by building with the cache disabled), and
  51 µs against 98 µs on the large documents. Scanning 200,000 small documents therefore takes tenths of a
  second instead of hundredths. The cost is zstd's per-frame setup, mostly
  decoding the Huffman and FSE tables every small frame carries, not the
  decompression context: creating and freeing one costs 25 ns, and the
  backend keeps one across calls anyway. The dictionary variant reads twice
  as fast because its frames reuse the entropy tables the dictionary carries
  and move a third of the bytes. Index the logical value so filters do not
  decompress every row.
- **Large documents already compress in TOAST**, so `zjsonb` gains a third
  over pglz through zstd itself and another quarter with a dictionary, and it
  reads faster than pglz while lz4 remains the fastest reader.
- **Writes cost about twice pglz on small documents** at level 6, and about
  the same on large ones, where pglz itself is slow. Of the 17 µs per small
  row, about 7 µs is the text round trip every variant pays and 6.7 µs is
  zstd's level-6 encode of a 0.6 kB document (`make bench-codec`); creating
  the compression context is 0.1 µs of that, since zstd sizes it to the
  pledged input, so a cached compressor would save 1% and is not done. These writes come from
  typed expressions and compress once; a literal or a parameter into a
  column with a non-default modifier compresses twice (see
  [When the modifier applies](#when-the-modifier-applies)).
- **Random content is the floor.** With nothing shared between documents,
  zstd alone saves 5%, and reads still pay the per-value decompression, about
  1.5–1.7 µs each. The dictionary still takes off a fifth, not by matching text
  but through the entropy tables it carries: base64 strings and jsonb's
  structure code below eight bits per byte even inside a 300-byte value,
  which a value on its own is too small to learn. If your JSON looks like
  this, `zjsonb` is a poor trade; keep plain `jsonb`.

Sizes and timings depend on the document mix and the machine; rerun
`tests/bench_zjsonb.py` on your own data before deciding. The small-value
read cost is inside zstd's frame decoding and is already near its floor for
a dictionary-free 0.6 kB frame; a dictionary is the lever that moves it.

### Request latency

The tables above run whole tables in one statement. An application mostly
runs one short statement per transaction over a pooled connection, so each
statement pays its round trip, and until this release each transaction that
touched a dictionary column also loaded the dictionary from the registry
again (one lookup plus building the zstd decompression object and, for a
write, the compression object). Measured with `make bench-latency` (libpq,
prepared statement over a Unix socket, 10,000 statements of one 645-byte ERP
document each, median of five runs after a warm-up, µs per statement, same
machine as above), first as one statement per transaction on a persistent
connection, then the same statements inside one transaction:

| one statement per transaction | `jsonb` | `zjsonb(6)` | `zjsonb(6, 'erp')`, 110 kB | `zjsonb(6, 'erp8')`, 8 kB |
|---|---:|---:|---:|---:|
| insert, typed `$1::jsonb` | 20.0 | 34.5 | 41.5 | 34.2 |
| point read, whole value | 16.1 | 22.0 | 18.3 | 19.3 |
| point read, one key `->>` | 14.1 | 18.0 | 15.4 | 15.8 |

| one transaction, cache warm | `jsonb` | `zjsonb(6)` | `zjsonb(6, 'erp')`, 110 kB | `zjsonb(6, 'erp8')`, 8 kB |
|---|---:|---:|---:|---:|
| insert, typed `$1::jsonb` | 18.4 | 31.9 | 32.7 | 34.0 |
| point read, whole value | 16.0 | 19.4 | 19.5 | 18.0 |
| point read, one key `->>` | 12.5 | 17.8 | 17.5 | 14.7 |

The dictionary cache is per backend and lives for the session (see
[Dictionaries](#dictionaries)), so the two shapes cost the same and the
probe's backend reports zero dictionary loads per statement in both. With
the transaction-local cache this release replaces, the same run gave 324.6 µs
per autocommit insert and 132.1 µs per point read on the 110 kB dictionary
column (73.6 and 44.5 µs with the 8 kB dictionary), against the same warm
figures: about 115 µs of reload per read and 295 µs per write, scaling with
the dictionary's size, and one load per statement. A row with four
dictionary columns cost 1,489 µs to insert and 594 µs to read as its own
transaction; now 71.6 and 34.1 µs, the same as four `zjsonb(6)` columns.
Saving the registry lookup's plan (also new) accounts for about 10 µs of the
difference; the rest was building the zstd objects.

What the cache cannot help is a connection per statement: with a new backend
for every statement, an insert costs about 1.3 ms into `jsonb` and 2.6 ms
into the dictionary column, and a point read 1.4 against 2.8 ms, dominated
by backend start-up and the first load of the dictionary in each of them. Use
a connection pool, as with any PostgreSQL workload; a pooled backend keeps
its dictionaries.

### Equality, grouping and joins

`=` and the hash operator classes compare decoded values, so a `GROUP BY`,
a hash join or an equality filter on a compressed column decodes every row
at least once, where the base type compares bytes it already has.
`tests/bench_hash.py` (`make bench-hash`) measures that on the small ERP
documents of the size benchmark, 200,000 rows holding 10,000 distinct values
each repeated 20 times in interleaved order, stored as `jsonb`, `zjsonb(6)`
and `zjsonb(6)` with the dictionary, and as text in the same three forms.
Per row: `GROUP BY` on the column and, for the compressed types, on the cast
to the base type (the only way before the operator classes), a hash join
against a table with one row per distinct value, an equality filter against
one constant, and `ANALYZE`, which gathers statistics through the equality
operator. Sorting is disabled so every variant takes the hash path;
microseconds per row, median of five runs after a warm-up, same machine as
the tables above (`results/bench/20260910T114754Z-full`).

| jsonb column | size | GROUP BY column | GROUP BY cast | hash join | `= constant` | ANALYZE |
|---|---|---|---|---|---|---|
| `jsonb` | 145.7 MB | 1.8 | — | 3.3 | 0.2 | 0.13 s |
| `zjsonb(6)` | 108.8 MB | 3.7 | 4.8 | 4.0 | 2.9 | 0.21 s |
| `zjsonb(6, 'erp')` | 35.4 MB | 2.0 | 2.8 | 1.9 | 1.1 | 0.14 s |

| text column | size | GROUP BY column | GROUP BY cast | hash join | `= constant` | ANALYZE |
|---|---|---|---|---|---|---|
| `text` | 134.6 MB | 0.5 | — | 1.6 | 0.1 | 0.04 s |
| `ztext(6)` | 97.4 MB | 3.0 | 2.9 | 3.2 | 0.2 | 0.12 s |
| `ztext(6, 'erp-text')` | 32.9 MB | 1.4 | 1.4 | 1.6 | 0.2 | 0.07 s |

What the numbers say. Hashing costs one decode per row, the same as the cast
(the benchmark counts the backend's decodes: 1.00 per row in `GROUP BY`,
1.05 in the join); the equality check that follows a hash match is free
within one column, because two equal values stored under the same policy
are byte-identical and the envelope decides. The dictionary makes the
compressed forms as fast to group as the base type is, since the decode of
a small frame with a dictionary is what dominates. An equality filter
against a constant decodes each `zjsonb` row once (the constant stays in the
backend's two-entry decode cache; with one entry it was decoded again on
every row, 6.0 µs instead of 2.9) and, for `ztext` and `zbytea`, almost
never: the envelope's declared length settles most pairs. `ANALYZE` costs
what it costs for the base type plus one decode per sample row, because
each type's analyzer decodes the sample once and runs the base type's
statistics over it; without that, PostgreSQL's analyzer for a type with
equality but no ordering counts distinct values pairwise through the
equality function, which took 31 s on 20,000 `zjsonb` rows. That pairwise
path, and the fact that a sort on the compressed type would decode two
values per comparison where a sort on the cast decodes each row once, is
why there is no btree operator class: `ORDER BY body::text` is the form to
write.

### Working memory

zstd's contexts, dictionary objects and training buffers are allocated outside
PostgreSQL's memory contexts: `work_mem` does not bound them and
`pg_backend_memory_contexts` does not show them. What one call costs, from
libzstd's own accounting (`make bench-memory`, `tests/codec_memory.c`,
libzstd 1.5.7), first the compression context per level and value size, freed
when the call ends. `zjsonb` and `ztext` pledge the exact input size, so zstd
sizes the window to the smaller of the value and the level's window, which is
why the columns stop growing at 1 MB for the low levels and keep growing at
level 22, whose window is 128 MB:

| level | 1 kB value | 64 kB | 1 MB | 16 MB | 128 MB |
|---:|---:|---:|---:|---:|---:|
| 1 | 40 kB | 489 kB | 1.3 MB | 1.3 MB | 1.3 MB |
| 3 | 44 kB | 841 kB | 2.5 MB | 3.5 MB | 3.5 MB |
| 6 | 44 kB | 1.1 MB | 4.2 MB | 5.2 MB | 5.2 MB |
| 9 | 48 kB | 1.1 MB | 12 MB | 15 MB | 15 MB |
| 19 | 199 kB | 1.9 MB | 19 MB | 90 MB | 90 MB |
| 22 | 199 kB | 1.9 MB | 19 MB | 274 MB | 834 MB |

With a 110 kB dictionary referenced the context is the same up to level 9;
at level 19 a 1 MB value takes 35 MB instead of 19 and at level 22 a 16 MB
value 402 MB instead of 274. The dictionary objects themselves are the part
ztype keeps and counts against `ztype.dictionary_cache_size` (the table under
[Building a good dictionary](#building-a-good-dictionary) is their sum):

| dictionary | decompression object | compression object, level 1 | level 3 | level 6 | level 9 | level 19 | level 22 |
|---|---:|---:|---:|---:|---:|---:|---:|
| 8 kB | 35 kB | 151 kB | 215 kB | 151 kB | 215 kB | 279 kB | 279 kB |
| 110 kB | 137 kB | 157 kB | 509 kB | 765 kB | 765 kB | 1.6 MB | 1.6 MB |
| 1 MB | 1.0 MB | 1.1 MB | 1.8 MB | 3.5 MB | 11 MB | 33 MB | 33 MB |

Decompression is cheap by construction. A full decode writes the frame
straight into the result datum, which serves as zstd's window, so the
decompression context is 94 kB whatever the frame; the backend keeps one and
reuses it. `prefix` is the exception: it streams into a bounded buffer, so
zstd allocates the frame's window itself, the size the *writing* level
chose: 990 kB for a level-1 frame of 1 MB or more, 2.5 MB for level 6, 4.5 MB
for level 9, 8.5 MB for level 19, and for level 22 the smaller of the value and
128 MB.

Those are bytes reserved. `make bench-memory` also checks them against a
backend: each statement in a fresh backend, its peak resident size sampled
every 5 ms, compared as the growth beyond the same statement at level 1,
which touches the same table pages and datums. On 16 MB values the resident
growth matched the reserved context within 10% (level 19: 83 MB against 88;
level 22: 264 MB against 272). On a 128 MB value the backend touched less than
it reserved (level 22: 501 MB resident against 834 reserved; level 19: 30 MB
against 88), because zstd's match tables are only touched as the input fills
them, so the tables above are the ceiling and a real value may stay under it.
A full decode of a 128 MB frame grew the backend by 274 MB, the frame's pages
plus the 128 MB result, with a 0.1 MB context; `prefix` on the same frame by
146 MB, the frame's pages alone, since the reserved window is not touched for
a 100-byte read. Training is the other native allocation: the backend grew by
65 MB to train a 110 kB dictionary from 12 MB of samples and by 154 MB for a
1 MB dictionary from the 64 MB sample maximum, so budget about two to five
times the sample bytes for a training session.

So the lever on working memory is the level in the column modifier, and the
cost is per call, not per session: a `ztext(6)` column never needs more than
about 5 MB to write a value of any size, `ztext(9)` 15 MB, `ztext(19)` 90 MB
plus the dictionary objects; `ztext(22)` reserves up to 834 MB for one large
value, which is a deliberate choice to make for a column. There is no
administrator limit on this memory beyond the level, and 1.0 will ship
without one: the one GUC, `ztype.dictionary_cache_size`, bounds the cached
objects only. A cap (a maximum level per database, or a budget per call)
would be a second setting whose only effect is to refuse a modifier someone
was allowed to declare, so the decision stays where the modifier is: whoever
may declare `ztext(22)` on a column grants that column up to the table's
level-22 figure per write, and the table above is the ceiling to grant it
by. Levels up to 9 stay under 15 MB whatever the value.

## Compression policy

The type modifier is `(level [, dictionary])`: levels 1–22; the dictionary is
a slot number 0–33554431 or the **name** of a registered dictionary, as a quoted
string or a bare identifier. Slot zero means no dictionary; no modifier means
`(6,0)` for values written as literals, parameters, `COPY` or casts from the
base type — but a column declared without a modifier stores a value moved from
another `ztype` column unchanged, so write `ztext(6)` when `(6,0)` must hold for
every row (see [When the modifier applies](#when-the-modifier-applies)). There
is no session-level compression setting; the only setting,
`ztype.dictionary_cache_size`, is a memory budget (see Dictionaries).

```sql
CREATE TABLE messages (body ztext(6, 'mail-2024'));   -- same as ztext(6,1) if mail-2024 is slot 1
ALTER TABLE messages ALTER COLUMN body TYPE ztext(9, 'mail-2026');
```

A name is looked up once, when the statement is parsed, and only the slot is
stored: `\d` and `pg_dump` show `ztext(6,1)`, never the name. The dictionary
must therefore already be registered when the DDL runs. In a migration, call
`ztype.train_and_add` or `ztype.add_dictionary` **before** the `CREATE TABLE`
or `ALTER COLUMN`; the same transaction is fine. An unregistered name is an
error, not a silent fallback. Names are unique and never all digits, so a
digit-only modifier is always a slot number. `ztype.dictionary_slot(name)`
returns the slot for use in expressions.

### When the modifier applies

**A column's modifier applies to every value stored in it**, however the
value got there: string literals, parameters (typed or inferred, text or
binary protocol), casts from `text`, `jsonb` or `bytea`, `COPY` in either
format, values selected from other columns, CTEs, `CASE` and other
expressions, typed and domain defaults, array elements. Every value carries
the level it was written with and a compressed frame names its dictionary,
so applying a modifier to a value that already has it is a no-op, and
otherwise the value is recompressed. The query's shape never changes the
outcome; `ztype.inspect` shows it.

**A column declared without a modifier is the exception.** Bare `ztext`,
`zjsonb` and `zbytea` carry no modifier at all, and PostgreSQL applies no
coercion towards a target that has none. Logical input still arrives at
`(6,0)`, because the input function and the base-type casts default to it, but a
value selected from another `ztype` column is stored exactly as it was written,
keeping that column's level and dictionary — so a bare column can hold rows that
depend on a dictionary:

```sql
CREATE TABLE plain (body ztext);
INSERT INTO plain SELECT body FROM messages;      -- messages.body is ztext(12,2)
SELECT level, dict_slot FROM plain p, ztype.inspect(p.body);   -- 12, 2
ALTER TABLE plain ALTER COLUMN body TYPE ztext(6);             -- rewrites to 6, no dictionary
```

Declare the column `ztext(6)` when `(6,0)` must hold for every row; that
modifier is applied like any other, including to rows already stored when it is
set with `ALTER TABLE ... ALTER COLUMN`.

Two consequences:

- `ALTER TABLE ... ALTER COLUMN body TYPE ztext(12,2)` with a **different**
  modifier rewrites the table, like any type change, and every row ends up
  with the new level and dictionary. The same modifier is a no-op. When the
  rewrite cannot be taken in one go, `ztype.set_column_policy` changes the
  modifier without one and the rows are caught up in batches; see
  [Changing the policy without a rewrite](#changing-the-policy-without-a-rewrite).
  Measured costs of the rewrite, by strategy, are under
  [What a rewrite costs](#what-a-rewrite-costs).
- PostgreSQL reads an unknown-type literal or an untyped parameter with the
  plain type first and applies the modifier in a second step, so `INSERT ...
  VALUES ($1)` into a column with a non-default modifier compresses the value
  twice, once at `(6,0)` and once at the column's policy, with a decode in
  between. Typing the parameter or literal as the base type, `$1::jsonb`,
  `$1::text`, `'...'::jsonb`, routes it through the base-type cast, which
  receives the column modifier and compresses once; so do `COPY` and values
  selected from other columns. Measured with `make bench-params` (libpq,
  prepared statement, 20,000 documents of 0.6 kB into a fresh unlogged table,
  µs per row):

  | server cost per row, pipelined | `jsonb` | `zjsonb(6)` | `zjsonb(6, 'erp')` |
  |---|---:|---:|---:|
  | untyped `$1` | 7.3 | 18.1 | 30.6 |
  | typed `$1::jsonb` | 7.5 | 18.1 | 16.5 |

  | one round trip per row, Unix socket | `jsonb` | `zjsonb(6)` | `zjsonb(6, 'erp')` |
  |---|---:|---:|---:|
  | untyped `$1` | 16.1 | 31.1 | 43.3 |
  | typed `$1::jsonb` | 17.1 | 28.9 | 28.0 |

  The default column needs no coercion for an untyped parameter, so the two
  shapes cost the same there. Into the dictionary column the second
  compression nearly doubles the server's work per row, and a typed
  parameter into the dictionary column is cheaper than any write into the
  plain `zjsonb(6)` column, because compressing a small document with a
  dictionary is faster. Applications that insert through parameters into a
  modified column should type the parameter; the server cannot do it for
  them, because the input function is called before the target is known.

For every write the dictionary is a preference: if the slot is not visible
yet, the value is stored without a dictionary at the column's level and the
backend warns, once per slot per transaction (see
[Backup, restore and upgrades](#backup-restore-and-upgrades)). Values too
short to compress (under 64 bytes) go through the same lookup and carry the
same level, so a bad slot warns on every path.

`ztype.recompress(value, level, slot)`, overloaded for all three types and
defaulting to `(6,0)`, produces a value with an explicit policy, for trials
(`pg_column_size(ztype.recompress(body, 6, 3))`) and for columns declared
without a modifier. Unlike ordinary writes it is strict: a missing dictionary
is an error. Assigning its result to a column with a modifier applies the
**column's** policy, like any other value. A domain can name a reusable
policy, but PostgreSQL has no `ALTER DOMAIN ... TYPE`, so changing it later is
more work than altering the columns directly.

### Changing the policy without a rewrite

`ALTER COLUMN TYPE` to a new modifier holds an `ACCESS EXCLUSIVE` lock for the
whole rewrite. When that is not affordable, change the modifier alone and
catch the rows up at your own pace:

```sql
SELECT ztype.set_column_policy('messages', 'body', 12, 'mail-2026');   -- catalog only, no rewrite
-- new writes now use (12, 'mail-2026'); stored rows keep what they had

-- once per batch, each in its own transaction, until it updates nothing
UPDATE messages SET body = body::ztext(12,2)
 WHERE id >= :lo AND id < :hi AND NOT ztype.matches_policy(body, 12, 2);

SELECT ztype.finish_column_policy('messages', 'body');   -- scans, then clears the pending mark
```

`set_column_policy(table, column, level, dictionary)` stores in
`pg_attribute` exactly what `ALTER COLUMN TYPE` would store, under the same
`ACCESS EXCLUSIVE` lock but for a catalog update instead of a rewrite. The
dictionary is a slot number or a registered name, as in a modifier, or
omitted for none. Until the column is finished its modifier reads
`ztext(12,2,pending)` in `\d`, `pg_dump` and `ztype.column_policies`
(`pending` is true): new writes get `(12,2)`, and stored rows may still carry
the policy they were written with. The rows are caught up with ordinary
`UPDATE`s; `ztype.matches_policy(value, level, slot)` is the predicate, the
same test as the [resumable catch-up](#resumable-catch-up) uses, and it reads
the envelope and frame header only. `finish_column_policy` takes the
`ACCESS EXCLUSIVE` lock, scans every row through that predicate, refuses with
the count while any is off the policy, and otherwise clears the mark, so a
finished column carries its modifier's promise like any other. Both functions
require ownership of the table and run with the caller's rights; a
partitioned or inheritance parent changes every partition or child with it,
and a partition on its own is refused.

Why the mark exists: PostgreSQL applies the same-type coercion only when a
value's modifier differs from the target's. A column whose modifier claimed
`(12,2)` while it still held `(6,1)` rows would hand those rows unchanged to
any other `(12,2)` column, and that column would silently stop being uniform.
A pending modifier equals no plain one, so every move out of a pending column
is coerced; a move into another pending column of the same modifier is not,
and that column's own `finish` is what sees the rows. Everything else keeps
working during the interval: the column's expression indexes, `CHECK`
constraints and trigger conditions are rewritten in place so their references
match the column (a stale one would put an expression index out of use; a
trigger on the column is something `ALTER COLUMN TYPE` refuses outright), a
default is coerced to the new modifier on every insert, and prepared
statements replan. A view or rule on the column, or extended statistics, block
the change as they block `ALTER TABLE`; drop and recreate them around it.

The other way out of the pending state is `ALTER COLUMN TYPE` to a plain
modifier, which rewrites and clears the mark with it. A logical dump carries
the mark and writes logical values, so the restore compresses every row under
the column's policy, dictionary permitting: as for any restored column, rows
loaded before the registry arrived are dictionary-free ([Recovery](#recovery)),
which `finish` reports and the same catch-up fixes.
`CREATE TABLE ... (LIKE ...)` copies the mark with the modifier. The state is
also declarable (`ztext(12,2,pending)`), which is what makes dumps and
`pg_upgrade` carry it; it means exactly "writes follow the policy, stored
rows may not, moves out are coerced".

What it costs: the catch-up is the batched `UPDATE` measured under
[What a rewrite costs](#what-a-rewrite-costs), about four times the WAL of
the rewrite and a table at old plus new size until `VACUUM FULL` or
`pg_repack`; `set_column_policy` and `finish_column_policy` add a catalog
update and one header-only scan.

## Dictionaries

A zstd dictionary is a ~100 kB bundle of substrings that occur across many
values, plus pre-tuned entropy tables. Every value is compressed as if that
bundle preceded it, so it helps exactly where per-value compression cannot:
the boilerplate a *small* value shares with its neighbours (greetings,
signatures, quoted headers, JSON key names). Large values already find their
own repetition and gain less. Decompression speed does not depend on the
dictionary, and a dictionary never needs to be present to *read* a value that
was written without one.

### The registry

Dictionaries are append-only rows in `ztype.dictionaries`, registered as
extension configuration data so `pg_dump` includes them. Only trained
dictionaries with a valid nonzero zstd dictionary ID are accepted; every frame
records the ID it needs, so a missing dictionary is a loud error, never
silent corruption.

```sql
-- As the extension administrator:
SELECT ztype.train_and_add(
  'mail-2024',
  'SELECT body::text FROM messages
    WHERE created_at >= ''2024-01-01'' AND octet_length(body::text) < 8192
    ORDER BY md5(id::text) LIMIT 20000');
-- Returns the slot, e.g. 1. Refer to it by name so the number is never assumed.
CREATE TABLE archive (body ztext(6, 'mail-2024'));
```

- `ztype.train_dictionary(query [, dict_bytes [, sample_bytes]])` trains and
  returns the bytes without registering them, for trials.
- `ztype.add_dictionary(name, bytea [, trained_from])` registers a dictionary,
  including one trained elsewhere with `zstd --train`.
- `ztype.import_dictionary(slot, name, bytea [, trained_from])` registers one
  under a *given* slot: the recovery and subscriber-seeding path, where the
  slot must be the one the source's columns name. Identical row already
  there: no-op, returns the slot. Anything else already there under that
  slot, name or dictionary ID: `42710` naming it, registry untouched. See
  [Exporting and importing the registry](#exporting-and-importing-the-registry).
- `ztype.train_and_add(name, query [, dict_bytes])` does both.
- `ztype.dictionary_slot(name)` resolves a name; `ztype.dict_id(bytea)`
  returns a dictionary's zstd ID.
- `ztype.dictionary_inventory` is a view readable by every role: slot, name,
  zstd ID, size in bytes and registration time, but never the bytes or the
  training query.
- `ztype.column_policies` is a view readable by every role: every stored
  `ztext`, `zjsonb` or `zbytea` column (tables, partitions and materialized
  views) with its decoded modifier — `level`, `slot`, `has_modifier` — and the
  `dict_name` and `dict_id` the slot resolves to here. A bare column shows the
  `(6, 0)` it applies to logical input, with `has_modifier` false. A non-zero
  slot with a NULL `dict_name` is a column asking for a dictionary this
  database does not have; [Recovery](#recovery) says how that happens.
  `pending` is true for a column re-pointed by `ztype.set_column_policy` and
  not yet finished.
- `ztype.build_info()` reports what the loaded library is: its `library`
  version (compare with `pg_extension.extversion` after a package upgrade),
  the storage `magic` and `jsonb_format` it writes (the magic names the
  committed fixture under `tests/fixtures/`), and libzstd `zstd_compiled`
  against `zstd_runtime`. `ztype.zstd_version()` is the runtime alone.

Slots are 1–33554431, allocated under a lock and never reused. `UPDATE`,
`DELETE` and `TRUNCATE` on the registry are blocked, also under
`session_replication_role = replica`. An administrator can still bypass that
by changing the schema; doing so can make stored values unreadable.

The slot field is not the practical ceiling, and it is not meant to invite one
dictionary per tenant. A frame names its dictionary by zstd's 32-bit
dictionary ID, which the registry requires to be unique, and that ID is a hash
of the dictionary bytes: by the birthday bound two independently trained
dictionaries collide with probability about 1 % at 10,000 of them and 39 % at
65,535. On one node a collision is a retry — retrain and the bytes, and so the
ID, differ — but two nodes that each already store rows under a colliding ID
cannot be merged without rewriting a table. Add to that the registry itself
(a dictionary is up to 1 MB, replicated, and `user_catalog_table = true` means
a logical slot's `catalog_xmin` holds back vacuum on it) and the per-backend
dictionary cache, which is a budget of a few entries: a workload spreading
reads over many dictionaries pays the cold-load cost in
[Request latency](#request-latency) on every statement. Plan for order 10⁴
dictionaries across a fleet, one per corpus rather than one per customer.

Ordinary roles read and write compressed columns without any access to the
registry: the codec entry points run as the extension owner with a fixed
`search_path` and execute only a fixed lookup. Registration
(`ztype.add_dictionary`, `ztype.import_dictionary`) runs as the extension
owner too, so that a role allowed to register never needs a privilege on the
registry table; training keeps caller privileges, so a training query cannot
read what its caller cannot. None of them is executable by `PUBLIC`;
delegating them to another role is described below. Each backend caches the dictionaries it
has used, for the rest of its session: the first statement of a session that
touches a dictionary column loads the dictionary from the registry, later
transactions find it in memory (see [Request latency](#request-latency) for
what that saves). Until the loading transaction commits an entry is
provisional: a rollback, a savepoint rollback or `PREPARE TRANSACTION` drops
what that transaction loaded, so a registration that did not commit is never
used by a later write. A newly committed dictionary is used by the next
statement under `READ COMMITTED`, whether or not anything was written in
between; under `REPEATABLE READ` it becomes visible with the next
transaction, like any other row. `ztype.reload_dictionaries()` clears the
backend's cache by hand; normal operation does not need it.

**Delegating registration to a non-superuser.** `EXECUTE` on the functions is
the whole grant:

```sql
GRANT EXECUTE ON FUNCTION ztype.train_dictionary(text, integer, integer),
  ztype.add_dictionary(text, bytea, text),
  ztype.train_and_add(text, text, integer) TO migrator;
```

Each of the three is load-bearing (`ztype.train_and_add` runs with the
caller's privileges and calls the other two as the caller), and nothing else
is needed: no privilege on `ztype.dictionaries`, and none on `ztype.dict_id`,
because the registry's `CHECK` constraint is evaluated inside
`ztype.add_dictionary`, which runs as the extension owner. The role can
register, see the result in `ztype.dictionary_inventory`, and use the
dictionary by name in a column definition. It cannot read the dictionary
bytes, count the registry's rows, or write to the table directly; every such
attempt fails with `42501`. The training query keeps running with that role's
own privileges, so it cannot train on a table the role cannot read.

What a `SECURITY DEFINER` registration hands the role is exactly the
registration: with `EXECUTE` it can fill slots, and slots are permanent and
never reused, so grant it to a role you would let append to the registry.
The function bodies are fixed SQL over the registry with the arguments as
data, no dynamic SQL, and every argument is checked by the table's
constraints (a `bytea` that is not a zstd dictionary is refused with `22023`).
The bytes the role passes in never come back out through any registration
function.

The cache is bounded. `ztype.dictionary_cache_size` (default 64 MB, settable
per session or with `SET LOCAL`) counts, per cached dictionary, its bytes plus
the zstd decompression object and the one compression object; when a load
would exceed the budget the least recently used dictionaries are dropped and
reloaded on their next use. The dictionary in use is never dropped, so a
budget smaller than one entry costs reloads, never an error.
`ztype.dictionary_cache_stats()` reports the backend's live entries and bytes,
the budget, and lifetime load and eviction counts; it is backend-local and
therefore not parallel safe.
Outside that budget each backend keeps one zstd decompression context, about
100 kB, reused across reads and dropped whenever a large value grows it past
1 MB, and two decoded values: the last two compressed values it decoded, up
to 256 kB raw each, together with their stored bytes, so that a second
reference to the same row (`doc ->> 'a'` next to `doc ->> 'b'`, a filter and
a projection on one column) and the constant side of `doc = $1` on every row
are a byte comparison and a copy instead of a decode.
`ztype.decode_cache_stats()` reports its hits, misses and bytes.

**Training data is not private merely because it became a dictionary.**
Fragments of the training set can be reconstructed from the dictionary bytes,
and compressed sizes reveal whether a value matches it. Dictionary bytes and
training queries are readable only by the extension owner and superusers, a
delegated registrar included, but pick a non-sensitive training corpus anyway. The registry is database-wide; it is not a per-tenant
permission system.

### Training input

The training query runs through a read-only cursor and must return exactly one
`text`, `bytea` or `jsonb` column. Each sample is the value's own stored
bytes, so **train `zjsonb` dictionaries on a `jsonb` column**, not on its text
rendering: the type compresses jsonb's binary form, and a dictionary learned
from text would barely match it. Limits: 1 KiB–1 MiB output dictionary,
1–65,536 bytes per sample (default 8,192), 20,000 non-empty samples, 64 MiB of
sample data, and at least eight non-empty samples. Cancellation is checked
between batches and around zstd calls.

### Building a good dictionary

Train on what the column will hold, sampled from history, and score on data
the training never saw. Measured with `tests/dict_report.py` on a private
helpdesk mail archive (22,000 messages, 1.1 GB of bodies from 2005 to 2026,
half HTML; anonymized, not public): a 110 kB dictionary trained on the
messages before 2020 and scored on the 8,977 after it cut level-6 storage by
16% overall (19% at level 12), by about 30% for messages between 256 bytes
and 4 kB and by 20% for those between 4 and 16 kB, while the messages above
64 kB, 60% of the bytes, gained 6%: the dictionary is for the small values.

1. **One dictionary per kind of content.** Mail bodies, JSON metadata and
   binary blobs each want their own. Mixing them dilutes the shared
   substrings. For JSON, train on the `jsonb` column itself, and keep one
   dictionary per document shape.
2. **Thousands of samples, spread across the corpus.** zstd wants roughly
   100× the dictionary size in sample bytes, so a 110 kB dictionary wants
   about 10 MB of samples. Pick them across time and authors, not the first
   20,000 rows of one era: a wide `ORDER BY created_at`, or a deterministic
   pseudo-random order such as `ORDER BY md5(id::text)`. A few hundred
   samples produce a dictionary that mostly memorises them.
3. **Keep the default 8 kB sample cut.** It keeps the dictionary focused on
   the beginning of values, where shared boilerplate lives and where small
   values, the ones that benefit, end anyway. A larger cut spends the sample
   budget on the unique middle of long values.
4. **Exclude what should not be learned.** Empty values, giant outliers,
   base64 attachments, and anything sensitive.
5. **Measure on held-out rows before registering.** Compare
   `pg_column_size(ztype.recompress(body, 6, slot))` against slot 0 on rows
   that were not in the training query. Scoring on the training rows
   overstates the gain. Trial in a scratch database with
   `ztype.train_dictionary`, and register only the winner: registrations are
   permanent.
6. **Retrain occasionally, not constantly.** Dictionaries decay gently: on
   the same archive, scored against the 4,301 messages from 2024 onwards, a
   dictionary trained on messages before 2011 still saved 10% at level 6,
   against 16% for one trained on everything before 2024. When the corpus has
   drifted, register a new slot and
   switch the column with `ALTER COLUMN ... TYPE ztext(6, 'mail-2026')`; that
   rewrites the column with the new dictionary, so schedule it like any
   rewrite. Old dictionaries stay registered, so old backups stay readable.

The default dictionary size (110 kB) is a sensible fixed point. Bigger buys
little for text and costs memory per backend: each cached dictionary holds
a decompression object plus one compression object for the level most recently
used with it (writing the same dictionary at alternating levels rebuilds that
object each time). What one cached dictionary costs, as counted against
`ztype.dictionary_cache_size` (libzstd 1.5.7):

| dictionary | read only | reading and writing at level 6 | at level 19 |
|---|---:|---:|---:|
| 8 kB | 43 kB | 193 kB | 321 kB |
| 110 kB | 247 kB | 1.0 MB | 1.9 MB |
| 820 kB | 1.6 MB | 4.9 MB | 18.4 MB |

The compression object dominates and grows with the level, so a transaction
that writes many dictionaries at a high level is what the budget is for.

### Evaluating a dictionary on your data

`tests/dict_report.py` answers the adoption question with numbers from your
own column before anything is registered. It fetches one column through a
read-only query, copies the sample into a disposable cluster built from this
tree, splits it into training and held-out rows, trains a dictionary on the
training rows only and reports the held-out rows: per level with and without
the dictionary, stored bytes and compression and decompression cost per
value; per value-size bucket, where the savings actually land; and the
dictionary's own cost, including how many values pay back its bytes.

```sh
make dict-report DICT_REPORT_ARGS="--source 'dbname=helpdesk' --query 'SELECT body FROM messages ORDER BY created_at' --split tail"
make dict-report                       # the benchmark's small synthetic corpus
python3 tests/dict_report.py --help    # levels, sizes, sample limits
```

`--split tail` holds out the end of the query's order, which is how a
dictionary ages when the query is ordered by time; the default interleaves
rows. Training rows are fed in a deterministic pseudo-random order across the
training part, because training stops at 20,000 samples or 64 MB and a
time-ordered query would otherwise train on its oldest rows only
(`--train-order source` keeps the query's order). Nothing is written to the
source database. On the benchmark's small
documents (20,000 rows, 5,000 held out, 0.7 kB each):

| level | stored, no dictionary | stored, dictionary | compress µs/value (plain → dict) | decompress µs/value (plain → dict) |
|---:|---:|---:|---:|---:|
| 1 | 2,540 kB (74%) | 793 kB (23%) | 6.4 → 1.9 | 2.5 → 1.0 |
| 3 | 2,555 kB (74%) | 744 kB (22%) | 6.8 → 2.3 | 2.6 → 1.0 |
| 6 | 2,516 kB (73%) | 745 kB (22%) | 10.2 → 9.7 | 3.2 → 1.0 |
| 9 | 2,516 kB (73%) | 722 kB (21%) | 13.2 → 14.6 | 2.8 → 1.0 |
| 19 | 2,490 kB (72%) | 658 kB (19%) | 60.1 → 233.8 | 2.9 → 0.9 |

At level 6 the dictionary saved 363 bytes per value, paying back its 110 kB
after 311 values, and read three times faster. Training it grew the backend
by 35 MB of resident memory at its peak, for 12 MB of samples. Two things the table shows
that the size ratio alone would not: the levels barely matter once there is
a dictionary (level 1 with one beats level 19 without), and at high levels a
dictionary makes writes much slower, four times at level 19, because the
match finder now searches the dictionary for every value. For small,
uniform documents the answer is a dictionary at a low level, not a high
level without one.

## Backup, restore and upgrades

A full `pg_dump` includes the registry rows, but **restore order is not
guaranteed**: pg_dump puts them in the data section with no ordering against
user tables, a serial restore loads `public` tables first, a parallel restore
loads everything concurrently, and a typed default such as
`DEFAULT 'x'::ztext(6,1)` runs during `CREATE TABLE`, before any data exists.
Nothing in PostgreSQL can move those rows earlier, so the write path tolerates
their absence instead:

- A **logical write** (type input, or a cast from `text`, `jsonb` or `bytea`)
  whose slot is not visible is stored without a dictionary at the column's
  level, and the backend raises one `WARNING` per slot per transaction. The
  value is exact and readable everywhere; only its size differs.
- **Decoding** a stored frame whose dictionary is missing is still an error.
- **`ztype.recompress`** and name lookups (`ztext(6,'name')`,
  `ztype.dictionary_slot`) are still strict, so a migration that names a
  dictionary the database does not have fails in the migration, not later.

Restores therefore need no settings:

```sh
pg_dump -Fc -f app.dump source_database
pg_restore --exit-on-error -j 4 -d fresh_database app.dump
psql -X -v ON_ERROR_STOP=1 -d fresh_database -f app.sql   # plain dumps work the same way
```

Rows restored before their dictionary row arrived stay dictionary-free until
rewritten (a typed default is evaluated at each `INSERT`, so it picks the
dictionary up as soon as it exists). Catch up when convenient; a plain
self-assignment is a no-op, so go through the logical type, which re-applies
the column policy row by row:

```sql
UPDATE messages SET body = body::text;
```

Outside a restore, the warning means a column points at a slot this database
does not have, which costs storage, not correctness. A missing slot is looked
up once per statement snapshot, not per row. Partial dumps that omit the `ztype` schema
lose the registry: the restored rows are dictionary-free themselves, later
writes fall back with warnings, and any frame that reached the database by
another route cannot be read until its dictionary is registered again. Moving
dictionaries between databases, and catching a large table up in batches, are
in **Recovery** below.

### Recovery

**A dump never carries a compressed frame.** The output function and `send`
both decode, so every dump path — plain, custom, binary `COPY` — writes
logical values, and the restore compresses each one again under the *target*
column's policy. Three consequences follow:

- a slot the target does not have degrades to dictionary-free with a
  `WARNING`, which is what makes an ordinary restore work with no settings;
- a *different* dictionary occupying that slot in the target is applied
  **silently**: no error, no warning, correct values, different bytes;
- the source's dictionary is never needed to restore, only to dump.

Stored frames survive only a **physical** restore — a base backup,
`pg_upgrade`, a physical replica. Those are the values that can fail to
decode, because a frame names its dictionary by zstd dictionary ID, never by
slot: reading it needs those exact bytes registered somewhere in this
database, under any slot.

#### Exporting and importing the registry

`ztype.dictionaries` is an ordinary table, and `COPY` moves it with slot,
`dict_id` and name preserved — which is what makes imported frames readable
again:

```sh
psql -d source -c "\copy ztype.dictionaries TO 'dicts.copy'"
psql -d target -c "\copy ztype.dictionaries FROM 'dicts.copy'"
```

`\copy` runs as the client; the server-side `COPY ... TO '/path'` form needs
superuser or `pg_write_server_files`, and reading a file back needs
`pg_read_server_files`. Reading the registry means reading dictionary bytes,
so the exporting role is the extension owner or a superuser.

So is the importing role, unless it is given both halves of the write.
`INSERT` alone fails with `42501`, `permission denied for function dict_id`,
and imports nothing: the registry carries `CHECK (dict_id =
ztype.dict_id(dict))`, a `CHECK` expression is evaluated as the inserting
role, and `EXECUTE` on `ztype.dict_id` is revoked from `PUBLIC` like the rest
of the admin surface.

```sql
GRANT INSERT ON ztype.dictionaries TO importer;
GRANT EXECUTE ON FUNCTION ztype.dict_id(bytea) TO importer;  -- the CHECK constraint calls it
```

`USAGE` on schema `ztype` is already `PUBLIC`, so that is the whole grant.

One dictionary at a time, `ztype.import_dictionary(slot, name, bytes)` does
the same insert with the slot preserved, and answers where `COPY` leaves a
constraint name: an identical row already present is a no-op that returns
the slot, so a re-run of an import script is free, and any other overlap is
refused with `42710` and a message naming what the target already holds
under that slot, name or dictionary ID. The bytes can come from any source,
including a registry exported into a staging table:

```sql
CREATE TEMP TABLE incoming (slot integer, dict_id bigint, name text, dict bytea,
                            trained_from text, created_at timestamptz);
\copy incoming FROM 'dicts.copy'
SELECT ztype.import_dictionary(slot, name, dict, trained_from) FROM incoming ORDER BY slot;
```

It runs as the extension owner, and its collision check reads
`ztype.dictionary_inventory`, not the bytes, so an importing role needs
`EXECUTE` on the function and nothing on the registry: no `INSERT`, no
`SELECT`, and no `EXECUTE` on `ztype.dict_id`; the `COPY` grants above are for
the `COPY` path only. Two concurrent
imports of one slot are serialized by the primary key; the loser re-checks
and gets the same answer as above under `READ COMMITTED`, and the
constraint's own error under `REPEATABLE READ`.

`pg_dump -t ztype.dictionaries --data-only` emits the rows too — the table is
registered with `pg_extension_config_dump`, so selecting it by name dumps its
data — which is a way to put the dictionaries in a file of their own next to a
table-only backup.

An import fills unused slots and rewrites nothing. Every collision is the
constraint that owns the column, and the transaction rolls back with the
registry untouched:

| what the target already has | constraint |
|---|---|
| the same dictionary bytes under another name | `dictionaries_dict_id_key` |
| a different dictionary under that name | `dictionaries_name_key` |
| that slot | `dictionaries_pkey` |

The answer to all three is the same: **keep the existing row**. Registry rows
are append-only (the `ENABLE ALWAYS` trigger refuses `UPDATE`, `DELETE` and
`TRUNCATE`) because stored frames reference dictionaries by ID; editing a row
would orphan them. Register the incoming dictionary under a free slot with
`ztype.add_dictionary`, which takes `max(slot) + 1` — gaps left by a partial
import are fine, and slots are never reused — and then point columns at it,
by slot number, by its new name, or by recompressing the rows.

To compare two registries without handing anyone the bytes:

```sql
SELECT slot, name, dict_id, dict_bytes FROM ztype.dictionary_inventory ORDER BY slot;
```

Matching `dict_id` values mean the same dictionary, whatever slot or name it
sits under on either side.

#### Table-only backups

`pg_dump -t messages` carries neither the extension nor the registry. The
target needs `CREATE EXTENSION ztype` before the restore; the modifier comes
back as numbers (`ztext(12,2)`), so no dictionary name has to exist. The
restore then writes dictionary-free rows and warns, or — if the slot is
occupied by something else — silently uses that. Either way, list the columns
whose modifier names a slot this database does not have:

```sql
SELECT schema_name, table_name, column_name, level, slot
  FROM ztype.column_policies
 WHERE slot > 0 AND dict_name IS NULL
 ORDER BY 1, 2, 3;
```

The view decodes `pg_attribute.atttypmod` (`& 31` is the level, `>> 5 &
33554431` the slot, bit 30 the pending mark; `-1` means `(6,0)`) and joins the
slot to the inventory. Drop the `WHERE` clause to see every column of the
three types and the dictionary name each slot currently resolves to.

#### The slot-mismatch pitfall

Slots are local registry numbers, so the same slot can mean different
dictionaries in two databases. That query is the only warning you get, and it
goes quiet in the dangerous case — the slot exists, it is just the wrong
dictionary:

- **Restored rows** are correct but compressed against the target's
  dictionary, so the ratio silently changes.
  `(ztype.inspect(body)).dict_name` tells you which one was used.
- **Physically restored frames** fail with
  `ztype: dictionary ID ... is not available` until the exported bytes are
  registered here — under a *free* slot, since the original one is taken.
  Reading works from that moment on, and `dict_slot` reports the new slot: the
  frame found its dictionary by ID.
- **New writes** keep using the column's slot, which is still the local
  dictionary. Re-point the column to fix that, which rewrites the table like
  any other modifier change, or without the rewrite through
  `ztype.set_column_policy` and a batched catch-up
  ([Changing the policy without a rewrite](#changing-the-policy-without-a-rewrite)):

```sql
ALTER TABLE messages ALTER COLUMN body TYPE ztext(12,'mail-2024-imported');
```

#### Resumable catch-up

The whole-table `UPDATE messages SET body = body::text` above is one
transaction and one lock. For a large table, walk the primary key in batches
and commit each one, with a predicate that skips rows already on the column's
policy — so an interrupted run resumes by simply running again, and a run
with nothing to do rewrites nothing:

```sql
-- once per batch, each in its own transaction
UPDATE messages SET body = body::text
 WHERE id >= :lo AND id < :hi AND NOT ztype.matches_policy(body, 12, 2);
```

`12` and `2` are the column's level and slot; `ztype.dictionary_slot('name')`
resolves the slot if you prefer to name it. `ztype.matches_policy` reads the
envelope and the frame header, not the frame, so the predicate costs no zstd
decompression and, on an out-of-line value, only the first TOAST chunk; a row
it selects is then decompressed and compressed again. It is true when the
level matches and the value is either raw or a frame naming the slot's
dictionary (none for slot 0); an unregistered slot is an error.

**Letting raw values pass is what makes the rerun free.** A value the codec
keeps raw — shorter than 64 bytes, or one zstd cannot shrink — has no frame
and therefore no dictionary ID, so no rewrite ever gives it one. A predicate
that demanded the dictionary of every row would select every such row on
every run, and a table with short or incompressible values would be rewritten
in full each time, at full cost and with nothing to show for it. The same
test, spelled out on `ztype.inspect`, is `level <> 12 OR (codec = 'zstd' AND
dict_slot IS DISTINCT FROM 2)`.

The price of the restriction is that a raw row already at the column's level
is left alone even when a dictionary registered since could now shrink it.
Those rows are the job of an explicit pass — `ztype.recompress(body, 12, 2)`
decodes and re-encodes unconditionally, and so does the plain whole-table
`body::text` assignment, whose coercion retries every raw row of compressible
size.

#### What a rewrite costs

Per row it touches, a rewrite writes a new tuple version and its WAL (a
full-row record, plus the whole page the first time a page is touched after
a checkpoint), rewrites any TOAST chunks, and adds an index entry to every
index unless the update is heap-only (HOT): the new version fits on the same
page and no indexed column changed. Measured with `make bench-rewrite`
(PostgreSQL 18.6, libzstd 1.5.7, Apple M1 Pro): the same table moved from
`zjsonb(6)` to `zjsonb(6, 'erp')` four ways, each from a fresh copy, median
of three; logged tables, `full_page_writes` on and a `CHECKPOINT` before each
run, autovacuum off, `fsync` and `synchronous_commit` off, 512 MB of
`shared_buffers`. The `UPDATE` rows start from the restore shape above: the
column already declares the dictionary, the rows do not have it. "+ index"
adds an expression index on `(doc ->> 'sku')`, the "index the logical value"
advice from [Limitations](#limitations). Sizes are heap plus TOAST; "after"
is unchanged by a plain `VACUUM`.

**200,000 documents × 645 bytes**, 112 MB as `zjsonb(6)` (312 MB as `jsonb`),
39 MB once the dictionary applies:

| strategy | indexes | time | WAL | heap+TOAST after | indexes after | HOT updates |
|---|---|---:|---:|---:|---:|---:|
| `ALTER COLUMN TYPE` | primary key | 2.7 s | 48 MB | 39 MB | 4.3 MB | — |
| `ALTER COLUMN TYPE` | + index | 2.9 s | 49 MB | 39 MB | 5.8 MB | — |
| whole-table `UPDATE` | primary key | 3.6 s | 184 MB | 148 MB | 8.5 MB | 355 of 200,000 |
| whole-table `UPDATE` | + index | 4.3 s | 201 MB | 148 MB | 11 MB | 0 |
| batched `UPDATE`, 20 transactions | primary key | 4.3 s | 184 MB | 148 MB | 8.5 MB | 520 of 200,000 |
| batched `UPDATE`, 20 transactions | + index | 4.9 s | 201 MB | 148 MB | 11 MB | 0 |
| `CREATE TABLE AS`, indexes, rename | primary key | 2.7 s | 48 MB | 39 MB | 4.3 MB | — |
| `CREATE TABLE AS`, indexes, rename | + index | 3.0 s | 49 MB | 39 MB | 5.8 MB | — |

**4,000 documents × 48 kB**, 44 MB as `zjsonb(6)`, 36 MB with the dictionary,
every value out of line in TOAST:

| strategy | indexes | time | WAL | heap+TOAST after | HOT updates |
|---|---|---:|---:|---:|---:|
| `ALTER COLUMN TYPE` | either | 2.1–2.3 s | 34 MB | 36 MB | — |
| whole-table `UPDATE` | either | 2.2–2.4 s | 77 MB | 78 MB | 0 |
| batched `UPDATE`, 20 transactions | either | 2.3–2.4 s | 77–78 MB | 78 MB | 502 of 4,000 / 0 |
| `CREATE TABLE AS`, indexes, rename | either | 2.1–2.3 s | 34 MB | 36 MB | — |

What the numbers say:

- **`ALTER COLUMN TYPE` and `CREATE TABLE AS` cost the same, and the least:**
  the WAL is the new table plus its indexes, and the heap ends at the new
  size. Both need the table to themselves for the whole run: `ALTER` takes
  `ACCESS EXCLUSIVE`, and a `CREATE TABLE AS` loses any write made to the old
  table while it runs. `ALTER` also keeps the table's identity: indexes,
  constraints, triggers, grants, views and foreign keys stay, which the copy
  has to recreate by hand.
- **An `UPDATE` writes about four times the WAL and leaves the table at old
  plus new size.** Every row is a delete and an insert: two tuple versions,
  two sets of TOAST chunks, two index entries; and the space of the old
  versions is inside the file, so `VACUUM` makes it reusable but the table
  stays at 148 MB where the rewrite produced 39 MB, until `VACUUM FULL`,
  `pg_repack` or `CLUSTER`, each of which is itself a rewrite. The indexes
  double as well.
- **HOT hardly helps here.** With the primary key alone, only 355 of 200,000
  updates were heap-only, because a freshly loaded table has full pages and
  the new version rarely fits next to the old one; an index on the column's
  value, even an expression index, makes every update non-HOT by rule and
  adds another 10% of WAL for the index entries. A `fillfactor` below 100
  would raise the HOT share, at the price of a larger table always.
- **Batching does not reduce the total.** Twenty transactions wrote the same
  WAL within 1% and took 15% longer than one. What the batches buy is the
  length of each lock and each transaction, so readers and replication keep
  up; the bytes are the same. Size the batch to your replication lag and
  autovacuum budget, not to any saving.
- **Time is codec time.** Every strategy decodes and re-encodes every value
  once, so all eight variants land within 2.7–4.9 s for 112 MB of small
  documents and within 0.3 s of each other on the large ones; with `fsync`
  on and a real disk, the WAL difference is where the strategies would
  separate further.

So: when you can take the lock, `ALTER COLUMN TYPE` is the cheapest by every
measure and leaves nothing to clean up. When you cannot, change the modifier
with `ztype.set_column_policy`, batch the `UPDATE` and budget four times the
WAL and a table twice its final size until the next `VACUUM FULL` or
`pg_repack` ([Changing the policy without a rewrite](#changing-the-policy-without-a-rewrite));
or, if writes can be paused or redirected, build the new table with
`CREATE TABLE AS` and swap.

### Replication

**Logical replication follows the dump rule**: no compressed frame ever
reaches the wire. The publisher's output function (or `send`, with
`binary = true`) decodes, and the subscriber's input function (or `receive`)
compresses again under **the subscriber column's own modifier** — the apply
worker and the initial copy both pass that column's modifier to it. So the
subscriber decides the policy, a registry it lacks degrades to
dictionary-free with a `WARNING` rather than failing, and a slot that means a
*different* dictionary there is applied silently.

On the publisher, logical decoding calls that output function inside a
historic snapshot, which sees only rows written by transactions marked as
catalog-changing. `ztype.dictionaries` is therefore declared
`WITH (user_catalog_table = true)`; without it a walsender cannot resolve a
dictionary that was registered inside the decoded WAL window and the
subscription stalls on `dictionary ID ... is not available`. Nothing to
configure — the cost is that a logical replication slot's `catalog_xmin` also
holds back vacuum on that eight-row table.

The procedure:

1. **Give the subscriber the dictionaries, one way only.** Either carry the
   registry over with `ztype-sync` (below), which imports identical rows with
   the same slots through `ztype.import_dictionary` and is the same as the
   manual export and import of
   [Exporting and importing the registry](#exporting-and-importing-the-registry),
   *or* put `ztype.dictionaries` in the publication. Never both: the initial
   copy would collide on `dictionaries_pkey`.
2. **Number the slots, or wait for the registry, before name-based DDL.**
   `ztext(6,'first')` fails with `dictionary "first" is not registered` until
   the row exists on the subscriber, so a subscriber that receives its
   registry through the publication must declare its columns as
   `ztext(6,1)` — or create the tables after the registry has arrived.
3. **Expect dictionary-free rows from the initial copy** when the registry
   travels in the publication: the registry and the user tables sync
   independently, so rows applied first are exact but dictionary-free, and the
   subscriber log carries one `WARNING` per slot per transaction. Bring them
   to the column policy afterwards, with the same catch-up as a restore:
   `UPDATE messages SET body = body::text, meta = meta::jsonb;` (batched, as in
   [Resumable catch-up](#resumable-catch-up)).
4. **Compare the two registries before subscribing**, and the columns after:
   `ztype-sync --check` does both, and is clean when every replicated column
   stores as the publisher's does. If a slot on the
   subscriber already holds a *different* dictionary, then with the registry
   in the publication the publisher's next registration collides on the
   primary key: the apply worker errors, `apply_error_count` in
   `pg_stat_subscription_stats` grows, the subscription stalls and the row
   never lands. Creating the subscription
   `WITH (disable_on_error = true)` turns that retry loop into one failure and
   a disabled subscription, which is easier to notice and to read in the log.
   With the registry *not* published, the same mismatch is
   **silent** — values stay correct, but they are stored under the
   subscriber's dictionary, and only `ztype.inspect` shows it.
   `ztype.dictionary_inventory` compares both registries without exposing
   bytes; `ztype.policy_differences` compares the columns themselves:

   ```sh
   psql -h publisher -XAtc "SELECT jsonb_agg(p) FROM ztype.column_policies p" > policies.json
   psql -h subscriber -v pub="$(cat policies.json)" \
        -c "SELECT * FROM ztype.policy_differences(:'pub')"
   ```

   It joins the publisher's `column_policies` rows, carried over as one
   `jsonb` value, with the subscriber's own view on schema, table and column
   name, and returns every column whose level or dictionary differs, the
   dictionary compared by ID and never by slot or name (a subscriber slot
   holding the publisher's bytes under another number is not a difference):
   `different dictionary`, `no dictionary here`, `slot not registered here`,
   `dictionary here, none on the publisher`, `different level`. An empty
   result means every replicated column stores as the publisher's does.
   Replication itself never reports any of this, which is why the sweep
   exists; it reads only the public view and its argument, so any role can
   run it.

#### Distributing dictionaries

`ztype-sync` is the transport for step 1 and the check for step 4, in one
command. `make install` puts it next to `psql`; it is a stdlib-only Python
script that drives `psql`, so it runs wherever `psql` does.

```sh
ztype-sync --source 'service=publisher' --target 'service=subscriber'
```

It reads both registries' public inventory (slot, name, zstd dictionary ID),
treats every source dictionary whose ID is registered on the target under any
slot as present, fetches the bytes of the others from the source once, and
registers them on the target through `ztype.import_dictionary`, all of them in
one transaction. A collision, the slot or the name taken by a different
dictionary, is SQLSTATE `42710` from the server: the transaction rolls back,
the target registry is untouched, and the run exits 1. The same transaction
then runs `ztype.policy_differences` with the source's `column_policies` and
prints every column whose level or dictionary differs. Modes and exit codes:

| run | writes | exit 0 | exit 1 | exit 2 | exit 3 |
|---|---|---|---|---|---|
| default | imports, in one transaction | in sync, or imported and no difference | collision, nothing written | policy differences remain (imports committed) | usage, connection, privilege or `psql` failure |
| `--dry-run` | the same, rolled back | what the run would give | | | |
| `--check` | nothing | in sync | | missing or different dictionaries, or policy differences | |

Privileges are the ones already documented, nothing more: the *source* role
must read the dictionary bytes (the extension owner or a superuser); the
*target* role needs `EXECUTE` on `ztype.import_dictionary` and nothing on the
registry. The bytes travel hex-encoded in the SQL text of the target
connection and appear nowhere else, not in arguments, files or the tool's
output; what the tool cannot prevent is a target with `log_statement = 'all'`,
which logs the statement text. Keep passwords in a service file or `.pgpass`:
the two conninfos are command-line arguments, visible to other users of the
machine.

Run it after every registration on the publisher and before name-based DDL
on the subscriber, from a deploy hook or a timer; a second run imports nothing
and rewrites nothing. It never removes or overwrites a row, and it never picks
a slot: a taken slot is reported, and the fix is the operator's (see
[When apply stops: a registry collision](#when-apply-stops-a-registry-collision)).

#### Slot ranges

`ztype.add_dictionary` (and so `train_and_add`) takes `max(slot) + 1`. That is
the right rule while one node registers and every other node only receives:
the receivers' slots are the publisher's, and a receiver that must register
something of its own (a local-only column, or the recovery below) does it
through `ztype.import_dictionary` with an explicit slot far above the
publisher's range, `60001` say, so the publisher never reaches it. Two
nodes that both register are a different matter: once node A has imported
node B's slot `60001`, A's next `add_dictionary` allocates `60002`, exactly
what B allocates next, and the two collide the moment the registries are
exchanged. So in a two-way setup both nodes register with explicit slots
from their own range, `ztype.import_dictionary(slot, name,
ztype.train_dictionary(...))`, and never through `add_dictionary`;
`make test-replication` pins both the convention and the pitfall.

#### Cascading and two-way

A subscriber can publish what it applied, registry included, to a third node.
The middle node needs `wal_level = logical`; its registry rows arrived by
apply, and logical decoding there resolves them under a historic snapshot the
same way the publisher does. The registry hops by publishing
`ztype.dictionaries` on every hop, or by running `ztype-sync` per hop with the
middle node as `--source`: a subscriber is as good a source as a publisher,
so a third node can be seeded from it and create its tables by name before
subscribing. Both shapes are what `make test-replication` runs, with a
dictionary registered on the publisher after the chain was built arriving
two hops away.

Two-way replication, each node subscribing to the other
`WITH (origin = none)` on disjoint key ranges, works the same way for the
values: each row lands at the local column's policy in both directions and
nothing loops. What it needs from ztype is the slot-range convention above,
explicit slots on both nodes, and `ztype-sync` run in *both* directions after
either side registers; `--check` in both directions is the proof that the
two registries agree.

#### When apply stops: a registry collision

With the registry in the publication, a publisher registration whose slot is
already taken on the subscriber fails apply on `dictionaries_pkey`; with
`disable_on_error = true` the subscription disables itself after one
attempt, and `pg_stat_subscription_stats.apply_error_count` counts it.
PostgreSQL has no apply-side conflict resolver, the registry is append-only,
and a rewrite of the column is a decision with a cost (see
[What a rewrite costs](#what-a-rewrite-costs)), so nothing here is automatic
and `ztype-sync` offers no `--resolve`. The runbook, which
`make test-replication` runs end to end:

1. **Keep the local row.** Frames already stored under it depend on it.
2. **Import the publisher's dictionaries under slots the publisher will never
   reach**, `60001` upwards, with the manual export and import of
   [Exporting and importing the registry](#exporting-and-importing-the-registry)
   and `ztype.import_dictionary(60000 + slot, name, dict, trained_from)`.
   From here `ztype-sync --check` reports them as present under the other
   slot, and reports the columns that still store under the local dictionary.
3. **Skip the failed remote transaction.** The apply error's `CONTEXT` line
   ends with `finished at <LSN>`; run
   `ALTER SUBSCRIPTION name SKIP (lsn = '<LSN>')` (the supported form since
   PostgreSQL 15; `pg_replication_origin_advance` is the fallback for older
   servers, with the subscription disabled), then `ALTER SUBSCRIPTION name
   ENABLE`. `pg_subscription.subskiplsn` returns to `0/0` once the skip is
   consumed, and the publisher's next registration lands.
4. **Local registrations continue above the publisher's range** on their own:
   `add_dictionary` allocates `max(slot) + 1`, which is now `60003`.
5. **Re-point the columns that should store as the publisher's do**, if any:
   `ALTER TABLE messages ALTER COLUMN body TYPE ztext(6, 60001)` rewrites the
   table under the imported slot, after which `ztype-sync --check` is clean.

#### Failover and promotion

The registry travels physically, so every node that was ever a standby of
the publisher has the dictionaries the moment it is primary. Three shapes
are qualified by `make test-replication-ha`, on four disposable clusters:

- **Promotion of a physical standby** (`pg_promote()`): the registry it
  replayed is complete, a write into a dictionary column compresses under it
  at once with no `WARNING`, `train_and_add` allocates the next slot, and a
  new subscriber seeded from the promoted node with `ztype-sync` applies at
  the policy. Nothing ztype-specific is needed.
- **A logical subscription following the failover** (PostgreSQL 17 and
  later): the subscription is created `WITH (failover = true)`, the standby
  synchronises the slot (`sync_replication_slots = on`,
  `hot_standby_feedback = on`, a `primary_slot_name`, and a
  `primary_conninfo` that carries `dbname=`, which
  `pg_basebackup -R -d 'host=... dbname=...'` writes), and the publisher names
  the standby's physical slot in `synchronized_standby_slots` so no change
  is acknowledged before the standby has it. After the promotion,
  `ALTER SUBSCRIPTION name CONNECTION '...'` points the subscriber at the
  new primary; it resumes on the synchronised slot, the dictionary registered
  after the promotion arrives through the published registry, and a table
  created on the new primary that names it applies at the policy. The old
  primary's copy of the slot goes idle and can be dropped. One trap, hit and
  measured here: the base backup copies the primary's
  `synchronized_standby_slots`, and a promoted node that keeps it waits for
  a standby slot it does not have, so every failover-slot walsender blocks
  and the repointed subscription never receives a row (the log says the slot
  "specified in parameter synchronized_standby_slots does not exist").
  Set it to `''` on the standby's own configuration before promoting.
- **`pg_createsubscriber`**: a base backup converted into a logical
  subscriber has the registry and every frame byte for byte, needs no
  `ztype-sync` and no catch-up, and its `FOR ALL TABLES` publication carries
  `ztype.dictionaries`, so a dictionary registered on the publisher
  afterwards arrives under the same slot and later rows apply at the policy
  with no `WARNING`.

DDL is not replicated: tables, dictionaries and any `ALTER PUBLICATION` still
need running on the subscriber yourself, and a published table must exist on
the subscriber before any row for it is decoded.

**Physical replication carries the stored bytes**, so both sides need
matching builds, and the registry reaches a standby with everything else. On a
hot standby every read path decodes dictionary frames normally — the registry
lookup goes through SPI on a read-only server — a dictionary registered on the
primary after the standby was taken is used as soon as it is replayed, the
dictionary cache is backend-local as on a primary, and writes (including
`ztype.train_and_add`) fail with SQLSTATE `25006` without breaking the
session.

`make test-replication` and `make test-replication-ha` build all of that
from disposable clusters; what they assert is listed under
[Testing](#testing).

### Major-version upgrades

Stored-value compatibility is decided by pg_ztype's own format identifiers,
not by the PostgreSQL major. `zjsonb` stores PostgreSQL's native binary JSON
tagged with a pg_ztype payload-format version (currently 1); any build that
supports that version reads the value on any PostgreSQL major, and an unknown
version is rejected rather than decoded. The extension **binary** is compiled
against one major's headers and must be built and installed for each major,
under the same module name.

Both upgrade paths are exercised between PostgreSQL 18.6 and 19beta3 by
`make test-cross`: values written by either build decode under the other, a
logical dump from 18 restores into 19 with no settings, and `pg_upgrade` from
18 to 19 keeps dictionaries, dictionary-compressed values, typed and domain
defaults and an expression index working. Physical replication uses the
stored bytes and needs matching builds on both sides; logical replication
carries logical values and has its own procedure, both under
[Replication](#replication).

## Storage format

The envelope is 16 bytes including PostgreSQL's 4-byte varlena header:
magic/version, uncompressed byte length, payload-format version (1 for
`zjsonb`, zero for `ztext` and `zbytea`), compression level, logical kind and
codec, all native-endian like jsonb itself. There is no envelope checksum. A
compressed payload is exactly one zstd frame with a decoded-content checksum,
which is what detects payload corruption; the frame also names its dictionary
by zstd dictionary ID. Readers validate the magic/version, level, allocation
bounds, frame size, declared content size, exact decoded length and, for raw
payloads, exact stored length. Input functions accept logical data only;
there is no way to hand the server compressed bytes from a client.

The zstd checksum detects accidental corruption in compressed payloads; raw
payloads (values under 64 bytes, or ones that did not shrink) are covered only
by the length checks, and native jsonb is a trusted PostgreSQL representation,
not an independently validated format for hostile binary input. None of it is
authentication. No on-disk architecture portability is promised.

`ztype.validate(value)` applies exactly that contract to one stored value and
reports instead of raising. It checks the envelope (magic/version, the logical
kind against the column's declared type, level, payload format, allocation
bounds), the exact stored length of a raw payload, and for a compressed one
that the payload is exactly one zstd frame carrying a content checksum, that
its dictionary resolves in the registry, and that a full decode yields exactly
the declared length with a matching checksum; `ztext` must additionally decode
to valid text in the database encoding, which is the one failure it words
itself (`ztype: decoded text is not valid in encoding …`) rather than leaving
to PostgreSQL as the cast does. It never walks a jsonb container: a
`zjsonb` payload is only checked to be large enough to hold one, so a value
whose frame is intact but whose bytes are not a well-formed container
validates clean. That is the same base-type limitation described under
[Testing](#testing).

Compression runs in 256 kB input steps with an interrupt check between
them. Decompression writes the frame straight into the result datum, which
serves as zstd's window, so no separate output buffer is allocated or copied
through; the input is fed in steps sized to yield about 1 MB of output each
at the frame's ratio, with an interrupt check between them. The suite times
this with the input materialised beforehand: a cancelled level-22 compression
of 8 MB returns in about 0.15 s where the full frame takes several seconds,
and a cancelled 128 MB decompression in about 0.01 s against 0.22 s. zstd's working
memory is allocated outside PostgreSQL's memory contexts and is not counted
against `work_mem`; level 22 with a 1 MB dictionary is the most expensive
combination a modifier can request, and [Working memory](#working-memory)
gives the numbers per level, value size and dictionary size. Cached
dictionaries are the one part of that memory ztype accounts for and caps,
through `ztype.dictionary_cache_size`; the per-call compression and
decompression contexts are freed with the call.

## Limitations

- No ordering: no `<`, no btree operator class, so `ORDER BY`, `min`/`max`,
  `DISTINCT` inside an aggregate and a merge join need the cast
  (`ORDER BY body::text`). Equality and hashing are native; GIN and every
  other index method need the cast expression.
- Codec working memory is bounded only by the level in the column modifier;
  no setting caps the level a column may declare (see
  [Working memory](#working-memory) for the per-level ceiling).
- The `jsonb_*` functions and the document-building operators (`||`, `-`,
  `#-`) need `::jsonb` on `zjsonb`; the reading operators are native and cost
  the same one decompression.
- `zjsonb` inherits jsonb normalisation: keys are reordered, duplicates
  dropped. Use `ztext` where the exact original bytes matter.
- Compression is per value. Redundancy *between* rows is captured only by a
  dictionary, never by cross-row batching.
- Every read of a `zjsonb` or `ztext` value decompresses it, once per row
  however many times the row is referenced; small-value scans are measurably
  slower than plain `jsonb` (see the timings above).
- Unknown-type literals and untyped parameters written into a column with a
  non-default modifier are compressed twice unless typed as the base type,
  and `ALTER COLUMN TYPE` to a different modifier rewrites the table; the
  rewrite-free path leaves the column marked `pending` until every row has
  been caught up and checked (see
  [When the modifier applies](#when-the-modifier-applies)).
- Logical replication carries logical values, never frames, so the subscriber
  needs the dictionaries itself and decides the policy; `ztype-sync` carries
  them and reports the columns that differ, but a subscriber slot that means
  a different dictionary is still applied silently unless the registry is
  published or the sweep is run. DDL is not replicated. A registry collision
  stops apply and has a runbook, not a resolver; failover is qualified for
  promoted standbys, failover slots and `pg_createsubscriber`, not for
  anything that hands out slots on two primaries at once
  (see [Replication](#replication)).

## Testing

```sh
make test       # disposable socket-only cluster; no system install needed
make test-install  # the files make install ships, staged under a scratch DESTDIR and loaded from there
make test-mutate  # bounded, seed-reproducible mutation fuzzer against the real extension
make test-asan  # AddressSanitizer build in build/asan, runs the fuzzer under it
make test-asan-suite  # the functional suite under that build (Linux only, see below)
make test-replication  # publisher, logical subscriber, physical standby and a third node, all disposable
make test-replication-ha  # promotion, a failover slot and pg_createsubscriber, four more disposable clusters
make test-cross NEW_PG_CONFIG=/path/to/newer/pg_config   # two majors side by side
make bench      # the size and timing tables above, a few minutes
make bench-all  # every benchmark below in one run, output kept under results/bench/, about an hour
make bench-smoke  # the same at tiny sizes in a few minutes: proves they run, measures nothing
make bench-codec # libzstd only: context creation versus a small-document encode and decode, seconds
make dict-report DICT_REPORT_ARGS='--source ... --query ...'  # dictionary evaluation on your own column
make bench-params  # parameter inserts over libpq: untyped versus typed, per row
make bench-latency # single statements over libpq: autocommit against batched, per statement
make bench-rewrite # policy change by ALTER COLUMN TYPE, UPDATE, batched UPDATE and CREATE TABLE AS: time, WAL, size
make bench-memory  # native working memory per level, value size and dictionary size, checked against backend RSS
make bench-hash  # equality and hash operator classes per row: GROUP BY, hash join, equality filter, ANALYZE, against the base type and the cast
```

Tests need Python 3, a C compiler with libpq headers (for the
extended-protocol test), PostgreSQL 18+ server binaries, and permission to
create local shared memory and Unix sockets. They load the freshly built extension
from a temporary directory through `extension_control_path`, so nothing is
installed system-wide, and they always stop their cluster afterwards.

`make test-install` is the one check that does not rewrite the control file:
it runs `make install` into a scratch `DESTDIR`, asserts that the staged
tree holds exactly the control file, the script and the module (plus the LLVM
bitcode a JIT-enabled PostgreSQL build emits), starts a disposable server
whose `extension_control_path` and `dynamic_library_path` name the staged
directories as a package would have filled them, and checks that the shipped
`module_pathname = '$libdir/ztype'` resolves, that the server offers exactly
one version with no update path, that `CREATE EXTENSION` from the installed
script works with every C function still recorded as `$libdir/ztype`, that
the three types round-trip through a dictionary, that `DROP EXTENSION` leaves
no schema or type behind, and that `make uninstall` removes every staged
file. `ZTYPE_INSTALL_ROOT=<dir>` points it at a tree someone else staged (a
package build) instead.

`make test` covers logical round trips, Unicode boundaries, TOAST, JSON nulls,
ordinary-role access, delegated registration (`EXECUTE` on the three
functions, each load-bearing and every smaller subset refused, then the
granted role registering and importing dictionaries and using one by name
while it still cannot read, count or write the registry), rollback and savepoints, concurrent
registration, named dictionaries, every write path against the column policy (literals, inferred
and typed parameters over the extended protocol in text and binary format
through libpq, expressions, CTEs, `CASE`, column moves, defaults, domains,
arrays, text and binary `COPY`, values too short to compress, all verified
with `ztype.inspect`), `ALTER COLUMN TYPE` rewriting to the new policy and
staying a no-op for the same one, cache misses across sessions with only
read-only statements in between and under `REPEATABLE READ`, the
missing-dictionary fallback with typed and domain defaults, the dictionary
cache budget (least-recently-used eviction, transparent reload, exact
accounting across compression-object swaps, errors and savepoint rollbacks,
and a zero budget), parallel plans
and dictionary decoding inside a parallel worker, per-level compression,
malformed and corrupted values (each one also checked through
`ztype.validate`, which returns the cast's message — its own for an encoding
failure — and never raises),
the native `zjsonb` operators (each against the base type's answer, strict on NULL, inlined into the cast in the plan, a GIN index on the cast and an expression index on an accessor matched from the operator form, a corrupt frame raising the cast's error), equality and the hash operator classes (one value under five policies equal
and hashing alike, the hash agreeing with `hashtext` under the C collation and
with `jsonb_hash`, envelope decisions decoding nothing, corrupt frames raising
what the cast raises, an ordinary role comparing dictionary frames, hash
aggregation, `UNION`, `INTERSECT`, a hash join, `IN`, `= ANY`, a hash index
and hash partitioning with pruning, sorting refused, and ANALYZE decoding each
sample row once, never a value declared wider than 1 kB, with the base type's
distinct count and most-common values stored under the type's own equality),
payload-format markers, partial reads (`raw_length` and `ztype.inspect`
touching a handful of buffers on a 1 MB out-of-line value where the decoding
cast touches over a hundred), `ztype.column_policies` against bare, named,
numeric, unregistered, pending and dropped columns as an ordinary role, the
rewrite-free policy change (the filenode unchanged, stored rows untouched and
new writes on the new policy, the modifier of a pending column coercing every
move into a plain column of the same policy, expression index, `CHECK` and
trigger condition rewritten and the index still chosen, `finish` refusing with
the count and clearing the mark after the batched catch-up, partitions
through the parent, views, statistics, non-owners and partitions refused, an
`ALTER COLUMN TYPE` out of the pending state, and a pending column through
plain and parallel dumps), `ztype.build_info`
against `pg_extension` and the fixture's magic, backend RSS across
commits, aborts, savepoint rollbacks, codec errors and a cancelled level-22
compression, plain and parallel logical restores without settings, and a
non-UTF8 database encoding. Recovery has its own section: registry export
and import with slot, ID and name preserved, a table-only restore into a
database with no registry (dictionary-free rows and a warning) and into one
whose slot means a different dictionary (silently re-encoded), stored frames
moved as a physical restore preserves them (strictly unreadable, and
`ztype.validate` names the missing ID, until the bytes are registered under
any slot), the three registry collisions, `ALTER COLUMN TYPE` re-pointing a
column, an import by a role holding `INSERT` alone (refused, `42501`, until it
also has `EXECUTE` on `ztype.dict_id`), `ztype.import_dictionary` re-running
as a no-op on an identical row and refusing each of the three collisions
with `42710` and the registry unchanged, by the owner and by an importing
role that holds `EXECUTE` on it and nothing on the registry, and the batched catch-up recipe over a
table that contains raw rows as well as frames, so that "a rerun rewrites
nothing" is measured and not assumed.

The libpq test goes further on the extended protocol than the write-path
list above: NULL parameters in both formats, a `bytea` with embedded zero
bytes sent as binary and as an escaped literal, Unicode text whose
`raw_length` is its UTF-8 byte count, and one prepared statement executed
eight times alternating text, binary and NULL parameters, so the plan cache
switches to a generic plan while the formats keep changing. The same name is
then prepared again while live (refused with `42P05`), deallocated and
prepared again with typed parameters, and executed in both formats; and the
statement runs in pipeline mode with a rejected payload in the middle of one
batch, which pins libpq's shape of that failure: the rows sent before it in
the same implicit transaction are rolled back with it, the ones after it
return `PGRES_PIPELINE_ABORTED`, and the batch after the next sync lands.
All twenty-two rows are then read back with binary results and every column
compared byte for byte against what was sent — the `jsonb` including its
version byte, the NULL rows asserted NULL in all three columns, the three ids
of the failed batch asserted absent — and checked against the column policy
with `ztype.inspect`. It also sends binary payloads the receive functions
must reject (invalid UTF-8, an embedded NUL in text, an unknown `jsonb`
version byte, a `jsonb` body that is not JSON), pins each SQLSTATE and
requires the same connection to run the next statement.

`make test-replication` starts a publisher with `wal_level = logical`, a
logical subscriber and a `pg_basebackup` standby, and qualifies both halves of
[Replication](#replication) on a 30-row table of `ztext`, `zjsonb` and
`zbytea` columns. For logical replication it asserts that a subscriber whose
registry was pre-seeded from the publisher's export receives every row
logically equal and at the column policy, with no fallback warning, through
both the text apply path and a second subscription with `binary = true`; that
with the registry in the publication instead, name-based DDL fails before the
registry arrives, every row is exact and either at the policy or
dictionary-free, and the catch-up `UPDATE` brings them all to the policy; that
a dictionary registered after the subscription started reaches the
subscriber's registry, a table naming it can then be created there, and rows
streamed with it arrive carrying it, alongside a replicated `UPDATE` and
`DELETE`; that a subscriber column declared `ztext(3)` stores at its own
modifier with no registry at all; and that a subscriber whose slot already
holds a different dictionary either applies rows silently under it (registry
not published) or fails apply visibly — `apply_error_count` in
`pg_stat_subscription_stats` grows, the subscription disables itself under
`disable_on_error` and the conflicting row never lands (registry published).
`ztype.policy_differences` is run with the publisher's policies against every
one of those subscribers: nothing for the pre-seeded and the published
registry, the remodified column's level and missing dictionary, and
`different dictionary` for the silent mismatch, by an ordinary role.
On the standby it asserts that the primary's values, `prefix`,
`raw_length`, `ztype.inspect` (dictionary name included) and `ztype.validate`
all read correctly, that a dictionary registered on the primary after the base
backup is used once replayed, that the dictionary cache holds entries inside a
transaction and keeps them past `COMMIT` until `ztype.reload_dictionaries()`,
that a zero cache budget still round
trips, and that an `INSERT` and `ztype.train_and_add` fail with `25006` while
the connection stays usable. The same run qualifies `ztype-sync` as the
seeding step (a role holding `EXECUTE` on `import_dictionary` only, an
idempotent second run, `--check` in sync, a source role that cannot read the
bytes failing with exit 3 and nothing imported, a taken slot refused with
exit 1 under `--dry-run` and for real with the registry untouched, `--check`
reporting the silent mismatch), cascading to a third cluster (rows and a
later registration arriving two hops away, the tool seeding a node from a
subscriber), two-way replication with `origin = none` under the slot-range
convention (rows at the local policy in both directions, no loop, no apply
error, the `max(slot) + 1` pitfall pinned), and the registry-collision
runbook end to end (import under `60000 + slot`, `SKIP` at the LSN from the
apply error, re-enable, the next registration lands, local registrations
continue above the publisher's range, the re-pointed columns leave
`--check` clean).

`make test-replication-ha` starts a publisher with
`synchronized_standby_slots`, a logical subscriber, a standby with
`sync_replication_slots = on` and a second base backup, and asserts that
`pg_createsubscriber` converts the latter with the registry and every frame
present physically, `created_at` for `created_at`, and applies later rows and
a later registration logically with no `WARNING`; that the promoted standby
has the full registry, writes into a dictionary column and allocates the next
slot at once, and seeds a new subscriber through `ztype-sync`; and that a
`failover = true` subscription whose slot was synchronised to the standby,
repointed after the promotion, resumes, receives the dictionary registered
there, applies a table created there at the policy, and leaves the old
primary's slot idle to drop. It also pins the trap: without an empty
`synchronized_standby_slots` on the promoted node the repointed subscription
never receives a row.

`make test-cross` builds the extension against
two installations and checks raw bytes in both directions, logical
dump/restore and `pg_upgrade` between them.

`make test` also decodes committed storage fixtures: the exact bytes an
earlier build stored, kept in `tests/fixtures/storage-<magic>.json` with each
value's logical SQL expression, type modifier and `ztype.inspect` row beside
it. `make test-cross` builds both majors from today's source, so these
fixtures are the only thing that would notice an accidental change to the
envelope, the frame options or the JSON payload tagging. Thirteen values cover
all three types, raw and compressed payloads, levels 1, 6 and 19, with and
without a dictionary (committed with the fixture and registered by the test),
an empty value, multibyte text, a JSON null inside an array, and `bytea` with
embedded zero bytes; each must decode to its recorded logical value —
evaluated live and compared — carry its recorded metadata, and validate clean.
Three more, one per type, are above the TOAST threshold once compressed
(5 to 7 kB stored from 10 to 18 kB raw); every value is also inserted into a
bare column of its type and read back, the large ones out of line, which the
test asserts from the table's TOAST relation. Envelopes recorded under a
retired magic must still be rejected as an unsupported storage format on
decode, on `raw_length` and through `ztype.validate`. The fixture data is
synthetic; `python3 tests/make_fixtures.py` regenerates it after a deliberate
format change, and the previous file stays as a must-reject entry. When a
fixture for the same magic already exists, the generator refuses to overwrite
it unless every existing entry comes out byte-identical (training is
deterministic for one libzstd version), because bytes moving under an
unchanged magic is exactly what the fixture is there to catch; `--replace`
is the override for a decision, and adding entries needs none. Envelopes are
native-endian, so the value checks are skipped where the byte order differs
from the fixture's.

`make test-mutate` runs a bounded, seed-reproducible mutation fuzzer against
the real extension. It keeps two concerns apart. *Client-input safety* is
everything a client can actually send (text literals, type modifiers, binary
`COPY` fields): these must end in a clean error or a stored value, never a
crash. *Stored-corruption robustness* is bytes that reach the decoder only
through disk corruption or a hostile superuser; the fuzzer reaches them only
through raw casts created inside a disposable superuser database, since there
is no client path for compressed bytes. It mutates the envelope fields,
truncates and appends frames, rewrites zstd frame-header bits and dictionary
IDs, splices payloads and relabels kinds, then exercises full decoding,
prefix, the same-type coercion, recompression, `inspect`, `validate`, binary
send and the JSON operators under zero and tiny dictionary-cache budgets with
dictionary traffic around each call, asserting cache accounting after each
case. `ztype.validate()` is run on every case and must always answer without
raising, with the same verdict the decoding cast reaches. Every
invalid envelope and every corrupt compressed frame is rejected; the
documented exceptions (raw payloads carry no checksum, an in-range level byte
is informational, a frame relabelled to a real registered dictionary that
still checksum-matches is a valid value) are not treated as corruption.
Cases derive from `seed:index`, so a failure replays with `--seed S --only I`
and its minimised bytes are written to `results/mutation/`.

`make test-asan` builds the extension with AddressSanitizer into a separate
directory (never the tree's own library, which a running cluster may have
mapped), preloads the runtime into a disposable server, proves it is live
with a deliberate-overflow positive control, then runs the mutation fuzzer
under it. Across seeds and under AddressSanitizer, no memory-safety defect was
found in ztype's own code. `make test-asan-suite` runs the functional suite
under the same build, with the same positive control first; the suite skips
its resident-memory bounds there, since the allocator is the sanitizer's, and
runs everything else. It is a Linux target: macOS's AddressSanitizer runtime
aborts on the second error raised through an instrumented frame in one
backend, and the suite raises many per session, so on macOS only the
fuzzer, which isolates each erroring call in its own backend, runs under
AddressSanitizer. The suite has also been run under an undefined-behaviour
sanitizer build and the Clang static analyzer.

`make bench-all` runs every benchmark above in one go and keeps what each
printed under `results/bench/<stamp>-<mode>/`, beside a manifest recording
the git revision, whether the tree was dirty, the PostgreSQL and libzstd
versions, the platform, the sizes in effect and each benchmark's wall time
and exit status; a number quoted in this README should have such a directory
behind it. `make bench-smoke` runs the same scripts at sizes that finish in
about two minutes, which is how the CI workflow keeps the benchmarks from
rotting; its numbers are not measurements and the manifest says so.

One class stays a *base-type* limitation, reported separately: a `zjsonb`
value whose compressed frame is intact and checksum-valid but whose
decompressed bytes are not a well-formed jsonb container. Its `->`/`->>`
operators and its `::jsonb` cast hand those bytes to PostgreSQL's own jsonb
reader, which trusts them; a corrupt container can make that reader error or
crash the backend, exactly as a corrupt native `jsonb` value would. ztype
validates its envelope and the zstd frame, not the jsonb container inside a
decoded payload, so this is not something it can prevent short of validating
every container. `ztype.validate()` draws the line in the same place: it
returns NULL for such a value, which the fuzzer asserts. Reachable only by
injecting raw bytes as a superuser; ordinary writes always produce a
well-formed container. None of the testing establishes production performance
on your data or replaces a sustained fuzzing campaign.

## Contributing

Issues and pull requests are welcome at
[github.com/xvaara/pg_ztype](https://github.com/xvaara/pg_ztype). Keep
changes small and covered by `make test`; anything that touches the storage
format or the dictionary registry should come with a `make test-cross` run
and a note in this README.

## License

[MIT](LICENSE). Copyright (c) 2026 Jukka Raimovaara.
