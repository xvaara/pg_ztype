#!/usr/bin/env python3
"""Qualify failover and promotion for dictionary-compressed columns: a physical standby promoted
to primary, a logical subscription that follows it through a failover slot, and a standby
converted into a logical subscriber with pg_createsubscriber.

Four disposable clusters from one installation, on their own port block: a publisher with
wal_level = logical and synchronized_standby_slots, a logical subscriber, a physical standby that
synchronises the publisher's failover slots (sync_replication_slots = on), and a second base
backup that pg_createsubscriber turns into a logical subscriber.

What ztype has to get right in each: the registry travels physically, so a promoted node has
every dictionary the moment it is primary and writes with them at once; a converted node has the
publisher's frames byte for byte and needs no catch-up; and a subscription that moves to the
promoted node keeps applying under its own column policy, dictionaries registered after the
failover included. The one setting that is easy to get wrong is recorded here because the harness
hit it: a base backup copies the primary's synchronized_standby_slots, and a promoted node that
keeps it waits for a standby slot it does not have, blocking every failover-slot walsender.

    python3 tests/test_replication_ha.py    # PG_CONFIG=... picks the installation; make test-replication-ha
"""
from pathlib import Path
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parent))
import test_ztype as t  # noqa: E402
from test_ztype import Cluster, run, share, training  # noqa: E402
from test_replication import (ROWS, WORKERS, body, clean_sql, conninfo, equal_sql, log_mark, log_since,  # noqa: E402
                              policy_sql, publisher_seed, register_second, subscribe, tool, wait, wait_replayed, wait_synced)

BIN = t.BIN
PUB_PORT, SUB_PORT, STANDBY_PORT, CONVERTED_PORT = 55461, 55462, 55463, 55464


def primary_conninfo(work):
    """The -d form of pg_basebackup is what makes -R write dbname= into primary_conninfo, which
    slot synchronisation requires."""
    return f'host={work} port={PUB_PORT} dbname=postgres'


def failover_subscription(sub, work):
    """A subscriber that receives the registry in the publication (so a dictionary registered on
    whichever node is primary reaches it the same way), subscribing WITH (failover = true), so the
    publisher's slot is marked for synchronisation to the standby. Columns name their slots, and
    the initial copy's dictionary-free rows are caught up as documented."""
    run([BIN / 'createdb', 'ha'], env=sub.env)
    h = sub.session('ha')
    h.query('CREATE EXTENSION ztype;')
    h.query('CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,1), meta zjsonb(6,1), blob zbytea);')
    h.query('CREATE TABLE later(id integer PRIMARY KEY, body ztext(6,2));')
    subscribe(h, 'sub_ha', work, 'pub_all', ' WITH (failover = true)', port=PUB_PORT)
    wait_synced(h, 'sub_ha', 3)
    h.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|first\n2|second')
    h.query('UPDATE messages SET body = body::text, meta = meta::jsonb; UPDATE later SET body = body::text;')
    h.equal(equal_sql())
    h.equal(policy_sql())
    h.equal(clean_sql())
    h.equal(f"SELECT count(*) = 10 AND bool_and(body::text = {body('id')}) FROM later;")
    h.equal(policy_sql(table='later', name="'second'"))
    return h


def slot_synced(p, st):
    """The failover slot exists on the standby as a synchronised, permanent copy. The slotsync
    worker's nap backs off to 30 s when nothing changes, hence the long bound."""
    p.equal("SELECT failover FROM pg_replication_slots WHERE slot_name = 'sub_ha';")
    wait(st, "SELECT synced AND NOT temporary AND failover FROM pg_replication_slots WHERE slot_name = 'sub_ha';",
         what='the failover slot to be synchronised to the standby', timeout=180)


def converted_subscriber(p, work, sharedir):
    """pg_createsubscriber: a second base backup, started in recovery, caught up, promoted and
    subscribed by the tool itself. The registry and every frame arrived physically, so nothing
    needs the tool or a catch-up, and the FOR ALL TABLES publication it creates carries the
    registry, so a registration made afterwards arrives logically under the same slot."""
    run([BIN / 'pg_basebackup', '-D', work / 'converted', '-d', primary_conninfo(work), '-R', '-X', 'stream'], env=p.cluster.env)
    converted = Cluster(BIN, work, 'converted', CONVERTED_PORT, sharedir, adopt=True, extra="synchronized_standby_slots = ''\n")
    try:
        run([BIN / 'pg_createsubscriber', '-D', work / 'converted', '-P', primary_conninfo(work), '-d', 'postgres',
             '-p', str(CONVERTED_PORT), '-s', str(work), '--publication', 'ztype_conv_pub', '--subscription', 'ztype_conv_sub',
             '--replication-slot', 'ztype_conv_slot', '-t', '120'], env=p.cluster.env)
    except AssertionError:
        for log in sorted((work / 'converted' / 'pg_createsubscriber_output.d').glob('*')):
            print(f'--- {log.name}:\n{log.read_text(errors="replace")[-4000:]}')
        raise
    converted.start()
    c = converted.session()
    c.equal('SELECT NOT pg_is_in_recovery();')
    registry = "SELECT string_agg(slot || '|' || name || '|' || created_at, ',' ORDER BY slot) FROM ztype.dictionary_inventory;"
    c.equal(registry, p.query(registry))
    c.equal(equal_sql())
    c.equal(policy_sql())
    c.equal(clean_sql())
    c.equal(f"SELECT count(*) = 10 AND bool_and((ztype.inspect(body)).dict_name = 'second') FROM later;")
    c.equal("SELECT subenabled FROM pg_subscription WHERE subname = 'ztype_conv_sub';")
    p.equal("SELECT count(*) FROM pg_publication_tables WHERE pubname = 'ztype_conv_pub' AND schemaname = 'ztype' AND tablename = 'dictionaries';", '1')
    return converted, c


def converted_verdict(p, converted, c):
    """Rows and a registration made on the publisher after the conversion arrive logically, and the
    converted node applies them under its own (physically inherited) column policy."""
    mark = log_mark(converted)
    p.query(f"INSERT INTO messages SELECT i, {body('i')}, jsonb_build_object('body', {body('i')}), "
            f"convert_to({body('i')}, 'UTF8') FROM generate_series(101, 110) i;")
    wait(c, f'SELECT count(*) = {ROWS + 10} FROM messages;', what='rows streamed to the converted node')
    wait(c, "SELECT count(*) = 1 FROM ztype.dictionary_inventory WHERE slot = 4 AND name = 'fourth';", what="the 'fourth' row on the converted node")
    c.equal(equal_sql(n=ROWS + 10))
    c.equal(policy_sql())
    c.equal(clean_sql())
    window = log_since(converted, mark)
    assert 'WARNING' not in window and 'ERROR' not in window, window
    print('PASS: pg_createsubscriber: registry and frames arrive physically, later rows and registrations arrive logically under the same slots', flush=True)


def promoted(p, st, sub, work, standby):
    """Physical promotion: the standby becomes primary with the registry it replayed, writes and
    training work at once, and a new subscriber seeded by the tool from it applies at the policy."""
    st.query('SELECT pg_promote(true, 60);')
    st.equal('SELECT NOT pg_is_in_recovery();')
    st.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|first\n2|second\n3|third\n4|fourth')
    mark = log_mark(standby)
    st.query(f"INSERT INTO messages VALUES (9999, {body('9999')}, jsonb_build_object('body', {body('9999')}), convert_to({body('9999')}, 'UTF8'));")
    st.equal("SELECT i.level, i.dict_name, ztype.validate(m.body) IS NULL FROM messages m, ztype.inspect(m.body) i WHERE m.id = 9999;", '6|first|t')
    st.equal(training('promoted'), '5')
    window = log_since(standby, mark)
    assert 'WARNING' not in window and 'ERROR' not in window, window
    run([BIN / 'createdb', 'promoted'], env=sub.env)
    n = sub.session('promoted')
    n.query('CREATE EXTENSION ztype;')
    code, out, err = tool('--source', conninfo(work, STANDBY_PORT), '--target', conninfo(work, SUB_PORT, 'promoted'))
    assert code == 0 and "imported slot 5 'promoted'" in out and out.count('imported') == 5, (code, out, err)
    n.query("CREATE TABLE messages(id integer PRIMARY KEY, body ztext(6,'first'), meta zjsonb(6,'first'), blob zbytea);")
    subscribe(n, 'sub_promoted', work, 'pub_tables', port=STANDBY_PORT)
    wait_synced(n, 'sub_promoted', 1)
    n.equal(equal_sql(n=ROWS + 11))
    n.equal(policy_sql())
    n.equal(clean_sql())
    n.close()
    print('PASS: promoted standby: registry present, writes and train_and_add work, a new subscription from it applies at the policy', flush=True)


def failed_over(p, st, h, sub, work):
    """The logical subscription follows: pointed at the promoted node, it resumes on the
    synchronised slot, receives the dictionary registered after the promotion, and applies a new
    table that names it. The old primary's copy of the slot goes idle and can be dropped."""
    mark = log_mark(sub)
    h.query(f"ALTER SUBSCRIPTION sub_ha CONNECTION 'host={work} port={STANDBY_PORT} dbname=postgres';")
    wait(h, "SELECT pid IS NOT NULL FROM pg_stat_subscription WHERE subname = 'sub_ha' AND relid IS NULL;", what='the apply worker on the new connection')
    wait(st, "SELECT active FROM pg_replication_slots WHERE slot_name = 'sub_ha';", what='the synchronised slot to be taken over')
    wait(h, "SELECT count(*) = 1 FROM ztype.dictionary_inventory WHERE slot = 5 AND name = 'promoted';", what="the 'promoted' row after failover")
    # The subscriber's table exists before any row for it is decoded: the apply worker resolves a
    # published relation locally before deciding whether it is subscribed yet.
    st.query("CREATE TABLE after_failover(id integer PRIMARY KEY, body ztext(6,'promoted'));")
    st.query('ALTER PUBLICATION pub_all ADD TABLE after_failover;')
    h.query("CREATE TABLE after_failover(id integer PRIMARY KEY, body ztext(6,'promoted'));")
    h.query('ALTER SUBSCRIPTION sub_ha REFRESH PUBLICATION;')
    wait_synced(h, 'sub_ha', 4)
    st.query(f"INSERT INTO after_failover SELECT i, {body('i')} FROM generate_series(1, 10) i;")
    wait(h, 'SELECT count(*) FROM after_failover;', '10', what='rows of the table created after failover')
    h.equal(f"SELECT bool_and(body::text = {body('id')}) FROM after_failover;")
    h.equal(policy_sql(table='after_failover', name="'promoted'"))
    h.equal('SELECT count(*) = 0 FROM after_failover WHERE ztype.validate(body) IS NOT NULL;')
    wait(h, f'SELECT count(*) = {ROWS + 11} FROM messages;', what='the row written on the promoted node')
    window = log_since(sub, mark)
    assert 'is not available' not in window and 'ERROR' not in window, window
    wait(p, "SELECT NOT active FROM pg_replication_slots WHERE slot_name = 'sub_ha';", what='the old primary slot to go idle')
    p.query("SELECT pg_drop_replication_slot('sub_ha');")
    print('PASS: failover slot: subscription repointed to the promoted node, streaming resumes, rows carry the dictionary registered after failover', flush=True)


def main():
    with tempfile.TemporaryDirectory(prefix='ztype-ha-') as tmp:
        work = Path(tmp)
        sharedir = share(work, 'share', t.library().with_suffix(''))
        pub = Cluster(BIN, work, 'pub', PUB_PORT, sharedir,
                      extra="wal_level = logical\nmax_wal_senders = 20\nmax_replication_slots = 20\n"
                            "synchronized_standby_slots = 'ztype_standby'\n")
        sub = Cluster(BIN, work, 'sub', SUB_PORT, sharedir, extra=WORKERS + 'wal_level = logical\n')
        pub.start()
        sub.start()
        standby = converted = None
        try:
            p = pub.session()
            publisher_seed(p)
            register_second(p)
            p.query(f"INSERT INTO later SELECT i, {body('i')} FROM generate_series(1, 10) i;")
            run([BIN / 'pg_basebackup', '-D', work / 'standby', '-d', primary_conninfo(work),
                 '-R', '-C', '-S', 'ztype_standby', '-X', 'stream'], env=pub.env)
            # hot_standby_feedback, sync_replication_slots and a primary_conninfo with dbname= are
            # what the slot synchronisation worker requires; the empty synchronized_standby_slots
            # overrides the primary's copied setting, which a promoted node must not keep.
            standby = Cluster(BIN, work, 'standby', STANDBY_PORT, sharedir, adopt=True,
                              extra="hot_standby = on\nhot_standby_feedback = on\nsync_replication_slots = on\n"
                                    "synchronized_standby_slots = ''\n")
            standby.start()
            st = standby.session()
            st.equal('SELECT pg_is_in_recovery();')
            wait_replayed(p, st)

            h = failover_subscription(sub, work)
            slot_synced(p, st)
            p.equal(training('third'), '3')
            converted, c = converted_subscriber(p, work, sharedir)
            p.equal(training('fourth'), '4')
            converted_verdict(p, converted, c)
            wait_replayed(p, st)
            st.equal('SELECT slot, name FROM ztype.dictionary_inventory ORDER BY slot;', '1|first\n2|second\n3|third\n4|fourth')
            wait(h, "SELECT count(*) = 4 FROM ztype.dictionary_inventory;", what='the registry on the failover subscriber')

            promoted(p, st, sub, work, standby)
            failed_over(p, st, h, sub, work)
            for s in (p, st, h, c):
                s.close()
        except BaseException:
            print(pub.diagnostics())
            print(sub.diagnostics())
            for cluster in (standby, converted):
                if cluster:
                    print(cluster.diagnostics())
            raise
        finally:
            for cluster in (converted, standby):
                if cluster:
                    cluster.stop('immediate')
            sub.stop('immediate')
            pub.stop('immediate')
        for cluster in (pub, sub, standby, converted):
            cluster.check_reports()


if __name__ == '__main__':
    main()
