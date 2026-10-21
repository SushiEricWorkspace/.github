"""M/server exact launchの合成pure検証。native/profile承認の試験ではない。"""
from copy import deepcopy
import json
from pathlib import Path, PurePosixPath, PureWindowsPath
import re
import unittest
from unittest.mock import patch

from .contracts import ContractError
from .runtime_bundle import _managed_argfile, validate_managed_launch


def fixture(system="Darwin", encoding="utf-8"):
    base = "C:\\fixture" if system == "Windows" else "/fixture"
    path = PureWindowsPath if system == "Windows" else PurePosixPath
    context = {"bundle": str(path(base) / "bundle"), "run": str(path(base) / "generation"),
               "auxiliary": str(path(base) / "aux"), "privateTmp": str(path(base) / "private tmp"),
               "controlDescriptor": str(path(base) / "control/mod-control.json"),
               "runtimeManifest": str(path(base) / "bundle/runtime.json"),
               "proofPath": str(path(base) / "proof.json"), "instanceId": "slot-a",
               "generationId": "00000000-0000-4000-8000-000000000001", "startNonce": "nonce-a",
               "controlDescriptorSha256": "a" * 64}
    profile = {"version": 1, "purpose": "managed-launch-exact", "role": "M", "platform": system,
               "argfileEncoding": encoding, "mainClass": "example.Main", "classpath": ["lib/a.jar", "classes/main"],
               "jvmArgs": ["-Xms128m", "-Xmx256m", "-XX:+DisableAttachMechanism", "-Dfixture=日本語"],
               "args": ["nogui"], "environment": {"TMPDIR": "${privateTmp}"},
               "dliText": "commonProperties\n\tfabric.remapClasspathFile=${auxiliary}/server-remap-classpath.txt\n"
                          "\tlog4j.configurationFile=${bundle}/logging.xml\n",
               "remapClasspath": ["lib/a.jar", "classes/main"]}
    for name in ("instanceId", "generationId", "startNonce", "proofPath", "runtimeManifest",
                 "controlDescriptor", "controlDescriptorSha256"):
        profile["jvmArgs"].append("-Dsushieric.managed." + name + "=${" + name + "}")
    profile["jvmArgs"] += ["-Djava.io.tmpdir=${privateTmp}", "-Dorg.sqlite.tmpdir=${privateTmp}",
                           "-Dfabric.dli.config=${auxiliary}/server-launch.cfg"]
    return profile, context, observed(profile, context)


def observed(profile, context):
    path = PureWindowsPath if profile["platform"] == "Windows" else PurePosixPath
    def expand(text):
        return re.sub(r"\$\{([^{}]+)\}", lambda match: context[match[1]], text)
    separator = ";" if profile["platform"] == "Windows" else ":"
    paths = lambda entries: separator.join(str(path(context["bundle"]) / entry) for entry in entries)
    arguments = [*[expand(a) for a in profile["jvmArgs"]], "-cp", paths(profile["classpath"]),
                 profile["mainClass"], *profile["args"]]
    codec = "cp932" if profile["argfileEncoding"] == "windows-31j" else "utf-8"
    argfile = b"\n".join(json.dumps(a.encode(codec).decode("latin-1"), ensure_ascii=False).encode("latin-1")
                         for a in arguments) + b"\n"
    dli = expand(profile["dliText"])
    if profile["platform"] == "Windows":
        dli = dli.replace("\n", "\r\n")
    return {"argv": [str(path(context["bundle"]) / "jdk/bin" /
                         ("java.exe" if profile["platform"] == "Windows" else "java")),
                     "@" + str(path(context["auxiliary"]) / "java.args")],
            "cwd": context["run"], "environment": {k: expand(v) for k, v in profile["environment"].items()},
            "auxiliaryFiles": {"java.args": argfile, "server-launch.cfg": dli.encode("utf-8"),
                               "server-remap-classpath.txt": paths(profile["remapClasspath"]).encode("utf-8")}}


class ManagedProfileTests(unittest.TestCase):
    def test_exact_launch_both_platforms_and_inputs_unchanged(self):
        for system, encoding in (("Darwin", "utf-8"), ("Windows", "utf-8"), ("Windows", "windows-31j")):
            with self.subTest(system=system, encoding=encoding):
                values = fixture(system, encoding)
                before = deepcopy(values)
                self.assertIsNone(validate_managed_launch(*values))
                self.assertEqual(before, values)

    def test_shared_static_argfile_byte_vectors(self):
        vectors = json.loads(Path(__file__).with_name("managed-argfile-vectors-v1.json").read_bytes())
        for case in vectors["cases"]:
            with self.subTest(case=case["id"]):
                self.assertEqual(bytes.fromhex(case["expectedHex"]),
                                 _managed_argfile(case["arguments"], case["argfileEncoding"]))

    def test_exact_objects_reject_extra_missing_and_wrong_types(self):
        for index in range(3):
            for mode in ("extra", "missing", "list"):
                with self.subTest(index=index, mode=mode):
                    values = list(fixture())
                    if mode == "extra": values[index]["unused"] = "x"
                    elif mode == "missing": del values[index][next(iter(values[index]))]
                    else: values[index] = []
                    with self.assertRaises(ContractError): validate_managed_launch(*values)
        for key, value in (("version", True), ("version", "1"), ("role", "O"), ("platform", []),
                           ("jvmArgs", ()), ("mainClass", None), ("classpath", [True])):
            profile, context, actual = fixture()
            profile[key] = value
            with self.subTest(key=key, value=value), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)

    def test_required_properties_need_tokens_once(self):
        for change in ("missing", "literal", "duplicate"):
            profile, context, actual = fixture()
            entry = "-Dsushieric.managed.startNonce=${startNonce}"
            if change == "missing": profile["jvmArgs"].remove(entry)
            elif change == "literal": profile["jvmArgs"][profile["jvmArgs"].index(entry)] = entry.replace("${startNonce}", context["startNonce"])
            else: profile["jvmArgs"].append(entry)
            with self.subTest(change=change), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)

    def test_unknown_wrong_slot_and_malformed_tokens(self):
        for value in ("${unknown}", "${observedPid}", "${run}", "${auxiliary", "${{auxiliary}}"):
            profile, context, actual = fixture()
            profile["environment"]["TMPDIR"] = value
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)
        for key in ("startNonce", "privateTmp"):
            profile, context, actual = fixture()
            context[key] = "${bundle}"
            with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)

    def test_context_uuid_hash_paths_and_unused_values(self):
        for key, value in (("generationId", "generation-01"), ("generationId", True),
                           ("controlDescriptorSha256", "A" * 64), ("bundle", "relative"),
                           ("startNonce", "nonce\x00"), ("observedPid", "123")):
            profile, context, actual = fixture()
            context[key] = value
            with self.subTest(key=key), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)

    def test_observed_nonce_generation_and_descriptor_bytes(self):
        for name in ("startNonce", "generationId", "controlDescriptorSha256"):
            profile, context, actual = fixture()
            actual["auxiliaryFiles"]["java.args"] = actual["auxiliaryFiles"]["java.args"].replace(
                context[name].encode(), b"different")
            with self.subTest(name=name), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)

    def test_observed_argv_cwd_env_and_file_set(self):
        for mode in ("argv", "cwd", "env", "file", "bytes"):
            profile, context, actual = fixture()
            if mode == "argv": actual["argv"].append("--other")
            elif mode == "cwd": actual["cwd"] += "/other"
            elif mode == "env": actual["environment"]["EXTRA"] = "x"
            elif mode == "file": actual["auxiliaryFiles"]["other"] = b"x"
            else: actual["auxiliaryFiles"]["java.args"] = bytearray(actual["auxiliaryFiles"]["java.args"])
            with self.subTest(mode=mode), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)

    def test_environment_mapping_case_collision_and_empty_mapping(self):
        for index in (0, 2):
            values = list(fixture("Windows"))
            values[index]["environment"] = {"TEMP": "x", "temp": "x"}
            with self.assertRaises(ContractError): validate_managed_launch(*values)
        profile, context, actual = fixture()
        profile["environment"] = {}; actual["environment"] = {}
        validate_managed_launch(profile, context, actual)
        profile["environment"] = {"${privateTmp}": "literal"}
        with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)
        profile["environment"] = ["TMP=x"]
        with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)

    def test_reserved_argfile_classpath_and_conflicting_flags(self):
        for value in ("@other", "-cp", "--class-path=other", "-XX:-DisableAttachMechanism", "-Xmx512m"):
            profile, context, actual = fixture()
            profile["jvmArgs"].append(value)
            with self.subTest(value=value), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)
        profile, context, actual = fixture()
        profile["jvmArgs"].remove("-XX:+DisableAttachMechanism")
        with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)

    def test_classpath_remap_order_and_duplicates(self):
        for field in ("classpath", "remapClasspath"):
            for mode in ("order", "duplicate", "escape"):
                profile, context, actual = fixture()
                if mode == "order": profile[field].reverse()
                elif mode == "duplicate": profile[field].append(profile[field][0])
                else: profile[field][0] = "../outside"
                with self.subTest(field=field, mode=mode), self.assertRaises(ContractError):
                    validate_managed_launch(profile, context, actual)

    def test_encoding_names_unrepresentable_and_non_roundtrip(self):
        for system, encoding in (("Darwin", "windows-31j"), ("Windows", "mbcs"), ("Windows", "UTF8")):
            profile, context, actual = fixture(system)
            profile["argfileEncoding"] = encoding
            with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)
        for value in ("emoji\U0001f363", "wave\u301c"):
            with self.subTest(value=value), self.assertRaises(ContractError):
                _managed_argfile([value], "windows-31j")

    def test_dli_remap_and_control_bytes_are_exact(self):
        for name in ("server-launch.cfg", "server-remap-classpath.txt", "java.args"):
            profile, context, actual = fixture("Windows")
            actual["auxiliaryFiles"][name] += b"\n"
            with self.subTest(name=name), self.assertRaises(ContractError):
                validate_managed_launch(profile, context, actual)
        for value in ("bad\r\n", "bad\x00", "bad\ud800", "\ufeffbad", "bad\x7f"):
            profile, context, actual = fixture()
            profile["dliText"] = value
            with self.assertRaises(ContractError): validate_managed_launch(profile, context, actual)

    def test_no_file_environment_or_bundle_reads_and_no_shell_expansion(self):
        profile, context, actual = fixture()
        profile["args"] = ["$HOME", "*.jar", "~", 'a"b', "tail\\"]
        actual = observed(profile, context)
        with patch("dev_server.runtime_bundle.verify_bundle", side_effect=AssertionError("bundle read")), \
                patch("dev_server.runtime_bundle._directory", side_effect=AssertionError("directory read")), \
                patch("pathlib.Path.open", side_effect=AssertionError("file read")), \
                patch("os.getenv", side_effect=AssertionError("env read")):
            validate_managed_launch(profile, context, actual)

    def test_del_in_args_env_context_and_direct_argfile(self):
        for system in ("Darwin", "Windows"):
            for field in ("args", "env", "path", "cwd"):
                profile, context, actual = fixture(system)
                if field == "args": profile["args"].append("bad\x7f")
                elif field == "env": profile["environment"]["BAD"] = "bad\x7f"
                elif field == "path": context["privateTmp"] += "\x7f"
                else: context["run"] += "\x7f"
                with self.subTest(system=system, field=field), self.assertRaises(ContractError):
                    validate_managed_launch(profile, context, actual)
        for encoding in ("utf-8", "windows-31j"):
            with self.subTest(encoding=encoding), self.assertRaises(ContractError):
                _managed_argfile(["bad\x7f"], encoding)
