"""実SushiEricDataModelの二つのソース組を並列発行する、明示実行のGradle統合試験。"""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from dev_server.build_inputs import capture_source


@unittest.skipUnless(os.environ.get("SUSHIERIC_DATAMODEL_GRADLE_SOURCE"), "実Gradle試験のSushiEricDataModel入力が未指定です")
class GradlePublicationTests(unittest.TestCase):
    def test_two_source_sets_publish_and_consumers_resolve_exact_coordinate(self):
        original = Path(os.environ["SUSHIERIC_DATAMODEL_GRADLE_SOURCE"]).absolute()
        inputs = capture_source(original, "SushiEricDataModel")["inputs"]
        with tempfile.TemporaryDirectory(prefix="sushieric-publication-test-") as temporary:
            root = Path(temporary)
            maven = root / "maven"
            data_models = []
            for marker in ("task-a", "task-b"):
                data_model = root / marker / "SushiEricDataModel"
                data_model.mkdir(parents=True)
                for entry in inputs:
                    if entry.get("deleted"):
                        continue
                    target = data_model / entry["path"]
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(original / entry["path"], target)
                    shutil.copymode(original / entry["path"], target)
                resource = data_model / "src/main/resources/task-input.txt"
                resource.parent.mkdir(parents=True, exist_ok=True)
                resource.write_text(marker, encoding="utf-8")
                data_models.append(data_model)

            def execute(data_model, arguments, log):
                wrapper = data_model / ("gradlew.bat" if os.name == "nt" else "gradlew")
                with log.open("wb") as output:
                    result = subprocess.run([str(wrapper), *arguments, "--console=plain"], cwd=data_model,
                                            stdout=output, stderr=subprocess.STDOUT)
                self.assertEqual(0, result.returncode, log.read_text(encoding="utf-8", errors="replace")[-6000:])

            with ThreadPoolExecutor(max_workers=2) as pool:
                futures = [pool.submit(execute, data_model,
                                       ["publishDevelopment", f"-PdataModelDevelopmentRepository={maven}"],
                                       root / f"publish-{index}.log") for index, data_model in enumerate(data_models)]
                for future in futures:
                    future.result()
            coordinates = []
            for data_model in data_models:
                values = dict(line.split("=", 1) for line in
                              (data_model / "build/development-publication.properties").read_text().splitlines())
                self.assertEqual({"editor", "mod"}, values.keys())
                coordinates.append(values)
            for role in ("editor", "mod"):
                self.assertNotEqual(coordinates[0][role], coordinates[1][role])

            # 同じMavenのmetadataに他方があっても、役割別の完全座標に対応する入力だけを読む。
            for index, data_model in enumerate(data_models):
                for role in ("editor", "mod"):
                    coordinate = coordinates[index][role]
                    group, module, version = coordinate.split(":")
                    self.assertEqual(f"sushieric-datamodel-{role}-dev", module)
                    directory = maven / group.replace(".", "/") / module / version
                    hashes = {suffix: hashlib.sha256((directory / f"{module}-{version}.{suffix}").read_bytes()).hexdigest()
                              for suffix in ("jar", "pom")}
                    print(json.dumps({"source": index, "role": role, "coordinate": coordinate, "sha256": hashes}))
                    consumer = data_model.parent / ("consumer-" + role)
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
                    execute(data_model, ["-p", str(consumer), "build", "verifyPublished", f"-Prepository={maven}",
                                     f"-Pcoordinate={coordinate}", f"-Pmarker=task-{'a' if index == 0 else 'b'}"],
                            root / f"consume-{index}-{role}.log")
