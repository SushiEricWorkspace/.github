"""メモリ集計は所有識別の前後照合が成立した標本だけを採用する。"""
import json
import os
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from . import verify_capacity
from .registry import RegistryError


@unittest.skipUnless(os.name == "nt", "Windows専用の計測")
class CapacityTests(unittest.TestCase):
    def setUp(self):
        self.runs = [{"identity": {"pid": number}} for number in (101, 102, 103)]
        self.values = [{"Id": number, "WorkingSet64": 10, "PrivateMemorySize64": 20} for number in (101, 102, 103)]

    def test_native_file_allocation_is_not_less_than_logical_size(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            (root / "data").write_bytes(b"data")
            value = verify_capacity.file_capacity(root)
            self.assertEqual(value["logicalBytes"], 4)
            self.assertGreaterEqual(value["fileAllocatedBytes"], 4)
            self.assertFalse(value["includesFilesystemMetadata"])

    def test_sum_requires_all_six_identity_checks(self):
        with patch.object(verify_capacity.runtime_process, "identity", side_effect=[run["identity"] for run in self.runs] * 2), patch.object(verify_capacity.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(self.values), "")):
            value = verify_capacity.memory_sample(self.runs)
        self.assertEqual(value["sumWorkingSetBytes"], 30)
        self.assertEqual(value["sumPrivateCommittedBytes"], 60)

    def test_exit_during_process_query_is_rejected_by_identity(self):
        with patch.object(verify_capacity.runtime_process, "identity", side_effect=[run["identity"] for run in self.runs] + [None]), patch.object(verify_capacity.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(RegistryError) as caught:
                verify_capacity.memory_sample(self.runs)
        self.assertEqual(caught.exception.code, "PROCESS_MISMATCH")

    def test_query_failure_with_live_identity_is_not_a_sample(self):
        with patch.object(verify_capacity.runtime_process, "identity", side_effect=[run["identity"] for run in self.runs] * 2), patch.object(verify_capacity.subprocess, "run", return_value=subprocess.CompletedProcess([], 1, "", "")):
            with self.assertRaises(RegistryError) as caught:
                verify_capacity.memory_sample(self.runs)
        self.assertEqual(caught.exception.code, "PROCESS_UNVERIFIED")

    def test_different_pid_set_is_rejected(self):
        self.values[-1]["Id"] = 999
        with patch.object(verify_capacity.runtime_process, "identity", side_effect=[run["identity"] for run in self.runs] * 2), patch.object(verify_capacity.subprocess, "run", return_value=subprocess.CompletedProcess([], 0, json.dumps(self.values), "")):
            with self.assertRaises(RegistryError):
                verify_capacity.memory_sample(self.runs)
