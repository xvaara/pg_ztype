MODULE_big = ztype
OBJS = ztype.o
EXTENSION = ztype
DATA = ztype--0.9.sql
# The registry transport, installed next to psql. A built copy at the top level, because PGXS
# uninstalls SCRIPTS under their source path (tools/ztype-sync) and would leave the file behind.
SCRIPTS_built = ztype-sync

# Override ZSTD_CFLAGS/ZSTD_LIBS for installations without pkg-config metadata.
PG_CONFIG ?= pg_config
PKG_CONFIG ?= pkg-config
ZSTD_CFLAGS ?= $(shell $(PKG_CONFIG) --cflags libzstd 2>/dev/null)
ZSTD_LIBS ?= $(shell $(PKG_CONFIG) --libs libzstd 2>/dev/null || echo -lzstd)
PG_CPPFLAGS += $(ZSTD_CFLAGS)
SHLIB_LINK += $(ZSTD_LIBS)
PGXS := $(shell $(PG_CONFIG) --pgxs)
include $(PGXS)

ztype-sync: tools/ztype-sync
	cp '$<' '$@' && chmod +x '$@'

# The registry transport from the tree: make sync-dictionaries SYNC_ARGS='--source ... --target ...'
sync-dictionaries:
	python3 tools/ztype-sync $(SYNC_ARGS)

# Creates its own socket-only cluster and installs into a temporary directory.
.PHONY: sync-dictionaries test test-install test-mutate asan-build test-asan test-asan-suite test-cross test-replication test-replication-ha bench bench-all bench-smoke bench-codec bench-params bench-latency bench-rewrite bench-memory bench-hash dict-report
test: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/test_ztype.py

# The files `make install` ships, staged under a scratch DESTDIR (never the real installation) and
# loaded by a disposable server exactly as a package would be: the untouched control file, the
# module through $libdir/ztype, the installed script; then `make uninstall` must leave nothing.
test-install: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/test_install.py

# Publisher, logical subscriber and physical standby, all disposable: dictionary columns through
# logical replication (pre-seeded and published registry, text and binary apply, a subscriber
# modifier of its own, a slot conflict) and through a hot standby's read-only decoding.
test-replication: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/test_replication.py

# Failover and promotion, four disposable clusters: a promoted physical standby, a logical
# subscription following it through a synchronised failover slot, and pg_createsubscriber.
test-replication-ha: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/test_replication_ha.py

# Seed-reproducible mutation harness against the built library (MUTATE_ARGS='--seed 7 --iterations 500').
test-mutate: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/mutate_ztype.py $(MUTATE_ARGS)

# AddressSanitizer: builds a sanitized copy in $(ASAN_DIR), never the tree's own library (a bench or
# cross-major cluster may have that one mapped), preloads the runtime into a disposable server and
# runs the mutation harness (with a positive control that proves the runtime is live). The harness
# isolates each erroring operation in its own backend, because macOS's ASan runtime CHECK-fails on a
# second ereport-longjmp in one forked backend; that isolation is why the mutation harness, not the
# many-errors-per-session functional suite, is the ASan target here. SANITIZE=address,undefined adds
# UBSan. MUTATE_ARGS passes through, e.g. MUTATE_ARGS='--seed 7 --iterations 800'.
ASAN_DIR ?= build/asan
ASAN_CC ?= clang
SANITIZE ?= address
ASAN_RUNTIME ?= $(shell python3 tests/sanitizer_runtime.py '$(ASAN_CC)')
asan-build:
	@test -n '$(ASAN_RUNTIME)' || { echo 'no AddressSanitizer runtime found for $(ASAN_CC); set ASAN_RUNTIME=/path/to/libclang_rt.asan*.{so,dylib}' >&2; exit 1; }
	mkdir -p '$(ASAN_DIR)/tools' && cp Makefile ztype.c ztype.control ztype--0.9.sql '$(ASAN_DIR)/' && cp tools/ztype-sync '$(ASAN_DIR)/tools/'
	$(MAKE) -C '$(ASAN_DIR)' PG_CONFIG='$(PG_CONFIG)' CC='$(ASAN_CC)' \
	  PG_CFLAGS='-fsanitize=$(SANITIZE) -fno-sanitize-recover=undefined -fno-omit-frame-pointer -g -O1' PG_LDFLAGS='-fsanitize=$(SANITIZE)'
test-asan: asan-build
	PG_CONFIG='$(PG_CONFIG)' ZTYPE_LIBRARY='$(ASAN_DIR)/ztype$(DLSUFFIX)' ZTYPE_SANITIZER_RUNTIME='$(ASAN_RUNTIME)' ZTYPE_CC='$(ASAN_CC)' \
	  python3 tests/mutate_ztype.py $(MUTATE_ARGS)

# The functional suite under the same sanitized build. Linux only: the suite raises many errors per
# backend, and macOS's ASan runtime CHECK-fails on the second ereport-longjmp in one backend (see
# CLAUDE.md). The suite skips its RSS bounds under an instrumented allocator and runs everything else.
test-asan-suite: asan-build
	PG_CONFIG='$(PG_CONFIG)' ZTYPE_LIBRARY='$(ASAN_DIR)/ztype$(DLSUFFIX)' ZTYPE_SANITIZER_RUNTIME='$(ASAN_RUNTIME)' ZTYPE_CC='$(ASAN_CC)' \
	  python3 tests/test_ztype.py

# Two majors side by side: raw bytes both ways, logical dump/restore and pg_upgrade.
# Builds its own copies, so it needs no prior make. NEW_PG_CONFIG is required.
test-cross:
	ZTYPE_OLD_PG_CONFIG='$(PG_CONFIG)' ZTYPE_NEW_PG_CONFIG='$(NEW_PG_CONFIG)' python3 tests/test_cross_major.py

# Size benchmark behind the README table: jsonb versus zjsonb on ERP-style documents.
bench: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_zjsonb.py

# Every benchmark below in one run, each one's Markdown and a manifest (git revision, versions,
# platform, sizes, wall times) kept under results/bench/<stamp>-<mode>/. bench-all is the full run
# behind the README numbers (allow an hour); bench-smoke runs the same scripts at tiny sizes in a few
# minutes to prove they still run against the current build, and measures nothing worth quoting.
# BENCH_ARGS passes through, e.g. BENCH_ARGS='--only latency,memory'.
bench-all: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_all.py $(BENCH_ARGS)
bench-smoke: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_all.py --smoke $(BENCH_ARGS)

# Parameter inserts over libpq: the double compression of an untyped parameter, per row.
bench-params: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_params.py

# Dictionary evaluation report on your own column (DICT_REPORT_ARGS='--source ... --query ...')
# or, by default, on the benchmark's small synthetic corpus.
DICT_REPORT_ARGS ?= --synthetic small
dict-report: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/dict_report.py $(DICT_REPORT_ARGS)

# libzstd-only microbenchmark: codec context creation versus a small-document encode and decode.
bench-codec:
	mkdir -p build
	$(CC) -O2 $(ZSTD_CFLAGS) -o build/codec_bench tests/codec_bench.c $(ZSTD_LIBS)
	./build/codec_bench

# Request latency over libpq: single statements as their own transactions against batched ones, and
# what the per-transaction dictionary reload costs (ZTYPE_BENCH_LATENCY=rows).
bench-latency: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_latency.py

# The equality and hash operator classes per row: GROUP BY on the column against the cast, a hash
# join, an equality filter and ANALYZE, for jsonb and text against their compressed forms
# (ZTYPE_BENCH_HASH=rows, ZTYPE_BENCH_HASH_REPEAT=copies of each distinct value).
bench-hash: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_hash.py

# Rewrite strategies for a policy change or a post-restore catch-up: ALTER COLUMN TYPE, one UPDATE,
# batched UPDATEs and CREATE TABLE AS, each with and without an expression index; time, WAL, heap
# and index size, HOT share (ZTYPE_BENCH_REWRITE_SMALL/LARGE=rows).
bench-rewrite: all
	PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_rewrite.py

# Native working memory outside work_mem: libzstd's own accounting per level, input size and
# dictionary size (tests/codec_memory.c), checked against a backend's peak resident size while it
# compresses, decodes, prefix-reads and trains (ZTYPE_BENCH_MEMORY_QUICK=1 skips the slow level-22 run).
bench-memory: all
	ZSTD_CFLAGS='$(ZSTD_CFLAGS)' ZSTD_LIBS='$(ZSTD_LIBS)' PG_CONFIG='$(PG_CONFIG)' python3 tests/bench_memory.py
