"""合成C artifactの固定収録・改変拒否。native owner取得の実機受入とは分ける。"""
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch

from .build_inputs import canonical
from .contracts import ContractError
from .offline_controller_bundle import (MAIN, SOURCE, DESCRIPTOR, DIRECTORY,
                                        freeze_offline_controller, verify_offline_controller,
                                        offline_controller_command, offline_worker_command)
from .runtime_bundle import Freezer, verify_bundle
from . import test_database_proxy_bundle as fixtures


class OfflineControllerBundleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.DatabaseProxyBundleTests('test_fixed_source_jar_jdk_and_classpath_are_in_collection')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bundle = self.fixture.bundle
        source_root = Path(self.fixture.lock['sources'][0]['root'])
        source = source_root / SOURCE
        source.write_text('synthetic controller source', encoding='utf-8')
        self.lock = {'sources': [{'root': str(source_root), 'record': {'repo': '.github'},
                                 'inputs': [{'path': SOURCE}]}]}

    def compile(self, *, exit_code=0, extra=False, worker=False):
        def popen(command, *, cwd, stdout, stderr):
            args = [json.loads(line) for line in Path(command[2][1:]).read_text(encoding='utf-8').splitlines()]
            self.assertEqual('--release', args[0])
            self.assertEqual('21', args[1])
            self.assertTrue(all(self.bundle in Path(name).parents
                                for name in args[args.index('-cp') + 1].split(os.pathsep)))
            target = Path(args[args.index('-d') + 1])
            name = MAIN.replace('.', '/') + '.class'
            (target / name).parent.mkdir(parents=True)
            (target / name).write_bytes(b'\xca\xfe\xba\xbe\0\0\0A' + b'synthetic C')
            if worker:
                worker_name = MAIN.replace('.', '/') + '$Worker.class'
                (target / worker_name).write_bytes(b'\xca\xfe\xba\xbe\0\0\0A' + b'synthetic W')
            if extra:
                (target / 'Worker.class').write_bytes(b'\xca\xfe\xba\xbe\0\0\0A')
            class Process:
                pid = 12346
                def wait(self):
                    return exit_code
            return Process()
        with patch('dev_server.database_proxy_bundle.subprocess.Popen', side_effect=popen):
            freeze_offline_controller(Freezer(self.bundle), self.lock, self.fixture.recipe)
        self.fixture.relabel()

    def test_fixed_controller_source_jar_jdk_and_host_dependencies(self):
        self.compile()
        value = verify_offline_controller(self.bundle)
        command = offline_controller_command(self.bundle)
        self.assertEqual(MAIN, command[-1])
        self.assertEqual(str(self.bundle / 'jdk/bin' / ('java.exe' if os.name == 'nt' else 'java')), command[0])
        self.assertEqual(value['classpath'], [str(Path(p).relative_to(self.bundle)).replace('\\', '/')
                                              for p in command[command.index('-cp') + 1].split(os.pathsep)])
        self.assertTrue({DESCRIPTOR, value['source'], value['jar']} <=
                        {entry['path'] for entry in verify_bundle(self.bundle)['files']})

    def test_worker_command_uses_fixed_nested_entrypoint_without_request_arguments(self):
        self.compile(worker=True)
        controller = offline_controller_command(self.bundle)
        worker = offline_worker_command(self.bundle)
        self.assertEqual([*controller[:-1], MAIN + '$Worker'], worker)

    def test_old_controller_verifies_but_worker_entrypoint_is_unavailable(self):
        self.compile()
        offline_controller_command(self.bundle)
        with self.assertRaises(ContractError):
            offline_worker_command(self.bundle)

    def test_freeze_does_not_need_completed_runtime_manifest(self):
        manifest = self.bundle / 'runtime.json'
        marker = self.bundle / 'COMPLETE'
        saved = manifest.read_bytes(), marker.read_bytes()
        manifest.unlink()
        marker.unlink()
        original = self.fixture.relabel
        def relabel():
            manifest.write_bytes(saved[0])
            marker.write_bytes(saved[1])
            original()
        with patch.object(self.fixture, 'relabel', side_effect=relabel):
            self.compile()
        verify_offline_controller(self.bundle)

    def test_missing_or_deleted_record_does_not_compile(self):
        for records in ([], [{'path': SOURCE, 'deleted': True}]):
            with self.subTest(records=records):
                self.lock['sources'][0]['inputs'] = records
                with patch('dev_server.database_proxy_bundle.subprocess.Popen') as child:
                    with self.assertRaises(ContractError):
                        freeze_offline_controller(Freezer(self.bundle), self.lock, self.fixture.recipe)
                    child.assert_not_called()

    def test_compile_failure_does_not_publish_artifact(self):
        with self.assertRaises(ContractError):
            self.compile(exit_code=1)
        self.assertFalse((self.bundle / DESCRIPTOR).exists())
        self.assertFalse((self.bundle / DIRECTORY / 'offline-controller.jar').exists())

    def test_unknown_generated_class_is_rejected(self):
        with self.assertRaises(ContractError):
            self.compile(extra=True)

    def test_old_bundle_verifies_but_controller_is_unavailable(self):
        verify_bundle(self.bundle)
        with self.assertRaises(FileNotFoundError):
            offline_controller_command(self.bundle)

    def test_changed_source_jar_or_compiler_is_rejected_even_with_new_manifest(self):
        self.compile()
        value = verify_offline_controller(self.bundle)
        for name in (value['source'], value['jar'], 'jdk/bin/' + ('javac.exe' if os.name == 'nt' else 'javac')):
            with self.subTest(name=name):
                path = self.bundle / name
                original = path.read_bytes()
                path.write_bytes(b'changed')
                self.fixture.relabel()
                with self.assertRaises(ContractError):
                    verify_bundle(self.bundle)
                path.write_bytes(original)
                self.fixture.relabel()

    def test_external_classpath_or_wrong_entrypoint_or_platform_is_rejected(self):
        self.compile()
        path = self.bundle / DESCRIPTOR
        value = json.loads(path.read_bytes())
        for field, changed in [('classpath', value['classpath'] + ['../external.jar']),
                               ('mainClass', 'Other'), ('platform', 'Other')]:
            with self.subTest(field=field):
                path.write_bytes(canonical(dict(value, **{field: changed})))
                self.fixture.relabel()
                with self.assertRaises(ContractError):
                    verify_bundle(self.bundle)
        path.write_bytes(canonical(value))
        self.fixture.relabel()
        with patch('dev_server.offline_controller_bundle.platform.machine', return_value='other'):
            with self.assertRaises(ContractError):
                offline_controller_command(self.bundle)

    def test_extra_old_file_or_missing_descriptor_is_rejected(self):
        self.compile()
        old = self.bundle / DIRECTORY / 'old.jar'
        old.write_bytes(b'old')
        self.fixture.relabel()
        with self.assertRaises(ContractError):
            verify_bundle(self.bundle)
        old.unlink()
        (self.bundle / DESCRIPTOR).unlink()
        self.fixture.relabel()
        with self.assertRaises(FileNotFoundError):
            verify_bundle(self.bundle)


if __name__ == '__main__':
    unittest.main()
