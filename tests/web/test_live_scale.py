"""Real HTTP/advisory-lock regressions for bounded live renewal/admission.

Loopback fixture only. Synthetic rows and lock barriers make cap/expiry races
repeatable; every operation under test travels through the application HTTP route.
"""
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
import hashlib
import json
import secrets
import time
import unittest
import uuid

import psycopg
import requests

import test_api as api
from test_live import LiveContract


class LiveScale(LiveContract):
    def setUp(self):
        self.extra = []
        super().setUp()

    def tearDown(self):
        try:
            for session,digest,_ in self.extra:
                session.close()
                self.db.execute('DELETE FROM photocraft.sessions WHERE hash=%s',(digest,))
        finally:
            super().tearDown()

    def actor(self, account=0):
        token = secrets.token_hex(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',(digest,self.accounts[account][0]))
        session = requests.Session(); session.trust_env = False
        session.headers.update({'Origin':api.BASE,'X-Photocraft-Account':self.accounts[account][0]})
        session.cookies.set('pc_session',token)
        self.extra.append((session,digest,token))
        return session,digest

    def seeded(self, count, account=0):
        result = []
        for _ in range(count):
            session,digest = self.actor(account)
            tab = str(uuid.uuid4())
            self.db.execute("INSERT INTO photocraft.live_previews(session_hash,tab_id,project_id,seq,base_revision,cursor) VALUES(%s,%s,%s,1,1,%s::jsonb)",
                            (digest,tab,self.pid,json.dumps({'x':1,'y':1})))
            result.append((session,digest,tab))
        # Fixture setup time must not accidentally expire early peers.
        self.db.execute('UPDATE photocraft.live_previews SET seen_at=clock_timestamp() WHERE project_id=%s',(self.pid,))
        return result

    def send(self, session, body, path=None):
        # Separate Requests transport per concurrent call; no shared session state.
        with requests.Session() as client:
            client.trust_env = False
            return client.put(api.BASE+(path or self.path),headers=dict(session.headers),cookies=session.cookies.get_dict(),json=body,timeout=5,allow_redirects=False)

    def active_count(self, digest=None):
        return self.db.execute("SELECT count(*) FROM photocraft.live_previews WHERE project_id=%s AND seen_at>clock_timestamp()-interval '2seconds' AND (cursor IS NOT NULL OR gesture IS NOT NULL) AND (%s::text IS NULL OR session_hash=%s)",
                               (self.pid,digest,digest)).fetchone()[0]

    def key(self, kind, ident):
        return self.db.execute('SELECT hashtextextended(%s,0)',('photocraft.live.'+kind+':'+ident,)).fetchone()[0]

    @contextmanager
    def barrier(self, key, shared=False):
        with psycopg.connect(api.DATABASE) as db:
            name = 'pg_advisory_xact_lock_shared' if shared else 'pg_advisory_xact_lock'
            db.execute('SELECT '+name+'(%s)',(key,))
            yield db

    def wait_blocked(self, key, count=1):
        unsigned = key & ((1<<64)-1)
        deadline = time.monotonic()+.5
        while time.monotonic()<deadline:
            rows = self.db.execute("SELECT count(*) FROM pg_locks WHERE locktype='advisory' AND NOT granted AND classid::bigint=%s AND objid::bigint=%s AND database=(SELECT oid FROM pg_database WHERE datname=current_database())",
                                   (unsigned>>32,unsigned&0xffffffff)).fetchone()[0]
            if rows>=count:
                return
            time.sleep(.005)
        self.fail('Expected live request did not wait on the fixture advisory barrier')

    def wait_row_blocked(self, blocker_pid):
        deadline = time.monotonic()+.5
        while time.monotonic()<deadline:
            # pg_stat_activity.query is truncated (normally to 1024 bytes), before
            # this statement reaches its INSERT after AUTH and decision CTEs.
            # This fixture backend holds only our unique preview-row lock: its
            # actual blocking relationship identifies the write without SQL text.
            blocked = self.db.execute("SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND state='active' AND wait_event_type='Lock' AND %s=ANY(pg_blocking_pids(pid))",
                                      (blocker_pid,)).fetchone()[0]
            if blocked:
                return
            time.sleep(.005)
        self.fail('Expected exchange did not reach its preview-row write barrier')

    def expire_during_wait(self, blocker, rows):
        blocker_pid = blocker.execute('SELECT pg_backend_pid()').fetchone()[0]
        started = self.db.execute('SELECT min(query_start) FROM pg_stat_activity WHERE datname=current_database() AND %s=ANY(pg_blocking_pids(pid))',
                                  (blocker_pid,)).fetchone()[0]
        self.assertIsNotNone(started, 'The exchange must already be waiting on our lock')
        # These leases are active at the outer function call's timestamp, but
        # expire before its post-lock query. Reusing statement_timestamp inside
        # PL/pgSQL would incorrectly retain them despite a fresh MVCC snapshot.
        for digest,tab in rows:
            self.db.execute("UPDATE photocraft.live_previews SET seen_at=%s::timestamptz-interval '1900milliseconds' WHERE session_hash=%s AND tab_id=%s",
                            (started,digest,tab))
        deadline = time.monotonic()+.5
        while time.monotonic()<deadline:
            expired = self.db.execute("SELECT clock_timestamp()>%s::timestamptz+interval '120milliseconds'",(started,)).fetchone()[0]
            if expired:
                return
            time.sleep(.005)
        self.fail('Fixture leases did not expire within the bounded lock wait')

    def test_scale_existing_renewal_shares_room_but_new_admission_waits(self):
        session,_,tab = self.seeded(1)[0]
        new,_ = self.actor()
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.barrier(key,shared=True):
                # Red on the previous all-exclusive path: it times out behind this
                # reader lock. A genuine shared renewal completes while it is held.
                renewal = pool.submit(self.send,session,self.state(2,tab=tab))
                response = renewal.result(timeout=1)
                self.assertEqual(response.status_code,200,response.text)
                self.assertTrue(response.json()['accepted'])
                admission = pool.submit(self.send,new,self.state(1,tab=str(uuid.uuid4())))
                self.wait_blocked(key)
                self.assertFalse(admission.done())
            response = admission.result(timeout=2)
            self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(self.active_count(),2)

    def test_scale_concurrent_room_admissions_never_exceed64(self):
        self.seeded(63)
        candidates = [(self.actor()[0],str(uuid.uuid4())) for _ in range(8)]
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda pair:self.send(pair[0],self.state(tab=pair[1])),candidates))
        self.assertEqual(sorted(r.status_code for r in responses),[200]+[429]*7)
        self.assertEqual(self.active_count(),64)

    def test_scale_concurrent_session_admissions_never_exceed8(self):
        session,digest = self.actor()
        other = self.project()
        self.upload(other)
        paths = [self.path,f'/api/projects/{other}/live']
        tabs = [str(uuid.uuid4()) for _ in range(15)]
        for tab in tabs[:7]:
            self.assertEqual(self.send(session,self.state(tab=tab)).status_code,200)
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda pair:self.send(session,self.state(tab=pair[1]),paths[pair[0]%2]),enumerate(tabs[7:])))
        self.assertEqual(sorted(r.status_code for r in responses),[200]+[429]*7)
        count = self.db.execute("SELECT count(*) FROM photocraft.live_previews WHERE session_hash=%s AND seen_at>clock_timestamp()-interval '2seconds' AND (cursor IS NOT NULL OR gesture IS NOT NULL)",(digest,)).fetchone()[0]
        self.assertEqual(count,8)

    def test_scale_rebound_active_tab_cannot_bypass_target_room_capacity(self):
        origin = self.project()
        self.upload(origin)
        rows = self.seeded(64)
        session,digest = self.actor()
        tab = str(uuid.uuid4())
        self.db.execute("INSERT INTO photocraft.live_previews(session_hash,tab_id,project_id,seq,base_revision,cursor) VALUES(%s,%s,%s,1,1,%s::jsonb)",(digest,tab,origin,json.dumps({'x':1,'y':1})))
        before = self.db.execute('SELECT project_id,seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone()
        response = self.send(session,self.state(2,tab=tab))
        self.assertEqual(response.status_code,429,response.text)
        self.assertEqual(self.db.execute('SELECT project_id,seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone(),before)
        self.assertEqual(self.active_count(),64)
        # Clearing one slot permits admission; the same session/tab row moves.
        peer,_,peer_tab = rows[0]
        self.assertEqual(self.send(peer,self.state(2,tab=peer_tab,cursor=None)).status_code,200)
        self.assertEqual(self.send(session,self.state(2,tab=tab)).status_code,200)
        self.assertEqual(self.active_count(),64)

    def test_scale_expired_renewal_reenters_exclusive_admission(self):
        rows = self.seeded(64)
        session,digest,tab = rows[0]
        new,_ = self.actor()
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.barrier(key) as db:
                renewal = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key)
                # Expire only the target row after the request has started waiting.
                db.execute("UPDATE photocraft.live_previews SET seen_at=clock_timestamp()-interval '3seconds' WHERE session_hash=%s",(digest,))
                admission = pool.submit(self.send,new,self.state(tab=str(uuid.uuid4())))
            responses = [renewal.result(timeout=2),admission.result(timeout=2)]
        self.assertEqual(sorted(r.status_code for r in responses),[200,429])
        self.assertEqual(self.active_count(),64)

    def test_scale_duplicate_and_replay_do_not_renew_lease(self):
        session,digest,tab = self.seeded(1)[0]
        self.assertEqual(self.send(session,self.state(20,tab=tab)).status_code,200)
        before = self.db.execute('SELECT seen_at FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s',(digest,tab)).fetchone()[0]
        with ThreadPoolExecutor(max_workers=8) as pool:
            responses = list(pool.map(lambda seq:self.send(session,self.state(seq,tab=tab)),[20,19,20,1,19,20,0,10]))
        self.assertTrue(all(r.status_code==200 and not r.json()['accepted'] for r in responses))
        after = self.db.execute('SELECT seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s',(digest,tab)).fetchone()
        self.assertEqual(after,(20,before))

    def test_scale_membership_is_rechecked_after_room_lock_wait(self):
        session,digest,tab = self.seeded(1,account=1)[0]
        before = self.db.execute('SELECT seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone()
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.barrier(key) as db:
                renewal = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key)
                # Same exclusive project barrier as the actual membership endpoint.
                db.execute('DELETE FROM photocraft.members WHERE project_id=%s AND email=%s',(self.pid,self.accounts[1][1]))
            response = renewal.result(timeout=2)
        self.assertEqual(response.status_code,404,response.text)
        self.assertNotIn('peers',response.json())
        self.assertEqual(self.db.execute('SELECT seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone(),before)
        self.assertEqual(self.peers(),[])

    def test_scale_logout_barrier_revokes_a_queued_live_request(self):
        session,digest,tab = self.seeded(1)[0]
        key = self.key('session',digest)
        def logout():
            with requests.Session() as client:
                client.trust_env=False
                return client.post(api.BASE+'/api/logout',headers=dict(session.headers),cookies=session.cookies.get_dict(),timeout=5,allow_redirects=False)
        with ThreadPoolExecutor(max_workers=2) as pool:
            with self.barrier(key):
                signed_out = pool.submit(logout)
                self.wait_blocked(key)
                renewal = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key,count=2)
            self.assertEqual(signed_out.result(timeout=2).status_code,200)
            response=renewal.result(timeout=2)
        self.assertEqual(response.status_code,401,response.text)
        self.assertNotIn('peers',response.json())
        self.assertEqual(self.db.execute('SELECT count(*) FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone()[0],0)

    def test_scale_expired_session_after_room_lock_wait_cannot_exchange(self):
        session,digest,tab = self.seeded(1)[0]
        self.seeded(1,account=1)
        before = self.db.execute('SELECT seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone()
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.barrier(key) as db:
                exchange = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key)
                db.execute("UPDATE photocraft.sessions SET expires_at=clock_timestamp()-interval '1second' WHERE hash=%s",(digest,))
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,401,response.text)
        self.assertNotIn('peers',response.json())
        self.assertEqual(self.db.execute('SELECT seq,seen_at FROM photocraft.live_previews WHERE session_hash=%s',(digest,)).fetchone(),before)

    def test_scale_exchange_filters_peers_using_post_lock_authorization(self):
        session,_,tab = self.seeded(1)[0]
        self.seeded(1,account=1)
        _,expired,_ = self.seeded(1,account=2)[0]
        _,_,retained_tab = self.seeded(1)[0]
        signed_out,_,_ = self.seeded(1)[0]
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.barrier(key) as db:
                exchange = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key)
                # Changes committed while the caller waits must affect the exchange,
                # not only its next separate GET. The retained peer prevents an
                # empty-response shortcut from satisfying the authorization check.
                self.req(signed_out,'POST','/api/logout',json={})
                db.execute('DELETE FROM photocraft.members WHERE project_id=%s AND email=%s',(self.pid,self.accounts[1][1]))
                db.execute("UPDATE photocraft.sessions SET expires_at=clock_timestamp()-interval '1second' WHERE hash=%s",(expired,))
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,200,response.text)
        reply = response.json()
        self.assertTrue(reply['accepted'])
        self.assertEqual(reply['role'],'owner')
        self.assertEqual([p['tab'] for p in reply['peers']],[retained_tab])

    def test_scale_exchange_role_reflects_downgrade_after_lock_wait(self):
        session,_,tab = self.seeded(1,account=1)[0]
        _,_,owner_tab = self.seeded(1)[0]
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.barrier(key) as db:
                exchange = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_blocked(key)
                db.execute("UPDATE photocraft.members SET role='view' WHERE project_id=%s AND email=%s",(self.pid,self.accounts[1][1]))
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['role'],'view')
        self.assertEqual([p['tab'] for p in response.json()['peers']],[owner_tab])
        denied = self.send(session,self.state(3,tab=tab,gesture={'events':[{'gesture':1,'sequence':1,'kind':'cancel'}]}))
        self.assertEqual(denied.status_code,403,denied.text)
        self.assertNotIn('peers',denied.json())

    def test_scale_exchange_aggregates_in_write_snapshot_and_waits_for_commit(self):
        session,digest,tab = self.seeded(1)[0]
        _,peer_digest,peer_tab = self.seeded(1,account=1)[0]
        with ThreadPoolExecutor(max_workers=1) as pool:
            with psycopg.connect(api.DATABASE) as db:
                db.execute('SELECT seq FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s FOR UPDATE',(digest,tab))
                blocker_pid = db.execute('SELECT pg_backend_pid()').fetchone()[0]
                exchange = pool.submit(self.send,session,self.state(2,tab=tab))
                self.wait_row_blocked(blocker_pid)
                self.assertFalse(exchange.done(),'Exchange returned before its own write committed')
                self.assertEqual(self.db.execute('SELECT seq FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s',(digest,tab)).fetchone()[0],1)
                # The UPDATE statement's snapshot already exists. A later peer
                # commit must appear in the next exchange/read, not be mixed into
                # this exchange by an added post-commit SELECT round trip.
                self.db.execute('UPDATE photocraft.live_previews SET seq=2,cursor=%s::jsonb WHERE session_hash=%s AND tab_id=%s',
                                (json.dumps({'x':99,'y':99}),peer_digest,peer_tab))
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,200,response.text)
        reply = response.json()
        self.assertTrue(reply['accepted'])
        self.assertEqual(reply['seq'],2)
        self.assertEqual(reply['peers'][0]['seq'],1)
        self.assertEqual(reply['peers'][0]['cursor'],{'x':1,'y':1})
        self.assertEqual(self.db.execute('SELECT seq FROM photocraft.live_previews WHERE session_hash=%s AND tab_id=%s',(digest,tab)).fetchone()[0],2)
        read = self.req(session,'GET',self.path+'?tab='+tab).json()
        self.assertEqual(read['peers'][0]['seq'],2)
        self.assertEqual(read['peers'][0]['cursor'],{'x':99,'y':99})

    def test_scale_post_lock_clock_expires_renewal_and_peer_before_exchange(self):
        session,digest,tab = self.seeded(1)[0]
        _,peer_digest,peer_tab = self.seeded(1,account=1)[0]
        room_key = self.key('project',self.pid)
        session_key = self.key('session',digest)
        with ThreadPoolExecutor(max_workers=1) as pool:
            # This reader permits the first shared attempt but blocks exclusive
            # re-admission. It makes the expiry decision externally observable.
            with self.barrier(room_key,shared=True):
                with self.barrier(session_key) as blocker:
                    exchange = pool.submit(self.send,session,self.state(2,tab=tab))
                    self.wait_blocked(session_key)
                    self.expire_during_wait(blocker,[(digest,tab),(peer_digest,peer_tab)])
                self.wait_blocked(room_key)
                self.assertFalse(exchange.done(),'An expired renewal skipped exclusive re-admission')
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['accepted'])
        self.assertEqual(response.json()['peers'],[], 'A peer expired during the lock wait leaked into the exchange')

    def test_scale_post_lock_clock_frees_expired_session_slot(self):
        session,digest = self.actor()
        tabs = [str(uuid.uuid4()) for _ in range(9)]
        for tab in tabs[:8]:
            self.db.execute("INSERT INTO photocraft.live_previews(session_hash,tab_id,project_id,seq,base_revision,cursor) VALUES(%s,%s,%s,1,1,%s::jsonb)",
                            (digest,tab,self.pid,json.dumps({'x':1,'y':1})))
        key = self.key('project',self.pid)
        with ThreadPoolExecutor(max_workers=1) as pool:
            with self.barrier(key,shared=True) as blocker:
                exchange = pool.submit(self.send,session,self.state(tab=tabs[8]))
                # A new tab first falls back, then waits for exclusive admission.
                self.wait_blocked(key)
                self.expire_during_wait(blocker,[(digest,tabs[0])])
            response = exchange.result(timeout=2)
        self.assertEqual(response.status_code,200,response.text)
        self.assertTrue(response.json()['accepted'])
        self.assertEqual(self.active_count(digest),8)

    def test_scale_denied_exchange_does_not_wait_for_private_project_lock(self):
        key = self.key('project',self.pid)
        with self.barrier(key):
            denied = self.send(self.outsider,self.state())
            self.assertEqual(denied.status_code,404,denied.text)
            self.assertNotIn('peers',denied.json())
            anonymous = self.send(self.anon,self.state())
            self.assertEqual(anonymous.status_code,401,anonymous.text)
        self.assertEqual(self.active_count(),0)


def load_tests(loader,tests,pattern):
    return unittest.TestSuite(LiveScale(name) for name in sorted(LiveScale.__dict__) if name.startswith('test_scale_'))


if __name__=='__main__':
    unittest.main()
