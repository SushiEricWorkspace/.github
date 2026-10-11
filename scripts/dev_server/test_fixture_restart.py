"""同世代再起動の証跡検査。合成文書を実サーバー成功とは扱わない。"""

from copy import deepcopy
import unittest

from dev_server.registry import RegistryError
from dev_server.verify_templates import verify_restart, verify_overlap


class FixtureRestartTests(unittest.TestCase):
    def setUp(self):
        self.created = {"action": "create", "nonce": "first", "schemaVersion": 1,
                        "fixture": "save-roundtrip-v1", "seed": 52452502, "instanceId": "persistent-01",
                        "generationId": "source", "oreRevivalSecond": 12, "inventoryCount": 2,
                        "money": 1234, "internalId": "synthetic", "files": [{"sha256": "synthetic"}]}
        self.verified = dict(deepcopy(self.created), action="verify", nonce="second")
        self.first = {"nonce": "first", "instanceId": "persistent-01", "generation": "source",
                      "runtimeStopped": True, "modSaveCompleted": True, "stopRequested": True,
                      "stopAcknowledged": True, "jvmAndChildrenExited": True, "exitCode": 0}
        self.second = dict(self.first, nonce="second")

    def test_same_values_in_distinct_clean_runs(self):
        verify_restart(self.created, self.verified, self.first, self.second)

    def test_overlap_requires_real_overlapping_intervals(self):
        records = [{"identity": {"startMillis": value * 1000}, "exitedAt": value + 10,
                    "jvmAndChildrenExited": True} for value in (1, 2, 3)]
        self.assertEqual({"overlapStartedAt": 3, "overlapEndedAt": 11}, verify_overlap(records))
        records[-1]["identity"]["startMillis"] = 12000
        with self.assertRaises(RegistryError):
            verify_overlap(records)

    def test_changed_data_or_reused_nonce_is_rejected(self):
        for key, value in (("nonce", "first"), ("money", 0), ("inventoryCount", 0), ("files", []),
                           ("generationId", "other"), ("action", "create")):
            with self.subTest(key=key), self.assertRaises(RegistryError):
                verify_restart(self.created, dict(self.verified, **{key: value}), self.first, self.second)

    def test_no_clean_claim_without_both_stops(self):
        for key, value in (("runtimeStopped", False), ("modSaveCompleted", False), ("exitCode", 1),
                           ("stopRequested", False), ("stopAcknowledged", False), ("jvmAndChildrenExited", False)):
            for which in ("first", "second"):
                with self.subTest(key=key, which=which), self.assertRaises(RegistryError):
                    first = dict(self.first, **{key: value}) if which == "first" else self.first
                    second = dict(self.second, **{key: value}) if which == "second" else self.second
                    verify_restart(self.created, self.verified, first, second)


if __name__ == "__main__":
    unittest.main()
