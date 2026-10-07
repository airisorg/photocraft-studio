"""Bounded loopback-only HTTP latency samples, not a hosted capacity/load test.

Uses existing native documents, 20 synthetic accounts, and a disposable service process.
Only this run's UUID-owned data is removed. No invitation endpoint is called.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import ipaddress
import json
import math
import os
from pathlib import Path
import platform
import secrets
import socket
import subprocess
import threading
import time
from urllib.parse import urlparse
import uuid
import zipfile

import psycopg
import requests

ROOT = Path(__file__).resolve().parents[2]
CHUNK = 524288
CLIENTS = (1, 5, 20)
POLL_SECONDS = 1.5


def local_database(value):
    parsed = urlparse(value)
    if (parsed.scheme not in {'postgres', 'postgresql'} or parsed.query or parsed.fragment
            or parsed.hostname not in {'localhost', '127.0.0.1', '::1'}):
        raise ValueError('Use a disposable loopback PostgreSQL URL without query/fragment overrides')
    addresses = {item[4][0] for item in socket.getaddrinfo(parsed.hostname, parsed.port or 5432)}
    if not addresses or any(not ipaddress.ip_address(address).is_loopback for address in addresses):
        raise ValueError('Database host must resolve only to loopback addresses')
    return parsed.hostname if parsed.hostname != 'localhost' else '127.0.0.1'


def fixture(path):
    path = path.resolve()
    if not 0 < path.stat().st_size <= 8 * 1024 * 1024:
        raise ValueError('Benchmark native fixtures must be at most 8 MiB')
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        if archive.getinfo('manifest.json').file_size > 2 * 1024 * 1024:
            raise ValueError('Native manifest is unexpectedly large')
        document = json.loads(archive.read('manifest.json'))['document']
    return data, {'file': str(path), 'name': document['name'], 'width': document['size']['width'],
                  'height': document['size']['height'], 'bytes': len(data),
                  'chunks': math.ceil(len(data) / CHUNK), 'sha256': hashlib.sha256(data).hexdigest()}


def distribution(values):
    ordered = sorted(values)
    return {'samples': len(values), **{f'p{percent}_ms': round(ordered[math.ceil(len(ordered)*percent/100)-1], 3)
            for percent in (50, 95, 99)}, 'max_ms': round(ordered[-1], 3),
            'samples_ms': [round(value, 3) for value in values]}


@contextmanager
def worker(binary, database, log):
    with socket.socket() as reserve:
        reserve.bind(('127.0.0.1', 0))
        port = reserve.getsockname()[1]
    base = f'http://127.0.0.1:{port}'
    # Explicitly disable provider configuration; this harness never sends mail.
    env = {key: value for key, value in os.environ.items() if not key.startswith('PG')}
    env.update(DATABASE_URL=database, CLOUD_LOCAL_DEV='1', APP_ORIGIN=base, PORT=str(port),
               SUPABASE_URL='', SUPABASE_ANON_KEY='')
    with log.open('w') as output:
        process = subprocess.Popen([str(binary)], env=env, stdout=output, stderr=subprocess.STDOUT)
        try:
            deadline = time.monotonic()+5
            while True:
                try:
                    with socket.create_connection(('127.0.0.1', port), timeout=.2):
                        break
                except OSError:
                    if process.poll() is not None or time.monotonic() >= deadline:
                        raise RuntimeError('Disposable benchmark worker did not listen')
                    time.sleep(.02)
            yield base
        finally:
            process.terminate()
            process.wait(timeout=5)


class Benchmark:
    def __init__(self, base, database, hostaddr):
        if urlparse(base).hostname != '127.0.0.1' or urlparse(base).scheme != 'http':
            raise ValueError('Benchmark HTTP origin must be literal loopback')
        self.base, self.database, self.hostaddr = base, database, hostaddr
        self.db = None
        self.accounts = []
        self.sessions = []
        self.request_count = 0
        self.count_lock = threading.Lock()

    def session(self, token=None):
        session = requests.Session()
        session.trust_env = False
        session.headers['Origin'] = self.base
        if token:
            session.cookies.set('pc_session', token)
        self.sessions.append(session)
        return session

    def request(self, client, method, path, **kwargs):
        if not path.startswith('/api/') or '://' in path:
            raise ValueError('Only relative local API requests are allowed')
        start = time.perf_counter_ns()
        response = client.request(method, self.base+path, timeout=20, allow_redirects=False, **kwargs)
        elapsed = (time.perf_counter_ns()-start)/1e6
        with self.count_lock:
            self.request_count += 1
        if response.status_code != 200:
            raise RuntimeError(f'{method} {path}: HTTP {response.status_code}: {response.text[:200]}')
        return response, elapsed

    def setup(self):
        config, _ = self.request(self.session(), 'GET', '/api/config')
        if not config.json()['cloud'] or config.json()['chunkBytes'] != CHUNK:
            raise RuntimeError('Local cloud storage is not ready or chunk contract changed')
        self.db = psycopg.connect(self.database, hostaddr=self.hostaddr, autocommit=True)
        for index in range(max(CLIENTS)):
            ident, token = str(uuid.uuid4()), secrets.token_hex(32)
            email = ident+'@example.invalid'
            self.db.execute('INSERT INTO photocraft.accounts(id,email,name) VALUES(%s,%s,%s)',
                (ident,email,f'Benchmark {index+1:02}'))
            self.accounts.append((ident,email,self.session(token)))
            self.db.execute('INSERT INTO photocraft.sessions(hash,account_id) VALUES(%s,%s)',
                (hashlib.sha256(token.encode()).hexdigest(),ident))

    def cleanup(self):
        ids = [account[0] for account in self.accounts]
        try:
            if self.db is not None and ids:
                with self.db.transaction():
                    self.db.execute('DELETE FROM photocraft.projects WHERE owner_id=ANY(%s::uuid[])',(ids,))
                    self.db.execute('DELETE FROM photocraft.sessions WHERE account_id=ANY(%s::uuid[])',(ids,))
                    self.db.execute('DELETE FROM photocraft.accounts WHERE id=ANY(%s::uuid[])',(ids,))
                remaining = self.db.execute('SELECT count(*) FROM photocraft.accounts WHERE id=ANY(%s::uuid[])',(ids,)).fetchone()[0]
                if remaining:
                    raise RuntimeError('Synthetic account cleanup incomplete')
        finally:
            if self.db is not None:
                self.db.close()
            for session in self.sessions:
                session.close()
        return {'synthetic_accounts_removed':len(ids), 'remaining_accounts':0}

    def project(self, title, members):
        owner = self.accounts[0][2]
        result, _ = self.request(owner,'POST','/api/projects',json={'title':title})
        pid = result.json()['id']
        for _,email,_ in self.accounts[1:members]:
            self.request(owner,'PUT',f'/api/projects/{pid}/members',json={'email':email,'role':'edit'})
        return pid

    def save_and_open(self, pid, data, info, revision):
        owner, peer = self.accounts[0][2], self.accounts[1][2]
        started = time.perf_counter_ns()
        result, begin_ms = self.request(owner,'POST',f'/api/projects/{pid}/uploads',json={
            'base_revision':revision,'bytes':info['bytes'],'parts':info['chunks'],'sha256':info['sha256'],
            'title':info['name'],'width':info['width'],'height':info['height']})
        uid = result.json()['id']
        chunk_started = time.perf_counter_ns()
        for part in range(info['chunks']):
            self.request(owner,'PUT',f'/api/uploads/{uid}/{part}',data=data[part*CHUNK:(part+1)*CHUNK])
        chunks_ms = (time.perf_counter_ns()-chunk_started)/1e6
        result, commit_ms = self.request(owner,'POST',f'/api/uploads/{uid}/commit',json={})
        save_ms = (time.perf_counter_ns()-started)/1e6
        if result.json()['revision'] != revision+1 or result.json()['merged']:
            raise RuntimeError('Sequential native save changed revision/merge contract')
        opened = time.perf_counter_ns()
        result, _ = self.request(peer,'GET',f'/api/projects/{pid}')
        metadata = result.json()
        if (metadata['revision'],metadata['width'],metadata['height'],metadata['content']['sha256']) != (
                revision+1,info['width'],info['height'],info['sha256']):
            raise RuntimeError('Peer reopened unexpected native document metadata')
        restored = b''.join(self.request(peer,'GET',f'/api/projects/{pid}/content?part={part}')[0].content
                            for part in range(info['chunks']))
        open_ms = (time.perf_counter_ns()-opened)/1e6
        if restored != data:
            raise RuntimeError('Peer reopened different native bytes')
        return {'save_total':save_ms,'upload_begin':begin_ms,'upload_chunks':chunks_ms,
                'commit':commit_ms,'peer_open':open_ms}

    def presence(self, pid, clients, rounds):
        self.db.execute('DELETE FROM photocraft.presence WHERE project_id=%s',(pid,))
        samples = []
        with ThreadPoolExecutor(max_workers=clients) as pool:
            for turn in range(rounds+2):
                barrier = threading.Barrier(clients)
                def poll(client):
                    barrier.wait(timeout=5)
                    response, elapsed = self.request(client,'POST',f'/api/projects/{pid}/presence',json={})
                    if response.json()['revision'] != 1:
                        raise RuntimeError('Presence returned the wrong committed revision')
                    if turn >= 2 and len(response.json()['people']) != clients:
                        raise RuntimeError('Presence did not retain all distinct benchmark accounts')
                    return elapsed
                started = time.monotonic()
                wave = list(pool.map(poll,[account[2] for account in self.accounts[:clients]]))
                if turn >= 2:
                    samples.extend(wave)
                if turn < rounds+1:
                    time.sleep(max(0,POLL_SECONDS-(time.monotonic()-started)))
        return {'clients':clients,'rounds':rounds,'interval_seconds':POLL_SECONDS,
                'warmup_rounds':2,**distribution(samples)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--binary',type=Path,default=Path(os.environ.get('PHOTOCRAFT_CLOUD_BIN',ROOT/'target/cloud/debug/photocraft-cloud')))
    parser.add_argument('--database',default=os.environ.get('PHOTOCRAFT_TEST_DATABASE_URL','postgresql://photocraft_test@127.0.0.1:55438/postgres'))
    parser.add_argument('--fixture',type=Path,default=os.environ.get('PHOTOCRAFT_FIXTURE'),required='PHOTOCRAFT_FIXTURE' not in os.environ)
    parser.add_argument('--template',type=Path,default=ROOT/'apps/photocraft-web/templates/noise.pcraft')
    parser.add_argument('--rounds',type=int,choices=range(10,21),default=10)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--context',default='No browser/build contention asserted; operator must record current host activity.')
    args = parser.parse_args()
    hostaddr = local_database(args.database)
    documents = [fixture(args.fixture),fixture(args.template)]
    binary = args.binary.resolve()
    binary_hash = hashlib.sha256(binary.read_bytes()).hexdigest()
    args.output.parent.mkdir(parents=True,exist_ok=True)
    report = {'started_utc':datetime.now(timezone.utc).isoformat(),'status':'running',
        'scope':'Local loopback HTTP only; not capacity, hosted latency, engine encoding or browser render time.',
        'environment':{'system':platform.platform(),'machine':platform.machine(),'cpus':os.cpu_count(),
            'python':platform.python_version(),'load_average_start':os.getloadavg(),'context':args.context,
            'binary':str(binary),'binary_sha256':binary_hash,
            'profile':'local debug binary; workspace dev opt-level=1, dependencies opt-level=2'},
        'quantiles':'nearest-rank; small-sample p95/p99 often equal the maximum, not tail-SLO estimates',
        'rounds':args.rounds,'presence':[],'documents':[]}
    try:
        with worker(binary,args.database,args.output.with_suffix('.server.log')) as base:
            benchmark = Benchmark(base,args.database,hostaddr)
            try:
                benchmark.setup()
                data,info = documents[0]
                pid = benchmark.project('Presence benchmark',max(CLIENTS))
                benchmark.save_and_open(pid,data,info,0)
                for clients in CLIENTS:
                    print(f'Sampling {clients} distinct presence clients...',flush=True)
                    report['presence'].append(benchmark.presence(pid,clients,args.rounds))
                for data,info in documents:
                    print(f'Sampling native save/reopen: {info["name"]} ({info["bytes"]} bytes)...',flush=True)
                    pid = benchmark.project('Native save benchmark',2)
                    benchmark.save_and_open(pid,data,info,0)
                    timings = [benchmark.save_and_open(pid,data,info,revision) for revision in range(1,args.rounds+1)]
                    report['documents'].append({**info,'warmup_saves':1,'rounds':args.rounds,
                        'timings':{name:distribution([row[name] for row in timings]) for name in timings[0]}})
                report['http_requests_including_setup_and_warmup'] = benchmark.request_count
            finally:
                report['cleanup'] = benchmark.cleanup()
        report['status'] = 'passed'
    except Exception as error:
        report['status'],report['error'] = 'failed',str(error)
        raise
    finally:
        report['finished_utc'] = datetime.now(timezone.utc).isoformat()
        report['environment']['load_average_end'] = os.getloadavg()
        args.output.write_text(json.dumps(report,indent=2)+'\n')
        print(f'Benchmark {report["status"]}: {args.output}',flush=True)


if __name__ == '__main__':
    main()
