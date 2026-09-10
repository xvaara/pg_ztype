# pg_ztype — guide for future development

Standalone PostgreSQL extension: zstd-compressed column types `ztext`, `zjsonb`,
`zbytea`. Everything a user needs is in README.md; this file is for whoever
changes the code. Keep it short and keep it true.

## Layout

| file | role |
|---|---|
| `ztype.c` | the whole extension: envelope, codec, dictionary cache, typmod, coercion, binary I/O, inspect, training |
| `ztype--0.9.sql` | types (each with its `typanalyze`), casts (including the same-type coercion), operators (the key accessors in C, the other jsonb reading operators as inlinable SQL over the cast so an index on the cast expression still matches), the hash operator classes, the `ztype` schema, registry table, the inventory and column-policy views, the `policy_differences` sweep, admin functions |
| `ztype.control` | extension metadata; `default_version` is the release knob. `ZT_VERSION` in `ztype.c` must match it: `build_info()` reports it and the suite compares it with `pg_extension.extversion` |
| `.github/workflows/ci.yml` | Linux (PG 18 and 19: `test`, `test-install`, `test-replication`, `test-replication-ha`, `bench-smoke` with the output kept as an artifact), Linux 18→19 `test-cross`, Linux/UBSan, Linux ASan (fuzzer, then `test-asan-suite`), macOS (`test`, `test-install`, ASan fuzzer) |
| `tools/ztype-sync` | the registry transport, stdlib Python over `psql`; `make install` ships a built copy (`SCRIPTS_built`, top-level `ztype-sync`, gitignored) next to `psql`. Reads bytes only on the source, writes only `ztype.import_dictionary` calls plus the `policy_differences` sweep in one target transaction |
| `tests/test_ztype.py` | the suite; stdlib only, spins its own cluster (`Cluster`, `share`), `make test` |
| `tests/test_install.py` | `make install` into a scratch `DESTDIR`, the staged file set, the shipped control file loaded through `dynamic_library_path`, `CREATE`/`DROP EXTENSION`, `make uninstall`; `make test-install` |
| `tests/pq_params.c` | libpq extended-protocol parameters, compiled and run by the suite |
| `tests/test_cross_major.py` | two installations: raw bytes both ways, dump/restore, `pg_upgrade` |
| `tests/test_replication.py` | publisher with `wal_level = logical`, logical subscriber (also publishing: cascading to a third cluster, two-way with `origin = none`), `pg_basebackup` standby, the tool as the seeding step, and the registry-collision runbook; `make test-replication` |
| `tests/test_replication_ha.py` | failover and promotion on its own port block: a standby with `sync_replication_slots` promoted by `pg_promote`, a `failover = true` subscription repointed to it, and `pg_createsubscriber` on a second base backup; imports the helpers of `test_replication.py`; `make test-replication-ha` |
| `tests/bench_all.py` | runs every benchmark, keeps each one's Markdown plus a manifest under `results/bench/<stamp>-<mode>/`; `make bench-all` (the run to quote from), `make bench-smoke` (tiny sizes, CI, proves they run) |
| `tests/bench_zjsonb.py` | the README benchmark; `make bench`. Five runs after a warm-up (`ZTYPE_BENCH_RUNS`), unlogged tables, autovacuum off; prints seconds and µs per row |
| `tests/mutate_ztype.py` | bounded, seed-reproducible mutation harness against the real extension; `make test-mutate`, and under ASan via `make test-asan` |
| `tests/bench_params.py`, `tests/param_insert.c` | parameter inserts over libpq, untyped against typed, pipelined and per round trip; `make bench-params` |
| `tests/bench_latency.py`, `tests/latency_probe.c` | request latency over libpq: typed inserts and point reads as one statement per transaction against one batched transaction, four dictionary columns per row, a new connection per statement; reports the backend's dictionary loads per statement; `make bench-latency` |
| `tests/bench_rewrite.py` | policy change or post-restore catch-up by `ALTER COLUMN TYPE`, one `UPDATE`, batched `UPDATE`s and `CREATE TABLE AS`, each with and without an expression index, on a fresh template-database copy per measurement: time, WAL, heap and index size, HOT share; `make bench-rewrite` |
| `tests/bench_hash.py` | the equality and hash operator classes per row: `GROUP BY` on the column and on the cast, a hash join, an equality filter against a constant and `ANALYZE`, for `jsonb` and `text` against their compressed forms, with the backend's decodes per row; `make bench-hash` |
| `tests/bench_memory.py`, `tests/codec_memory.c` | native working memory outside `work_mem`: libzstd's own accounting per level, pledged input size and dictionary size (compression context, dictionary objects, decompression context for full decodes and for `prefix`), checked against a backend's peak RSS while it compresses, decodes, prefix-reads and trains; `make bench-memory` |
| `tests/dict_report.py` | dictionary evaluation report on a query's column or a synthetic corpus, in a disposable cluster; `make dict-report`; the suite runs it against its own cluster |
| `tests/codec_bench.c` | libzstd-only microbenchmark: codec context creation versus a small-document encode and decode, per level; `make bench-codec` |
| `tests/make_fixtures.py` | writes `tests/fixtures/storage-<magic>.json` from a disposable cluster; run by hand after a deliberate format change or to add entries. Refuses to change existing entries under the same magic without `--replace` |
| `tests/fixtures/storage-5a540003.json` | stored bytes under the current magic (synthetic only): a dictionary, sixteen values (three above the TOAST threshold) with their logical SQL and `inspect` rows, and retired-magic entries the suite must reject |
| `tests/asan_probe.c` | positive control: a deliberate heap overflow the ASan run must report |
| `tests/sanitizer_runtime.py` | prints the compiler's ASan runtime for preloading |

## Build and test

```sh
make && make test                                    # PG_CONFIG=... to pick an installation
make test-install                                    # staged make install, loaded as a package would be
make test-mutate MUTATE_ARGS='--seed 7 --iterations 800'  # mutation fuzzer, normal build
make test-asan                                       # sanitized build in build/asan + mutation fuzzer
make test-asan-suite                                 # the functional suite under that build; Linux only
make test-replication                                # logical subscriber + hot standby
make test-cross NEW_PG_CONFIG=/path/to/other/pg_config
make bench
make bench-all                                       # every benchmark, output kept under results/bench/
make bench-smoke BENCH_ARGS='--only latency'         # same scripts at tiny sizes, a few minutes
make dict-report DICT_REPORT_ARGS='--source ... --query ...'   # held-out dictionary evaluation
make bench-params                                    # parameter inserts over libpq
make bench-latency                                   # single statements: autocommit against batched
make bench-rewrite                                   # rewrite strategies: time, WAL, size, HOT share
make bench-memory                                    # native memory per level/size/dictionary, RSS check
make bench-hash                                      # equality and hash operator classes per row, ANALYZE
```

Run `make test` after every change to `ztype.c` or the SQL script (and
`make test-cross` when the typmod encoding or its text form changes: the
modifier travels through `pg_dump` and `pg_upgrade` there), and
`make test-asan` after anything touching the codec, the dictionary cache
(eviction, accounting) or codec error cleanup — it preloads an
AddressSanitizer build into a disposable server and runs the mutation
harness. Add `make test-cross` when the storage format, the registry or
dictionary loading changes, `make test-replication` when the registry
table, its reloptions, the lenient/strict lookup split or `tools/ztype-sync`
changes, `make test-replication-ha` when `import_dictionary`, slot allocation
or anything a promoted node depends on changes,
`make test-install` when the control file, the script name, `tools/ztype-sync` or the Makefile
changes, and `make bench-smoke` after editing any benchmark script or the
harness they import. Regenerate
the storage fixture (`python3 tests/make_fixtures.py`) only when `ZT_MAGIC` or
`ZT_JSONB_FORMAT` is bumped on purpose, or to add entries (the generator
refuses to alter existing ones under the same magic); the suite failing on
it otherwise is the point of it. Rerun `make bench` and update the README tables
when the codec changes, and `make bench-latency` (README "Request latency")
when the registry lookup or the dictionary cache changes, `make bench-rewrite`
(README "What a rewrite costs") when the coercion or what a rewrite writes per
row changes, `make bench-memory` (README "Working memory") when codec
parameters, contexts or the dictionary objects change, and `make bench-hash`
(README "Equality, grouping and joins") when `zt_equal`, `zt_hash`, the decode
cache or `zt_typanalyze` changes; never edit a number in
the README without a run behind it, and `make bench-all` is the run that
keeps its own record (`results/bench/<stamp>-full/manifest.json`).

The mutation harness (`tests/mutate_ztype.py`) is the memory-safety fuzzer:
it corrupts envelope fields, frame headers, dictionary IDs, truncates and
appends frames, splices payloads and relabels kinds, then runs decode,
prefix, coercion, recompress, inspect, binary send and the JSON operators
under zero and tiny cache budgets with dictionary traffic around each call.
It separates client-input safety (nothing a client sends may crash) from
stored-corruption robustness (raw injection only in a disposable superuser
database), and it reports the native-jsonb container limitation on its own
rather than as a defect. Cases are `seed:index`, so a failure replays with
`--seed S --only I`; failing seeds land in `results/mutation/`.

## Invariants — do not break without a format or design decision

- **Storage envelope** (`ZtValue`): 16 bytes, native-endian: magic, rawlen,
  format, level, kind, codec. `ZT_MAGIC` is the layout version: bump it when
  the layout changes, so old envelopes fail as "unsupported storage format"
  instead of being misread. `ZT_JSONB_FORMAT` versions the jsonb payload
  independently of the PostgreSQL major; readers must never compare stored
  values against `PG_VERSION_NUM`. The level is stored so the coercion can be
  a no-op; the dictionary is identified only by the frame's zstd dictionary
  ID, never by slot (slots are local registry numbers). Magic history, all
  2026-09-08 and all pre-release: `0x5a540001` was 20 bytes with an outer
  CRC32C and a PostgreSQL-major field; `0x5a540002` dropped the CRC and
  replaced the major with `ZT_JSONB_FORMAT`; `0x5a540003` split the format
  field into format + level. No shipped data exists under the older two.
  A bump is a three-step change, not a one-line one: bump `ZT_MAGIC`, run
  `python3 tests/make_fixtures.py` to write `tests/fixtures/storage-<new>.json`,
  and keep the old fixture file in place — the suite decodes the file whose
  magic matches the library and requires every other file, plus each `retired`
  entry, to be refused as "unsupported storage format" on decode, on
  `raw_length` and through `ztype.validate`. `fixtures()` fails with the
  regenerate instruction when no committed fixture matches, which is the wanted
  signal; regenerating without a deliberate format decision defeats the check,
  and since 2026-09-09 `make_fixtures.py` refuses it mechanically: with a
  fixture of the same magic present, any existing entry or the dictionary
  coming out different aborts the write, `--replace` being the override for a
  decision (a libzstd that encodes differently is the expected reason). Adding
  entries needs nothing; the suite also inserts every entry into a bare column
  and reads it back, out of line for the three above the TOAST threshold.
  A `ZT_JSONB_FORMAT` bump on its own also invalidates the fixture — `zt_header`
  refuses a stored jsonb payload format this build does not write — but the
  magic, and so the file name, would not change, and `retired` entries assert
  the magic message ("unsupported storage format") rather than the payload one.
  Bump `ZT_MAGIC` together with it, so the one rule above still holds; carrying
  the old jsonb envelopes any other way means giving `retired` an expected
  message first. Fixture data is synthetic by rule: never a corpus value, never
  a dictionary trained on one.
- **Integrity** comes from the zstd content checksum (`ZSTD_c_checksumFlag`,
  and `zt_frame` requires it) plus structural checks in `zt_header`,
  `zt_frame` and `zt_decompress`. There is no envelope or raw-payload
  checksum by design (decided 2026-09-08, closing the open question left when
  the outer CRC32C was dropped): raw payloads get exactly the protection a
  native `text`/`bytea` value has, PostgreSQL page checksums, and every
  compressed value additionally gets zstd's end-to-end content checksum. A
  per-value CRC would only detect corruption between disk and datum, which
  is a ztype bug or a wild write, and that belongs to the test suite, not the
  storage format. `ztype.validate()` (done 2026-09-09) follows the same
  contract: structural checks for raw values, frame integrity for compressed
  ones, database-encoding validity for `ztext`, and never a jsonb container
  walk. It is a *soft-error* decoder: `zt_header`, `zt_frame`, the strict path
  of `zt_dictionary` and `zt_decompress` take an `escontext` and `errsave`
  instead of `ereport` when one is set (every other caller passes NULL and
  behaves exactly as before). That is what keeps it `PARALLEL SAFE`, leaves no
  SPI state behind — `zt_dictionary` runs `SPI_finish`/`PopActiveSnapshot`
  before the soft return — and avoids a longjmp through instrumented frames,
  so the mutation harness can run it in its shared backend under macOS ASan.
  A soft error inside `zt_decompress`'s `PG_TRY` sets a flag and breaks; it
  never returns from there, so `PG_FINALLY` still releases the context. With an
  `escontext` set, `zt_decompress` neither reads nor seeds the decode cache.
- **Typmod** encodes `level | slot << 5` with 16 slot bits, plus bit 21
  (`ZT_TM_PENDING`, 22 bits total); `-1` means `(6,0)`. Names are resolved to
  slots in `ztext_typmod_in` and only the slot is ever stored, so `typmod_out`
  must stay numeric or dumps stop restoring; the pending bit prints as a third
  element, `(9,2,pending)`, and `typmod_in` accepts exactly that word there.
  `zt_policy` strips the bit: a write or coercion never sees it. Because it reads the registry, `ztext_typmod_in` is `STABLE`,
  not `IMMUTABLE`. Digit-only components are converted with
  `pg_strtoint32_safe` under a local `ErrorSaveContext`, so an unrepresentable
  modifier gets ztype's own `22023` message instead of the integer parser's
  "out of range". The encoding is persisted in catalogs: changing it after
  release needs an update script that rewrites `pg_attribute.atttypmod`.
- **Soft input errors.** `zjsonb_in` and `zbytea_in` parse through
  `DirectInputFunctionCallSafe` with `fcinfo->context`, so `pg_input_is_valid`
  and `COPY ... ON_ERROR ignore` work as for the base types. The base-type
  parse runs before `zt_compress`: a rejected value stores nothing and loads no
  dictionary. `ztext_in` needs none of this (`textin` cannot fail) and the
  `recv` functions stay hard by decision (2026-09-09): PostgreSQL has no
  `ReceiveFunctionCallSafe`, and binary `COPY` refuses every `ON_ERROR` mode
  but `stop` on 18.6 and 19beta3 ("only ON_ERROR STOP is allowed in BINARY
  mode"), so no caller can pass a receive function an error context and a
  soft path there would be code no test could reach. `tests/pq_params.c`
  pins the hard SQLSTATEs. Revisit only if a release adds a receive-side
  safe call.
- **Policy application** happens in exactly two places: the logical entry
  points (input, recv, the three casts from base types, which get the target
  typmod directly) and the same-type coercion `ztype_coerce`, which
  PostgreSQL applies whenever a value's modifier differs from the target's
  (literals and typmod -1 parameters, column moves, ALTER COLUMN TYPE). The
  contract is "every value in a column has the column's policy", with no
  exceptions by query shape: a planner support function that skipped the
  coercion for column references was tried and removed because CTEs, CASE
  and explicit casts leaked through it (2026-09-08). ALTER COLUMN TYPE to a
  new modifier therefore rewrites; the test asserts the filenode changes. Do
  not make the coercion IMMUTABLE (it reads the registry). The contract holds
  for a target *with* a modifier only: PostgreSQL applies no length coercion
  towards typmod -1, so a bare `ztext` column stores a value moved from a
  modified column unchanged, dictionary included (documented 2026-09-09,
  pinned by `bare_column` in the suite). Do not try to fix that in C —
  `ztype_coerce` is never called for such a target.
- **Rewrite-free policy change is an explicit operation with a truthful
  modifier** (2026-09-09). `ztype.set_column_policy` writes
  `pg_attribute.atttypmod` in place with the pending bit set and rewrites
  nothing; `ztype.finish_column_policy` scans through `ztype.matches_policy`
  under `ACCESS EXCLUSIVE` and clears the bit only when no row is off the
  policy. The bit is what keeps the policy contract: PostgreSQL elides the
  same-type coercion when modifiers are equal, so a column claiming `(9,2)`
  while holding `(6,1)` rows would leak them unchanged into any plain `(9,2)`
  column; a pending modifier equals no plain one, so every move out is
  coerced (`deferred_policy` pins the leak test with a plain and a twin
  pending target). A catalog-only typmod change also strands every Var that
  names the column: the planner matches an index expression by node
  equality, typmod included, and a stale one turns the index scan into a
  sequential scan (measured 2026-09-09), so `zt_retypmod_dependents` rewrites
  `indexprs`/`indpred`, `conbin` and `tgqual` in place (ALTER TABLE refuses a
  trigger on the column; here it is handled), allows defaults (re-coerced on
  use) and publication column lists, and refuses views, rules and extended
  statistics as ALTER TABLE refuses a view. Both functions run
  with the caller's rights: ownership of every relation in the inheritance
  set is checked in C, names resolve through `ztype.dictionary_slot`, and the
  registry is read only through the PUBLIC inventory. `matches_policy` is the
  catch-up contract (level equal; raw passes; a frame must name the slot's
  dictionary), deliberately looser than the coercion for raw values of
  compressible size so a rerun selects nothing. Never a planner rule: the
  support function that skipped the coercion for column references was tried
  and removed (2026-09-08) because CTEs, CASE and explicit casts leaked
  through it.
- **Registry** (`ztype.dictionaries`) is append-only, slots 1–65535 never
  reused, names unique and never all digits. Registered dictionaries are
  permanent because frames reference them by zstd dictionary ID. It is
  declared `WITH (user_catalog_table = true)`: logical decoding runs a
  published value's output function under a historic snapshot, which sees only
  rows written by transactions marked as catalog-changing, so without the
  reloption a walsender cannot resolve a dictionary registered inside the
  decoded WAL window and the whole subscription stalls on "dictionary ID ...
  is not available" (measured 2026-09-09; `tests/test_replication.py` fails
  exactly there without it). Do not drop it; the only cost is that a logical
  slot's `catalog_xmin` also holds back vacuum on this table.
- **Lenient vs strict dictionary lookup.** Logical writes (type input, the
  three assignment casts) pass `missing_ok = true` to `zt_dictionary`: a slot
  that is not visible yields dictionary-free storage plus one `WARNING` per
  slot per transaction. Decoding, `ztype.recompress` and name lookups are
  strict. This is also what makes a logical subscriber work whose registry
  lags: the apply worker and the initial copy are logical writes with the
  subscriber column's typmod. This is what makes `pg_dump`/`pg_restore` work with no settings;
  do not reintroduce a restore-mode GUC. The alternative considered and
  rejected in review (2026-09-08) was storing each dictionary as a
  SQL function so pg_dump would order it before the tables: function bodies
  are world-readable in `pg_proc` regardless of EXECUTE, domains sort before
  functions in a dump, integrity would move from constraints to DDL
  conventions, and logical replication does not carry DDL at all.
- **Misses are cached per snapshot content** (xmin, xmax, hash of the
  in-progress xid list) plus command id, never by
  `snapXactCompletionCount`, which `CopySnapshot` zeroes on every pushed
  snapshot. The miss context lives one transaction (a `TopTransactionContext`
  child, separate from the session cache) and is dropped on savepoint abort.
  The two-session read-only regression in the suite is the behaviour boundary.
- **Privileges.** Codec entry points and `ztext_typmod_in` are
  `SECURITY DEFINER` with a pinned `search_path` and execute only fixed
  registry lookups. Registration (`add_dictionary`, `import_dictionary`) is
  `SECURITY DEFINER` too (decided 2026-09-09; until then it kept caller
  privileges and delegation meant `SELECT, INSERT, UPDATE` on the registry,
  which handed the role the bytes): the bodies are fixed SQL with the
  arguments as data, so `EXECUTE` is the whole delegation grant and the
  authorization check *is* the `EXECUTE` privilege, revoked from `PUBLIC` in
  the script. Training (`train_dictionary`, and `train_and_add`, which calls
  the other two as the caller) keeps caller privileges and must: its query
  runs with the caller's rights, and a definer there would read any table.
  Dictionary bytes must never become readable through a new path; they can
  contain training data, and no registration function returns them.
  `delegation()` in the suite pins the grant list, that each function in it
  is load-bearing, and that the role still cannot read, count or write the
  registry; the `COPY` import path is unchanged (`INSERT` plus `EXECUTE` on
  `dict_id`, because the `CHECK` runs as the inserting role). Anything new
  that reads the registry as the definer must return no more than the
  inventory view does. `tools/ztype-sync` keeps to this: it writes nothing
  but `ztype.import_dictionary` calls and the sweep, in one transaction (a
  dry run is the same transaction rolled back), reads bytes only on the source
  as a role that already may, never on the target, and otherwise reads the two
  PUBLIC views. It never picks a slot and never removes or overwrites a row.
- **Training samples are stored bytes** (text, bytea or jsonb varlena
  slices). Rejecting the compressed types as training input is deliberate.
- **Parallel safety.** Everything that only reads the registry through SPI
  and backend-local caches is `PARALLEL SAFE`; training, registration,
  `reload_dictionaries` and `dictionary_cache_stats` are not labelled and must
  stay that way. A new function that writes, or depends on session state,
  must not be marked safe.
- **Registry lookups are saved generic plans.** `zt_plan` prepares each of the
  four fixed queries (`zt_dictionary` by slot and by ID, `zt_slot_by_name`,
  `ztype_inspect`) once per backend with `CURSOR_OPT_GENERIC_PLAN` and
  `SPI_keepplan`; the plan cache invalidates and replans them when the registry
  changes, which `saved_plans` in the suite pins across `ALTER TABLE` and
  `DROP`/`CREATE EXTENSION` in one session. Privileges are checked per
  execution, so a plan is not a grant. Add a new registry query the same way.
- **Dictionary cache is backend-local, provisional until commit, and bounded.**
  Entries live in a `TopMemoryContext` child for the session. Each records the
  backend transaction counter `zt_gen` and the subtransaction that loaded it;
  `zt_xact` bumps the counter at every transaction end and, on abort or
  `PREPARE TRANSACTION`, first drops the entries of the finishing transaction;
  `zt_subxact` drops those with a subtransaction ID at or above the aborted one.
  A promoted entry is sound because it was loaded under a snapshot of this
  backend that saw the committed row, registered dictionaries are immutable and
  slots are never reused; the provisional rule exists so a registration this
  backend rolled back (or prepared and never committed) cannot be written by a
  later statement. Misses stay transaction-local in their own context. A hit
  is never provisional. `reload_dictionaries()` is the flush. Measured
  2026-09-09 (`make bench-latency`): the per-transaction reload of a 110 kB
  dictionary cost about 115 µs per point read and 295 µs per insert, against
  15-30 µs warm. `ztype.dictionary_cache_size`
  is the only GUC (a budget per backend, never a policy or a restore switch).
  Codec working memory has no GUC either, decided 2026-09-09 for 1.0: the
  level in the modifier is the lever, the README "Working memory" table is the
  ceiling per level, and whoever may declare `ztext(22)` on a column grants
  that memory; a cap would be a second setting that only refuses a modifier
  someone was allowed to declare, and nothing asks for it. The list is
  most-recently-used first; `zt_cache_reserve` evicts from the tail and never
  the entry passed as `keep`. Accounting is exact: an entry's `size` is the
  struct, the dictionary datum, `ZSTD_sizeof_DDict` and, once compiled,
  `ZSTD_sizeof_CDict`, and every path that creates, swaps or frees one of
  those updates `cache_bytes` in the same place. A `ZtDict *` is valid only
  until the next `zt_dictionary` call, which may evict it: look up, use, and
  never hold one across another lookup or a decode. Nothing native is held
  while a load can still error. `dictionary_cache_stats` is the test's oracle;
  keep it cheap and backend-local.
- **Decompression context is backend-cached.** `zt_dctx_acquire` resets the
  one `ZSTD_DCtx` (session and parameters) before every use, which also drops
  the previous `DDict` reference without dereferencing it: the entry it points
  at may have been evicted since. Release runs from `PG_FINALLY`, never
  errors, and frees the context when a call grew it past `ZT_DCTX_KEEP`.
  Never call zstd on `zt_dctx` outside acquire/release.
- **Full decodes use zstd's stable output buffer** (`ZSTD_d_stableOutBuffer`,
  an experimental-section parameter, hence `ZSTD_STATIC_LINKING_ONLY` and the
  libzstd 1.5 floor): the datum is the window, so `ob.size` is fixed at
  `rawlen` for the whole loop and must never be adjusted between calls, and
  interrupt checks come from stepping the *input* (`ZT_DECODE_STEP` of output
  per step at the frame's average ratio, at least 64 input bytes). `prefix`
  keeps the old output-bounded streaming because its output is partial.
- **Decode cache is two entries, keyed by stored bytes.** `zt_decode_cached`
  hits only on a byte-identical envelope (header included, so kind and level
  are part of the key), moves the hit to the front and returns a fresh copy;
  `zt_decode_remember` runs only after every check passed, including
  `pg_verify_mbstr`, in one allocation in `TopMemoryContext`, and releases the
  least recently used entry. It is invisible by construction because decode
  is a pure function of the bytes: registered dictionaries are permanent and
  named by ID inside the frame. Two entries, not one, since 2026-09-10: the
  two operands of a binary operator then both stay cached, which is what
  makes `col = constant` decode the constant once per statement rather than
  once per row (measured in `make bench-hash`: 2.00 decodes per row with one
  entry). Values above `ZT_DECODE_CACHE_MAX` raw bytes are not kept; the
  memory test bounds the retention. `decode_cache_stats` is the test's oracle.
- **Equality and hashing are the base type's, on the decoded value; there is
  no ordering.** `zt_equal` decides from the envelope when the stored bytes
  are identical (equal) or, for text and bytea, the declared lengths differ
  (unequal), and otherwise decodes both sides: text is bytewise (ztext is not
  collatable, so it compares as text under the C collation), bytea bytewise,
  jsonb through `jsonb_eq`. `zt_hash` is `hash_any` over the decoded bytes for
  text and bytea (what `hashtext` computes under a deterministic collation)
  and `jsonb_hash` for jsonb, so equal logical values hash alike under any
  policy; the suite pins the agreement with `hashtext` and `jsonb_hash`. The
  functions are `SECURITY DEFINER` like the casts, because a comparison may
  have to load a dictionary. No btree operator class, decided 2026-09-10 with
  `make bench-hash` behind it: hashing costs one decode per row, the same as
  the cast, but a sort on the compressed type would decode two values per
  comparison, so `ORDER BY body` is refused and `ORDER BY body::text` is the
  fast form. Do not add `<`. Every type has its own `typanalyze`
  (`zt_typanalyze`): without an ordering operator `std_typanalyze` would count
  distinct values pairwise with the equality function (31 s against 0.14 s for
  jsonb on 20,000 rows, measured 2026-09-10), so instead each sample row is
  decoded once (never one whose envelope declares more than `ZT_ANALYZE_WIDE`
  bytes; those count as distinct, as the standard analyzer counts too-wide
  values) and the base type's analyzer runs over the decoded values through a
  fetch function of ours. Only the null fraction, the stored width, the
  distinct estimate and the most-common-value slot reach `pg_statistic`, the
  values re-encoded under the column's policy and `staop` set to the type's
  `=`; histogram and correlation are dropped. Results must be allocated in
  `stats->anl_context` (`compute_stats` runs in a per-column context reset
  before `pg_statistic` is written; the first version crashed there).
- **Codec loops** check `CHECK_FOR_INTERRUPTS` between steps: `ZT_CHUNK` of
  input for compression and prefix, ratio-sized input steps for full decodes;
  the memory test measures cancel latency and backend RSS.

## Conventions

- One coherent change per commit, with the test that proves it. Tests are
  behaviour boundaries, not mirrors of the implementation.
- Comments above functions say how they are used; update them with the code.
- Errors are `ereport` with a proper `errcode` and a `ztype:` prefix; data
  problems are `ERRCODE_DATA_CORRUPTED`, unknown formats
  `ERRCODE_FEATURE_NOT_SUPPORTED`, missing objects `ERRCODE_UNDEFINED_OBJECT`.
- Keep README.md the single user-facing document and keep it accurate:
  a claim that was not measured or tested here does not go in.

## Gotchas that have already cost time

- A base backup copies the primary's `synchronized_standby_slots`. A standby
  promoted with it still set waits for a physical slot it does not have, and
  every failover-slot walsender on it blocks: the repointed subscription
  never receives a row, the log says the slot "specified in parameter
  synchronized_standby_slots does not exist". `test_replication_ha.py`
  overrides it to `''` on both adopted data directories; measured 2026-09-09
  (the run without the override times out waiting for the first row).
- `add_dictionary` allocates `max(slot) + 1` over the whole registry, so a
  node that has imported another node's high slot allocates inside that
  node's range next. That is why two-way setups register with explicit slots
  through `import_dictionary` on both sides (README "Slot ranges"; `twoway`
  in the suite pins the pitfall with a rolled-back `train_and_add`).
- A published relation must exist on the subscriber before any row for it is
  decoded, refreshed or not: the apply worker resolves the remote name
  locally first and errors with "logical replication target relation does
  not exist", and the subscription restarts on that error until the table is
  there. Create the subscriber's table before writing to a newly published one.

- On macOS, PostgreSQL refuses to start from a shell with no locale
  ("postmaster became multithreaded"). The harness sets `LC_ALL=C`; keep it.
- Unix socket paths are limited to about 100 bytes; the harness uses the
  system temp dir, not a deep working directory.
- In size assertions, compare only values large enough to be compressed:
  short raw values differ by varlena header width alone (1 byte on disk,
  4 bytes in memory).
- A datum copied from another table keeps its source TOAST method. The
  benchmark re-materialises through `::text` so `COMPRESSION pglz/lz4`
  actually applies.
- A SQL aggregate whose arguments reference only outer-query columns is
  attached to the outer query. Put the row-producing part in a subquery.
- `pg_upgrade` dumps the old cluster's schema, and `format_type` calls our
  `typmod_out`, so the old server loads the extension during the upgrade:
  each major needs its own build under the same module name.
- Running an external CLI from a harness: close stdin
  (`</dev/null`) or they can wait forever on it.
- Rebuilding `ztype.dylib` while a harness cluster (bench, cross-major) has it
  mapped can crash that cluster; build sanitizer or experimental variants in
  a copy of the tree, as `tests/test_cross_major.py` does.
- UBSan builds: `make PG_CFLAGS="-fsanitize=undefined
  -fno-sanitize-recover=all" PG_LDFLAGS="-fsanitize=undefined"`, with
  `CC=clang` on macOS and gcc on Linux: clang's Linux UBSan runtime is a
  static archive that never reaches a shared module, so the module fails to
  load with `undefined symbol: __ubsan_handle_...` (first CI run,
  2026-09-10); gcc links its shared libubsan. Passing `SHLIB_LINK` on the
  command line replaces the Makefile's `-lzstd`.
- PostgreSQL betas on apt.postgresql.org live in the `<codename>-pgdg-testing`
  suite under the major's component (`Components: main 19`), not in the main
  suite, and that suite is `NotAutomatic` (priority 100), so `apt-get install
  -t <codename>-pgdg-testing` is needed or the main suite's `libpq5`/`libpq-dev`
  18 block `postgresql-server-dev-19`; `ci.yml` does both for 19 until the
  release lands.
- `scan-build` fails silently under PGXS on macOS; run
  `clang --analyze` directly with the flags from `make -B -n ztype.o`.
- **ASan runtimes before clang 18 on a recent kernel**: with 32 bits of mmap
  randomisation (Linux 6.5+, and the Proxmox 7.x kernel on the `dev` box) the
  preloaded runtime of clang 16 CHECK-fails at random during postmaster start:
  the server log is nothing but `AddressSanitizer:DEADLYSIGNAL` repeated, no
  report file, and the same command works on the next try. It is the LLVM
  shadow-mapping bug fixed in 18; `ASAN_CC=clang-19` (Debian 13, needs
  `libclang-rt-19-dev`) or a `vm.mmap_rnd_bits=28` sysctl avoids it. Ubuntu
  24.04's `clang` in CI is 18. Seen 2026-09-09 on the first Linux run of
  `make test-asan-suite`.
- **ASan into a stock server**: only the module is instrumented, so the
  runtime must be preloaded before the server's own libraries. `make
  test-asan` does this (`DYLD_INSERT_LIBRARIES`/`LD_PRELOAD` +
  `ZTYPE_SANITIZER_RUNTIME`) and launches the postmaster directly — **not**
  through `pg_ctl start`, because macOS SIP strips `DYLD_*` from the `/bin/sh`
  that `pg_ctl` execs, silently leaving the run without its runtime. The
  positive control (`tests/asan_probe.c`) fails the run if the runtime is not
  actually live.
- **macOS ASan + PostgreSQL's `siglongjmp`**: Apple's and Homebrew's ASan
  runtimes CHECK-fail (`asan_poisoning.cpp` / `PlatformUnpoisonStacks` on
  `__asan_handle_no_return`) on the *second* `ereport`-longjmp through an
  instrumented frame in one forked backend. It is a platform artefact, not a
  ztype defect (a fresh backend per error avoids it entirely). This is why
  `make test-asan` runs the *mutation harness* (which isolates each erroring
  operation in its own backend) and not the many-errors-per-session functional
  suite. The harness also recognises the artefact stack and discounts it; on
  Linux it does not occur. The functional suite's sanitizer coverage is UBSan.
- A backend SIGSEGV (a native-jsonb container crash) makes the postmaster
  recycle every backend, so a persistent observer connection dies too; the
  mutation harness's shared session reconnects lazily (`s_query`).
