"""Opt-in bounded HTTP load on a uniquely created, disposable loopback database.

Default is a dry plan. --execute creates its own database and bounded worker fleet
from an existing binary, never builds, and drops only that database after stopping
every worker. Actors route evenly and stick to one worker sharing the same database.
Logical clients model one in-flight GET and one PUT each; missed ticks coalesce.
HTTP service time is not browser paint latency. Admission failures fail the gate.
"""
import argparse
import asyncio
from collections import Counter
from contextlib import contextmanager, ExitStack
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import platform
import resource
import secrets
import signal
import socket
import subprocess
import time
from urllib.parse import urlparse, urlunparse, urlencode
import uuid

import psycopg
from psycopg import sql
import requests

from benchmark_collaboration import fixture


ROOT = Path(__file__).resolve().parents[2]
MAX_BODY = 8*1024*1024


def arguments():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute', action='store_true', help='Explicitly run; omitted means no DB/process/network activity')
    p.add_argument('--binary', type=Path, default=ROOT.parent/'photocraft/target/cloud/debug/photocraft-cloud')
    p.add_argument('--database', default='postgresql://photocraft_test@127.0.0.1:55438/postgres')
    p.add_argument('--tls-ca', type=Path, help='Public CA for an operator-owned temporary TLS cluster; enforces verify-full in both clients')
    p.add_argument('--fixture', type=Path, default=ROOT.parent/'fixture.pcraft')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--context', required=True)
    p.add_argument('--clients', type=int, choices=[2, 10, 100, 1000], default=10)
    p.add_argument('--workers', type=int, choices=[1,2,4,8], default=1)
    p.add_argument('--db-pool-size', type=int, choices=range(1,9), default=5)
    p.add_argument('--scenario', choices=['rooms10', 'hot-room'], default='rooms10')
    p.add_argument('--pacing', choices=['current', 'reduced'], default='current',
                   help='current=GET80/PUT40ms; reduced=GET250/PUT100ms experimental comparison only')
    p.add_argument('--payload', choices=['cursor', 'points'], default='cursor')
    p.add_argument('--duration', type=float, default=10)
    p.add_argument('--warmup', type=float, default=3)
    p.add_argument('--cooldown', type=float, default=10)
    p.add_argument('--max-inflight', type=int, default=64)
    p.add_argument('--max-requests', type=int, default=100000)
    p.add_argument('--max-response-mib', type=int, default=100)
    a = p.parse_args()
    u = urlparse(a.database)
    if (u.scheme not in {'postgres', 'postgresql'} or u.hostname != '127.0.0.1' or u.password
            or not u.username or u.path != '/postgres' or u.query or u.fragment):
        p.error('Only a password-free literal127.0.0.1 maintenance /postgres URL is accepted')
    if not (0 < a.duration <= 20 and 0 <= a.warmup <= 3 and 0 <= a.cooldown <= 10
            and 1 <= a.max_inflight <= 1000 and 1 <= a.max_requests <= 1000000
            and 1 <= a.max_response_mib <= 1024):
        p.error('Exceeds bounded duration/concurrency/request/byte limits')
    if a.workers*a.db_pool_size > 32:
        p.error('Simulated fleet database pool budget must not exceed32')
    if a.tls_ca is not None:
        a.tls_ca = a.tls_ca.resolve()
        if not a.tls_ca.is_file() or a.tls_ca.stat().st_size > 65536:
            p.error('TLS CA must be an existing public PEM file <=64KiB')
    return a


def distribution(values):
    v = sorted(values)
    if not v:
        return {'count': 0}
    return {'count': len(v), **{f'p{q}_ms': round(v[math.ceil(len(v)*q/100)-1], 3) for q in [50,95,99]},
            'max_ms': round(v[-1], 3)}


def db_stats(db):
    db.execute('SELECT pg_stat_clear_snapshot()')
    r = db.execute('SELECT xact_commit,xact_rollback,blks_read,blks_hit,tup_returned,tup_fetched,tup_inserted,tup_updated,tup_deleted,deadlocks,temp_bytes FROM pg_stat_database WHERE datname=current_database()').fetchone()
    names = ['xact_commit','xact_rollback','blks_read','blks_hit','tup_returned','tup_fetched','tup_inserted','tup_updated','tup_deleted','deadlocks','temp_bytes']
    return dict(zip(names,r)) | {'database_bytes': db.execute('SELECT pg_database_size(current_database())').fetchone()[0]}


@contextmanager
def isolated(a, report):
    name = 'photocraft_scale_'+uuid.uuid4().hex
    u = urlparse(a.database)
    database = urlunparse(u._replace(path='/'+name))
    processes = []
    workers = []
    report['workers'] = workers
    created = False
    tls = {'sslmode':'verify-full','sslrootcert':str(a.tls_ca)} if a.tls_ca else {'sslmode':'disable'}
    with psycopg.connect(a.database, autocommit=True, connect_timeout=5, **tls) as admin:
        try:
            if int(admin.info.server_version) < 130000:
                raise RuntimeError('Disposable cleanup requires PostgreSQL13+')
            admin.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier(name)))
            created = True
            report['disposable_database'] = name
            with ExitStack() as reserved:
                sockets = [reserved.enter_context(socket.socket()) for _ in range(a.workers)]
                for item in sockets:
                    item.bind(('127.0.0.1',0))
                ports = [item.getsockname()[1] for item in sockets]
                base = f'http://127.0.0.1:{ports[0]}'
                for index,(port,item) in enumerate(zip(ports,sockets)):
                    # Release only the next worker's reserved port immediately
                    # before spawning; the others stay reserved during startup.
                    item.close()
                    app_name = f'photocraft_scale_worker_{index}'
                    worker_db = urlunparse(urlparse(database)._replace(query=urlencode({'application_name':app_name})))
                    # No inherited provider credentials or database overrides.
                    env = {'PATH':os.environ.get('PATH','/usr/bin:/bin'),'DATABASE_URL':worker_db,
                           'APP_ORIGIN':base,'PORT':str(port),'CLOUD_LOCAL_DEV':'1',
                           'PHOTOCRAFT_DB_POOL_SIZE':str(a.db_pool_size),
                           'SUPABASE_URL':'','SUPABASE_ANON_KEY':'','PUBLIC_DIR':str(a.output.parent/'no-public')}
                    if a.tls_ca:
                        env.pop('CLOUD_LOCAL_DEV')
                        env['SUPABASE_CA_CERT'] = a.tls_ca.read_text()
                    log_path = a.output.with_suffix('.server.log' if a.workers==1 else f'.worker-{index}.server.log')
                    with log_path.open('w') as log:
                        process = subprocess.Popen([str(a.binary.resolve())],env=env,stdout=log,stderr=subprocess.STDOUT)
                    processes.append(process)
                    workers.append({'index':index,'port':port,'pid':process.pid,'db_application_name':app_name})
            report['workers'] = workers
            for worker,process in zip(workers,processes):
                session = requests.Session(); session.trust_env = False
                deadline = time.monotonic()+15
                try:
                    while True:
                        if process.poll() is not None:
                            raise RuntimeError('Isolated server exited before readiness')
                        try:
                            r = session.get(f"http://127.0.0.1:{worker['port']}/api/config",timeout=2,allow_redirects=False)
                            if r.status_code == 200 and r.json().get('cloud'):
                                break
                        except requests.RequestException:
                            pass
                        if time.monotonic() >= deadline:
                            raise RuntimeError('Isolated server failed cloud readiness; see sanitized server log')
                        time.sleep(.05)
                finally:
                    session.close()
            with psycopg.connect(database,autocommit=True,connect_timeout=5,application_name='photocraft_scale_monitor',**tls) as db:
                ssl = db.execute('SELECT bool_and(s.ssl),count(*),array_agg(DISTINCT s.version) FROM pg_stat_ssl s JOIN pg_stat_activity a USING(pid) WHERE a.datname=current_database()').fetchone()
                versions = [v.decode('ascii') if isinstance(v,bytes) else v for v in (ssl[2] or [])]
                report['database_tls_observed'] = {'all_connections_encrypted':ssl[0],'connection_count':ssl[1],'versions':versions}
                if a.tls_ca and not ssl[0]:
                    raise RuntimeError('TLS stage observed a non-TLS database connection')
                yield base, db, workers
        finally:
            for process in processes:
                if process.poll() is None:
                    process.terminate()
            for process in processes:
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill(); process.wait(timeout=5)
            if created:
                # Name is generated in this scope, never accepted from caller input.
                admin.execute(sql.SQL('DROP DATABASE {} WITH (FORCE)').format(sql.Identifier(name)))
                report['cleanup'] = {'own_worker_stopped':all(process.poll() is not None for process in processes),
                                     'workers_started':len(processes),'workers_stopped':sum(process.poll() is not None for process in processes),
                                     'own_database_dropped': True, 'shared_application_rows_touched': 0}


def seed(db, a, data, info):
    actors = []
    projects = [str(uuid.uuid4()) for _ in range(1 if a.scenario == 'hot-room' else math.ceil(a.clients/10))]
    for index in range(a.clients):
        ident, token = str(uuid.uuid4()), secrets.token_hex(32)
        actors.append({'id': ident, 'email': ident+'@example.invalid', 'token': token,
                       'tab': str(uuid.uuid4()),'worker':index%a.workers, 'project': projects[0 if a.scenario == 'hot-room' else index//10]})
    with db.transaction():
        with db.cursor() as c:
            c.executemany('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                          [(v['id'],v['email'],f'Scale actor{i}') for i,v in enumerate(actors)])
            c.executemany('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                          [(hashlib.sha256(v['token'].encode()).hexdigest(),v['id']) for v in actors])
            for project in projects:
                members = [v for v in actors if v['project'] == project]
                owner = members[0]['id']
                c.execute('INSERT INTO photocraft.projects(id,owner_id,title,revision,width,height) VALUES(%s,%s,%s,1,%s,%s)',
                          (project,owner,'Disposable scale fixture',info['width'],info['height']))
                c.execute('INSERT INTO photocraft.versions(project_id,revision,author_id,title,data,sha256) VALUES(%s,1,%s,%s,%s,%s)',
                          (project,owner,'Disposable scale fixture',data,info['sha256']))
                c.executemany('INSERT INTO photocraft.members(project_id,email,role) VALUES(%s,%s,\'edit\')',
                              [(project,v['email']) for v in members[1:]])
    return actors, projects


class Load:
    def __init__(self, a, base, actors, report, db, workers):
        self.a, self.base, self.actors, self.report = a, base, actors, report
        self.db, self.workers = db, workers
        self.resource_samples = []
        self.sem = asyncio.Semaphore(a.max_inflight)
        self.pools = [asyncio.LifoQueue() for _ in workers]
        self.worker_requests = [0 for _ in workers]
        self.worker_statuses = [Counter() for _ in workers]
        self.worker_active_statuses = [Counter() for _ in workers]
        self.worker_errors = [Counter() for _ in workers]
        self.stop = asyncio.Event()
        self.statuses = Counter(); self.errors = Counter()
        self.all_statuses = Counter(); self.all_errors = Counter()
        self.client_completed = Counter(); self.client_succeeded = Counter()
        self.http_failure_categories = Counter()
        self.status_samples = {}
        self.samples = {'GET': [], 'PUT': []}; self.lags = []; self.admissions = []
        self.peer_counts = Counter(); self.requests = 0; self.bytes_in = 0; self.bytes_out = 0
        self.inflight = 0; self.peak = 0; self.coalesced = 0; self.reason = None
        self.active_start = 0; self.active_end = 0
        self.payload = None if a.payload == 'cursor' else {'events': [
            {'gesture': 1, 'sequence': 0, 'kind': 'points', 'points': [[i%320,i%240,1,.1,.2,.3] for i in range(256)]}]}

    def halt(self, reason):
        if not self.stop.is_set():
            self.reason = reason
        self.stop.set()

    async def connection(self, worker):
        pool = self.pools[worker]
        if not pool.empty():
            return pool.get_nowait()
        return await asyncio.open_connection('127.0.0.1',self.workers[worker]['port'],limit=65536)

    async def http(self, method, actor, body):
        started = time.monotonic()
        worker = actor['worker']
        port = self.workers[worker]['port']
        reader, writer = await self.connection(worker)
        reusable = False
        try:
            path = f"/api/projects/{actor['project']}/live"+('?tab='+actor['tab'] if method == 'GET' else '')
            headers = (f'{method} {path} HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nOrigin: {self.base}\r\n'
                f"Cookie: pc_session={actor['token']}\r\nX-Photocraft-Account: {actor['id']}\r\n"
                'Accept-Encoding: identity\r\nConnection: keep-alive\r\nContent-Type: application/json\r\n'
                f'Content-Length: {len(body)}\r\n\r\n').encode()+body
            self.bytes_out += len(headers)
            writer.write(headers); await writer.drain()
            raw = await reader.readuntil(b'\r\n\r\n')
            if len(raw) > 16384:
                raise ValueError('response_header_limit')
            lines = raw.decode('ascii').split('\r\n')
            status = int(lines[0].split()[1])
            h = {k.lower():v.strip() for k,v in (line.split(':',1) for line in lines[1:] if ':' in line)}
            length = int(h.get('content-length','-1'))
            if not 0 <= length <= MAX_BODY:
                raise ValueError('response_length_or_transfer_encoding_unsupported')
            capture = bytearray()
            remaining = length
            while remaining:
                block = await reader.readexactly(min(remaining,65536))
                remaining -= len(block); self.bytes_in += len(block)
                if len(capture)+len(block) <= 65536:
                    capture.extend(block)
                if self.bytes_in > self.a.max_response_mib*1024*1024:
                    self.halt('response_byte_budget'); raise ValueError('response_byte_budget')
            self.bytes_in += len(raw)
            if status >= 400:
                error = json.loads(capture).get('error') if length <= 65536 and h.get('content-type','').startswith('application/json') else None
                # Retain only static categories, never request/account values or
                # arbitrary response text. Latencies stay separated by status.
                category = 'ordinary_admission' if error == 'This service is busy. Your local work is safe; retry shortly.' else 'other_http_error'
                self.http_failure_categories[f'{status} {category}'] += 1
            if status == 200 and length <= 65536:
                value = json.loads(capture)
                if value.get('revision') != 1:
                    raise ValueError('wrong_canonical_revision')
                if method == 'GET':
                    self.peer_counts[len(value.get('peers',[]))] += 1
            reusable = h.get('connection','').lower() != 'close'
            return status, (time.monotonic()-started)*1000
        finally:
            # Bound retained idle sockets as well as active operations, while
            # allowing a worker's actual request burst to exceed its even share.
            if reusable and self.pools[worker].qsize() < math.ceil(self.a.max_inflight/self.a.workers):
                self.pools[worker].put_nowait((reader,writer))
            else:
                writer.close()
                await writer.wait_closed()

    async def client(self, actor, method, index):
        period = (.08 if method == 'GET' else .04) if self.a.pacing == 'current' else (.25 if method == 'GET' else .1)
        due = self.active_start-self.a.warmup+(index%100)/100*period
        sequence = 0
        while not self.stop.is_set() and time.monotonic() < self.active_end:
            await asyncio.sleep(max(0,due-time.monotonic()))
            if self.stop.is_set() or time.monotonic() >= self.active_end:
                break
            offered = time.monotonic()
            sampled = offered >= self.active_start
            body = b''
            if method == 'PUT':
                sequence += 1
                body = json.dumps({'tab':actor['tab'],'seq':sequence,'baseRevision':1,
                    'cursor':{'x':sequence%320,'y':index%240},'gesture':self.payload}, separators=(',',':')).encode()
            status = None
            try:
                async with asyncio.timeout(2):
                    async with self.sem:
                        acquired = time.monotonic()
                        if self.requests >= self.a.max_requests:
                            self.halt('request_budget'); break
                        self.requests += 1
                        self.worker_requests[actor['worker']] += 1
                        self.inflight += 1; self.peak = max(self.peak,self.inflight)
                        try:
                            status, elapsed = await self.http(method,actor,body)
                            self.all_statuses[f'{method} {status}'] += 1
                            self.worker_statuses[actor['worker']][f'{method} {status}'] += 1
                            if sampled:
                                self.statuses[f'{method} {status}'] += 1
                                self.worker_active_statuses[actor['worker']][f'{method} {status}'] += 1
                                self.samples[method].append(elapsed)
                                self.status_samples.setdefault(f'{method} {status}',[]).append(elapsed)
                                self.client_completed[index] += 1
                                if status == 200:
                                    self.client_succeeded[index] += 1
                        finally:
                            self.inflight -= 1
                        if sampled:
                            self.admissions.append((acquired-offered)*1000)
            except Exception as error:
                kind = type(error).__name__+(':'+str(error) if isinstance(error,ValueError) else '')
                self.all_errors[kind] += 1
                self.worker_errors[actor['worker']][kind] += 1
                if sampled:
                    self.errors[kind] += 1
            ended = time.monotonic()
            if sampled:
                self.lags.append((ended-due)*1000)
                self.coalesced += max(0,int((ended-offered)/period)-1)
            # One in-flight request per direction, coalescing missed ticks. The
            # current Rust client backs failed writes off500ms and reads250ms.
            retry = .5 if method == 'PUT' else .25
            due = ended+retry+period if status is None or status >= 400 else max(offered+period,ended)

    async def run(self):
        now = time.monotonic(); self.active_start = now+self.a.warmup; self.active_end = self.active_start+self.a.duration
        finished = asyncio.Event()
        def resource_sample():
            row = self.db.execute("SELECT count(*),count(*) FILTER(WHERE state='active'),count(*) FILTER(WHERE wait_event_type='Lock') FROM pg_stat_activity WHERE datname=current_database()").fetchone()
            connections = {name:(count,active,locks) for name,count,active,locks in self.db.execute("SELECT application_name,count(*),count(*) FILTER(WHERE state='active'),count(*) FILTER(WHERE wait_event_type='Lock') FROM pg_stat_activity WHERE datname=current_database() GROUP BY application_name").fetchall()}
            usage = subprocess.check_output(['ps','-p',','.join(str(w['pid']) for w in self.workers),'-o','pid=,rss=,%cpu='],text=True).splitlines()
            process_usage = {int(parts[0]):(int(parts[1]),float(parts[2])) for line in usage if len(parts:=line.split())==3}
            per_worker = [{'index':w['index'],'rss_kib':process_usage.get(w['pid'],(0,0))[0],
                           'cpu_percent':process_usage.get(w['pid'],(0,0))[1],
                           'db_connections':connections.get(w['db_application_name'],(0,0,0))[0],
                           'db_active':connections.get(w['db_application_name'],(0,0,0))[1],
                           'db_lock_waits':connections.get(w['db_application_name'],(0,0,0))[2]} for w in self.workers]
            failure = None
            if any(w['db_connections']>self.a.db_pool_size for w in per_worker) or row[0]>self.a.workers*self.a.db_pool_size+1:
                failure = 'database_connection_budget'
            if len(process_usage)!=len(self.workers):
                failure = 'worker_exited'
            return {'seconds':round(time.monotonic()-now,3),'db_connections':row[0],
                    'db_active_including_monitor':row[1],'db_lock_waits':row[2],
                    'worker_rss_kib':sum(w['rss_kib'] for w in per_worker),
                    'worker_cpu_percent':sum(w['cpu_percent'] for w in per_worker),'workers':per_worker,'failure':failure}
        async def monitor():
            while not finished.is_set():
                sample = await asyncio.to_thread(resource_sample)
                self.resource_samples.append(sample)
                if sample['failure']:
                    self.halt(sample['failure'])
                try:
                    await asyncio.wait_for(finished.wait(),timeout=1)
                except TimeoutError:
                    pass
        monitoring = asyncio.create_task(monitor())
        try:
            await asyncio.gather(*(self.client(actor,method,index) for index,actor in enumerate(self.actors) for method in ['GET','PUT']))
        finally:
            finished.set()
            await monitoring
            for pool in self.pools:
                while not pool.empty():
                    _,writer = pool.get_nowait(); writer.close(); await writer.wait_closed()
        elapsed = time.monotonic()-now
        return {'elapsed_including_warmup_seconds':elapsed, 'requested_active_seconds':self.a.duration,
            'completed_active_seconds':max(0,min(self.a.duration,elapsed-self.a.warmup)),
            'resource_samples':self.resource_samples,
            'ideal_request_opportunities_active': int(self.a.clients*self.a.duration*(37.5 if self.a.pacing=='current' else 14)),
            'stop_reason':self.reason,'requests_including_warmup':self.requests,'status_counts_active':dict(self.statuses),
            'status_counts_including_warmup':dict(self.all_statuses),'errors_including_warmup':dict(self.all_errors),
            'workers':[{'index':i,'requests_total':self.worker_requests[i],'statuses_total':dict(self.worker_statuses[i]),'statuses_active':dict(self.worker_active_statuses[i]),'errors_total':dict(self.worker_errors[i])} for i in range(self.a.workers)],
            'http_failure_categories_including_warmup':dict(self.http_failure_categories),
            'http_status_ms_active':{k:distribution(v) for k,v in self.status_samples.items()},
            'client_active_request_counts':[{'client':i,'completed':self.client_completed[i],'succeeded':self.client_succeeded[i]} for i in range(self.a.clients)],
            'errors_active':dict(self.errors),'peak_inflight':self.peak,'body_and_header_response_bytes':self.bytes_in,
            'request_bytes_including_headers':self.bytes_out,'get_ms':distribution(self.samples['GET']),
            'put_ms':distribution(self.samples['PUT']),'admission_wait_ms':distribution(self.admissions),
            'scheduled_completion_lag_ms':distribution(self.lags),'coalesced_nominal_ticks_active':self.coalesced,
            'peer_count_histogram_including_warmup':dict(self.peer_counts),
            'observed_completion_ratio':sum(self.statuses.values())/(self.a.clients*self.a.duration*(37.5 if self.a.pacing=='current' else 14)),
            'successful_completion_ratio':sum(v for k,v in self.statuses.items() if k.endswith(' 200'))/(self.a.clients*self.a.duration*(37.5 if self.a.pacing=='current' else 14)),
            'capacity_slo_certified':False,
            'all_requests_succeeded':not self.errors and all(k.endswith(' 200') for k in self.statuses) and bool(self.statuses) and not self.reason}


def main():
    a = arguments()
    plan = {'clients':a.clients,'scenario':a.scenario,'rooms':1 if a.scenario=='hot-room' else math.ceil(a.clients/10),
        'workers':a.workers,'db_pool_size_per_worker':a.db_pool_size,'database_connection_budget_workers':a.workers*a.db_pool_size,
        'routing':'Sticky actor index modulo workers; peers in the same room cross workers; one canonical Origin; simulated local fleet, not hosted autoscaling',
        'pacing':a.pacing,'read_ms':80 if a.pacing=='current' else 250,'write_ms':40 if a.pacing=='current' else 100,
        'payload':a.payload,'warmup_seconds':a.warmup,'active_seconds':a.duration,'cooldown_seconds':a.cooldown,
        'max_inflight':a.max_inflight,'max_requests':a.max_requests,'max_response_mib':a.max_response_mib}
    plan['database_transport'] = 'TLS verify-full' if a.tls_ca else 'local plaintext debug-only'
    plan['response_encoding'] = 'identity; no browser compression or rendering cost'
    plan['fixture_admission'] = 'Direct synthetic DB seed; hot-room sizes above ordinary membership limits are adversarial, unsupported fixtures'
    plan['omitted_workloads'] = ['1500ms presence requests','durable saves/uploads','browser frame scheduling and native rendering']
    if not a.execute:
        print(json.dumps({'dry_run':True,'plan':plan,'scope':'No connections, workers, databases or load created.'},indent=2)); return
    data,info = fixture(a.fixture)
    if len(data)>1024*1024:
        raise ValueError('Scale fixture must be <=1MiB to keep100room setup bounded')
    # Fail before creating a database if the requested concurrency exceeds available descriptors.
    soft,hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    if soft < a.max_inflight*2+64:
        raise ValueError(f'File descriptor limit{soft} too low for requested inflight{a.max_inflight}; operator must choose a safe limit')
    a.output.parent.mkdir(parents=True,exist_ok=True)
    report = {'started_utc':datetime.now(timezone.utc).isoformat(),'status':'running','plan':plan,
        'scope':'Disposable loopback HTTP/database, synthetic accounts; no browser/native paint, hosted capacity or1000hot-room support claim.',
        'environment':{'system':platform.platform(),'cpus':os.cpu_count(),'load_start':os.getloadavg(),
            'process_fd_limit':{'soft':soft,'hard':hard,'changed_by_harness':False},
            'context':a.context,'binary_sha256':hashlib.sha256(a.binary.read_bytes()).hexdigest()},'fixture':info}
    cpu = resource.getrusage(resource.RUSAGE_SELF)
    try:
        with isolated(a,report) as (base,db,workers):
            actors,projects = seed(db,a,data,info)
            before = db_stats(db)
            report['load'] = asyncio.run(Load(a,base,actors,report,db,workers).run())
            time.sleep(a.cooldown)
            after = db_stats(db)
            report['database_counters_delta'] = {k:after[k]-before[k] for k in before}
            report['database_counters_note'] = 'Includes warmup and monitoring; local DB stats may lag. No per-query CPU attribution.'
            report['native_unchanged'] = db.execute('SELECT count(*) FROM photocraft.projects WHERE revision<>1').fetchone()[0]==0
            report['status'] = 'passed' if report['load']['all_requests_succeeded'] and report['native_unchanged'] else 'failed'
    except Exception as error:
        report['status']='error'; report['error_type']=type(error).__name__; raise
    finally:
        usage = resource.getrusage(resource.RUSAGE_SELF); child=resource.getrusage(resource.RUSAGE_CHILDREN)
        report['resources']={'generator_user_cpu_seconds':usage.ru_utime-cpu.ru_utime,
            'generator_system_cpu_seconds':usage.ru_stime-cpu.ru_stime,'generator_peak_rss_native_units':usage.ru_maxrss,
            'child_user_cpu_seconds':child.ru_utime,'child_system_cpu_seconds':child.ru_stime,
            'child_peak_rss_native_units':child.ru_maxrss,'rss_unit':'bytes on macOS; KiB on Linux'}
        report['resources']['note'] = 'Child totals include the HTTP worker and small ps probes; PostgreSQL CPU/RSS and unrelated host work are not attributed. Database activity/counters are reported separately.'
        report['environment']['load_end']=os.getloadavg()
        report['finished_utc']=datetime.now(timezone.utc).isoformat()
        a.output.write_text(json.dumps(report,indent=2)+'\n')
        print(f"Scale benchmark {report['status']}: {a.output}",flush=True)
    raise SystemExit(0 if report['status']=='passed' else 1)


if __name__=='__main__':
    main()
