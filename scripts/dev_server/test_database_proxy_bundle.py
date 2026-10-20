"""合成helperの収録契約。Java実compile/socket試験の代用にはしない。"""
import io
import json
import os
from pathlib import Path
import unittest
from unittest.mock import patch
import zipfile

from .build_inputs import canonical, digest
from .contracts import ContractError
from .database_proxy_bundle import CODEC, DESCRIPTOR, MAIN, MODULES, SOURCE, freeze_proxy, proxy_command, verify_proxy
from .runtime_bundle import Freezer, _files, verify_bundle
from . import test_runtime_bundle as fixtures


class DatabaseProxyBundleTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.RuntimeBundleTests('test_launch_has_no_source_cache_or_gradle_dependency')
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.bundle = self.fixture.freeze()
        classes = self.bundle / 'classes/proxy-fixture'
        (classes / CODEC).parent.mkdir(parents=True)
        (classes / CODEC).write_bytes(b'synthetic Mod DTO class')
        launch = self.bundle / 'launch/profiles.json'
        profiles = json.loads(launch.read_bytes())
        profiles['profiles']['server']['classpath'].append('classes/proxy-fixture')
        dependencies_path = self.bundle / 'inputs/dependencies.json'
        dependencies = json.loads(dependencies_path.read_bytes())
        library = io.BytesIO()
        with zipfile.ZipFile(library, 'w') as archive:
            archive.writestr('META-INF/MANIFEST.MF', b'Manifest-Version: 1.0\r\n\r\n')
        kotlin = next(item for item in dependencies if item['coordinate'].startswith(MODULES[0] + ':'))
        (self.bundle / kotlin['file']).write_bytes(library.getvalue())
        for module in MODULES[1:]:
            name = 'files/proxy-fixture/' + module.split(':')[1] + '.jar'
            path = self.bundle / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(library.getvalue())
            dependencies.append({'coordinate': module + ':1.9.0', 'file': name})
            profiles['profiles']['server']['classpath'].append(name)
        dependencies_path.write_bytes(canonical(dependencies))
        launch.write_bytes(canonical(profiles))
        compiler = self.bundle / 'jdk/bin' / ('javac.exe' if os.name == 'nt' else 'javac')
        compiler.write_bytes(b'synthetic JDK21 compiler')
        source_root = self.fixture.root / 'github-source'
        source = source_root / SOURCE
        source.parent.mkdir(parents=True)
        source.write_text('synthetic Java source', encoding='utf-8')
        self.lock = {'sources': [{'root': str(source_root), 'record': {'repo': '.github'}, 'inputs': [{'path': SOURCE}]}]}
        self.profiles = profiles['profiles']
        self.recipe = {'dataModelCoordinate': self.fixture.data_model_coordinate, 'kotlinVersion': '2.2.21', 'javaHome': str(self.bundle / 'jdk'), 'javaVersion': json.loads((self.bundle / 'runtime.json').read_bytes())['javaVersion']}
        self.relabel()

    def relabel(self):
        manifest = json.loads((self.bundle / 'runtime.json').read_bytes())
        manifest['files'] = [item for item in _files(self.bundle) if item['path'] not in {'runtime.json', 'COMPLETE'}]
        manifest['artifactId'] = digest(canonical({key: value for key, value in manifest.items() if key != 'artifactId'}))
        (self.bundle / 'runtime.json').write_bytes(canonical(manifest))
        (self.bundle / 'COMPLETE').write_text(manifest['artifactId'])

    def compile(self, *, exit_code=0, extra=False):
        def popen(command, *, cwd, stdout, stderr):
            self.assertEqual('-J-Xmx512m', command[1])
            args = [json.loads(line) for line in Path(command[2][1:]).read_text(encoding='utf-8').splitlines()]
            classpath = args[args.index('-cp') + 1].split(os.pathsep)
            self.assertTrue(all(self.bundle in Path(path).parents for path in classpath))
            target = Path(args[args.index('-d') + 1])
            name = MAIN.replace('.', '/') + '.class'
            (target / name).parent.mkdir(parents=True)
            (target / name).write_bytes(b'\xca\xfe\xba\xbe\0\0\0A' + b'synthetic class')
            if extra:
                (target / 'Other.class').write_bytes(b'\xca\xfe\xba\xbe\0\0\0A')
            class Process:
                pid = 12345
                def wait(self):
                    return exit_code
            return Process()
        with patch('dev_server.database_proxy_bundle.subprocess.Popen', side_effect=popen):
            freeze_proxy(Freezer(self.bundle), self.lock, self.recipe, self.profiles)
        self.relabel()

    def test_fixed_source_jar_jdk_and_classpath_are_in_collection(self):
        self.compile()
        value = verify_proxy(self.bundle)
        self.assertEqual(MAIN, value['mainClass'])
        command = proxy_command(self.bundle)
        self.assertEqual(MAIN, command[-1])
        self.assertTrue(all(self.bundle in Path(name).parents for name in command[command.index('-cp') + 1].split(os.pathsep)))
        files = {entry['path'] for entry in verify_bundle(self.bundle)['files']}
        self.assertTrue({DESCRIPTOR, value['source'], value['jar']} <= files)

    def test_missing_source_record_rejects_before_compiler(self):
        self.lock['sources'][0]['inputs'] = []
        with patch('dev_server.database_proxy_bundle.subprocess.Popen') as child:
            with self.assertRaises(ContractError):
                freeze_proxy(Freezer(self.bundle), self.lock, self.recipe, self.profiles)
            child.assert_not_called()

    def test_compile_failure_has_no_descriptor_or_jar(self):
        with self.assertRaises(ContractError):
            self.compile(exit_code=2)
        self.assertFalse((self.bundle / DESCRIPTOR).exists())
        self.assertFalse((self.bundle / 'adapter/proxy/database-proxy.jar').exists())

    def test_unknown_generated_class_is_not_packaged(self):
        with self.assertRaises(ContractError):
            self.compile(extra=True)
        self.assertFalse((self.bundle / DESCRIPTOR).exists())

    def test_extra_old_helper_is_rejected_even_if_manifest_relabelled(self):
        self.compile()
        (self.bundle / 'adapter/proxy/old-helper.jar').write_bytes(b'old')
        self.relabel()
        with self.assertRaises(ContractError):
            verify_bundle(self.bundle)

    def test_changed_source_or_compiler_is_rejected_even_if_manifest_relabelled(self):
        self.compile()
        value = verify_proxy(self.bundle)
        for name in (value['source'], 'jdk/bin/' + ('javac.exe' if os.name == 'nt' else 'javac')):
            path = self.bundle / name
            before = path.read_bytes()
            path.write_bytes(b'changed')
            self.relabel()
            with self.assertRaises(ContractError):
                verify_bundle(self.bundle)
            path.write_bytes(before)
            self.relabel()

    def test_descriptor_cannot_add_external_classpath(self):
        self.compile()
        path = self.bundle / DESCRIPTOR
        value = json.loads(path.read_bytes())
        value['classpath'].append('../outside.jar')
        path.write_bytes(canonical(value))
        self.relabel()
        with self.assertRaises(ContractError):
            verify_bundle(self.bundle)

    def test_new_database_entry_requires_descriptor_but_old_bundle_verifies(self):
        verify_bundle(self.bundle)
        with self.assertRaises(FileNotFoundError):
            proxy_command(self.bundle)

    def test_jar_manifest_cannot_supply_additional_classpath(self):
        self.compile()
        value = verify_proxy(self.bundle)
        jar = self.bundle / value['jar']
        with zipfile.ZipFile(jar, 'a') as archive:
            archive.writestr('META-INF/MANIFEST.MF', b'Manifest-Version: 1.0\r\nClass-Path: outside.jar\r\n')
        value['jarSha256'] = digest(jar.read_bytes())
        (self.bundle / DESCRIPTOR).write_bytes(canonical(value))
        self.relabel()
        with self.assertRaises(ContractError):
            verify_bundle(self.bundle)
    def test_mod_serialization_version_mismatch_is_rejected_before_compile(self):
        path = self.bundle / 'inputs/dependencies.json'
        rows = json.loads(path.read_bytes())
        next(item for item in rows if item['coordinate'].startswith(MODULES[1] + ':'))['coordinate'] = MODULES[1] + ':other-version'
        path.write_bytes(canonical(rows))
        with patch('dev_server.database_proxy_bundle.subprocess.Popen') as child:
            with self.assertRaises(ContractError):
                freeze_proxy(Freezer(self.bundle), self.lock, self.recipe, self.profiles)
            child.assert_not_called()

    def test_duplicate_resolved_dependency_is_rejected_before_compile(self):
        path = self.bundle / 'inputs/dependencies.json'
        rows = json.loads(path.read_bytes())
        rows.append(next(item for item in rows if item['coordinate'].startswith(MODULES[1] + ':')))
        path.write_bytes(canonical(rows))
        with patch('dev_server.database_proxy_bundle.subprocess.Popen') as child:
            with self.assertRaises(ContractError):
                freeze_proxy(Freezer(self.bundle), self.lock, self.recipe, self.profiles)
            child.assert_not_called()

    def test_unrecorded_server_dependency_is_rejected_before_compile(self):
        path = self.bundle / 'inputs/dependencies.json'
        rows = json.loads(path.read_bytes())
        row = next(item for item in rows if item['coordinate'].startswith(MODULES[1] + ':'))
        self.profiles['server']['classpath'].remove(row['file'])
        with patch('dev_server.database_proxy_bundle.subprocess.Popen') as child:
            with self.assertRaises(ContractError):
                freeze_proxy(Freezer(self.bundle), self.lock, self.recipe, self.profiles)
            child.assert_not_called()
