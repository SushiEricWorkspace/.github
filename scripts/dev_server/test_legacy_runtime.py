"""保存済v1の起動・snapshot復元互換を合成bundle/プロセスで確認する。"""
import shutil
import sys
import threading
import unittest
from unittest.mock import patch

from dev_server.build_inputs import canonical, digest
from dev_server.contracts import ContractError
from dev_server.runtime_bundle import launch_command, verify_bundle
from dev_server.runtime_process import command_hash
from dev_server.local_ipc import make_private
from dev_server import test_generation_control as generation_tests


@unittest.skipUnless(sys.platform in ("darwin", "win32"), "対象OSのprivate root試験")
class LegacyRuntimeTests(unittest.TestCase):
    bundle_version = 1
    quota_bytes = 10000000
    command = generation_tests.GenerationTests.command
    acquire = generation_tests.GenerationTests.acquire
    assert_code = generation_tests.GenerationTests.assert_code
    prepare = generation_tests.GenerationTests.prepare
    start = generation_tests.GenerationTests.start
    stop = generation_tests.GenerationTests.stop
    rcon_stop = generation_tests.GenerationTests.rcon_stop
    state = generation_tests.GenerationTests.state
    create = generation_tests.GenerationTests.create
    def setUp(self):
        # 旧Pで作成済みの停止記録を合成する。新通常起動の7項目契約には流用しない。
        from .runtime_control import RuntimeControl
        with patch.object(RuntimeControl, '_start', self.historical_start):
            generation_tests.GenerationTests.setUp(self)

    def historical_start(self, row, payload, generation, endpoints, operation, deadline):
        runtime = self.registry.runtime
        nonce = 'historical-nonce'
        run_id = 'historical-run'
        diagnostics = runtime._paths(row['id']) / 'diagnostics' / run_id
        diagnostics.mkdir(parents=True)
        make_private(diagnostics, directory=True)
        properties = dict(instanceId=row['id'], generationId=row['generation'], startNonce=nonce,
                          proofPath=str(diagnostics / 'proof.json'), runtimeManifest=str(generation / 'runtime/runtime.json'))
        arguments = launch_command(generation / 'runtime', 'server', generation,
                                   auxiliary=diagnostics / '.launch')
        argfile = diagnostics / '.launch/java.args'
        argfile.write_bytes(b'\n'.join(canonical('-Dsushieric.managed.' + key + '=' + value)
                                     for key, value in properties.items()) + b'\n' + argfile.read_bytes())
        process = self.adapter.OwnedProcess()
        run = dict(runId=run_id, instanceId=row['id'], generationId=row['generation'], artifactId=row['artifact'], nonce=nonce,
                   startOperationId=operation, startRequestHash=digest(canonical(dict(command='start', payload=payload))),
                   proofPath=properties['proofPath'], manifestHash=digest((generation / 'runtime/runtime.json').read_bytes()),
                   cwd=str(generation), endpoints=endpoints, identity=None, commandHash=command_hash(arguments),
                   stopRequested=False, jvmAndChildrenExited=False, runtimeSaveCompleted=False)
        expected = runtime._record(run)
        with runtime._transaction(row['id']) as connection:
            connection.execute('UPDATE instances SET run_id=? WHERE id=?', (run_id, row['id']))
        with (diagnostics / 'console.log').open('xb') as log:
            process.start(arguments, generation, log, nonce, lambda: None)
        run.update(identity=process.identity, creation=dict(pid=process.pid, stage=process.stage, ownerGroup=process.group))
        runtime._record(run, expected=expected)
        runtime.processes[run_id] = process
        done = threading.Event()
        done.set()
        runtime.managed.sessions[row['id']] = dict(stop=threading.Event(), done=done, error=None)
        return dict(instanceId=row['id'], generation=row['generation'], runId=run_id, running=True, snapshotClean=False)

    def bundle_hashes(self):
        bundle = self.root / "artifacts" / self.artifact
        return {p.relative_to(bundle).as_posix(): digest(p.read_bytes())
                for p in bundle.rglob("*") if p.is_file()}

    def test_saved_v1_restore_keeps_bytes_and_requires_new_managed_approval(self):
        bundle = self.root / "artifacts" / self.artifact
        before = self.bundle_hashes()
        self.assertEqual(1, verify_bundle(bundle)["schemaVersion"])
        self.command("restore", self.restore, "legacy-restore")
        self.assertEqual((self.generation / "world/level.dat").read_bytes(), (self.new / "world/level.dat").read_bytes())
        self.assertEqual(self.artifact, verify_bundle(self.new / "runtime")["artifactId"])
        self.token["expectedGeneration"] = self.restore["generationId"]
        self.registry.runtime.managed = None
        self.assert_code('PROFILE_REQUIRED', lambda: self.start('legacy-start'))
        self.assertEqual(before, self.bundle_hashes())
        self.assertEqual(self.artifact, (bundle / "COMPLETE").read_text())

    def test_v1_snapshot_is_not_converted_into_new_v2_artifact(self):
        before = self.bundle_hashes()
        newer = self.bundle_fixture.freeze()
        shutil.copytree(newer, self.root / "artifacts" / newer.name)
        self.assertEqual(2, verify_bundle(newer)["schemaVersion"])
        request = dict(self.restore, artifactId=newer.name, generationId="00000000-0000-4000-8000-000000000002")
        with self.assertRaisesRegex(ContractError, "compatibility"):
            self.command("restore", request, "wrong-artifact")
        self.assertFalse((self.generation.parent / request["generationId"]).exists())
        self.assertEqual(before, self.bundle_hashes())
