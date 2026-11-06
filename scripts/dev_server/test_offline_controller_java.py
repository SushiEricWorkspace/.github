"""指定固定Data121 artifactでCをcompileし、native/実rootを使わないprotocol vectorsを実行する。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('SUSHIERIC_OFFLINE_TEST_BUNDLE'), '固定Data121 bundleを明示する')
class OfflineControllerJavaTests(unittest.TestCase):
    def test_compile_and_protocol_vectors(self):
        self.vectors(False)

    def test_owner_lifecycle_with_fake_data_and_identities(self):
        self.vectors(True)

    def vectors(self, fake):
        bundle = Path(os.environ['SUSHIERIC_OFFLINE_TEST_BUNDLE'])
        host = json.loads((bundle / 'adapter/host.json').read_bytes())
        classpath = os.pathsep.join(str(bundle / name) for name in host['classpath'])
        suffix = '.exe' if os.name == 'nt' else ''
        root = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            classes = Path(directory)
            args = ['--release', '21', '-encoding', 'UTF-8', '-proc:none', '-cp', classpath, '-d', str(classes),
                    str(root / 'java/OfflineControllerMain.java'),
                    str(root / 'java/test/OfflineControllerVectors.java')]
            if fake:
                args = args[:-1] + [str(root / 'java/test/OfflineControllerLifecycleVectors.java'),
                                    *[str(path) for path in sorted((root / 'java/test/fake').glob('*.java'))]]
            argfile = classes / 'javac.args'
            argfile.write_text('\n'.join(json.dumps(value.replace('\\', '/')) for value in args), encoding='utf-8')
            result = subprocess.run([str(bundle / ('jdk/bin/javac' + suffix)), '@' + str(argfile)],
                                    capture_output=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            result = subprocess.run([str(bundle / ('jdk/bin/java' + suffix)), '-cp',
                                     str(classes) + os.pathsep + classpath,
                                     'io.github.sushiericworkspace.devserver.' +
                                     ('OfflineControllerLifecycleVectors' if fake else 'OfflineControllerVectors')],
                                    capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            self.assertTrue(result.stdout.startswith(b'offline-lifecycle-vectors=') if fake
                            else b'offline-vectors=' in result.stdout)


if __name__ == '__main__':
    unittest.main()
