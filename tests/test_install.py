#!/usr/bin/env python3
"""The extension as `make install` ships it, loaded the way a packaged installation loads it.

`make test` rewrites the control file to point at the tree's own library and copies the SQL script
into a scratch directory, so nothing in it proves that the installed files work: that `make install`
puts the control file, the script and the module where the server looks, that the control file's
`module_pathname = '$libdir/ztype'` resolves, that the shipped script creates the extension, and that
`make uninstall` takes everything back out. This does, against a staged tree: `make install
DESTDIR=<scratch>` (never the real installation; on this machine that would be a shared cluster's),
then a disposable cluster whose `extension_control_path` and `dynamic_library_path` name the staged
directories exactly as a package would have populated them.

Checks: the installed file set is the control file, one SQL script per version the control file
names, the module and the registry transport `ztype-sync` in the bin directory (executable, and its
`--help` runs from the staged tree), plus at most the LLVM bitcode PGXS emits on builds with JIT; the staged control
file is byte-identical to the tree's and its `default_version` matches `ZT_VERSION`; the server lists
the extension with that one version and no update path; `CREATE EXTENSION` from the installed script
loads the module through `$libdir/ztype` (`probin` stays exactly that string, so a dump restores on
another installation); one dictionary round trip per type; `DROP EXTENSION` leaves no schema behind;
`make uninstall DESTDIR=<scratch>` removes every installed file.

ZTYPE_INSTALL_ROOT=<dir> checks an already staged tree (a package's DESTDIR) instead of running
`make install`, and skips the uninstall step.

    python3 tests/test_install.py            # PG_CONFIG=... picks the installation; `make test-install`
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import BIN, Cluster, PG_CONFIG, ROOT, run  # noqa: E402

PORT = 55481


def pg_dir(flag):
    return Path(subprocess.check_output([PG_CONFIG, flag], text=True).strip())


def installed_files(root):
    return sorted(p.relative_to(root) for p in root.rglob('*') if p.is_file())


def main():
    sharedir, pkglibdir, bindir = pg_dir('--sharedir'), pg_dir('--pkglibdir'), pg_dir('--bindir')
    control = (ROOT / 'ztype.control').read_text()
    version = re.search(r"^default_version\s*=\s*'([^']+)'", control, re.M).group(1)
    scripts = sorted(p.name for p in ROOT.glob('ztype--*.sql'))
    assert f'ztype--{version}.sql' in scripts, (version, scripts)
    staged = os.environ.get('ZTYPE_INSTALL_ROOT')
    with tempfile.TemporaryDirectory(prefix='ztype-install-') as tmp:
        work = Path(tmp)
        root = Path(staged).resolve() if staged else work / 'root'
        if not staged:
            run(['make', '-C', ROOT, 'install', f'DESTDIR={root}', f'PG_CONFIG={PG_CONFIG}'])
        share = root / sharedir.relative_to('/')
        lib = root / pkglibdir.relative_to('/')
        # What a package would carry: nothing missing, nothing stray.
        files = installed_files(root)
        module = [f for f in files if f.parent == pkglibdir.relative_to('/') and f.stem == 'ztype' and f.suffix in ('.so', '.dylib', '.dll')]
        assert len(module) == 1, ('one module expected in the staged pkglibdir', files)
        expected = {sharedir.relative_to('/') / 'extension' / 'ztype.control', module[0]}
        expected |= {sharedir.relative_to('/') / 'extension' / s for s in scripts}
        tool = bindir.relative_to('/') / 'ztype-sync'
        expected.add(tool)
        stray = [f for f in files if f not in expected and 'bitcode' not in f.parts]
        assert set(files) >= expected and not stray, (sorted(str(f) for f in files), sorted(str(f) for f in expected), stray)
        assert (share / 'extension' / 'ztype.control').read_text() == control, 'installed control file differs from the tree'
        assert (share / 'extension' / f'ztype--{version}.sql').read_bytes() == (ROOT / f'ztype--{version}.sql').read_bytes()
        assert (root / tool).read_bytes() == (ROOT / 'tools' / 'ztype-sync').read_bytes()
        assert os.access(root / tool, os.X_OK), f'{tool} is not executable'
        usage = subprocess.run([str(root / tool), '--help'], capture_output=True, text=True)
        assert usage.returncode == 0 and '--source' in usage.stdout and '--target' in usage.stdout, usage
        print(f'PASS: make install stages {len(expected)} files (module {module[0].name}, control, {len(scripts)} script(s), ztype-sync)'
              + (f' and {len(files) - len(expected)} bitcode file(s)' if len(files) > len(expected) else ''), flush=True)

        # The staged directories as the server would see them from a real package: the shipped
        # control file untouched, the module found through dynamic_library_path.
        cluster = Cluster(BIN, work, 'pg', PORT, share, libdir=lib)
        cluster.start()
        try:
            s = cluster.session()
            s.equal("SELECT default_version, installed_version IS NULL FROM pg_available_extensions WHERE name = 'ztype';", f'{version}|t')
            s.equal("SELECT string_agg(version, ',' ORDER BY version), bool_and(NOT relocatable AND schema = 'public') "
                    "FROM pg_available_extension_versions WHERE name = 'ztype';", f'{version}|t')
            s.equal("SELECT count(*) FROM pg_extension_update_paths('ztype') WHERE path IS NOT NULL;", '0')
            s.query('CREATE EXTENSION ztype;')
            s.equal("SELECT extversion = (SELECT library FROM ztype.build_info()), extversion FROM pg_extension WHERE extname = 'ztype';",
                    f't|{version}')
            # Every C function names the module as the control file does, never a staged absolute path.
            s.equal("SELECT count(*) > 0, bool_and(probin = '$libdir/ztype') FROM pg_proc p JOIN pg_depend d ON d.objid = p.oid "
                    "JOIN pg_extension e ON e.oid = d.refobjid WHERE e.extname = 'ztype' AND d.deptype = 'e' AND p.prolang = "
                    "(SELECT oid FROM pg_language WHERE lanname = 'c');", 't|t')
            s.equal(t.training('installed'), '1')
            s.query("CREATE TABLE installed (id int, a ztext(6, 'installed'), b zjsonb(6, 'installed'), c zbytea(6, 'installed'));")
            s.query("INSERT INTO installed SELECT i, repeat('installed sample subject ' || i, 40), "
                    "jsonb_build_object('subject', repeat('installed sample subject ', 40), 'i', i), "
                    "convert_to(repeat('installed sample subject ', 40), 'UTF8') FROM generate_series(1, 20) i;")
            s.equal("SELECT count(*) FILTER (WHERE a::text = repeat('installed sample subject ' || id, 40)), "
                    "count(*) FILTER (WHERE (b ->> 'i')::int = id AND b::jsonb ->> 'subject' = repeat('installed sample subject ', 40)), "
                    "count(*) FILTER (WHERE convert_from(c::bytea, 'UTF8') = repeat('installed sample subject ', 40)) FROM installed;",
                    '20|20|20')
            s.equal("SELECT count(*) FROM installed, LATERAL ztype.inspect(a) ia, LATERAL ztype.inspect(b) ib, LATERAL ztype.inspect(c) ic "
                    "WHERE ia.dict_name = 'installed' AND ib.dict_name = 'installed' AND ic.dict_name = 'installed' "
                    "AND ia.codec = 'zstd' AND ib.codec = 'zstd' AND ic.codec = 'zstd';", '20')
            s.equal("SELECT count(*) FROM installed WHERE ztype.validate(a) IS NULL AND ztype.validate(b) IS NULL AND ztype.validate(c) IS NULL;", '20')
            s.query('DROP TABLE installed; DROP EXTENSION ztype;')
            s.equal("SELECT count(*) FROM pg_namespace WHERE nspname = 'ztype';", '0')
            s.equal("SELECT count(*) FROM pg_type WHERE typname IN ('ztext', 'zjsonb', 'zbytea');", '0')
            s.close()
            cluster.check_reports()
        except BaseException:
            print(cluster.diagnostics())
            raise
        finally:
            cluster.stop('immediate')
        print(f'PASS: installed extension {version} loads through $libdir/ztype, round-trips three dictionary columns and drops clean',
              flush=True)

        if not staged:
            run(['make', '-C', ROOT, 'uninstall', f'DESTDIR={root}', f'PG_CONFIG={PG_CONFIG}'])
            left = installed_files(root)
            assert not left, ('make uninstall left files behind', [str(f) for f in left])
            print('PASS: make uninstall removes every staged file', flush=True)


if __name__ == '__main__':
    main()
