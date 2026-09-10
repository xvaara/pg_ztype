#!/usr/bin/env python3
"""Qualify logical replication and hot-standby reads for dictionary-compressed columns.

Three disposable clusters from one installation: a publisher with wal_level = logical, a
logical subscriber, and a physical standby taken from the publisher with pg_basebackup.

The two replication paths reach ztype from opposite ends. *Logical* replication never carries
a frame: the publisher's output (or send) function decodes and the subscriber's input (or
receive) function compresses again under the subscriber column's own modifier, exactly like a
dump and restore. So the subscriber decides the policy, a registry it does not have degrades to
dictionary-free with a WARNING instead of failing, and a slot that means a *different*
dictionary there is applied silently. *Physical* replication carries the stored bytes, so the
standby needs the same registry rows - which it has, because they are replayed with everything
else - and every read path must decode them on a read-only server.

The publisher side is the part that is not free: logical decoding calls the output function
inside a historic snapshot, which only sees rows written by transactions marked as
catalog-changing. That is why ztype.dictionaries is declared WITH (user_catalog_table = true);
without it a walsender cannot resolve a dictionary registered inside the decoded WAL window and
the subscription stalls on "dictionary ID ... is not available".
"""
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, ROOT, run, share, training  # noqa: E402

BIN = t.BIN
TOOL = ROOT / 'tools' / 'ztype-sync'
ROWS = 30
PUB_PORT, SUB_PORT, STANDBY_PORT, THIRD_PORT = 55451, 55452, 55453, 55454
WORKERS = 'max_worker_processes = 24\nmax_logical_replication_workers = 16\nmax_replication_slots = 20\nwal_retrieve_retry_interval = 1s\n'


def body(col):
    """A body well above the 64-byte compression floor, in the vocabulary training() trains on."""
    return f"repeat('first sample subject ' || {col} || ' ', 12)"


def equal_sql(n=ROWS, table='messages'):
    """Every replicated row equals the publisher's logical value, in all three types."""
    return (f"SELECT count(*) = {n} AND bool_and(body::text = {body('id')} "
            f"AND (meta ->> 'body') = {body('id')} "
            f"AND blob::bytea = convert_to({body('id')}, 'UTF8')) FROM {table};")


def clean_sql(table='messages'):
    return (f"SELECT count(*) = 0 FROM {table} WHERE ztype.validate(body) IS NOT NULL "
            f"OR ztype.validate(meta) IS NOT NULL OR ztype.validate(blob) IS NOT NULL;")


def policy_sql(table='messages', level=6, name="'first'"):
    return (f"SELECT bool_and(i.level = {level} AND i.dict_name IS NOT DISTINCT FROM {name}) "
            f"FROM {table} m, ztype.inspect(m.body) i;")


def wait(session, sql, expected='t', what='', timeout=120):
    """Replication is asynchronous; every assertion about the other side polls for its condition."""
    deadline = time.monotonic() + timeout
    while True:
        actual = session.query(sql)
        if actual == expected:
            return actual
        if time.monotonic() > deadline:
            raise AssertionError(f'timed out waiting for {what or sql}: got {actual!r}')
        time.sleep(0.2)


def wait_synced(session, subname, tables):
    """srsubstate 'r' is the end of the initial copy: from here the subscription is streaming."""
    wait(session, f"SELECT count(*) = {tables} AND bool_and(srsubstate = 'r') FROM pg_subscription_rel r "
                  f"JOIN pg_subscription s ON s.oid = r.srsubid WHERE s.subname = '{subname}';",
         what=f'initial sync of {subname}')


def log_mark(cluster):
    path = cluster.work / f'{cluster.data.name}.log'
    return len(path.read_text(errors='replace')) if path.exists() else 0


def log_since(cluster, mark):
    return (cluster.work / f'{cluster.data.name}.log').read_text(errors='replace')[mark:]


def subscribe(session, name, work, publication, options='', port=PUB_PORT, dbname='postgres'):
    session.query(f"CREATE SUBSCRIPTION {name} CONNECTION 'host={work} port={port} dbname={dbname}' "
                  f"PUBLICATION {publication}{options};")


def conninfo(work, port, dbname='postgres', user=None):
    return f'host={work} port={port} dbname={dbname}' + (f' user={user}' if user else '')


def tool(*args):
    """Run the shipped registry transport as a user would, from the tree, against the harness's
    psql. Returns (exit code, stdout, stderr); the environment carries no PG* connection settings
    beyond what the two conninfos say, so a wrong conninfo fails rather than falling back."""
    env = {k: v for k, v in os.environ.items() if not k.startswith('PG')}
    env['LC_ALL'] = 'C'
    r = subprocess.run([sys.executable, str(TOOL), '--psql', str(BIN / 'psql'), *map(str, args)],
                       env=env, capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout, r.stderr


def publisher_seed(p):
    """One dictionary, one 30-row table whose every value is dictionary-compressed, and a copy of
    it for the binary subscription. The copy is made datum for datum: both columns have the same
    modifier, so no coercion runs and the two tables hold identical stored bytes."""
    p.query('CREATE EXTENSION ztype;')
    p.equal(training('first'), '1')
    p.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'), "
            "meta zjsonb(6,'first'), blob zbytea);")
    p.query(f"INSERT INTO messages SELECT i, {body('i')}, jsonb_build_object('body', {body('i')}), "
            f"convert_to({body('i')}, 'UTF8') FROM generate_series(1,{ROWS}) i;")
    p.equal(policy_sql())
    p.equal('SELECT bool_and(pg_column_size(body) < octet_length(body::text)) FROM messages;')
    p.query('CREATE TABLE messages_bin (LIKE messages INCLUDING ALL);')
    p.query('INSERT INTO messages_bin SELECT * FROM messages;')
    p.query('CREATE PUBLICATION pub_tables FOR TABLE messages;')
    p.query('CREATE PUBLICATION pub_binary FOR TABLE messages_bin;')
    p.query('CREATE PUBLICATION pub_all FOR TABLE messages, ztype.dictionaries;')
    p.query('CREATE PUBLICATION pub_registry FOR TABLE ztype.dictionaries;')


def preseeded(sub, work, registry):
    """(1) The recommended procedure: the subscriber's registry is pre-seeded from the publisher's
    export with the same slots, the publication carries user tables only, and the subscriber
    creates the table by dictionary *name*. Nothing about the column is left to the initial copy.
    (4) A second subscription on a copy of the table pins the binary path, where the value travels
    through ztext_send/ztext_recv instead of output/input."""
    run([BIN / 'createdb', 'seeded'], env=sub.env)
    s = sub.session('seeded')
    mark = log_mark(sub)
    s.query('CREATE EXTENSION ztype;')
    s.query(f"COPY ztype.dictionaries FROM '{registry}';")
    s.equal('SELECT slot, name FROM ztype.dictionary_inventory;', '1|first')
    s.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'), "
            "meta zjsonb(6,'first'), blob zbytea);")
    s.query('CREATE TABLE messages_bin (LIKE messages INCLUDING ALL);')
    subscribe(s, 'sub_seeded', work, 'pub_tables')
    subscribe(s, 'sub_binary', work, 'pub_binary', ' WITH (binary = true)')
    wait_synced(s, 'sub_seeded', 1)
    wait_synced(s, 'sub_binary', 1)
    for table in ('messages', 'messages_bin'):
        s.equal(equal_sql(table=table))
        s.equal(policy_sql(table=table))
        s.equal(clean_sql(table=table))
    window = log_since(sub, mark)
    assert 'is not available' not in window, window
    assert 'ERROR' not in window, window
    print('PASS: pre-seeded registry, text and binary apply, every row at the column policy', flush=True)
    return s


def republished(sub, work):
    """(2) The registry travels in the publication instead, into an empty subscriber registry.
    Name-based DDL cannot work yet, so the column names the slot; and because the two tables sync
    independently, a row may be applied before its dictionary row arrives. Such a row is exact and
    dictionary-free, and the documented catch-up UPDATE brings it to the column policy."""
    run([BIN / 'createdb', 'published'], env=sub.env)
    s = sub.session('published')
    s.query('CREATE EXTENSION ztype;')
    s.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'));",
            error='dictionary "first" is not registered')
    s.query('CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,1), meta zjsonb(6,1), blob zbytea);')
    subscribe(s, 'sub_published', work, 'pub_all')
    wait_synced(s, 'sub_published', 2)
    s.equal('SELECT slot, name FROM ztype.dictionary_inventory;', '1|first')
    s.equal(equal_sql())
    s.equal(clean_sql())
    # Sync order is not guaranteed, so the assertion is the partition, not a count: every row is
    # either at the column policy or dictionary-free, and none is anything else.
    s.equal('SELECT bool_and(i.level = 6 AND (i.dict_name = \'first\' OR i.dict_id IS NULL)) '
            'FROM messages m, ztype.inspect(m.body) i;')
    s.query('UPDATE messages SET body = body::text, meta = meta::jsonb;')
    s.equal(equal_sql())
    s.equal(policy_sql())
    s.equal(clean_sql())
    print('PASS: registry in the publication, dictionary-free rows caught up by UPDATE', flush=True)
    return s


def cascaded(published, third, work):
    """Cascading: a third node subscribes to the subscriber, which publishes what it applied,
    registry included. Its own registry rows arrived by apply, and logical decoding on that middle
    node reads them under a historic snapshot exactly as on the publisher (user_catalog_table). The
    third node sees the same shape as (2): rows exact, at the policy or dictionary-free until the
    catch-up UPDATE."""
    published.query('CREATE PUBLICATION pub_hop FOR TABLE messages, ztype.dictionaries;')
    published.query('CREATE PUBLICATION pub_hop_tables FOR TABLE messages;')
    run([BIN / 'createdb', 'hop'], env=third.env)
    h = third.session('hop')
    h.query('CREATE EXTENSION ztype;')
    h.query('CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,1), meta zjsonb(6,1), blob zbytea);')
    subscribe(h, 'sub_hop', work, 'pub_hop', port=SUB_PORT, dbname='published')
    wait_synced(h, 'sub_hop', 2)
    h.equal('SELECT slot, name FROM ztype.dictionary_inventory;', '1|first')
    h.equal(equal_sql())
    h.equal(clean_sql())
    h.equal('SELECT bool_and(i.level = 6 AND (i.dict_name = \'first\' OR i.dict_id IS NULL)) '
            'FROM messages m, ztype.inspect(m.body) i;')
    h.query('UPDATE messages SET body = body::text, meta = meta::jsonb;')
    h.equal(equal_sql())
    h.equal(policy_sql())
    h.equal(clean_sql())
    # From here on the registry is complete: the streamed changes must arrive without a fallback.
    h.mark = log_mark(third)
    return h


def cascade_verdict(h, third, work):
    """After the publisher's second registration and the streamed changes: both reach the third
    node through two hops, and the tool seeds another third-node database from the *subscriber*,
    whose registry is itself replicated, so name-based DDL works there before subscribing."""
    wait(h, "SELECT string_agg(slot || '|' || name, ',' ORDER BY slot) = '1|first,2|second' FROM ztype.dictionary_inventory;",
         what="the 'second' registry row two hops away")
    wait(h, f"SELECT count(*) = {ROWS - 1} AND bool_and(id <> {ROWS}) FROM messages;", what='the DELETE two hops away')
    wait(h, f"SELECT body::text = 'updated ' || {body('id')} FROM messages WHERE id = 1;", what='the UPDATE two hops away')
    h.equal("SELECT i.level, i.dict_name FROM messages m, ztype.inspect(m.body) i WHERE m.id = 1;", '6|first')
    window = log_since(third, h.mark)
    assert 'is not available' not in window and 'ERROR' not in window, window
    run([BIN / 'createdb', 'hop_named'], env=third.env)
    n = third.session('hop_named')
    n.query('CREATE EXTENSION ztype;')
    code, out, err = tool('--source', conninfo(work, SUB_PORT, 'published'), '--target', conninfo(work, THIRD_PORT, 'hop_named'))
    assert code == 0 and "imported slot 1 'first'" in out and "imported slot 2 'second'" in out, (code, out, err)
    n.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'), meta zjsonb(6,'first'), blob zbytea);")
    mark = log_mark(third)
    subscribe(n, 'sub_hop_named', work, 'pub_hop_tables', port=SUB_PORT, dbname='published')
    wait_synced(n, 'sub_hop_named', 1)
    n.equal(f"SELECT count(*) = {ROWS - 1} AND bool_and(id = 1 OR body::text = {body('id')}) FROM messages;")
    n.equal(policy_sql())
    n.equal(clean_sql())
    window = log_since(third, mark)
    assert 'WARNING' not in window and 'ERROR' not in window, window
    n.close()
    print('PASS: cascading: rows and the registry reach node 3 through two hops, and the tool seeds a third node from a subscriber', flush=True)


def remodifier(sub, work):
    """(5) The subscriber decides the policy: a column declared ztext(3) stores what arrives at its
    own level and with no dictionary, and that database needs no registry at all."""
    run([BIN / 'createdb', 'remod'], env=sub.env)
    s = sub.session('remod')
    s.query('CREATE EXTENSION ztype;')
    s.query('CREATE TABLE messages(id integer PRIMARY KEY, body ztext(3), meta zjsonb, blob zbytea);')
    subscribe(s, 'sub_remod', work, 'pub_tables')
    wait_synced(s, 'sub_remod', 1)
    s.equal(equal_sql())
    s.equal(policy_sql(level=3, name='NULL'))
    s.equal(clean_sql())
    s.equal('SELECT count(*) FROM ztype.dictionaries;', '0')
    print('PASS: subscriber column modifier decides the stored policy', flush=True)
    return s


def conflicted(sub, work):
    """(6) A subscriber whose slots already mean something else. Without the registry in the
    publication the mismatch is *silent*: rows are exact but stored under the subscriber's own
    slot-1 dictionary. With the registry in the publication it is loud: the publisher's next
    registration collides on the primary key, apply fails, and the row never lands."""
    run([BIN / 'createdb', 'conflict'], env=sub.env)
    s = sub.session('conflict')
    s.query('CREATE EXTENSION ztype;')
    s.equal(training('local-a'), '1')
    s.equal(training('local-b'), '2')
    s.query('CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,1), meta zjsonb(6,1), blob zbytea);')
    mark = log_mark(sub)
    subscribe(s, 'sub_mismatch', work, 'pub_tables')
    wait_synced(s, 'sub_mismatch', 1)
    s.equal(equal_sql())
    s.equal(clean_sql())
    s.equal(policy_sql(name="'local-a'"))
    window = log_since(sub, mark)
    assert 'is not available' not in window and 'ERROR' not in window, window
    # copy_data = false: the collision must be an *apply* failure on a streamed row, not a copy.
    # disable_on_error = true bounds it to exactly one failure: without it the apply worker is
    # restarted every wal_retrieve_retry_interval and keeps writing the same ERROR into this
    # cluster's one log file, which every later log-window assertion shares.
    s.mark = log_mark(sub)
    subscribe(s, 'sub_registry', work, 'pub_registry',
              ' WITH (copy_data = false, disable_on_error = true)')
    print('PASS: unpublished registry means a silent slot mismatch, values still exact', flush=True)
    return s


def tooled(sub, work):
    """The registry transport, ztype-sync, as the subscriber-seeding step of the procedure: a role
    holding EXECUTE on ztype.import_dictionary and nothing else receives the publisher's registry
    under the same slots, name-based DDL then works, and the subscription needs no fallback. The
    tool is idempotent (a second run imports nothing and rewrites nothing), --check reports the
    state without writing, and a source role that cannot read the bytes fails with exit 3 having
    imported nothing."""
    run([BIN / 'createdb', 'tooled'], env=sub.env)
    run([BIN / 'createdb', 'tooled_empty'], env=sub.env)
    s = sub.session('tooled')
    s.query('CREATE EXTENSION ztype;')
    s.query('CREATE ROLE distributor LOGIN; GRANT EXECUTE ON FUNCTION ztype.import_dictionary(integer, text, bytea, text) TO distributor;')
    source = conninfo(work, PUB_PORT)
    target = conninfo(work, SUB_PORT, 'tooled', user='distributor')
    code, out, err = tool('--source', source, '--target', target, '--check')
    assert code == 2 and "missing slot 1 'first'" in out, (code, out, err)
    s.equal('SELECT count(*) FROM ztype.dictionaries;', '0')
    mark = log_mark(sub)
    code, out, err = tool('--source', source, '--target', target)
    assert code == 0 and "imported slot 1 'first'" in out and 'in sync' in out, (code, out, err)
    s.equal('SELECT slot, name FROM ztype.dictionary_inventory;', '1|first')
    created = s.query('SELECT created_at FROM ztype.dictionary_inventory WHERE slot = 1;')
    s.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'), "
            "meta zjsonb(6,'first'), blob zbytea);")
    subscribe(s, 'sub_tooled', work, 'pub_tables')
    wait_synced(s, 'sub_tooled', 1)
    s.equal(equal_sql())
    s.equal(policy_sql())
    s.equal(clean_sql())
    window = log_since(sub, mark)
    assert 'WARNING' not in window and 'ERROR' not in window, window
    # The tool's role got nothing but the function: the registry itself stays closed to it.
    s.query('SET ROLE distributor;')
    assert s.sqlstate('SELECT count(*) FROM ztype.dictionaries;') == '42501'
    s.query('RESET ROLE;')
    code, out, err = tool('--source', source, '--target', target)
    assert code == 0 and "present slot 1 'first'" in out and 'imported' not in out, (code, out, err)
    s.equal('SELECT created_at FROM ztype.dictionary_inventory WHERE slot = 1;', created)
    code, out, err = tool('--source', source, '--target', target, '--check')
    assert code == 0 and out.strip().splitlines() == ["present slot 1 'first'", 'in sync'], (code, out, err)
    # A source role that may not read the bytes: the run fails before anything reaches the target.
    e = sub.session('tooled_empty')
    e.query('CREATE EXTENSION ztype;')
    s.query('CREATE ROLE reader LOGIN;')  # cluster-wide, so it exists on the publisher's side too
    code, out, err = tool('--source', conninfo(work, SUB_PORT, 'tooled', user='reader'),
                          '--target', conninfo(work, SUB_PORT, 'tooled_empty'))
    assert code == 3 and 'permission denied' in err and 'extension owner' in err, (code, out, err)
    e.equal('SELECT count(*) FROM ztype.dictionaries;', '0')
    e.close()
    code, out, err = tool('--source', f'host={work} port=1 dbname=postgres', '--target', target)
    assert code == 3 and 'source' in err, (code, out, err)
    print('PASS: ztype-sync fills the subscriber registry as a role holding EXECUTE only, is idempotent, and reports in sync', flush=True)
    return s


def twoway(pub, sub, work):
    """Two-way: each node publishes and subscribes to the other with origin = none, on disjoint
    key ranges. Both nodes register dictionaries, so the slots must come from disjoint ranges,
    and that means explicit slots through import_dictionary on both sides (train_dictionary
    supplies the bytes): add_dictionary allocates max(slot) + 1, so once the other node's range
    has arrived it would continue above it and the next registrations of the two nodes would
    collide. The tool carries each registry to the other side, and every row lands at the local
    policy in both directions without looping."""
    run([BIN / 'createdb', 'pair'], env=pub.env)
    run([BIN / 'createdb', 'pair'], env=sub.env)
    a, b = pub.session('pair'), sub.session('pair')
    for s in (a, b):
        s.query('CREATE EXTENSION ztype;')
    def register(slot, label):
        return (f"SELECT ztype.import_dictionary({slot}, '{label}', ztype.train_dictionary($$SELECT '{label} sample subject ' || i || "
                "repeat(md5(i::text),4) FROM generate_series(1,1000) i$$, 2048));")
    a.equal(register(1, 'a-dict'), '1')
    b.equal(register(60001, 'b-dict'), '60001')
    to_b, to_a = conninfo(work, SUB_PORT, 'pair'), conninfo(work, PUB_PORT, 'pair')
    code, out, err = tool('--source', to_a, '--target', to_b)
    assert code == 0 and "imported slot 1 'a-dict'" in out, (code, out, err)
    code, out, err = tool('--source', to_b, '--target', to_a)
    assert code == 0 and "imported slot 60001 'b-dict'" in out and "present slot 1 'a-dict'" in out, (code, out, err)
    for s in (a, b):
        s.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|a-dict\n60001|b-dict')
    a.equal(register(2, 'a-later'), '2')
    b.equal(register(60002, 'b-later'), '60002')
    # The pitfall the convention exists for: max(slot) + 1 on A is now inside B's range.
    a.equal(f"BEGIN; {training('stray')} ROLLBACK;", '60002')
    a.equal('SELECT max(slot) FROM ztype.dictionary_inventory;', '60001')
    for s in (a, b):
        s.query("CREATE TABLE events(id integer PRIMARY KEY, body ztext(6,'a-dict'), note ztext(6,'b-dict'));")
        s.query('CREATE PUBLICATION pub_pair FOR TABLE events;')
    subscribe(a, 'sub_from_b', work, 'pub_pair', ' WITH (origin = none, copy_data = false)', port=SUB_PORT, dbname='pair')
    subscribe(b, 'sub_from_a', work, 'pub_pair', ' WITH (origin = none, copy_data = false)', port=PUB_PORT, dbname='pair')
    wait_synced(a, 'sub_from_b', 1)
    wait_synced(b, 'sub_from_a', 1)
    note = "repeat('b-dict sample subject ' || {} || ' ', 12)"  # above the compression floor, in b-dict's vocabulary
    a.query(f"INSERT INTO events SELECT i, {body('i')}, {note.format('i')} FROM generate_series(1, 10) i;")
    b.query(f"INSERT INTO events SELECT i, {body('i')}, {note.format('i')} FROM generate_series(1001, 1010) i;")
    policy = ("SELECT bool_and(ib.level = 6 AND ib.dict_name = 'a-dict' AND i_n.level = 6 AND i_n.dict_name = 'b-dict' "
              f"AND e.body::text = {body('e.id')} AND e.note::text = {note.format('e.id')}) "
              "FROM events e, ztype.inspect(e.body) ib, ztype.inspect(e.note) i_n;")
    for s, name in ((a, 'sub_from_b'), (b, 'sub_from_a')):
        wait(s, 'SELECT count(*) FROM events;', '20', what='rows from both directions')
        s.equal(policy)
    time.sleep(2)  # a loop would keep applying its own rows back; a conflict would count an apply error
    for s, name in ((a, 'sub_from_b'), (b, 'sub_from_a')):
        s.equal('SELECT count(*) FROM events;', '20')
        s.equal(f"SELECT coalesce(max(apply_error_count), 0) FROM pg_stat_subscription_stats WHERE subname = '{name}';", '0')
    # The registrations made after the first exchange are carried the same way, each direction.
    for source, target, slot, label in ((to_a, to_b, 2, 'a-later'), (to_b, to_a, 60002, 'b-later')):
        code, out, err = tool('--source', source, '--target', target, '--check')
        assert code == 2 and f"missing slot {slot} '{label}'" in out, (code, out, err)
        code, out, err = tool('--source', source, '--target', target)
        assert code == 0 and f"imported slot {slot} '{label}'" in out, (code, out, err)
    for s in (a, b):
        s.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|a-dict\n2|a-later\n60001|b-dict\n60002|b-later')
    for source, target in ((to_a, to_b), (to_b, to_a)):
        code, out, err = tool('--source', source, '--target', target, '--check')
        assert code == 0 and 'in sync' in out, (code, out, err)
    print('PASS: two-way: registries synced both ways under disjoint slot ranges, rows land at the local policy in both directions, no loop', flush=True)
    return a, b


def tool_catch_up(p, tooled, conflict, work, second_id):
    """After the publisher registered a second dictionary: the tool fills the lagging slot on the
    subscriber it seeded; against the subscriber whose slots mean something else it refuses with
    exit 1, under --dry-run and for real alike, and the target registry is untouched; --check on
    that subscriber reports the silent mismatch the sweep reports."""
    source = conninfo(work, PUB_PORT)
    code, out, err = tool('--source', source, '--target', conninfo(work, SUB_PORT, 'tooled'))
    assert code == 0 and "present slot 1 'first'" in out and "imported slot 2 'second'" in out, (code, out, err)
    tooled.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|first\n2|second')
    target = conninfo(work, SUB_PORT, 'conflict')
    before = conflict.query('SELECT count(*), max(created_at) FROM ztype.dictionaries;')
    for mode in (('--dry-run',), ()):
        code, out, err = tool('--source', source, '--target', target, *mode)
        assert code == 1 and 'ERROR: 42710: ztype: slot 1 is taken by dictionary "local-a"' in err and 'untouched' in err, (mode, code, out, err)
        assert 'imported' not in out and 'would import' not in out, (mode, out)
        conflict.equal('SELECT count(*), max(created_at) FROM ztype.dictionaries;', before)
        conflict.equal(f'SELECT count(*) FROM ztype.dictionaries WHERE dict_id = {second_id};', '0')
    code, out, err = tool('--source', source, '--target', target, '--check')
    assert code == 2, (code, out, err)
    assert "different slot 1: here 'local-a'" in out and "different slot 2: here 'local-b'" in out, out
    assert 'public.messages.body: different dictionary' in out and 'public.messages.meta: different dictionary' in out, out
    print('PASS: ztype-sync fills a lagging slot, refuses a taken slot with exit 1 and the registry untouched, and --check reports the silent mismatch', flush=True)


def register_second(p):
    """A second dictionary and a table that names it, registered on the publisher after every
    subscription is already streaming. This is what the conflict case collides with and what the
    streaming case must carry, so it happens once, before either looks."""
    p.equal(training('second'), '2')
    second_id = p.query("SELECT dict_id FROM ztype.dictionaries WHERE name = 'second';")
    p.query("CREATE TABLE later(id integer PRIMARY KEY, body ztext(6,'second'));")
    p.query('ALTER PUBLICATION pub_all ADD TABLE later;')
    return second_id


def conflict_verdict(s, second_id):
    """The publisher's slot-2 registration collides with this database's own slot 2. The failure is
    loud and bounded: the apply worker reports the error and disables its own subscription, so it
    fails exactly once and the log is quiet again afterwards - which is why this runs before the
    streaming case marks the log."""
    wait(s, "SELECT NOT subenabled FROM pg_subscription WHERE subname = 'sub_registry';",
         what='the conflicting subscription to disable itself')
    wait(s, "SELECT apply_error_count > 0 FROM pg_stat_subscription_stats WHERE subname = 'sub_registry';",
         what='apply error on the conflicting registry row')
    s.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|local-a\n2|local-b')
    s.equal(f'SELECT count(*) FROM ztype.dictionaries WHERE dict_id = {second_id};', '0')
    print('PASS: an occupied slot fails apply visibly and disables the subscription; the conflicting row never lands', flush=True)


def sweep(p, seeded, published, remod, conflict):
    """The subscriber-side sweep, ztype.policy_differences: the publisher's column_policies carried
    over as one jsonb value, joined on schema, table and column. Nothing about replication fails in
    any of these databases, which is the point: the pre-seeded and the published registries report
    no difference, the remodified subscriber reports its own level and missing dictionary, and the
    conflicting one, whose slot 1 is another dictionary, reports 'different dictionary' by ID."""
    policies = p.query('SELECT jsonb_agg(p) FROM ztype.column_policies p;')
    sql = f"SELECT table_name || '.' || column_name || ': ' || difference FROM ztype.policy_differences($j${policies}$j$) ORDER BY 1;"
    seeded.equal(sql, '')
    published.equal(sql, '')
    remod.equal(sql, 'messages.body: different level, no dictionary here\nmessages.meta: no dictionary here')
    conflict.equal(sql, 'messages.body: different dictionary\nmessages.meta: different dictionary')
    conflict.equal("SELECT local_dict_name, publisher_dict_name, local_dict_id <> publisher_dict_id "
                   f"FROM ztype.policy_differences($j${policies}$j$) WHERE column_name = 'body';", 'local-a|first|t')
    # An ordinary role can run it: it reads the public view and its argument, nothing else.
    conflict.query('CREATE ROLE sweeper; SET ROLE sweeper;')
    conflict.equal(f'SELECT count(*) FROM ztype.policy_differences($j${policies}$j$);', '2')
    conflict.query('RESET ROLE;')
    print('PASS: policy_differences reports the silent slot mismatch and the remodified column, nothing else', flush=True)


def streaming(p, published):
    """(3) After the initial copy the values go through logical decoding, where the output function
    runs under a historic snapshot. The dictionary registered by register_second() reaches the
    subscriber's registry (the append-only trigger fires on UPDATE/DELETE/TRUNCATE only, so the
    apply worker's INSERT passes), a table that names it is created there, and rows written with it
    arrive carrying it."""
    wait(published, "SELECT count(*) = 1 FROM ztype.dictionary_inventory WHERE name = 'second';",
         what="the 'second' registry row on the subscriber")
    published.query("CREATE TABLE later(id integer PRIMARY KEY, body ztext(6,'second'));")
    published.query('ALTER SUBSCRIPTION sub_published REFRESH PUBLICATION;')
    wait_synced(published, 'sub_published', 3)
    mark = log_mark(published.cluster)
    p.query(f"INSERT INTO later SELECT i, {body('i')} FROM generate_series(1,10) i;")
    p.query(f"UPDATE messages SET body = 'updated ' || {body('id')} WHERE id = 1;")
    p.query(f'DELETE FROM messages WHERE id = {ROWS};')
    wait(published, 'SELECT count(*) FROM later;', '10', what='streamed rows in later')
    published.equal(f"SELECT bool_and(body::text = {body('id')}) FROM later;")
    published.equal(policy_sql(table='later', name="'second'"))
    published.equal('SELECT count(*) = 0 FROM later WHERE ztype.validate(body) IS NOT NULL;')
    wait(published, f"SELECT count(*) = {ROWS - 1} AND bool_and(id <> {ROWS}) FROM messages;",
         what='the replicated DELETE')
    wait(published, f"SELECT body::text = 'updated ' || {body('id')} FROM messages WHERE id = 1;",
         what='the replicated UPDATE')
    published.equal("SELECT i.level, i.dict_name FROM messages m, ztype.inspect(m.body) i WHERE m.id = 1;", '6|first')
    window = log_since(published.cluster, mark)
    assert 'is not available' not in window and 'ERROR' not in window, window
    print('PASS: streamed rows carry the dictionary registered after the subscription started', flush=True)


def conflict_recovery(p, s, sub, work):
    """The runbook for a registry collision, after conflict_verdict() saw the subscription disable
    itself: keep the local rows, import the publisher's dictionaries under slots the publisher will
    never reach, skip the failed remote transaction at the LSN the apply error named, re-enable,
    and re-point the columns that should store as the publisher's do. The next registration on
    the publisher then lands, local registrations continue above the publisher's range, and the
    sweep is clean. Nothing here is automatic by design: the slots and the rewrite are the
    operator's decisions, and the tool only ever calls import_dictionary."""
    window = log_since(sub, s.mark)
    assert 'dictionaries_pkey' in window, window
    lsn = re.findall(r'finished at ([0-9A-F]+/[0-9A-F]+)', window)[-1]
    p.query(f"COPY (SELECT slot, dict_id, name, dict, trained_from, created_at FROM ztype.dictionaries "
            f"WHERE name IN ('first', 'second') ORDER BY slot) TO '{work}/pub.copy';")
    s.query('CREATE TEMP TABLE incoming (slot integer, dict_id bigint, name text, dict bytea, trained_from text, created_at timestamptz);')
    s.query(f"COPY incoming FROM '{work}/pub.copy';")
    s.equal("SELECT ztype.import_dictionary(60000 + slot, name, dict, trained_from) FROM incoming ORDER BY slot;", '60001\n60002')
    # With the bytes present under any slot the tool has nothing to refuse; what remains is the
    # columns, which it reports and leaves to the operator.
    code, out, err = tool('--source', conninfo(work, PUB_PORT), '--target', conninfo(work, SUB_PORT, 'conflict'), '--check')
    assert code == 2 and "present slot 1 'first' (here slot 60001 'first')" in out and "present slot 2 'second' (here slot 60002 'second')" in out, (code, out, err)
    assert 'public.messages.body: different dictionary' in out and 'missing' not in out and 'different slot' not in out, out
    s.query(f"ALTER SUBSCRIPTION sub_registry SKIP (lsn = '{lsn}'); ALTER SUBSCRIPTION sub_registry ENABLE;")
    mark = log_mark(sub)
    wait(s, "SELECT subenabled AND subskiplsn = '0/0' FROM pg_subscription WHERE subname = 'sub_registry';",
         what='the skip to be consumed and the subscription to stay enabled')
    p.equal(training('third'), '3')
    wait(s, "SELECT count(*) = 1 FROM ztype.dictionary_inventory WHERE slot = 3 AND name = 'third';", what="the 'third' row after recovery")
    s.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|local-a\n2|local-b\n3|third\n60001|first\n60002|second')
    s.equal("SELECT apply_error_count FROM pg_stat_subscription_stats WHERE subname = 'sub_registry';", '1')
    window = log_since(sub, mark)
    assert 'ERROR' not in window, window
    s.equal(training('local-c'), '60003')
    s.query('ALTER TABLE messages ALTER COLUMN body TYPE ztext(6,60001), ALTER COLUMN meta TYPE zjsonb(6,60001);')
    s.equal(f"SELECT count(*) = {ROWS - 1} AND bool_and(id = 1 OR body::text = {body('id')}) FROM messages;")  # streamed UPDATE and DELETE included
    s.equal(policy_sql())
    s.equal(clean_sql())
    code, out, err = tool('--source', conninfo(work, PUB_PORT), '--target', conninfo(work, SUB_PORT, 'conflict'), '--check')
    assert code == 0 and 'in sync' in out, (code, out, err)
    print('PASS: conflict runbook: skip the failed transaction, import under a high slot, re-enable; the next registration lands and the sweep is clean', flush=True)


def standby_reads(st, label):
    """Physical replication carries the stored bytes, so the standby resolves the same dictionary
    from its own replayed registry - on a read-only server, where the lookup still goes through
    SPI. Every read path is exercised, and the backend cache is proven here too."""
    st.equal(equal_sql(table='messages'))
    st.equal(policy_sql())
    st.equal(clean_sql())
    st.equal(f"SELECT bool_and(raw_length(body) = octet_length({body('id')}) "
             f"AND prefix(body, 5) = substr({body('id')}, 1, 5)) FROM messages;")
    st.query('BEGIN;')
    st.equal(f'SELECT count(*) = {ROWS} FROM messages WHERE length(body::text) > 0;')
    st.equal('SELECT entries > 0 AND bytes > 0 FROM ztype.dictionary_cache_stats();')
    st.query('COMMIT;')
    st.equal('SELECT entries > 0 AND bytes > 0 FROM ztype.dictionary_cache_stats();')  # kept past the commit
    st.query('SELECT ztype.reload_dictionaries();')
    st.equal('SELECT entries, bytes FROM ztype.dictionary_cache_stats();', '0|0')
    st.query('SET ztype.dictionary_cache_size = 0;')
    st.equal(equal_sql(table='messages'))
    st.query('RESET ztype.dictionary_cache_size;')
    # A standby refuses writes; the connection must survive both refusals and stay usable.
    assert st.sqlstate(f"INSERT INTO messages(id, body) VALUES (9999, {body('9999')});") == '25006'
    assert st.sqlstate(training('standby')) == '25006'
    st.equal('SELECT count(*) FROM ztype.dictionaries;', '1')
    st.equal(equal_sql(table='messages'))
    print(f'PASS: {label}', flush=True)


def wait_replayed(p, st):
    lsn = p.query('SELECT pg_current_wal_lsn();')
    wait(st, f"SELECT pg_last_wal_replay_lsn() >= '{lsn}'::pg_lsn;", what='WAL replay on the standby')


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-rep-') as tmp:
        work = Path(tmp)
        sharedir = share(work, 'share', t.library().with_suffix(''))
        # One publisher for six subscriptions plus a base backup: the stock worker and slot limits
        # are too small for that. The short retry interval only keeps worker restarts prompt; the
        # conflict case bounds its own failure with disable_on_error rather than relying on it.
        pub = Cluster(BIN, work, 'pub', PUB_PORT, sharedir,
                      extra='wal_level = logical\nmax_wal_senders = 20\nmax_replication_slots = 20\n')
        # The subscriber also publishes (cascading, two-way), so it decodes logically too.
        sub = Cluster(BIN, work, 'sub', SUB_PORT, sharedir, extra=WORKERS + 'wal_level = logical\nmax_wal_senders = 10\n')
        third = Cluster(BIN, work, 'third', THIRD_PORT, sharedir, extra=WORKERS)
        pub.start()
        sub.start()
        third.start()
        standby = None
        try:
            p = pub.session()
            publisher_seed(p)
            p.query(f"COPY ztype.dictionaries TO '{work}/registry.copy';")
            # The physical leg starts here, so the dictionary registered later is one the standby
            # has never seen and can only get by replay.
            run([BIN / 'pg_basebackup', '-D', work / 'standby', '-h', work, '-p', PUB_PORT,
                 '-R', '-C', '-S', 'ztype_standby', '-X', 'stream'], env=pub.env)
            standby = Cluster(BIN, work, 'standby', STANDBY_PORT, sharedir, adopt=True, extra='hot_standby = on\n')
            standby.start()
            st = standby.session()
            st.equal('SELECT pg_is_in_recovery();')
            wait_replayed(p, st)
            standby_reads(st, 'hot standby decodes every read path and refuses writes with 25006')

            seeded = preseeded(sub, work, work / 'registry.copy')
            published = republished(sub, work)
            hop = cascaded(published, third, work)
            remod = remodifier(sub, work)
            conflict = conflicted(sub, work)
            synced = tooled(sub, work)
            pair = twoway(pub, sub, work)
            second_id = register_second(p)
            # The conflicting subscription must have failed and disabled itself before the
            # streaming case reads this cluster's shared log for its own window.
            conflict_verdict(conflict, second_id)
            tool_catch_up(p, synced, conflict, work, second_id)
            sweep(p, seeded, published, remod, conflict)
            streaming(p, published)
            cascade_verdict(hop, third, work)
            conflict_recovery(p, conflict, sub, work)

            wait_replayed(p, st)
            st.equal("SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;", '1|first\n2|second\n3|third')
            st.equal(f"SELECT count(*) = 10 AND bool_and(body::text = {body('id')} "
                     'AND (ztype.inspect(body)).dict_name = \'second\' AND ztype.validate(body) IS NULL) FROM later;')
            st.equal(f"SELECT count(*) = {ROWS - 1} AND bool_and(ztype.validate(body) IS NULL) FROM messages;")
            st.equal(f"SELECT body::text = 'updated ' || {body('id')} FROM messages WHERE id = 1;")
            print('PASS: a dictionary registered after the base backup is used by standby reads', flush=True)

            for s in (p, st, seeded, published, remod, conflict, synced, hop, *pair):
                s.close()
        except BaseException:
            print(pub.diagnostics())
            print(sub.diagnostics())
            print(third.diagnostics())
            if standby:
                print(standby.diagnostics())
            raise
        finally:
            if standby:
                standby.stop('immediate')
            third.stop('immediate')
            sub.stop('immediate')
            pub.stop('immediate')
        pub.check_reports()
        sub.check_reports()
        third.check_reports()


if __name__ == '__main__':
    main()
