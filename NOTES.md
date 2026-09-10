# pg_ztype development notes

Measurements, methodology and the test inventory that README.md summarises.
Every number here comes from a run of the named benchmark on PostgreSQL 18.6
with libzstd 1.5.7 on an Apple M1 Pro unless the section says otherwise;
`make bench-all` keeps each run's output and a manifest under
`results/bench/<stamp>-full/`. The invariants a change must not break are in
CLAUDE.md, the user documentation in README.md.


## The size and timing benchmark

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
  [When the modifier applies](README.md#when-the-modifier-applies)).
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


## Request latency

The tables above run whole tables in one statement. An application mostly
runs one short statement per transaction over a pooled connection, so each
statement pays its round trip, and before the session-scoped dictionary
cache each transaction that touched a dictionary column also loaded the
dictionary from the registry again (one lookup plus building the zstd decompression object and, for a
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
[Dictionaries](README.md#dictionaries)), so the two shapes cost the same and the
probe's backend reports zero dictionary loads per statement in both. With
the transaction-local cache this release replaces, the same run gave 324.6 µs
per autocommit insert and 132.1 µs per point read on the 110 kB dictionary
column (73.6 and 44.5 µs with the 8 kB dictionary), against the same warm
figures: about 115 µs of reload per read and 295 µs per write, scaling with
the dictionary's size, and one load per statement. A row with four
dictionary columns cost 1,489 µs to insert and 594 µs to read as its own
transaction; now 71.6 and 34.1 µs, the same as four `zjsonb(6)` columns.
Saving the registry lookup's plan accounts for about 10 µs of the
difference; the rest was building the zstd objects.

What the cache cannot help is a connection per statement: with a new backend
for every statement, an insert costs about 1.3 ms into `jsonb` and 2.6 ms
into the dictionary column, and a point read 1.4 against 2.8 ms, dominated
by backend start-up and the first load of the dictionary in each of them. Use
a connection pool, as with any PostgreSQL workload; a pooled backend keeps
its dictionaries.


## Equality, grouping and joins

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


## Working memory

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
[Building a good dictionary](README.md#building-a-good-dictionary) is their sum):

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


## Parameter shapes

PostgreSQL reads an unknown-type literal or an untyped parameter with the
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


## Why the pending mark exists

PostgreSQL applies the same-type coercion only when a
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


## Dictionary ID collisions

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


## Dictionary evaluation on the synthetic corpus

On the benchmark's small
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


## Why the catch-up predicate lets raw values pass

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


## What a rewrite costs

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
advice from [Limitations](README.md#limitations). Sizes are heap plus TOAST; "after"
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
`pg_repack` ([Changing the policy without a rewrite](README.md#changing-the-policy-without-a-rewrite));
or, if writes can be paused or redirected, build the new table with
`CREATE TABLE AS` and swap.


## Replication procedure, in full

1. **Give the subscriber the dictionaries, one way only.** Either carry the
   registry over with `ztype-sync` (below), which imports identical rows with
   the same slots through `ztype.import_dictionary` and is the same as the
   manual export and import of
   [Exporting and importing the registry](README.md#moving-the-registry),
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
   [Resumable catch-up](README.md#backup-restore-and-upgrades)).
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


## ztype-sync details

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


## Slot ranges

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


## Cascading and two-way

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


## When apply stops: a registry collision

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
   [Exporting and importing the registry](README.md#moving-the-registry)
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


## Failover and promotion

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


## Validation contract and cancellation

`ztype.validate(value)` applies the storage contract (README, "Storage
format") to one stored value and
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
validates clean. That is the base-type limitation described at the end of
[What the suites assert](#what-the-suites-assert).

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


## What the suites assert

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
[Replication](README.md#replication) on a 30-row table of `ztext`, `zjsonb` and
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
