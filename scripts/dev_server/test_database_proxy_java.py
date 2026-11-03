"""指定された固定bundleの実Mod DTOでproxyをJDK21 compileし共有vectorを実行する。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipUnless(os.environ.get('SUSHIERIC_PROXY_TEST_BUNDLE'), '実Java試験は固定bundleを明示する')
class DatabaseProxyJavaTests(unittest.TestCase):
    def test_compile_and_shared_rejection_vectors(self):
        bundle = Path(os.environ['SUSHIERIC_PROXY_TEST_BUNDLE'])
        jdk = Path(os.environ['JAVA_HOME'])
        root = Path(__file__).parent
        names = json.loads((bundle / 'launch/profiles.json').read_bytes())['profiles']['server']['classpath']
        names = [name for name in names if (bundle / name).is_dir() or any(token in name for token in
                 ('sushieric-datamodel', 'kotlin-stdlib', 'kotlinx-serialization'))]
        classpath = os.pathsep.join(str(bundle / name) for name in names)
        suffix = '.exe' if os.name == 'nt' else ''
        with tempfile.TemporaryDirectory() as temporary:
            classes = Path(temporary)
            args = ['--release', '21', '-encoding', 'UTF-8', '-proc:none', '-cp', classpath, '-d', str(classes),
                    str(root / 'java/DatabaseProxyMain.java'), str(root / 'java/test/DatabaseProxyVectors.java')]
            argfile = classes / 'javac.args'
            argfile.write_text('\n'.join(json.dumps(value.replace('\\', '/')) for value in args), encoding='utf-8')
            result = subprocess.run([str(jdk / ('bin/javac' + suffix)), '@' + str(argfile)], capture_output=True, timeout=60)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            result = subprocess.run([str(jdk / ('bin/java' + suffix)), '-cp', str(classes) + os.pathsep + classpath,
                                     'DatabaseProxyVectors', str(root / 'management-stop-vectors.json')], capture_output=True, timeout=30)
            self.assertEqual(0, result.returncode, result.stderr.decode(errors='replace'))
            self.assertIn(b'shared-vectors=19', result.stdout)


if __name__ == '__main__':
    unittest.main()
