"""停止中controller Cを同版Host配布と固定JDKへ封じる。起動・Registry更新は行わない。"""

import io
import json
import os
from pathlib import Path
import platform
import zipfile

from .build_inputs import canonical, digest, regular_bytes
from .database_adapter import DESCRIPTOR as HOST_DESCRIPTOR, verify_host_adapter
from .database_proxy_bundle import _classpath, _compile, _jar, _require

MAIN = 'io.github.sushiericworkspace.devserver.OfflineControllerMain'
SOURCE = 'scripts/dev_server/java/OfflineControllerMain.java'
DESCRIPTOR = 'adapter/offline-controller.json'
DIRECTORY = 'adapter/offline-controller'


def freeze_offline_controller(freezer, lock, recipe):
    """記録済みsourceと同版Host全依存を使う。旧source-lockへ後付けしない。"""
    sources = [source for source in lock['sources'] if source['record']['repo'] == '.github']
    if not sources:
        return
    _require(len(sources) == 1 and any(item['path'] == SOURCE and not item.get('deleted')
                                     for item in sources[0]['inputs']), 'controller sourceが入力記録にありません')
    host = json.loads(freezer.read(freezer.root / HOST_DESCRIPTOR))
    _require(host['dataModelCoordinate'] == recipe['dataModelCoordinate'], 'controller DataModel座標が異なります')
    source_bytes = freezer.read(Path(sources[0]['root']) / SOURCE)
    source_name = DIRECTORY + '/OfflineControllerMain.java'
    freezer.write(source_name, source_bytes)
    jar, compiler_hash = _compile(freezer, recipe, host['classpath'], source_name, DIRECTORY, MAIN)
    jar_name = DIRECTORY + '/offline-controller.jar'
    freezer.write(jar_name, jar)
    value = {'version': 1, 'mainClass': MAIN, 'source': source_name, 'sourceSha256': digest(source_bytes),
             'jar': jar_name, 'jarSha256': digest(jar), 'classpath': [jar_name, *host['classpath']],
             'coordinate': host['coordinate'], 'javaVersion': recipe['javaVersion'],
             'compilerSha256': compiler_hash, 'platform': platform.system(), 'machine': platform.machine()}
    freezer.write(DESCRIPTOR, canonical(value))


def verify_offline_controller(root):
    """全bundle hash検証後、専用main・固定Host依存・source/compilerを照合する。"""
    value = json.loads(regular_bytes(root / DESCRIPTOR))
    _require(type(value) is dict and value.keys() == {
        'version', 'mainClass', 'source', 'sourceSha256', 'jar', 'jarSha256', 'classpath',
        'coordinate', 'javaVersion', 'compilerSha256', 'platform', 'machine'}, '未知のcontroller契約です')
    _require(type(value['version']) is int and value['version'] == 1 and value['mainClass'] == MAIN,
             '未知のcontroller entrypointです')
    _require(value['source'] == DIRECTORY + '/OfflineControllerMain.java' and
             value['jar'] == DIRECTORY + '/offline-controller.jar', 'controller保存先が異なります')
    host = verify_host_adapter(root)
    _require(value['coordinate'] == host['coordinate'] and
             value['classpath'] == [value['jar'], *host['classpath']], 'controller依存が固定Hostと異なります')
    runtime = json.loads(regular_bytes(root / 'runtime.json'))
    _require(value['javaVersion'] == runtime['javaVersion'] and value['javaVersion'].startswith('21.'),
             'controller JDK21版が一致しません')
    _require(value['platform'] == host['platform'] and value['machine'] == host['machine'],
             'controller作成OSが異なります')
    compiler = root / 'jdk/bin' / ('javac.exe' if value['platform'] == 'Windows' else 'javac')
    _require(digest(regular_bytes(compiler)) == value['compilerSha256'], 'controller compile時のJDKが異なります')
    for field in ('source', 'jar'):
        _require(digest(regular_bytes(root / value[field])) == value[field + 'Sha256'],
                 'controller bytesが異なります')
    _jar(regular_bytes(root / value['jar']), MAIN)
    observed = {path.relative_to(root).as_posix() for path in (root / DIRECTORY).rglob('*') if path.is_file()}
    _require(observed == {value['source'], value['jar']}, '未知又は旧controller fileが混入しています')
    return value


def offline_controller_command(root):
    """P専用。私設pipe/OwnedProcessと受付排他は呼出元の責務。本文を引数へ追加しない。"""
    from .runtime_bundle import verify_bundle
    verify_bundle(root)
    value = verify_offline_controller(root)
    _require(value['platform'] == platform.system() and value['machine'] == platform.machine(),
             'controller作成OS/アーキテクチャが異なります')
    java = root / 'jdk/bin' / ('java.exe' if os.name == 'nt' else 'java')
    return [str(java), '-Xmx256m', '-cp', _classpath(root, value['classpath']), MAIN]


def offline_worker_command(root):
    """P専用の別Wプロセス用固定entrypoint。Cとの別OwnedProcess所有は呼出元が行う。"""
    arguments = offline_controller_command(root)
    main = MAIN + '$Worker'
    with zipfile.ZipFile(root / DIRECTORY / 'offline-controller.jar') as archive:
        _require(main.replace('.', '/') + '.class' in archive.namelist(), '固定helperにworkerがありません')
    return [*arguments[:-1], main]


def offline_ready_command(root):
    """P専用の単発readStatus検証。排他・期限・終了/解放とstart許可は呼出元の責務。"""
    from .runtime_bundle import verify_bundle
    arguments = offline_controller_command(root)
    manifest = verify_bundle(root)
    main = MAIN + '$Ready'
    name = DIRECTORY + '/offline-controller.jar'
    expected = next(entry['sha256'] for entry in manifest['files'] if entry['path'] == name)
    data = regular_bytes(root / name)
    _require(digest(data) == expected, 'Ready検証のJAR原文が固定manifestと異なります')
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        _require(main.replace('.', '/') + '.class' in archive.namelist(), '固定helperにReady検証がありません')
    return [*arguments[:-1], main]
