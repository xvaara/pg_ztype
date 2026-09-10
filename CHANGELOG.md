# Changelog

## 0.9 — 2026-09-10

First public pre-release. The storage format (magic `0x5a540003`) and the
type-modifier encoding are versioned but not frozen: they may still change
before 1.0, and there are no `ALTER EXTENSION ... UPDATE` scripts. Keep the
ability to dump data logically. README.md is the user documentation; the
section names below refer to it.

### Types and storage

- `ztext`, `zjsonb` and `zbytea`: zstd-compressed column types with a
  16-byte envelope, one zstd frame with a content checksum, and raw storage
  for values that are short or that zstd cannot shrink. Text input and
  output, binary send and receive carrying logical values, casts to and from
  the base types, `prefix`, `raw_length`, `ztype.inspect`, `ztype.validate`
  (a soft-error integrity check that never aborts the sweeping transaction),
  `ztype.build_info` and `ztype.zstd_version`.
- `zjsonb` accessors `->` and `->>` by key in C; the remaining jsonb reading
  operators (`->` and `->>` by index, `#>`, `#>>`, `?`, `?|`, `?&`, `@>`,
  `<@`, `@?`, `@@`) as inlinable SQL over the cast, so a GIN index on
  `((doc::jsonb))` is used from the operator form.
- Equality and hashing on the decoded value with the base type's semantics,
  and a hash operator class per type, so `GROUP BY`, `DISTINCT`, `IN`, hash
  joins, hash partitioning and hash indexes work on the column itself. No
  ordering operator and no btree class by decision; `ORDER BY body::text` is
  the form to use. A `typanalyze` per type decodes each sample value once
  and stores null fraction, width, distinct estimate and most-common values.
- Full decodes go straight into the result datum; compression and decoding
  check for interrupts between steps; one decompression context and a
  two-entry decode cache per backend. `zjsonb` and `zbytea` input report
  malformed values through the caller's error context, so
  `pg_input_is_valid` and `COPY ... ON_ERROR ignore` work; the receive
  functions stay hard by decision.
- Requires PostgreSQL 18 or newer and libzstd 1.5 or newer.

### Compression policy

- The type modifier `(level, dictionary)` is the policy, dictionary by name
  or slot, `(6, 0)` when absent. Applied at input, receive, the casts from
  the base types and by a same-type coercion, so every value in a column
  with a modifier has the column's policy; `ALTER COLUMN TYPE` to another
  modifier rewrites the table. A bare column applies `(6, 0)` to logical
  input only ("When the modifier applies").
- `ztype.set_column_policy` changes a column's modifier in place without a
  rewrite and marks it `pending`; `ztype.finish_column_policy` clears the
  mark once every row matches `ztype.matches_policy`. Expression indexes,
  `CHECK` constraints and trigger conditions on the column are rewritten in
  place; views, rules and extended statistics are refused ("Changing the
  policy without a rewrite").
- `ztype.column_policies` lists every compressed column with its decoded
  modifier, dictionary and pending state.

### Dictionaries

- `ztype.train_dictionary` from a query over stored `text`, `bytea` or
  `jsonb`, `ztype.add_dictionary`, `ztype.train_and_add`, and
  `ztype.import_dictionary(slot, name, bytes)` for restores and subscribers.
  The registry `ztype.dictionaries` is append-only, slots never reused,
  names unique; frames name their dictionary by zstd dictionary ID, never by
  slot. Registration runs as the extension owner, so delegation is `EXECUTE`
  on the functions and nothing on the registry; training keeps caller
  privileges. Dictionary bytes are readable only through the registry table
  itself, never through a function.
- `ztype.dictionary_inventory` (slot, name, ID, size, never the bytes) for
  comparing two registries; `ztype.dictionary_cache_size` as the one GUC, a
  budget per backend; `ztype.reload_dictionaries`,
  `ztype.dictionary_cache_stats`, `ztype.decode_cache_stats`.
- A logical write whose dictionary slot is not registered stores
  dictionary-free with one `WARNING` per slot per transaction; decoding is
  strict. This is what makes `pg_dump`/`pg_restore` and logical replication
  work without settings ("Backup, restore and upgrades").
- `tests/dict_report.py` (`make dict-report`): held-out evaluation of a
  dictionary on your own column, by level and value size.

### Replication and recovery

- Logical replication carries logical values; the subscriber compresses
  under its own column's modifier. The registry is a
  `user_catalog_table`, which logical decoding needs to resolve a
  dictionary registered inside the decoded WAL window. Physical replication
  carries the stored bytes, and a hot standby decodes every read path.
- `ztype-sync` (`tools/ztype-sync`, installed next to `psql`): carries one
  registry to another through `ztype.import_dictionary` in one transaction,
  then runs `ztype.policy_differences` against the source's column policies;
  `--dry-run` and `--check` modes, exit codes for collisions and remaining
  differences.
- Documented and tested procedures for restore catch-up, registry export and
  import, slot collisions, slot ranges for two-way setups, cascading,
  failover slots, promotion and `pg_createsubscriber` ("Moving the registry",
  "Replication").

### Tests and benchmarks

- `make test`: the functional suite on a disposable cluster, including a
  committed storage fixture (`tests/fixtures/storage-<magic>.json`) that
  pins stored bytes and requires retired magics to be refused.
- `make test-install`, `make test-cross` (18 to 19: raw bytes both ways,
  dump and restore, `pg_upgrade`), `make test-replication`,
  `make test-replication-ha`, `make test-mutate` (a seed-reproducible
  mutation fuzzer), `make test-asan` and `make test-asan-suite`.
- Benchmarks under `make bench-all`: storage and timing (`bench`), request
  latency, parameter shapes, rewrite strategies, working memory, equality
  and hashing, and a libzstd-only codec microbenchmark. The README quotes
  only numbers from these runs.
- CI on Linux (PostgreSQL 18 and 19, UBSan, ASan) and macOS.
