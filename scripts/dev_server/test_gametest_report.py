"""合成XMLでテスト結果の検査を確認する。実JVMの停止証明には使わない。"""

from pathlib import Path
import tempfile
import unittest

from dev_server.build_inputs import digest
from dev_server.contracts import ContractError
from dev_server.gametest_report import read_report


class GameTestReportTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.path = Path(temporary.name) / "report.xml"

    def report(self, body):
        self.path.write_text(body, encoding="utf-8")
        return read_report(self.path)

    def test_success_preserves_exact_report_hash(self):
        body = '<testsuite tests="2"><testcase name="a"/><testcase name="b"/></testsuite>'
        result = self.report(body)
        self.assertTrue(result["testsPassed"])
        self.assertEqual(2, result["tests"])
        self.assertEqual(digest(body.encode()), result["reportSha256"])
        self.assertEqual(body, self.path.read_text())

    def test_nested_suites(self):
        self.assertTrue(self.report('<testsuites tests="1"><testsuite><testcase name="a"/></testsuite></testsuites>')["testsPassed"])

    def test_failure_error_skip_and_empty_are_not_success(self):
        for element in ("failure", "error", "skipped"):
            with self.subTest(element=element):
                self.assertFalse(self.report(f'<testsuite><testcase name="a"><{element}/></testcase></testsuite>')["testsPassed"])
        self.assertFalse(self.report('<testsuite tests="0"/>')["testsPassed"])

    def test_missing_or_broken_report_is_rejected(self):
        with self.assertRaises(ContractError):
            read_report(self.path)
        for body in ("", "<testsuite>", "<result/>"):
            with self.subTest(body=body), self.assertRaises(ContractError):
                self.report(body)

    def test_inconsistent_or_invalid_counts_are_rejected(self):
        for key, value in (("tests", "2"), ("failures", "1"), ("errors", "1"),
                           ("skipped", "1"), ("tests", "-1"), ("tests", "invalid")):
            with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                self.report(f'<testsuite {key}="{value}"><testcase name="a"/></testsuite>')

    def test_nested_inconsistent_counts_are_rejected(self):
        with self.assertRaises(ContractError):
            self.report('<testsuites><testsuite failures="1"><testcase name="a"/></testsuite></testsuites>')


if __name__ == "__main__":
    unittest.main()
