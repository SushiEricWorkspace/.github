"""Minecraftと同じJava FileChannelの排他を対象OSで確認する。"""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from .import_data import source_guard
from .registry import RegistryError


@unittest.skipUnless(os.environ.get("SUSHIERIC_TEST_JAVA21"), "実JDK 21未指定")
class JavaSourceLockTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="import-lock-", dir="/private/tmp" if sys.platform == "darwin" else None)
        self.addCleanup(temporary.cleanup)
        self.source = Path(temporary.name)
        (self.source / "world").mkdir()
        (self.source / "server.properties").write_bytes(b"level-name=world\n")
        self.lock = self.source / "world/session.lock"
        self.lock.write_bytes(b"original-lock")
        java = self.source / "WorldLock.java"
        java.write_text('''import java.nio.channels.FileChannel;
import java.nio.file.Path;
import java.nio.file.StandardOpenOption;
class WorldLock {
    public static void main(String[] args) throws Exception {
        try (var channel = FileChannel.open(Path.of(args[0]), StandardOpenOption.READ, StandardOpenOption.WRITE);
             var lock = channel.tryLock()) {
            System.out.println(lock == null ? "busy" : "locked");
            System.out.flush();
            if (lock != null) System.in.read();
        }
    }
}
''', encoding="utf-8")
        self.command = [os.environ["SUSHIERIC_TEST_JAVA21"], str(java), str(self.lock)]

    def test_java_world_lock_blocks_python_import(self):
        process = subprocess.Popen(self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        try:
            self.assertEqual("locked\n", process.stdout.readline())
            with self.assertRaises(RegistryError) as error:
                with source_guard(str(self.source)):
                    self.fail("Javaが使用中のworldを取り込みました")
            self.assertEqual("SOURCE_BUSY", error.exception.code)
        finally:
            _, errors = process.communicate("done\n", timeout=30)
        self.assertEqual(0, process.returncode, errors)
        self.assertEqual(b"original-lock", self.lock.read_bytes())

    def test_python_import_lock_blocks_java_world(self):
        with source_guard(str(self.source)):
            process = subprocess.run(self.command, input="", capture_output=True, text=True, timeout=30)
        self.assertEqual(0, process.returncode, process.stderr)
        self.assertEqual("busy\n", process.stdout)
        self.assertEqual(b"original-lock", self.lock.read_bytes())
