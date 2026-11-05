"""Bootstrap pinned Git maintenance data and rebuildable local tooling.

Git carries code and text inputs. The game and signing key remain operator
inputs; no private data repository scripts are executed.
"""
import hashlib
import json
from pathlib import Path
import re
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[2]
LOCK = ROOT / 'maintenance-source.json'
DEFAULT_CONFIG = Path.home() / 'Library/Application Support/MTGAChineseMaintainer/maintenance.json'
CACHE = Path.home() / 'Library/Caches/MTGAChineseMaintainer/online.noindex'


def need(value, message):
    if not value:
        raise RuntimeError(message)


def fingerprint(path):
    digest = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return {'sha256': digest.hexdigest(), 'bytes': Path(path).stat().st_size}


def run(args):
    result = subprocess.run([str(item) for item in args], text=True, capture_output=True)
    need(result.returncode == 0,
         f'线上维护准备失败：{args[0]}：{result.stderr[-1500:]}')
    return result.stdout.strip()


def load_lock(path=LOCK):
    value = json.loads(Path(path).read_text())
    need(value.get('schema') == 'MTGA_CN_ONLINE_SOURCE_LOCK_V1', '线上维护数据锁格式无效')
    need(value.get('repository') == '1996Neil/mtga-chinese-baseline',
         '维护数据必须来自既有私有基线库')
    need(bool(re.fullmatch(r'[0-9a-f]{40}', value.get('commit', ''))),
         '维护数据必须锁定完整 Git commit')
    relative = value.get('directory', '')
    need(isinstance(relative, str) and relative and not Path(relative).is_absolute() and
         '..' not in relative.split('/') and '.' not in relative.split('/') and
         str(Path(relative)) == relative, '维护数据目录无效')
    expected = value.get('manifest_fingerprint', {})
    need(type(expected.get('bytes')) is int and expected['bytes'] > 0 and
         bool(re.fullmatch(r'[0-9a-f]{64}', expected.get('sha256', ''))),
         '维护数据清单缺少完整性指纹')
    return value


def sync_data(lock, cache=CACHE):
    cache = Path(cache).expanduser().resolve()
    cache.mkdir(parents=True, exist_ok=True)
    repository, commit = lock['repository'], lock['commit']
    destination = cache / 'sources' / commit
    destination.parent.mkdir(parents=True, exist_ok=True)
    visibility = json.loads(run(['gh', 'repo', 'view', repository,
                                 '--json', 'nameWithOwner,visibility']))
    need(visibility['nameWithOwner'] == repository and visibility['visibility'] == 'PRIVATE',
         '维护数据仓库身份或私有可见性发生变化')
    if not destination.exists():
        temporary = Path(tempfile.mkdtemp(prefix='fetch-', dir=destination.parent))
        checkout = temporary / 'checkout'
        # No hooks, installation steps or executables from the data repository.
        run(['git', '-c', 'http.version=HTTP/1.1', '-c', 'http.lowSpeedLimit=1024',
             '-c', 'http.lowSpeedTime=30', 'clone', '--quiet', '--no-checkout',
             f'https://github.com/{repository}.git', checkout])
        run(['git', '-C', checkout, 'checkout', '--quiet', '--detach', commit])
        need(run(['git', '-C', checkout, 'rev-parse', 'HEAD']) == commit,
             '下载的维护数据 Git commit 不匹配')
        checkout.rename(destination)
        temporary.rmdir()
    need(destination == destination.resolve() and not destination.is_symlink(),
         '维护数据缓存不能是符号链接')
    need(run(['git', '-C', destination, 'rev-parse', 'HEAD']) == commit and
         not run(['git', '-C', destination, 'status', '--porcelain']),
         '维护数据缓存含本地修改；保留现场，不自动重置')
    data = destination / lock['directory']
    manifest = data / 'manifest.json'
    need(data == data.resolve() and manifest.is_file() and not manifest.is_symlink() and
         fingerprint(manifest) == lock['manifest_fingerprint'],
         '锁定的维护数据清单缺失或哈希不符')
    return data


def save(path, value):
    path = Path(path).expanduser().absolute()
    need(path == path.resolve() and not path.is_symlink(), '维护配置不能是符号链接')
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists():
        existing = json.loads(path.read_text())
        # Keep the preceding operator configuration byte-for-byte for recovery.
        if not existing.get('online'):
            previous = path.with_name(path.stem + '-legacy' + path.suffix)
            if not previous.exists():
                previous.write_bytes(path.read_bytes())
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent,
                                     prefix='.maintenance-', delete=False) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        temporary = Path(stream.name)
    temporary.chmod(0o600)
    temporary.replace(path)


def prepare_online(config_path=None):
    """Prepare from committed source/data; no game writes or signing operations."""
    lock = load_lock()
    data = sync_data(lock)
    tools = CACHE / 'tools'
    tools.mkdir(parents=True, exist_ok=True)
    build_inputs = {name: fingerprint(ROOT / name) for name in
                    ('tools/subscriber/Client.swift', 'tools/subscriber/build.sh')}
    stamp = tools / 'build.json'
    known = json.loads(stamp.read_text()) if stamp.exists() else {}
    client = Path(known.get('path', str(tools / 'client/mtga-cn')))
    if (known.get('inputs') != build_inputs or not client.exists() or
            known.get('binary') != fingerprint(client)):
        fresh = Path(tempfile.mkdtemp(prefix='client-', dir=tools)) / 'bin'
        run(['/bin/zsh', ROOT / 'tools/subscriber/build.sh', fresh])
        # Keep content-addressed build folders; do not overwrite prior artifacts.
        client = fresh / 'mtga-cn'
        need(client.is_file(), '正式客户端编译结果缺失')
        stamp.write_text(json.dumps({'inputs': build_inputs, 'binary': fingerprint(client),
                                     'path': str(client)}))
    else:
        client = Path(known.get('path', client))
    # Existing signing protocol and trust key are preserved. No private key read here.
    keys = Path.home() / 'Library/Application Support/MTGAChinesePublisher/signing'
    public_file = tools / 'subscription-public.txt'
    expected_key = json.loads((ROOT / 'tools/subscriber/profile.json').read_text())['public_key']
    need(json.loads((data / 'manifest.json').read_text())['public_key'] == expected_key,
         '维护数据公钥与既有订阅协议不同')
    public_file.write_text(expected_key + '\n')
    profile = tools / 'profile.json'
    profile_value = {'schema': 'MTGA_CN_PROFILE_V1', 'channel': 'stable',
                     'feed': 'https://raw.githubusercontent.com/1996Neil/mtga-chinese-updates/main/feed',
                     'public_key': public_file.read_text().strip()}
    profile.write_text(json.dumps(profile_value, indent=2) + '\n')
    installer_id = hashlib.sha256((fingerprint(client)['sha256'] +
                                  fingerprint(profile)['sha256']).encode()).hexdigest()[:20]
    folder = tools / 'installers' / installer_id / 'MTGA-Chinese-Subscriber-v3.3'
    archive = Path(str(folder) + '.zip')
    if not archive.exists():
        run([__import__('sys').executable, ROOT / 'tools/subscriber/package_installer.py',
             '--client', client, '--profile', profile, '--output', folder])
    result = {
        'schema': 'MTGA_CN_MAINTENANCE_CONFIG_V1', 'online': True,
        'repo': '1996Neil/mtga-chinese-updates',
        'state_root': str(CACHE / 'state'), 'evidence_root': str(CACHE / 'state/evidence'),
        'portable_root': str(data), 'output_root': str(CACHE / 'work'),
        'portable_manifest_sha256': lock['manifest_fingerprint']['sha256'],
        'client': str(client), 'installer': str(archive),
        'key_file': str(keys / 'subscription-v1.ed25519'), 'public_key_file': str(public_file),
        'online_source_commit': lock['commit'],
    }
    save(config_path or DEFAULT_CONFIG, result)
    return result
