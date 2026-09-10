#!/usr/bin/env python3
"""Run every benchmark behind the README tables in one go and keep what they printed.

Each benchmark prints Markdown and nothing else keeps it, so a README number has no record of
the run behind it, and a benchmark nobody has run since the last change to the harness can rot
unnoticed. This runner runs them all, in order, and writes one directory per run under
`results/bench/`: the Markdown each benchmark printed (`<name>.md`), its diagnostics (`<name>.log`)
and `manifest.json` with what was measured under: git revision and whether the tree was dirty,
PostgreSQL and libzstd versions, platform, mode, the size overrides in effect, and each
benchmark's wall time and status. Every benchmark runs even after one fails; the exit status is
non-zero if any did.

Two modes. The default is the full run whose numbers go into the README (allow an hour; level 22
on a 128 MB value dominates). `--smoke` runs the same scripts at sizes that finish in a few
minutes: it proves the benchmarks still run against the current build, and its numbers mean
nothing; the manifest says so. `--only name,name` restricts the set (names as in the table below).

    python3 tests/bench_all.py [--smoke] [--only zjsonb,latency] [--out DIR]   # PG_CONFIG=... picks the installation
"""
import argparse
import datetime
import json
import os
from pathlib import Path
import platform
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
PG_CONFIG = os.environ.get('PG_CONFIG', 'pg_config')

# name, command (relative to ROOT), smoke-mode environment, smoke-mode extra arguments. The full run
# uses each script's defaults.
BENCHES = [
    ('codec', ['make', '-s', 'bench-codec'], {}, []),
    ('zjsonb', [sys.executable, 'tests/bench_zjsonb.py'],
     {'ZTYPE_BENCH_SMALL': '2000', 'ZTYPE_BENCH_LARGE': '100', 'ZTYPE_BENCH_RANDOM': '5000', 'ZTYPE_BENCH_RUNS': '1'}, []),
    ('params', [sys.executable, 'tests/bench_params.py'], {'ZTYPE_BENCH_PARAMS': '500', 'ZTYPE_BENCH_RUNS': '1'}, []),
    ('latency', [sys.executable, 'tests/bench_latency.py'],
     {'ZTYPE_BENCH_LATENCY': '200', 'ZTYPE_BENCH_RECONNECTS': '5', 'ZTYPE_BENCH_RUNS': '1'}, []),
    ('rewrite', [sys.executable, 'tests/bench_rewrite.py'],
     {'ZTYPE_BENCH_REWRITE_SMALL': '2000', 'ZTYPE_BENCH_REWRITE_LARGE': '50', 'ZTYPE_BENCH_RUNS': '1'}, []),
    ('memory', [sys.executable, 'tests/bench_memory.py'], {'ZTYPE_BENCH_MEMORY_QUICK': '1', 'ZTYPE_BENCH_MEMORY_SMOKE': '1'}, []),
    ('hash', [sys.executable, 'tests/bench_hash.py'], {'ZTYPE_BENCH_HASH': '4000', 'ZTYPE_BENCH_RUNS': '1'}, []),
    ('dict-report', [sys.executable, 'tests/dict_report.py', '--synthetic', 'small'], {}, ['--rows', '2000', '--runs', '1']),
]


def git(*args):
    try:
        return subprocess.check_output(['git', *args], cwd=ROOT, text=True, stderr=subprocess.DEVNULL).strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        return None


def environment():
    def out(args):
        try:
            return subprocess.check_output(args, text=True, stderr=subprocess.DEVNULL).strip()
        except (subprocess.CalledProcessError, FileNotFoundError):
            return None
    return {'postgresql': out([PG_CONFIG, '--version']), 'pg_config': PG_CONFIG,
            'zstd': out(['pkg-config', '--modversion', 'libzstd']),
            'platform': platform.platform(), 'machine': platform.machine(), 'python': platform.python_version(),
            'git_head': git('rev-parse', 'HEAD'), 'git_dirty': bool(git('status', '--porcelain'))}


def main():
    ap = argparse.ArgumentParser(description=__doc__.split('\n\n')[0])
    ap.add_argument('--smoke', action='store_true', help='tiny sizes: proves the benchmarks run, numbers meaningless')
    ap.add_argument('--only', help='comma-separated benchmark names (default: all)')
    ap.add_argument('--out', help='results directory (default results/bench/<UTC stamp>-<mode>)')
    args = ap.parse_args()
    names = [n for n, *_ in BENCHES]
    wanted = args.only.split(',') if args.only else names
    unknown = sorted(set(wanted) - set(names))
    assert not unknown, f'unknown benchmark(s) {unknown}; choose from {names}'
    mode = 'smoke' if args.smoke else 'full'
    started = datetime.datetime.now(datetime.timezone.utc)
    out = Path(args.out) if args.out else ROOT / 'results' / 'bench' / f"{started.strftime('%Y%m%dT%H%M%SZ')}-{mode}"
    out.mkdir(parents=True, exist_ok=True)
    manifest = {'mode': mode, 'started': started.isoformat(timespec='seconds'), **environment(),
                'note': ('smoke run: sizes chosen to finish quickly; the numbers are not measurements'
                         if args.smoke else 'full run at each benchmark\'s default sizes'),
                'benchmarks': []}
    failed = []
    for name, base_cmd, smoke_env, smoke_args in BENCHES:
        if name not in wanted:
            continue
        env = dict(os.environ, PG_CONFIG=PG_CONFIG)
        overrides = smoke_env if args.smoke else {}
        env.update(overrides)
        cmd = base_cmd + (smoke_args if args.smoke else [])
        print(f'== {name}: {" ".join(cmd)}' + (f'  ({" ".join(f"{k}={v}" for k, v in overrides.items())})' if overrides else ''),
              flush=True)
        t0 = time.monotonic()
        with (out / f'{name}.md').open('w') as md, (out / f'{name}.log').open('w') as log:
            rc = subprocess.run(cmd, cwd=ROOT, env=env, stdout=md, stderr=log, stdin=subprocess.DEVNULL).returncode
        secs = time.monotonic() - t0
        entry = {'name': name, 'command': cmd, 'env': overrides, 'seconds': round(secs, 1), 'status': 'ok' if rc == 0 else f'exit {rc}'}
        manifest['benchmarks'].append(entry)
        (out / 'manifest.json').write_text(json.dumps(manifest, indent=1) + '\n')  # after each, so a crash keeps the partial record
        print(f'   {entry["status"]} in {secs:,.0f} s -> {out / (name + ".md")}', flush=True)
        if rc:
            failed.append(name)
            print((out / f'{name}.log').read_text()[-3000:], file=sys.stderr, flush=True)
    latest = out.parent / 'latest'
    if latest.is_symlink() or latest.exists():
        latest.unlink()
    latest.symlink_to(out.name)
    total = sum(b['seconds'] for b in manifest['benchmarks'])
    print(f'\n{mode} run: {len(manifest["benchmarks"]) - len(failed)} of {len(manifest["benchmarks"])} benchmarks ok '
          f'in {total:,.0f} s; results in {out}')
    if failed:
        print(f'FAILED: {", ".join(failed)}', file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
