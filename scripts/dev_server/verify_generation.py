"""許可された使い捨てrootだけで、実runtimeのsnapshot/restore往復を検証する。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import socket
import struct
import subprocess
import sys
import time

parser = argparse.ArgumentParser()
for name in ('repo', 'root', 'bundle', 'evidence'):
    parser.add_argument('--' + name, type=Path, required=True)
args = parser.parse_args()
sys.path.insert(0, str(args.repo / 'scripts'))
from dev_server.fixtures import contract_documents
from dev_server.local_ipc import request, make_private, plain_path
from dev_server.registry import initialize, Registry
from dev_server.runtime_bundle import verify_bundle
from dev_server.runtime_ports import instance_endpoints, prebind, require_listeners
from dev_server import runtime_process
from dev_server.supervisor import new_request
from dev_server.snapshot_control import verify_snapshot

report = {'testOnly': True, 'backupConfirmedForProduction': False, 'head': subprocess.run(['git', 'rev-parse', 'HEAD'], cwd=args.repo, capture_output=True, text=True, check=True).stdout.strip(), 'steps': []}

def record(step, value):
    report['steps'].append({'step': step, 'result': value})
    path = args.evidence / 'report.json'
    path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    make_private(path)
    print(json.dumps({'step': step, 'result': value}, ensure_ascii=False), flush=True)

def call(command, payload, operation):
    response = request(args.root, new_request(command, payload, operation_id=operation), timeout=900)
    if not response['ok']:
        record('failed-request', {'command': command, 'operationId': operation, 'error': response['error']})
        raise RuntimeError(response['error']['code'])
    return response['result']

def rcon(command, run, generation):
    def verify():
        runtime_process.require_identity(run['identity'], runtime_process.identity(run['identity']['pid']))
        proof = Registry(args.root).runtime._proof(run, generation)
        require_listeners(run['endpoints'], proof['startup']['listeners'], runtime_process.sockets(run['identity']['pid']))
    properties = dict(line.split('=', 1) for line in (generation / 'server.properties').read_text(encoding='utf-8').splitlines() if '=' in line and not line.startswith('#'))
    endpoint = run['endpoints']['rcon']
    assert endpoint['address'] == '127.0.0.1' and endpoint['port'] == 26075
    verify()
    with socket.create_connection((endpoint['address'], endpoint['port']), timeout=20) as client:
        client.settimeout(20)
        def packet(kind, value):
            body = struct.pack('<ii', 533, kind) + value.encode() + b'\0\0'
            client.sendall(struct.pack('<i', len(body)) + body)
        def response():
            def exact(length):
                value = b''
                while len(value) < length:
                    block = client.recv(length - len(value))
                    if not block:
                        raise RuntimeError('RCON応答が途中で終了しました')
                    value += block
                return value
            size = struct.unpack('<i', exact(4))[0]
            assert 10 <= size <= 65536
            body = exact(size)
            return struct.unpack('<ii', body[:8]), body[8:-2].decode('utf-8')
        packet(3, properties['rcon.password'])
        auth, _ = response()
        if auth[1] == 0:
            auth, _ = response()
        assert auth == (533, 2)
        verify()
        packet(2, command)
        identity, text = response()
        assert identity == (533, 0)
        return text

def main():
    for path in (args.root, args.bundle, args.evidence):
        plain_path(path)
    assert not args.root.exists() and not args.evidence.exists(), '既存環境・証跡は上書きしません'
    manifest = verify_bundle(args.bundle)
    args.evidence.mkdir(parents=True)
    make_private(args.evidence, directory=True)
    inventory = contract_documents('smoke-v1')[0]['inventory']
    inventory.update(operator='issue-533-disposable-test-only', managementRoot=str(args.root), quotaBytes=32 * 1024**3, reserveBytes=1024**3)
    inventory['backup'] = {'destination': str(args.evidence / 'synthetic-backup-not-real-medium'), 'separateMediumConfirmedBy': inventory['operator']}
    for declaration in inventory['instances']:
        declaration['ports'] = {service: port + 400 for service, port in declaration['ports'].items()}
    for declaration in inventory['instances']:
        with prebind(instance_endpoints(declaration)):
            pass
    initialize(inventory, operation_id='init-533')
    shutil.copytree(args.bundle, args.root / 'artifacts' / manifest['artifactId'])
    verify_bundle(args.root / 'artifacts' / manifest['artifactId'])
    record('initialized', {'artifactId': manifest['artifactId'], 'registryVersion': 4})
    with (args.evidence / 'supervisor.log').open('xb') as log:
        process = subprocess.Popen([sys.executable, '-B', str(args.repo / 'scripts/dev-server.py'), 'supervise', '--root', str(args.root)],
                                   stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                                   creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0)
    record('supervisor', {'pid': process.pid})
    deadline = time.monotonic() + 30
    while True:
        assert process.poll() is None, 'supervisorが終了しました'
        try:
            call('doctor', {}, 'doctor-533')
            break
        except OSError:
            assert time.monotonic() < deadline
            time.sleep(0.2)
    lease = call('acquire', {'instanceId': 'persistent-01', 'owner': 'codex-issue-533-real-test', 'ttlSeconds': 7200}, 'acquire-533')
    token = {'instanceId': 'persistent-01', 'leaseId': lease['leaseId'], 'epoch': lease['epoch'], 'expectedGeneration': None}
    prepared = call('prepare', dict(token, artifactId=manifest['artifactId'], generationId='generation-533', seed=52452502, acceptEula=True), 'prepare-533')
    token['expectedGeneration'] = prepared['generation']
    generation = args.root / 'instances/persistent-01/generations/generation-533'
    (generation / 'world').mkdir()
    marker = generation / 'world/unknown-533.txt'
    marker.write_bytes(b'issue-533-unknown-world-file\n')
    make_private(marker)
    record('prepared', {'generation': prepared['generation']})
    record('started', call('start', dict(token, timeoutSeconds=240), 'start-533'))
    registry = Registry(args.root)
    with registry.transaction(read_only=True) as connection:
        row = dict(connection.execute("SELECT * FROM instances WHERE id='persistent-01'").fetchone())
        run = json.loads(connection.execute('SELECT record FROM runs WHERE id=?', (row['run_id'],)).fetchone()[0])
    for dimension in ('minecraft:overworld', 'minecraft:the_nether', 'minecraft:the_end'):
        loaded = rcon(f'execute in {dimension} run forceload add 0 0', run, generation)
        time.sleep(2)
        placed = rcon(f'execute in {dimension} run setblock 0 100 0 minecraft:gold_block', run, generation)
        record('dimension-marker', {'dimension': dimension, 'loaded': loaded, 'placed': placed})
    record('scoreboard-created', {'reply': rcon('scoreboard objectives add generationproof dummy', run, generation)})
    record('scoreboard-value', {'reply': rcon('scoreboard players set test533 generationproof 17', run, generation)})
    record('normally-stopped', call('stop', dict(token, timeoutSeconds=180), 'stop-533'))
    payload = dict(token, action='create', snapshotId='snapshot-533')
    preview = request(args.root, new_request('snapshot', payload, operation_id='snapshot-533-create', dry_run=True), timeout=900)
    assert preview['ok'], preview
    record('snapshot-dry-run', preview['result'])
    record('snapshot-created', call('snapshot', payload, 'snapshot-533-create'))
    verified = call('snapshot', {'action': 'verify', 'snapshotId': 'snapshot-533'}, 'snapshot-533-verify')
    snapshot = args.root / 'snapshots/snapshot-533'
    document = verify_snapshot(snapshot)
    names = {item['path'] for item in document['files']}
    assert 'world/unknown-533.txt' in names
    for prefix in ('world/region/', 'world/DIM-1/region/', 'world/DIM1/region/'):
        assert any(name.startswith(prefix) and name.endswith('.mca') for name in names), prefix
    assert 'world/session.lock' not in names
    copied_properties = (snapshot / 'data/server.properties').read_bytes()
    for key in ('rcon.password=', 'server-port=', 'rcon.port=', 'server-ip='):
        assert key.encode() not in copied_properties
    for item in document['files']:
        if item['path'] in {'server.properties', 'config/SushiEricServerMod/config.yml', 'config/SushiEricServerMod/management/config.yml'}:
            continue
        assert hashlib.sha256((generation / item['path']).read_bytes()).hexdigest() == item['sha256']
    marker.write_bytes(b'changed-source-after-snapshot\n')
    assert (snapshot / 'data/world/unknown-533.txt').read_bytes() == b'issue-533-unknown-world-file\n'
    call('snapshot', {'action': 'verify', 'snapshotId': 'snapshot-533'}, 'snapshot-533-after-source-change')
    listed = call('list', {}, 'list-533-final')
    record('snapshot-verified', dict(verified, files=len(names), allVanillaDimensions=True, sourceHashMatch=True,
                                   unknownWorldFile=True, secretsAndPortsExcluded=True, independentCopy=True,
                                   snapshotClean=[item.get('snapshotClean') for item in listed['instances'] if item['instanceId'] == 'persistent-01']))
    record('restored', call('restore', dict(token, artifactId=manifest['artifactId'], generationId='restored-533', acceptEula=True, snapshotId='snapshot-533'), 'restore-533'))
    restored = generation.parent / 'restored-533'
    for item in document['files']:
        if item['path'] in {'server.properties', 'config/SushiEricServerMod/management/config.yml'}:
            continue
        assert hashlib.sha256((restored / item['path']).read_bytes()).hexdigest() == item['sha256']
    assert (restored / 'world/unknown-533.txt').read_bytes() == b'issue-533-unknown-world-file\n'
    assert marker.read_bytes() == b'changed-source-after-snapshot\n'
    assert not (restored / 'world/session.lock').exists()
    token['expectedGeneration'] = 'restored-533'
    record('restore-started', call('start', dict(token, timeoutSeconds=240), 'start-restored-533'))
    with Registry(args.root).transaction(read_only=True) as connection:
        current = dict(connection.execute("SELECT * FROM instances WHERE id='persistent-01'").fetchone())
        restored_run = json.loads(connection.execute('SELECT record FROM runs WHERE id=?', (current['run_id'],)).fetchone()[0])
    score = rcon('scoreboard players get test533 generationproof', restored_run, restored)
    assert '17' in score
    record('scoreboard-restored', {'reply': score})
    for dimension in ('minecraft:overworld', 'minecraft:the_nether', 'minecraft:the_end'):
        rcon(f'execute in {dimension} run forceload add 0 0', restored_run, restored)
        time.sleep(2)
        verified_block = rcon(f'execute in {dimension} if block 0 100 0 minecraft:gold_block', restored_run, restored)
        record('restored-block', {'dimension': dimension, 'reply': verified_block})
        assert 'Test passed' in verified_block
    record('restore-stopped', call('stop', dict(token, timeoutSeconds=180), 'stop-restored-533'))
    record('release', call('release', token, 'release-533'))
    record('restore-verified', {'allSourceHashes': True, 'allVanillaDimensions': True, 'scoreboard17': True, 'independentCopy': True, 'oldGenerationRetained': generation.exists()})
    record('shutdown', call('shutdown', {}, 'shutdown-533'))
    process.wait(timeout=30)
    record('finished', {'supervisorExitCode': process.returncode, 'sourceAndSnapshotPreserved': True})

try:
    main()
except BaseException as error:
    if args.evidence.exists():
        record('failure-preserved', {'type': type(error).__name__, 'message': str(error), 'automaticKillOrRetry': False})
    raise
