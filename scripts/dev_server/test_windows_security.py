"""新規管理資源の所有者を、昇格時の既定所有者へ残さない。"""
import os
from pathlib import Path
import tempfile
import unittest

from .local_ipc import require_private
from .windows_security import make_private


@unittest.skipUnless(os.name == "nt", "Windows本人所有者・DACLのnative試験")
class WindowsSecurityTests(unittest.TestCase):
    def test_new_directory_and_file_have_process_user_as_owner(self):
        with tempfile.TemporaryDirectory(prefix="owner-native-") as temporary:
            directory = Path(temporary) / "private"
            directory.mkdir()
            make_private(directory, directory=True)
            require_private(directory, directory=True)
            file = directory / "record.json"
            file.write_text("{}", encoding="utf-8")
            make_private(file)
            require_private(file)


if __name__ == "__main__":
    unittest.main()
