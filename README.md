# pg_ztype

**zstd-compressed column types for PostgreSQL: `ztext`, `zjsonb`, `zbytea`.**

## TL;DR

Change a column's type and it takes a quarter of the space. Nothing else
changes: the same `INSERT`, the same `SELECT`, the same `->>`, the same
dumps and replication.

- **Small values finally compress.** PostgreSQL never compresses a value
  that fits in its row, so a table of 0.6 kB JSON documents is stored
  uncompressed whatever `default_toast_compression` says. `zjsonb` compresses
  every value, and with a trained dictionary the same table stores at **24%**
  of `jsonb`'s size; large documents that TOAST already compresses drop to
  **51%** ([the numbers](#what-it-saves-and-what-it-costs)).
- **Dictionaries, built in.** `ztype.train_and_add('name', 'SELECT ...')`
  trains a zstd dictionary from your own data and registers it; a column
  declared `ztext(6, 'name')` uses it. Dictionaries are permanent, dumped
  with the database and replicated to standbys, so old data stays readable.
- **It behaves like the base type.** Literals, parameters, `COPY`, `pg_dump`,
  logical replication and the binary protocol all carry plain `text`,
  `jsonb` or `bytea`. Equality, `GROUP BY`, `DISTINCT`, hash joins and hash
  indexes work on the column; jsonb reading operators and GIN indexes work
  on `zjsonb`. Compression is a per-column policy in the type modifier, and
  every row in the column has it.
- **Operationally boring.** Restores need no settings, a missing dictionary
  degrades to a warning rather than an error, every stored value carries a
  checksum, `ztype.validate` sweeps a table, and `ztype-sync` carries the
  dictionary registry between databases. Tested on PostgreSQL 18 and 19 with
  a functional suite, replication and failover suites, a cross-major upgrade
  suite, a mutation fuzzer and sanitizer builds.

The trade: every read of a compressed value decompresses it, about 1 µs per
small document with a dictionary and 3 µs without, so wide scans of small
values are slower than plain `jsonb`, and content with nothing shared between
rows saves little. The rest of this document says where the line is.

## Why

PostgreSQL compresses large values in TOAST with pglz or lz4, and that is the
whole menu: the method is not extensible, and values that fit in a row,
roughly anything under 2 kB, are never compressed at all. That line is
`TOAST_TUPLE_THRESHOLD`, a compile-time constant; the `toast_tuple_target`
storage parameter decides how far a row is shrunk once it is past the line
and cannot lower it. The suite pins this: a table of 1,000 jsonb rows of
about 540 bytes with `toast_tuple_target = 128` and lz4 holds zero compressed
values and is byte for byte the size of the same table at the default,
while 3 kB rows in the same table compress. pg_ztype adds three types that
compress their own bytes with [zstd](https://facebook.github.io/zstd/),
optionally with a trained dictionary, and otherwise behave like `text`,
`jsonb` and `bytea`.

Ordering is the one thing the types leave to the cast, on purpose. Sorting,
`min`/`max` or `DISTINCT` inside an aggregate on the compressed column itself
is rare (a row is ordered by a key or a timestamp, not by its 5 kB body),
and where it is wanted `ORDER BY body::text` is also the faster form: a
comparison on the compressed type would decode two values, a sort on the
cast decodes each row once. Equality, `GROUP BY`, hash joins and expression
or GIN indexes work on the column directly.

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
compares against a literal without a cast. The cast is what gives a client a
`text`, `jsonb` or `bytea` result type instead of `ztext`, and what the base
type's functions and operators (`length`, `LIKE`, `jsonb_typeof`, `ORDER BY`)
need, since the compressed types carry none of them.

On small ERP-style JSON documents, `zjsonb` with a dictionary stores **a
quarter of what `jsonb` needs**; on random content it saves almost nothing
and every read pays a decompression. [What it saves, and what it
costs](#what-it-saves-and-what-it-costs) has the numbers.

## Status

Pre-release, extension version `0.9`. The storage format and the
type-modifier encoding are versioned but not frozen: they may still change
before a tagged 1.0, and there are no `ALTER EXTENSION ... UPDATE` scripts.
If you store data with this version, keep the ability to dump it logically.
Changes are in [CHANGELOG.md](CHANGELOG.md), security reporting in
[SECURITY.md](SECURITY.md), and the measurements and test inventory behind
this document in [NOTES.md](NOTES.md).

Requires PostgreSQL 18 or newer and libzstd 1.5 or newer. CI builds and tests
on Linux against PostgreSQL 18 and 19 (including the cross-major suite from
18 to 19, an undefined-behaviour sanitizer build and the suite under
AddressSanitizer) and on macOS against 18.

## Installation

```sh
git clone https://github.com/xvaara/pg_ztype
cd pg_ztype
make            # needs pg_config and pkg-config libzstd on PATH
make install    # into pg_config's PostgreSQL
```

`make install` puts the module and the SQL script where the server looks, and
`ztype-sync`, the registry transport for replication and restores, next to
`psql`. Then, in each database, as a superuser:

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
| `zjsonb` | `jsonb` | PostgreSQL's native binary jsonb, tagged with a payload-format version |
| `zbytea` | `bytea` | arbitrary bytes |

Each value is one zstd frame with a content checksum. Values under 64 bytes,
and values that do not shrink, are stored raw. The types are `STORAGE
EXTERNAL`, so TOAST moves large values off-page without a second compression
pass.

What works without a cast:

- **Input and output** in text and binary form carry the logical value;
  `COPY` in either format, `pg_dump`, drivers requesting binary results and
  logical replication never see compressed bytes. Malformed `zjsonb` and
  `zbytea` text input is reported softly, so `pg_input_is_valid()` and
  `COPY ... WITH (ON_ERROR ignore)` behave as for `jsonb` and `bytea`.
- **Assignment casts** in both directions: `INSERT` from a base-type
  expression, `UPDATE t SET textcol = body`.
- **Equality and hashing.** `=` and `<>` compare decoded values with the base
  type's equality (bytewise for `ztext`, which is not collatable, and
  `zbytea`; structural for `zjsonb`), and each type has a default hash
  operator class, so `GROUP BY`, `DISTINCT`, `UNION`, `IN`, `= ANY`, hash
  joins, hash partitioning and hash indexes work on the column, and `ANALYZE`
  gathers distinct-value and most-common-value statistics. Comparing against
  a base-type value needs a cast on one side (`body = $1::ztext` or
  `body::text = $1`).
- **jsonb reading operators** on `zjsonb`: `->` and `->>` by key and index,
  `#>`, `#>>`, `?`, `?|`, `?&`, `@>`, `<@`, `@?`, `@@`. They plan as the
  operator over `doc::jsonb`, so a GIN index on `((doc::jsonb))` is used from
  the operator form. The `jsonb_*` functions and the document-building
  operators (`||`, `-`, `#-`) need `::jsonb`.
- **`raw_length(value)`**: the uncompressed length in bytes, read from the
  envelope without decoding. **`prefix(value, n)`** on `ztext`: the first `n`
  bytes clipped to a whole character, decompressing only that much.
- **`ztype.inspect(value)`**: `kind`, `codec` (`raw` or `zstd`), `level`,
  `format`, `raw_length`, `stored_bytes`, and for dictionary frames
  `dict_id`, `dict_slot`, `dict_name`, read without decoding or fetching an
  out-of-line value. It is how you check that a column uses its dictionary:

  ```sql
  SELECT i.codec, i.level, i.dict_name, count(*)
  FROM messages m, ztype.inspect(m.body) i GROUP BY 1, 2, 3;
  ```

- **`ztype.validate(value)`**: NULL when the stored value passes every check
  ztype makes, otherwise the message the decoding cast would raise. It
  reports instead of raising, so one query sweeps a table:

  ```sql
  SELECT id, v.problem
  FROM messages m, LATERAL ztype.validate(m.body) v(problem)
  WHERE v.problem IS NOT NULL;
  ```

There is no ordering operator and no btree operator class, by decision: a
sort on the compressed type would decode both sides of every comparison, so
`ORDER BY body` is an error and `ORDER BY body::text` the form to write. Every
index other than hash goes on the cast expression:

```sql
CREATE INDEX messages_metadata ON messages USING gin ((metadata::jsonb));
```

All codec functions, casts and accessors are `PARALLEL SAFE`.

## What it saves, and what it costs

Measured with `make bench` (PostgreSQL 18.6, libzstd 1.5.7, Apple M1 Pro,
level 6) on generated ERP-style documents: `small` is one order line, `large`
a sales invoice with 20–120 lines, `random` small objects of random strings
and numbers with nothing shared between documents. Dictionaries were trained
on separately generated documents. Sizes are `pg_total_relation_size`
relative to `jsonb` with pglz. Method, spreads and the per-run figures are in
[NOTES.md](NOTES.md#the-size-and-timing-benchmark).

| documents | raw JSON text | `jsonb`, pglz | `jsonb`, lz4 | `zjsonb(6)` | `zjsonb(6)` + dictionary |
|---|---:|---:|---:|---:|---:|
| small: 200,000 × 0.6 kB | 123 MB | 146 MB (100%) | 146 MB (100%) | 109 MB (75%) | **36 MB (24%)** |
| large: 4,000 × 47 kB | 183 MB | 67 MB (100%) | 65 MB (97%) | 44 MB (66%) | **34 MB (51%)** |
| random: 1,000,000 × 0.3 kB | 300 MB | 348 MB (100%) | 348 MB (100%) | 331 MB (95%) | **273 MB (78%)** |

Microseconds per row, median of five runs, serial sequential scans on
unlogged tables:

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

- **Plain `jsonb` leaves small documents uncompressed.** TOAST compression
  runs only when a row exceeds about 2 kB, so pglz and lz4 store the small
  shape identically and read it for free. `zjsonb` compresses every value of
  64 bytes or more, and the dictionary is what makes small values compress:
  the shared substrings live in it instead of in every value.
- **Small values pay per read**, about 3 µs without a dictionary and 1 µs
  with one, because a dictionary frame reuses the dictionary's entropy tables
  instead of carrying its own. A backend keeps its last two decoded values,
  so several references to one row cost one decode. Index the logical value
  so filters do not decompress every row.
- **Large documents already compress in TOAST**; `zjsonb` gains a third over
  pglz and another quarter with a dictionary, and reads faster than pglz.
- **Writes cost about twice pglz on small documents** at level 6, the same
  on large ones. A literal or untyped parameter into a column with a
  non-default modifier compresses twice; type it as the base type
  ([When the modifier applies](#when-the-modifier-applies)).
- **Random content is the floor.** zstd alone saves 5%, the dictionary a
  fifth through its entropy tables, and every read still pays. If your data
  looks like this, keep plain `jsonb`.

Rerun `tests/bench_zjsonb.py` on your own data before deciding, or
`make dict-report` on a column you already have
([Evaluating a dictionary](#evaluating-a-dictionary-on-your-data)).

### Request latency

One short statement per transaction over a pooled connection costs the same
as the same statement inside a longer transaction, because each backend
keeps the dictionaries it has used for the rest of its session. Measured
with `make bench-latency` (libpq, prepared statement, one 645-byte document,
µs per statement, one statement per transaction):

| | `jsonb` | `zjsonb(6)` | `zjsonb(6, 'erp')`, 110 kB dictionary | `zjsonb(6, 'erp8')`, 8 kB |
|---|---:|---:|---:|---:|
| insert, typed `$1::jsonb` | 20.0 | 34.5 | 41.5 | 34.2 |
| point read, whole value | 16.1 | 22.0 | 18.3 | 19.3 |
| point read, one key `->>` | 14.1 | 18.0 | 15.4 | 15.8 |

A new backend per statement pays the dictionary load every time (about
2.6 ms per insert into the dictionary column against 1.3 ms into `jsonb`).
Use a connection pool.

### Equality, grouping and joins

Hashing and equality decode each row once, the same as the cast, and the
dictionary makes the compressed forms as fast to group as the base type.
Microseconds per row on 200,000 small documents with 10,000 distinct values
(`make bench-hash`):

| column | size | `GROUP BY` | hash join | `= constant` | `ANALYZE` |
|---|---|---:|---:|---:|---:|
| `jsonb` | 145.7 MB | 1.8 | 3.3 | 0.2 | 0.13 s |
| `zjsonb(6)` | 108.8 MB | 3.7 | 4.0 | 2.9 | 0.21 s |
| `zjsonb(6, 'erp')` | 35.4 MB | 2.0 | 1.9 | 1.1 | 0.14 s |

For `ztext` and `zbytea` an equality filter almost never decodes: the
envelope's declared length settles most pairs.

### Working memory

zstd's contexts, dictionary objects and training buffers are allocated
outside PostgreSQL's memory contexts: `work_mem` does not bound them. The
compression context per call, from libzstd's own accounting
(`make bench-memory`), is the ceiling a column's level grants:

| level | 1 kB value | 64 kB | 1 MB | 16 MB | 128 MB |
|---:|---:|---:|---:|---:|---:|
| 1 | 40 kB | 489 kB | 1.3 MB | 1.3 MB | 1.3 MB |
| 3 | 44 kB | 841 kB | 2.5 MB | 3.5 MB | 3.5 MB |
| 6 | 44 kB | 1.1 MB | 4.2 MB | 5.2 MB | 5.2 MB |
| 9 | 48 kB | 1.1 MB | 12 MB | 15 MB | 15 MB |
| 19 | 199 kB | 1.9 MB | 19 MB | 90 MB | 90 MB |
| 22 | 199 kB | 1.9 MB | 19 MB | 274 MB | 834 MB |

Levels up to 9 stay under 15 MB whatever the value; level 22 reserves up to
834 MB for one 128 MB value, a deliberate choice to make for a column. A full
decode needs a 94 kB context whatever the frame, because it writes straight
into the result datum; `prefix` allocates the writing level's window (990 kB
at level 1, 2.5 MB at 6, 8.5 MB at 19). Training grows the backend by two to
five times the sample bytes. There is no setting that caps the level a column
may declare: whoever may declare `ztext(22)` grants that memory. The one GUC,
`ztype.dictionary_cache_size`, bounds cached dictionary objects only. The
dictionary-object sizes and the resident-memory check are in
[NOTES.md](NOTES.md#working-memory).

## Compression policy

The type modifier is `(level [, dictionary])`: levels 1–22; the dictionary is
a slot number or the **name** of a registered dictionary. Slot zero means no
dictionary; no modifier means `(6,0)` for logical input.

```sql
CREATE TABLE messages (body ztext(6, 'mail-2024'));   -- ztext(6,1) if mail-2024 is slot 1
ALTER TABLE messages ALTER COLUMN body TYPE ztext(9, 'mail-2026');
```

A name is resolved when the statement is parsed and only the slot is stored:
`\d` and `pg_dump` show `ztext(6,1)`. The dictionary must be registered
before the DDL runs (the same transaction is fine); an unregistered name is
an error. Names are never all digits, so a digit-only modifier is always a
slot. `ztype.dictionary_slot(name)` returns the slot for use in expressions.
There is no session-level compression setting.

### When the modifier applies

**A column's modifier applies to every value stored in it**, however the
value got there: literals, parameters, casts from the base type, `COPY`,
values selected from other columns, expressions, defaults, array elements.
A value already at the policy is stored as is; otherwise it is recompressed.
`ztype.inspect` shows the result.

Consequences:

- `ALTER COLUMN TYPE` to a **different** modifier rewrites the table; the
  same modifier is a no-op. [Changing the policy without a
  rewrite](#changing-the-policy-without-a-rewrite) is the alternative.
- An unknown-type literal or an untyped parameter into a column with a
  non-default modifier is parsed at `(6,0)` first and recompressed at the
  column's policy: twice the work. Type it as the base type (`$1::jsonb`,
  `'...'::text`) and it compresses once. Measured with `make bench-params`,
  a typed parameter into a dictionary column costs 16.5 µs of server time
  per 0.6 kB row against 30.6 µs untyped.
- **A column declared without a modifier is the exception.** PostgreSQL
  applies no coercion towards a target with no modifier, so bare `ztext`
  compresses logical input at `(6,0)` but stores a value selected from
  another `ztype` column unchanged, level and dictionary included. Declare
  `ztext(6)` when `(6,0)` must hold for every row.
- A dictionary slot that is not visible yet is a preference, not an error,
  for a write: the value is stored without a dictionary at the column's level
  and the backend warns once per slot per transaction. Decoding a stored
  frame whose dictionary is missing is an error.

`ztype.recompress(value, level, slot)` produces a value with an explicit
policy, for trials (`pg_column_size(ztype.recompress(body, 6, 3))`); unlike a
write it is strict about a missing dictionary. Assigning its result to a
column with a modifier applies the column's policy.

### Changing the policy without a rewrite

`ALTER COLUMN TYPE` holds `ACCESS EXCLUSIVE` for the whole rewrite. When that
is not affordable:

```sql
SELECT ztype.set_column_policy('messages', 'body', 12, 'mail-2026');   -- catalog only
-- new writes now use (12, 'mail-2026'); stored rows keep what they had

-- once per batch, each in its own transaction, until it updates nothing
UPDATE messages SET body = body::ztext(12,2)
 WHERE id >= :lo AND id < :hi AND NOT ztype.matches_policy(body, 12, 2);

SELECT ztype.finish_column_policy('messages', 'body');   -- scans, then clears the pending mark
```

`set_column_policy` stores the new modifier in `pg_attribute` under the same
lock but for a catalog update only. Until the column is finished the
modifier reads `ztext(12,2,pending)` in `\d`, `pg_dump` and
`ztype.column_policies`: new writes follow the policy, stored rows may not,
and every value moved out of the column is coerced. `ztype.matches_policy`
reads the envelope and frame header only, so the predicate costs no
decompression. `finish_column_policy` scans every row under `ACCESS
EXCLUSIVE`, refuses with the count while any is off the policy, and otherwise
clears the mark. Both require ownership; a partitioned parent changes its
partitions with it. Expression indexes, `CHECK` constraints and trigger
conditions on the column are rewritten in place; a view, rule or extended
statistics on the column blocks the change as they block `ALTER TABLE`.
The other way out of the pending state is `ALTER COLUMN TYPE` to a plain
modifier.

The catch-up is an `UPDATE`, which writes about four times the WAL of a
rewrite and leaves the table at old plus new size until `VACUUM FULL` or
`pg_repack`; `ALTER COLUMN TYPE` and `CREATE TABLE AS` cost the same as each
other and the least. Figures per strategy are in
[NOTES.md](NOTES.md#what-a-rewrite-costs).

## Dictionaries

A zstd dictionary is a bundle of substrings that occur across many values
plus pre-tuned entropy tables, about 110 kB by default. It helps exactly where
per-value compression cannot: the boilerplate a small value shares with its
neighbours. Large values gain less. A dictionary is never needed to read a
value written without one.

### The registry

Dictionaries are append-only rows in `ztype.dictionaries`, dumped with the
extension's configuration. Every frame records the zstd dictionary ID it
needs, so a missing dictionary is an error, never silent corruption.

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
  returns the bytes without registering them.
- `ztype.add_dictionary(name, bytea [, trained_from])` registers a
  dictionary under the next slot, including one trained with `zstd --train`.
- `ztype.import_dictionary(slot, name, bytea [, trained_from])` registers
  under a *given* slot, for restores and subscribers. An identical row is a
  no-op returning the slot; any other overlap on slot, name or dictionary ID
  is `42710` naming what the target holds, registry untouched.
- `ztype.train_and_add(name, query [, dict_bytes])` does both.
- `ztype.dictionary_slot(name)` resolves a name; `ztype.dict_id(bytea)`
  returns a dictionary's zstd ID.
- `ztype.dictionary_inventory`, readable by every role: slot, name, zstd ID,
  size and registration time, never the bytes or the training query.
- `ztype.column_policies`, readable by every role: every stored column of the
  three types with its decoded modifier (`level`, `slot`, `has_modifier`,
  `pending`) and the `dict_name` and `dict_id` the slot resolves to here. A
  non-zero slot with a NULL name is a column asking for a dictionary this
  database does not have.
- `ztype.build_info()`: the library version (compare with
  `pg_extension.extversion`), the storage magic and jsonb payload format it
  writes, libzstd compiled against runtime.

Slots are allocated under a lock and never reused; `UPDATE`, `DELETE` and
`TRUNCATE` on the registry are refused. Plan for order 10⁴ dictionaries, one
per corpus rather than one per customer: a frame names its dictionary by
zstd's 32-bit ID, a hash of the bytes, and two independently trained
dictionaries collide with probability about 1% at 10,000 of them; the
registry replicates and the per-backend cache holds a few entries.

**Privileges.** Ordinary roles read and write compressed columns without any
access to the registry. Registration functions run as the extension owner and
are not executable by `PUBLIC`; `EXECUTE` is the whole delegation:

```sql
GRANT EXECUTE ON FUNCTION ztype.train_dictionary(text, integer, integer),
  ztype.add_dictionary(text, bytea, text),
  ztype.train_and_add(text, text, integer) TO migrator;
```

The role can register and use dictionaries by name; it cannot read the
bytes, count the rows or write the table. Training keeps the caller's
privileges, so a training query cannot read what its caller cannot. Slots
are permanent, so grant registration to a role you would let append to the
registry. **Training data is not private merely because it became a
dictionary**: fragments of the training set can be reconstructed from the
bytes, so pick a non-sensitive corpus. The registry is database-wide, not a
per-tenant permission system.

**The cache.** Each backend keeps the dictionaries it has used for the rest
of its session; an entry loaded by a transaction that rolls back is dropped
with it. `ztype.dictionary_cache_size` (default 64 MB, settable per session)
counts each cached dictionary's bytes plus its zstd objects and evicts the
least recently used; the dictionary in use is never dropped, so a small
budget costs reloads, never an error. `ztype.dictionary_cache_stats()` and
`ztype.decode_cache_stats()` report the backend's caches;
`ztype.reload_dictionaries()` clears the dictionary cache by hand.

### Training input

The query runs through a read-only cursor and must return one `text`, `bytea`
or `jsonb` column. Each sample is the value's stored bytes, so **train
`zjsonb` dictionaries on a `jsonb` column**, not on its text rendering.
Limits: 1 KiB–1 MiB output, 1–65,536 bytes per sample (default 8,192), 20,000
non-empty samples, 64 MiB of sample data, at least eight samples.

### Building a good dictionary

Train on what the column will hold and score on rows the training never saw.
On a private helpdesk mail archive (22,000 messages, 1.1 GB of bodies, half
HTML), a 110 kB dictionary trained on messages before 2020 and scored on the
8,977 after it cut level-6 storage by 16% overall, by about 30% for messages
between 256 bytes and 4 kB and by 6% above 64 kB: the dictionary is for the
small values.

1. **One dictionary per kind of content.** Mail bodies, JSON metadata and
   blobs each want their own; for JSON, one per document shape.
2. **Thousands of samples, spread across the corpus.** About 100× the
   dictionary size in sample bytes, picked across time and authors
   (`ORDER BY md5(id::text)`), not the first 20,000 rows of one era.
3. **Keep the default 8 kB sample cut.** Shared boilerplate lives at the
   start of values, and small values end there anyway.
4. **Exclude what should not be learned**: empty values, giant outliers,
   base64 attachments, anything sensitive.
5. **Measure on held-out rows before registering**, with
   `pg_column_size(ztype.recompress(body, 6, slot))` against slot 0 or with
   `make dict-report`. Registrations are permanent.
6. **Retrain occasionally.** Dictionaries decay gently: on the same archive a
   dictionary trained before 2011 still saved 10% on 2024 messages, against
   16% for one trained before 2024. Register a new slot and switch the
   column; old dictionaries stay, so old backups stay readable.

The default 110 kB size is a sensible fixed point. Each cached dictionary
costs its decompression object plus one compression object for the level
last used with it: 247 kB read-only, 1.0 MB reading and writing at level 6,
1.9 MB at level 19 for a 110 kB dictionary.

### Evaluating a dictionary on your data

`make dict-report` fetches one column read-only, copies it into a disposable
cluster, trains on part of it and reports the held-out rows per level with
and without the dictionary: stored bytes, codec cost per value, savings by
value size, and how many values pay back the dictionary's bytes.

```sh
make dict-report DICT_REPORT_ARGS="--source 'dbname=helpdesk' --query 'SELECT body FROM messages ORDER BY created_at' --split tail"
python3 tests/dict_report.py --help    # levels, sizes, sample limits
```

`--split tail` holds out the end of the query's order, which is how a
dictionary ages. Two things the report shows on small uniform documents:
levels barely matter once there is a dictionary (level 1 with one beats
level 19 without), and at high levels a dictionary makes writes much slower.
The answer for small documents is a dictionary at a low level.

## Backup, restore and upgrades

**A dump never carries a compressed frame.** Every dump path writes logical
values, and the restore compresses each one again under the *target*
column's policy. `pg_dump` includes the registry rows, but restore order
against user tables is not guaranteed, so the write path tolerates a missing
dictionary: rows restored before their dictionary arrived are stored
dictionary-free with a `WARNING`, exact and readable, and restores need no
settings:

```sh
pg_dump -Fc -f app.dump source_database
pg_restore --exit-on-error -j 4 -d fresh_database app.dump
```

Catch those rows up when convenient by going through the logical type, which
re-applies the column policy row by row (a plain self-assignment is a no-op):

```sql
UPDATE messages SET body = body::text;
```

For a large table, walk the primary key in batches with a predicate that
skips rows already on the policy, so a rerun rewrites nothing:

```sql
UPDATE messages SET body = body::text
 WHERE id >= :lo AND id < :hi AND NOT ztype.matches_policy(body, 12, 2);
```

Stored frames survive only a **physical** restore (base backup,
`pg_upgrade`, a physical replica). Those values fail with `ztype: dictionary
ID ... is not available` until the same bytes are registered in this
database, under any slot.

### Moving the registry

`ztype.dictionaries` is an ordinary table; `COPY` moves it with slot, ID and
name preserved:

```sh
psql -d source -c "\copy ztype.dictionaries TO 'dicts.copy'"
psql -d target -c "\copy ztype.dictionaries FROM 'dicts.copy'"
```

Reading the registry means reading dictionary bytes, so the exporting role is
the extension owner or a superuser. The `COPY` import path needs `INSERT` on
the table and `EXECUTE` on `ztype.dict_id` (the `CHECK` constraint calls it
as the inserting role). `ztype.import_dictionary(slot, name, bytes)` does the
same one row at a time and needs only `EXECUTE` on itself; it is what
`ztype-sync` uses.

A collision is one of three constraints, and the transaction rolls back with
the registry untouched: `dictionaries_dict_id_key` (the same bytes under
another name), `dictionaries_name_key` (a different dictionary under that
name), `dictionaries_pkey` (that slot). The answer to all three is **keep the
existing row**, register the incoming dictionary under a free slot, and point
columns at it. Compare two registries without exposing bytes through
`ztype.dictionary_inventory`: matching `dict_id` values mean the same
dictionary whatever the slot or name.

### The slot-mismatch pitfall

Slots are local numbers, so the same slot can mean different dictionaries in
two databases. A table-only restore (`pg_dump -t messages`) brings the
modifier back as numbers, and if the target's slot holds a *different*
dictionary the restored rows are compressed against it **silently**: correct
values, different bytes, and only `ztype.inspect` shows it. List the columns
whose slot this database cannot resolve:

```sql
SELECT schema_name, table_name, column_name, level, slot
  FROM ztype.column_policies
 WHERE slot > 0 AND dict_name IS NULL;
```

Fix a mismatched column by re-pointing it, which rewrites, or with
`set_column_policy` and a batched catch-up:

```sql
ALTER TABLE messages ALTER COLUMN body TYPE ztext(12, 'mail-2024-imported');
```

### Replication

**Logical replication follows the dump rule**: the publisher decodes, the
subscriber compresses again under **its own column's modifier**. The
subscriber decides the policy, a registry it lacks degrades to
dictionary-free with a `WARNING`, and a slot that means a different
dictionary there is applied silently. Nothing needs configuring on the
publisher: the registry is a `user_catalog_table`, which logical decoding
needs to resolve a dictionary registered inside the decoded WAL window.

1. **Give the subscriber the dictionaries, one way only**: `ztype-sync`
   (below), *or* `ztype.dictionaries` in the publication. Never both.
2. **Number the slots, or wait for the registry, before name-based DDL** on
   the subscriber: `ztext(6,'first')` fails until the row exists there.
3. **Expect dictionary-free rows from the initial copy** when the registry
   travels in the publication, and catch them up as after a restore.
4. **Compare the registries before subscribing, and the columns after**:
   `ztype-sync --check` does both. `ztype.policy_differences(publisher
   jsonb)`, fed the publisher's `column_policies` rows, returns every
   subscriber column whose level or dictionary (compared by ID) differs.

**`ztype-sync`** carries one registry to another in one transaction through
`ztype.import_dictionary`, then runs the sweep:

```sh
ztype-sync --source 'service=publisher' --target 'service=subscriber'
```

| run | writes | exit 0 | exit 1 | exit 2 | exit 3 |
|---|---|---|---|---|---|
| default | imports, one transaction | in sync, or imported and no difference | collision, nothing written | policy differences remain | usage, connection, privilege or `psql` failure |
| `--dry-run` | the same, rolled back | what the run would give | | | |
| `--check` | nothing | in sync | | missing or different dictionaries, or policy differences | |

The source role must read the bytes; the target role needs `EXECUTE` on
`ztype.import_dictionary` only. The bytes travel in the SQL text of the
target connection, which `log_statement = 'all'` would log. Run it after every
registration on the publisher; a second run imports nothing.

**Slot ranges.** `add_dictionary` takes `max(slot) + 1`, which is right while
one node registers. A node that must register something of its own imports
under an explicit slot far above the publisher's range (`60001`), and two
nodes that both register, as in two-way replication with `origin = none`,
both use explicit slots from their own range and run `ztype-sync` in both
directions. A subscriber can publish onward to a third node, with the
registry hopping the same way.

**When apply stops on a registry collision** (the registry published, the
publisher's new slot already taken on the subscriber; create the subscription
`WITH (disable_on_error = true)` so it fails once instead of retrying):

1. Keep the local row; stored frames depend on it.
2. Import the publisher's dictionaries under `60000 + slot` with
   `ztype.import_dictionary`.
3. Skip the failed remote transaction at the LSN from the apply error's
   `CONTEXT` line: `ALTER SUBSCRIPTION name SKIP (lsn = '<LSN>')`, then
   `ENABLE`.
4. Re-point columns that should store as the publisher's do, with
   `ALTER COLUMN TYPE ztext(6, 60001)`, after which `ztype-sync --check` is
   clean.

**Failover.** The registry travels physically, so a promoted standby has it
complete and needs nothing ztype-specific. A `failover = true` subscription
repointed to a promoted standby resumes and receives later registrations
through the published registry. One trap: a base backup copies the primary's
`synchronized_standby_slots`, and a promoted node that keeps it blocks every
failover-slot walsender; set it to `''` on the standby before promoting.
`pg_createsubscriber` on a base backup has the registry and every frame and
needs no catch-up.

**Physical replication carries the stored bytes**, so both sides need
matching builds. A hot standby decodes everything, uses a dictionary as soon
as its registration is replayed, and refuses writes with SQLSTATE `25006`.
DDL is not replicated; a published table must exist on the subscriber before
any row for it is decoded.

### Major-version upgrades

Stored-value compatibility is decided by pg_ztype's own format identifiers,
not by the PostgreSQL major. The binary is compiled against one major's
headers and must be built for each major under the same module name;
`pg_upgrade` loads the old server's copy too. Both paths are exercised
between 18 and 19 by `make test-cross`: values decode under either build, a
logical dump from 18 restores into 19, and `pg_upgrade` keeps dictionaries,
compressed values, defaults and an expression index working.

## Storage format

The envelope is 16 bytes including the varlena header: magic/version,
uncompressed length, payload-format version (1 for `zjsonb`), compression
level, logical kind and codec, native-endian. A compressed payload is exactly
one zstd frame with a content checksum, which is what detects corruption;
the frame names its dictionary by zstd dictionary ID. Raw payloads are
covered by length checks only, like a native `text` value, and native jsonb
inside a decoded payload is trusted as PostgreSQL trusts it: a frame whose
bytes are intact but whose container is malformed can crash the jsonb reader
exactly as a corrupt native `jsonb` value would, and `ztype.validate` never
walks the container. Input functions accept logical data only; there is no
client path for compressed bytes. No on-disk architecture portability is
promised.

Compression checks for interrupts every 256 kB of input; decompression
writes straight into the result datum and checks between ratio-sized input
steps, so a cancelled level-22 compression of 8 MB returns in about 0.15 s.

## Limitations

- No ordering: no `<`, no btree operator class, so `ORDER BY`, `min`/`max`,
  `DISTINCT` inside an aggregate and a merge join need the cast. GIN and
  every other index method need the cast expression.
- Codec working memory is bounded only by the level in the column modifier.
- The `jsonb_*` functions and the document-building operators need `::jsonb`.
- `zjsonb` inherits jsonb normalisation: keys reordered, duplicates dropped.
  Use `ztext` where the exact original bytes matter.
- Compression is per value; redundancy between rows is captured only by a
  dictionary.
- Every read of a compressed value decompresses it, once per row however
  many times the row is referenced; small-value scans are measurably slower
  than plain `jsonb`.
- Untyped literals and parameters into a column with a non-default modifier
  compress twice; `ALTER COLUMN TYPE` to another modifier rewrites the table.
- Logical replication carries logical values, so the subscriber needs the
  dictionaries and decides the policy; a subscriber slot that means a
  different dictionary is applied silently unless the registry is published
  or the sweep is run. DDL is not replicated. A registry collision has a
  runbook, not a resolver; failover is qualified for promoted standbys,
  failover slots and `pg_createsubscriber`, not for anything that hands out
  slots on two primaries at once.

## Testing

```sh
make test                 # the functional suite on a disposable cluster; no install needed
make test-install         # the files make install ships, staged and loaded from a scratch DESTDIR
make test-cross NEW_PG_CONFIG=/path/to/newer/pg_config   # two majors: raw bytes, dump/restore, pg_upgrade
make test-replication     # publisher, logical subscriber, physical standby, third node
make test-replication-ha  # promotion, a failover slot, pg_createsubscriber
make test-mutate          # seed-reproducible mutation fuzzer against the real extension
make test-asan            # AddressSanitizer build, runs the fuzzer under it
make test-asan-suite      # the functional suite under that build (Linux only)
make bench-all            # every benchmark, output kept under results/bench/
make bench-smoke          # the same at tiny sizes: proves they run, measures nothing
```

Tests need Python 3, a C compiler with libpq headers and PostgreSQL 18+
server binaries; they load the built extension through
`extension_control_path` and stop their clusters afterwards. What each suite
asserts, and the storage fixture that pins stored bytes across builds, is
listed in [NOTES.md](NOTES.md#what-the-suites-assert). The mutation fuzzer
and AddressSanitizer found no memory-safety defect in ztype's own code; none
of this establishes production performance on your data.

## Contributing

Issues and pull requests are welcome at
[github.com/xvaara/pg_ztype](https://github.com/xvaara/pg_ztype). Keep
changes small and covered by `make test`; anything that touches the storage
format or the dictionary registry should come with a `make test-cross` run.
CLAUDE.md holds the invariants a change must not break.

## License

[MIT](LICENSE). Copyright (c) 2026 Jukka Raimovaara.
