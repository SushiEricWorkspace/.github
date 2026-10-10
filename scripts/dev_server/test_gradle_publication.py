"""実Commonの二つのソース組を並列発行する、明示実行のGradle統合試験。"""

from concurrent.futures import ThreadPoolExecutor
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dev_server.build_inputs import capture_source


@unittest.skipUnless(os.environ.get("SUSHIERIC_COMMON_GRADLE_SOURCE"), "実Gradle試験のCommon入力が未指定です")
class GradlePublicationTests(unittest.TestCase):
    def test_two_source_sets_publish_and_consumers_resolve_exact_coordinate(self):
        original = Path(os.environ["SUSHIERIC_COMMON_GRADLE_SOURCE"]).absolute()
        inputs = capture_source(original, "Common")["inputs"]
        with tempfile.TemporaryDirectory(prefix="sushieric-publication-test-") as temporary:
            root = Path(temporary)
            maven = root / "maven"
            commons = []
            for marker in ("task-a", "task-b"):
                common = root / marker / "Common"
                common.mkdir(parents=True)
                for entry in inputs:
                    if entry.get("deleted"):
                        continue
                    target = common / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original / entry["path"], target)
                    shutil.copymode(original / entry["path"], target)
                resource = common / "src/main/resources/task-input.txt"
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_text(marker, encoding="utf-8")
                commons.append(common)

            def execute(common, arguments, log):
                wrapper = common / ("gradlew.bat" if os.name == "nt" else "gradlew")
                with log.open("wb") as output:
                    result = subprocess.run([str(wrapper), *arguments, "--console=plain"], cwd=common,
                                            stdout=output, stderr=subprocess.STDOUT)
                self.assertEqual(0, result.returncode, log.read_text(encoding="utf-8", errors="replace")[-6000:])

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, common,
                                       ["publishDevelopment", f"-PcommonDevelopmentRepository={maven}"],
                                       root / f"publish-{index}.log") for index, common in enumerate(commons)]
                for future in futures:
                    future.result()
            coordinates = []
            for common in commons:
                values = dict(line.split("=", 1) for line in
                              (common / "build/development-publication.properties").read_text().splitlines())
                coordinates.append(values["mod"])
            self.assertNotEqual(*coordinates)

            # 同じMavenのmetadataに他方があっても、完全座標に対応する入力だけを読む。
            for index, common in enumerate(commons):
                consumer = common.parent / "consumer"
                consumer.mkdir()
                (consumer / "settings.gradle").write_text("rootProject.name='fixed-consumer'\n")
                (consumer / "build.gradle").write_text('''
plugins { id 'java' }
repositories { maven { url = uri(providers.gradleProperty('repository').get()) } }
configurations { smoke }
dependencies { smoke(providers.gradleProperty('coordinate').get()) { transitive = false } }
tasks.register('verifyPublished') {
    doLast {
        def artifacts = configurations.smoke.resolvedConfiguration.resolvedArtifacts
        assert artifacts.size() == 1
        assert artifacts.first().moduleVersion.id.toString() == providers.gradleProperty('coordinate').get()
        def jar = new java.util.zip.ZipFile(artifacts.first().file)
        try {
            assert jar.getInputStream(jar.getEntry('task-input.txt')).getText('UTF-8') == providers.gradleProperty('marker').get()
        } finally { jar.close() }
    }
}
''', encoding="utf-8")
                execute(common, ["-p", str(consumer), "build", "verifyPublished", f"-Prepository={maven}",
                                 f"-Pcoordinate={coordinates[index]}", f"-Pmarker=task-{'a' if index == 0 else 'b'}"],
                        root / f"consume-{index}.log")
