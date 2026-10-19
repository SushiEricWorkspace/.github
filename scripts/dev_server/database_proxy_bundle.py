"""固定Modの公開DTOと解決済み依存からDB proxyをコンパイルし、bundle内へ封じる。"""
import io
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import tempfile
import zipfile

from .build_inputs import canonical, digest, regular_bytes
from .contracts import ContractError, validate_relative_path
from .database_adapter import _check_jar, verify_host_adapter

MAIN = 'io.github.sushiericworkspace.devserver.DatabaseProxyMain'
SOURCE = 'scripts/dev_server/java/DatabaseProxyMain.java'
DESCRIPTOR = 'adapter/proxy.json'
DIRECTORY = 'adapter/proxy'
MODULES = ('org.jetbrains.kotlin:kotlin-stdlib', 'org.jetbrains.kotlinx:kotlinx-serialization-core-jvm',
           'org.jetbrains.kotlinx:kotlinx-serialization-json-jvm')
CODEC = 'io/github/sushiericworkspace/sushiericservermod/management/ManagementMessageCodec.class'


def _require(condition, message):
    if not condition:
        raise ContractError('proxy: ' + message)


def _mod_classes(root, classpath):
    for name in classpath:
        validate_relative_path(name)
    candidates = [name for name in classpath if (root / name).is_dir() and (root / name / CODEC).is_file()]
    _require(len(candidates) == 1, '固定server classpathの公開Mod codecが一意ではありません')
    return candidates[0]


def _dependencies(root, coordinate, classpath, kotlin_version):
    entries = json.loads(regular_bytes(root / 'inputs/dependencies.json'))
    selected = []
    for module in (*MODULES, coordinate.rsplit(':', 1)[0]):
        matches = [item for item in entries if item['coordinate'].rsplit(':', 1)[0] == module
                   and item['file'].endswith('.jar')]
        _require(len(matches) == 1, 'Mod解決済み依存の完全座標が一意ではありません')
        item = matches[0]
        validate_relative_path(item['file'])
        _require(item['file'] in classpath, 'Mod server classpathにない依存です')
        _check_jar(regular_bytes(root / item['file']))
        selected.append(item)
    _require(selected[0]['coordinate'] == MODULES[0] + ':' + kotlin_version,
             'Mod Kotlin版が一致しません')
    _require(selected[1]['coordinate'].rsplit(':', 1)[1] == selected[2]['coordinate'].rsplit(':', 1)[1],
             'Mod serializationのcore/json版が異なります')
    _require(selected[-1]['coordinate'] == coordinate, 'DataModel完全座標が一致しません')
    return selected


def _jar(data):
    _check_jar(data)
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        names = archive.namelist()
        prefix = MAIN.replace('.', '/')
        _require(prefix + '.class' in names and len(names) == len(set(names)), 'helper class集合が不正です')
        for name in names:
            _require(re.fullmatch(re.escape(prefix) + r'(?:\$[A-Za-z0-9]+)*\.class', name), '未知のhelper classです')
            content = archive.read(name)
            _require(content[:4] == b'\xca\xfe\xba\xbe' and int.from_bytes(content[6:8], 'big') == 65,
                     'helperはJDK21 classだけを使用します')


def _classpath(root, names):
    for name in names:
        validate_relative_path(name)
    paths = [str((root / name).absolute()) for name in names]
    _require(all(os.pathsep not in path for path in paths), 'bundle pathにclasspath区切り文字は使用できません')
    return os.pathsep.join(paths)


def freeze_proxy(freezer, lock, recipe, profiles):
    """登録済み.githubの記録済みsourceを固定JDK21でcompileする。旧bundleへ後付けしない。"""
    sources = [source for source in lock['sources'] if source['record']['repo'] == '.github']
    if not sources:
        return
    _require(len(sources) == 1 and any(item['path'] == SOURCE and not item.get('deleted')
                                     for item in sources[0]['inputs']), 'helper sourceが入力記録にありません')
    source_bytes = freezer.read(Path(sources[0]['root']) / SOURCE)
    source_name = DIRECTORY + '/DatabaseProxyMain.java'
    freezer.write(source_name, source_bytes)
    mod_classes = _mod_classes(freezer.root, profiles['server']['classpath'])
    libraries = _dependencies(freezer.root, recipe['dataModelCoordinate'],
                              profiles['server']['classpath'], recipe['kotlinVersion'])
    dependencies = [mod_classes, *[item['file'] for item in libraries]]
    java_home = Path(recipe['javaHome'])
    compiler = java_home / 'bin' / ('javac.exe' if os.name == 'nt' else 'javac')
    compiler_hash = digest(freezer.read(compiler))
    work = Path(tempfile.mkdtemp(prefix='.proxy-compile-', dir=freezer.root.parent))
    classes = work / 'classes'
    classes.mkdir()
    arguments = ['--release', '21', '-encoding', 'UTF-8', '-cp', _classpath(freezer.root, dependencies),
                 '-d', str(classes), str(freezer.root / source_name)]
    argfile = work / 'javac.args'
    argfile.write_text('\n'.join(json.dumps(value.replace('\\', '/')) for value in arguments), encoding='utf-8')
    with (work / 'javac.log').open('wb') as output:
        process = subprocess.Popen([str(compiler), '-J-Xmx512m', '@' + str(argfile)],
                                   cwd=work, stdout=output, stderr=subprocess.STDOUT)
        code = process.wait()
    (work / 'javac-exit.json').write_bytes(canonical({'pid': process.pid, 'exit': code}))
    _require(code == 0, f'helper compileが失敗しました。原本: {work}')
    data = io.BytesIO()
    with zipfile.ZipFile(data, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(classes.rglob('*')):
            if path.is_file():
                entry = zipfile.ZipInfo(path.relative_to(classes).as_posix(), (1980, 1, 1, 0, 0, 0))
                entry.external_attr = 0o100644 << 16
                archive.writestr(entry, regular_bytes(path))
    jar = data.getvalue()
    _jar(jar)
    jar_name = DIRECTORY + '/database-proxy.jar'
    freezer.write(jar_name, jar)
    descriptor = {'version': 1, 'mainClass': MAIN, 'source': source_name, 'sourceSha256': digest(source_bytes),
                  'jar': jar_name, 'jarSha256': digest(jar), 'modClasses': mod_classes,
                  'classpath': [jar_name, *dependencies], 'dependencies': libraries, 'javaVersion': recipe['javaVersion'],
                  'compilerSha256': compiler_hash, 'platform': platform.system(), 'machine': platform.machine()}
    freezer.write(DESCRIPTOR, canonical(descriptor))


def verify_proxy(root):
    """bundle全体のhash検証後に、追加helper・外部classpath・別OSへの差替えを拒否する。"""
    value = json.loads(regular_bytes(root / DESCRIPTOR))
    _require(type(value) is dict and value.keys() == {'version', 'mainClass', 'source', 'sourceSha256',
             'jar', 'jarSha256', 'modClasses', 'classpath', 'dependencies', 'javaVersion', 'compilerSha256', 'platform', 'machine'},
             '未知のhelper契約です')
    _require(type(value['version']) is int and value['version'] == 1 and value['mainClass'] == MAIN,
             '未知のhelper entrypointです')
    _require(value['source'] == DIRECTORY + '/DatabaseProxyMain.java' and
             value['jar'] == DIRECTORY + '/database-proxy.jar', 'helper保存先が異なります')
    host = verify_host_adapter(root)
    launch = json.loads(regular_bytes(root / 'launch/profiles.json'))
    mod_classes = _mod_classes(root, launch['profiles']['server']['classpath'])
    runtime = json.loads(regular_bytes(root / 'runtime.json'))
    kotlin = [item['version'] for item in runtime['tools'] if item['name'] == 'kotlin']
    _require(len(kotlin) == 1, 'Mod Kotlin toolが一意ではありません')
    libraries = _dependencies(root, runtime['dataModelCoordinate'], launch['profiles']['server']['classpath'], kotlin[0])
    _require(value['modClasses'] == mod_classes and value['dependencies'] == libraries and value['classpath'] ==
             [value['jar'], mod_classes, *[item['file'] for item in libraries]], 'helper classpathが固定入力と異なります')
    _require(value['javaVersion'] == runtime['javaVersion'] and value['javaVersion'].startswith('21.'),
             'JDK21版が一致しません')
    _require(type(value['compilerSha256']) is str and re.fullmatch('[0-9a-f]{64}', value['compilerSha256']),
             'compiler hashがありません')
    compiler = root / 'jdk/bin' / ('javac.exe' if value['platform'] == 'Windows' else 'javac')
    _require(digest(regular_bytes(compiler)) == value['compilerSha256'], 'compile時のJDKが異なります')
    for field in ('source', 'jar'):
        _require(digest(regular_bytes(root / value[field])) == value[field + 'Sha256'], 'helper bytesが異なります')
    _jar(regular_bytes(root / value['jar']))
    observed = {path.relative_to(root).as_posix() for path in (root / DIRECTORY).rglob('*') if path.is_file()}
    _require(observed == {value['source'], value['jar']}, '未知又は旧helper fileが混入しています')
    _require(value['platform'] == host['platform'] and value['machine'] == host['machine'], '作成OSが異なります')
    return value


def proxy_command(root):
    """実行先は固定bundleのJDKと専用mainのみ。秘密・要求本文を引数へ追加しない。"""
    from .runtime_bundle import verify_bundle
    verify_bundle(root)
    value = verify_proxy(root)
    _require(value['platform'] == platform.system() and value['machine'] == platform.machine(),
             '作成OS/アーキテクチャが異なります')
    java = root / 'jdk/bin' / ('java.exe' if os.name == 'nt' else 'java')
    return [str(java), '-Xmx256m', '-cp', _classpath(root, value['classpath']), MAIN]