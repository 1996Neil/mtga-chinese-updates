"""Resume maintainer work through stable publication without inventing evidence."""
import argparse
import datetime as dt
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import sqlite3
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
DEFAULT_CONFIG = Path.home() / 'Library/Application Support/MTGAChineseMaintainer/maintenance.json'
ENVIRONMENT = {
    'state_root': 'MTGA_STATE_ROOT', 'evidence_root': 'MTGA_EVIDENCE_ROOT',
    'compatibility_root': 'MTGA_COMPATIBILITY_ROOT', 'game_path': 'MTGA_GAME_PATH',
    'backup_root': 'MTGA_BACKUP_ROOT', 'history_root': 'MTGA_HISTORY_ROOT',
    'baseline_root': 'MTGA_BASELINE_ROOT', 'baseline_version': 'MTGA_BASELINE_VERSION',
    'portable_root': 'MTGA_PORTABLE_ROOT', 'client': 'MTGA_PUBLISHER_CLIENT',
    'public_key_file': 'MTGA_PUBLIC_KEY_FILE',
    'portable_manifest_sha256': 'MTGA_PORTABLE_MANIFEST_SHA256',
}
PATH_FIELDS = set(ENVIRONMENT) - {'baseline_version', 'portable_manifest_sha256'} | {
    'client', 'key_file', 'public_key_file', 'acceptance', 'run', 'since_run',
    'output_root', 'installer', 'network_acceptance', 'player_log',
}


def need(condition, message):
    if not condition:
        raise RuntimeError(message)


def load_config(path):
    path = Path(path).expanduser()
    need(path.is_file() and not path.is_symlink(), f'维护配置缺失或不是普通文件：{path}')
    value = json.loads(path.read_text())
    need(value.get('schema') == 'MTGA_CN_MAINTENANCE_CONFIG_V1', '维护配置格式不正确')
    for field in PATH_FIELDS:
        if field in value:
            need(isinstance(value[field], str) and Path(value[field]).expanduser().is_absolute(),
                 f'维护配置 {field} 必须是绝对路径')
            value[field] = str(Path(value[field]).expanduser().resolve())
    return value


def configure(value):
    # Shell overrides remain explicit and take precedence over saved defaults.
    for field, variable in ENVIRONMENT.items():
        if field in value:
            os.environ.setdefault(variable, value[field])
    if value.get('online') is True:
        os.environ.setdefault('MTGA_ONLINE', '1')


def bootstrap(argv):
    """Apply local paths before common/cli modules capture their environment."""
    try:
        if argv and argv[0] == 'maintain':
            raise SystemExit(main(argv[1:]))
        path = Path(os.environ.get('MTGA_MAINTENANCE_CONFIG', str(DEFAULT_CONFIG))).expanduser()
        if path.exists():
            configure(load_config(path))
    except (RuntimeError, OSError, ValueError) as error:
        print(f'维护已停止：{error}', file=sys.stderr)
        raise SystemExit(2)


def invoke(argv, log=None):
    result = subprocess.run([str(v) for v in argv], text=True, capture_output=True)
    if log is not None:
        Path(log).write_text(result.stdout + result.stderr)
    need(result.returncode == 0, result.stderr[-1800:] or result.stdout[-1800:] or
         f'{Path(str(argv[0])).name} 返回 {result.returncode}')
    return result.stdout


def read_snapshot():
    from detect import detect
    return detect()


def diagnose(snapshot):
    from official_base import diagnose as inspect
    return inspect(snapshot)


def latest():
    from cli import latest as read_latest
    return read_latest()


def source_preflight():
    """List every missing/changed registered source without creating a database."""
    from common import STATE, fingerprint
    path = STATE / 'registry.sqlite'
    if not path.is_file():
        if os.environ.get('MTGA_ONLINE') == '1':
            from portable_registry import configured
            configured().guard()
            return []  # A new isolated online Registry is initialized from pinned Git text.
        return [{'path': str(path), 'reason': '维护 Registry 缺失，不能自动假定历史译文已核验'}]
    failures = []
    with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as connection:
        for ident, source, sha, size in connection.execute('SELECT id,path,sha256,bytes FROM sources'):
            try:
                if fingerprint(Path(source)) != {'sha256': sha, 'bytes': size}:
                    failures.append({'source_id': ident, 'path': source,
                                     'reason': '来源哈希与原收据不一致'})
            except (OSError, RuntimeError, ValueError) as error:
                failures.append({'source_id': ident, 'path': source, 'reason': str(error)})
    return failures


def next_revision(index, releases, version, build):
    """Count drafts and both channels; a new tag never reuses a revision."""
    need(index.get('schema') == 'MTGA_CN_INDEX_V1' and index.get('channel') == 'stable',
         '远端签名索引不是 stable')
    expiry = dt.datetime.fromisoformat(index['expires_at'].replace('Z', '+00:00'))
    need(expiry > dt.datetime.now(dt.timezone.utc), '远端 stable 索引已过期，需先核对续期')
    numbers = [r['revision'] for r in index['releases']
               if (r['client_version'], r['steam_build']) == (version, build)]
    prefix = re.escape(f'mtga-{version}-{build}-r')
    for row in releases:
        match = re.fullmatch(prefix + r'(\d+)', row.get('tag_name', ''))
        if match:
            numbers.append(int(match[1]))
    return max(numbers, default=0) + 1


def remote_state(config, work):
    sys.path.insert(0, str(ROOT.parent / 'subscriber'))
    from github_publish import download, signed, validate_repo
    repo = config.get('repo', '1996Neil/mtga-chinese-updates')
    validate_repo(repo)
    feed = f'https://raw.githubusercontent.com/{repo}/main/feed'
    path = work / 'remote-index-stable.json'
    download(feed + '/index-stable.json', path, 2 * 1024 * 1024, 180)
    index = signed(path, config['client'], config['public_key_file'])
    pages = json.loads(invoke(['gh', 'api', '--paginate', '--slurp',
                              f'repos/{repo}/releases?per_page=100']))
    return index, [item for page in pages for item in page]


def stage_capture(snapshot, path):
    from common import fingerprint, read_json
    from detect import stable_signature
    from steam_capture import stage
    if not path.exists():
        stage(snapshot, path)
    else:
        need(stable_signature(read_json(path / 'snapshot.json')) == stable_signature(snapshot),
             '既有底稿取证不属于当前资源，保留现场并检查接续记录')
        need(read_json(path / 'base-capture.json')['all_pass'] is True, '既有底稿取证未通过')
        for name in ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'):
            row = snapshot['files'][name]
            need(fingerprint(path / 'official' / row['relative']) ==
                 {key: row[key] for key in ('sha256', 'bytes')}, '底稿取证缓存损坏')


def network_gate(config, run, subject):
    """Existing truthful machine evidence must precede stable publication."""
    from publisher import canonical, fingerprint
    path = Path(config.get('network_acceptance', str(run / 'network-acceptance.json')))
    need(path.is_file(), '缺少发布前正式客户端 HTTPS 下载和重建证据：' + str(path))
    evidence = json.loads(path.read_text())
    expected = {layer: {k: row[k] for k in ('sha256', 'bytes')}
                for layer, row in subject['output_files'].items()}
    need(evidence.get('schema') == 'MTGA_CN_NATIVE_NETWORK_ACCEPTANCE_V1' and
         evidence.get('status') == 'PASS' and evidence.get('anonymous_https') is True and
         evidence.get('native_reconstruction') is True and
         evidence.get('subject_sha256') == hashlib.sha256(canonical(subject)).hexdigest() and
         evidence.get('client') == fingerprint(config['client']) and evidence.get('outputs') == expected,
         '发布前联网证据不属于当前底稿、候选、构建代码及正式客户端')
    files = evidence.get('evidence_files', {})
    need(bool(files), '联网证据缺少原始预演日志/输出指纹')
    for name, expected_fp in files.items():
        need(Path(name).is_absolute() and fingerprint(name) == expected_fp,
             '联网证据原始文件缺失或变化')
    return fingerprint(path)


def sync_monitor(config, work, run, plan):
    """CAS-update only the current public probe seed, preserving all other settings."""
    import base64
    from export_local_resource_watch import export
    repo = config.get('repo', '1996Neil/mtga-chinese-updates')
    endpoint = f'repos/{repo}/contents/mtga-watch/config.json'
    envelope = json.loads(invoke(['gh', 'api', endpoint + '?ref=main']))
    monitor = json.loads(base64.b64decode(envelope['content'], validate=False))
    public_key = Path(config['public_key_file']).read_text().strip()
    need(monitor['feed'] == plan['feed'] and monitor['channel'] == 'stable' and
         monitor['public_key'] == public_key, '远端监控信任设置不同，拒绝覆盖')
    release = plan['release']
    previous = monitor['resource_probe']
    if all(previous.get(field) == release[field] for field in ('client_version', 'steam_build')) and \
            previous.get('published_release_id') == release['id']:
        return
    snapshot = json.loads((run / 'snapshot.json').read_text())
    log = Path(config.get('player_log', str(
        Path.home() / 'Library/Logs/Wizards Of The Coast/MTGA/Player.log')))
    seed = export(Path(snapshot['game']), log, release['id'])
    need(all(seed[field] == release[field] for field in ('client_version', 'steam_build')),
         '公开监控种子不属于当前发布版本')
    monitor['resource_probe'] = seed
    request = {'message': 'Follow signed stable release ' + release['id'],
               'branch': 'main', 'sha': envelope['sha'],
               'content': base64.b64encode((json.dumps(monitor, ensure_ascii=False, indent=2) +
                                           '\n').encode()).decode()}
    payload = work / 'monitor-cas-request.json'
    payload.write_text(json.dumps(request))
    invoke(['gh', 'api', '--method', 'PUT', endpoint, '--input', payload], work / 'monitor-sync.log')


def verify_publication(config, work, package, release_id):
    """Anonymous bodies and production-client reconstruction; never game install."""
    sys.path.insert(0, str(ROOT.parent / 'subscriber'))
    from github_publish import download, signed, publication_plan, inspect_installer
    from publisher import check, fingerprint
    repo = config.get('repo', '1996Neil/mtga-chinese-updates')
    plan = publication_plan(package, repo, 'mtga-' + release_id,
                            config['client'], config['public_key_file'])
    cache = work / 'verification'
    cache.mkdir(exist_ok=True)
    download(plan['feed'] + '/index-stable.json', cache / 'index.json', 2 * 1024 * 1024, 180)
    index = signed(cache / 'index.json', config['client'], config['public_key_file'])
    need(index['channel'] == 'stable', '发布后索引渠道不一致')
    ref = next((r for r in index['releases'] if r['id'] == release_id), None)
    need(ref == plan['index']['releases'][0], '发布后签名索引引用不一致')
    manifest = cache / ref['manifest']['name']
    download(plan['feed'] + '/' + manifest.name, manifest, 2 * 1024 * 1024, 180)
    check(manifest, ref['manifest']['fingerprint'])
    need(signed(manifest, config['client'], config['public_key_file']) == plan['release'],
         '发布后清单不一致')
    for row in plan['release']['resources']:
        for kind in ('patch', 'full'):
            asset = row[kind]
            path = cache / asset['name']
            if not path.exists() or fingerprint(path) != asset['fingerprint']:
                download(asset['url'], path, asset['fingerprint']['bytes'])
            check(path, asset['fingerprint'])
    download(f'https://raw.githubusercontent.com/{repo}/main/download.json',
             cache / 'download.json', 2 * 1024 * 1024, 180)
    pointer = json.loads((cache / 'download.json').read_text())
    need(pointer['channel'] == 'stable' and pointer['feed'] == plan['feed'],
         '当前下载入口与发布渠道不一致')
    prefix = f'https://github.com/{repo}/releases/download/mtga-{release_id}/'
    need(pointer['installer_url'].startswith(prefix), '当前安装包入口未跟随新修订')
    installer = cache / 'installer.zip'
    download(pointer['installer_url'], installer, pointer['fingerprint']['bytes'])
    check(installer, pointer['fingerprint'])
    inspect_installer(installer, plan, config['public_key_file'], config['client'])
    native = native_preview(config, work, plan)
    download(f'https://raw.githubusercontent.com/{repo}/main/mtga-watch/config.json',
             cache / 'monitor.json', 2 * 1024 * 1024, 180)
    monitor = json.loads((cache / 'monitor.json').read_text())
    need(monitor['feed'] == plan['feed'] and monitor['channel'] == 'stable' and
         monitor['public_key'] == Path(config['public_key_file']).read_text().strip() and
         monitor['resource_probe']['published_release_id'] == release_id,
         '监控入口尚未与新发布同步；请更新当前监控种子后重跑维护验证')
    return {'status': 'PASS', 'release_id': release_id,
            'signed_index': 'PASS', 'anonymous_all_resource_bodies': 'PASS',
            'installer_profile_hash': 'PASS', 'monitor_entry': 'PASS',
            'production_client_reconstruction': native,
            'game_installation': 'NOT_RUN'}


def native_preview(config, work, plan):
    """Exercise the production client using copied official inputs, never live writes."""
    from publisher import check, fingerprint
    release = plan['release']
    state = json.loads((work / 'maintenance.json').read_text())
    run = Path(state['run'])
    snapshot = json.loads((run / 'snapshot.json').read_text())
    live = Path(snapshot['game'])
    library = work / 'preview-library'
    game = library / 'steamapps/common/MTGA'
    preview_state = work / 'preview-state'
    cache = work / 'native-preview.json'
    binding = {'manifest': fingerprint(plan['manifest']),
               'client': fingerprint(config['client'])}
    original = {}
    for row in release['resources'] + release['protected_files']:
        relative = Path(row['relative'])
        need(not relative.is_absolute() and '..' not in relative.parts, '预演路径无效')
        original[str(relative)] = fingerprint(live / relative)
    if cache.exists():
        saved = json.loads(cache.read_text())
        if saved['binding'] == binding and saved['live_files'] == original:
            expected = {r['layer']: r['output'] for r in release['resources']}
            need(saved.get('status') == 'PASS' and
                 set(saved.get('reconstructed', {})) == set(expected),
                 '正式客户端预演缓存缺少完整五层验证')
            for layer, item in saved['reconstructed'].items():
                path = Path(item['path'])
                need(item['fingerprint'] == expected[layer] and path.is_absolute() and
                     path == path.resolve() and not path.is_symlink() and
                     path.parent.parent == preview_state / 'transactions' and
                     path.name == 'after-' + layer, '正式客户端预演缓存输出身份不符')
                check(path, expected[layer])
            return {'status': 'PASS', 'reused_verified_preview': True}
    for row in release['resources']:
        source = run / 'official' / row['relative']
        check(source, row['input'])
        destination = game / row['relative']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    for row in release['protected_files']:
        source = live / row['relative']
        check(source, row['fingerprint'])
        destination = game / row['relative']
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
    shutil.copyfile(live.parents[1] / 'appmanifest_2141910.acf',
                    library / 'steamapps/appmanifest_2141910.acf')
    invoke([config['client'], 'preview', '--profile', Path(plan['manifest']).parent / 'profile.json',
            '--state', preview_state, '--steam-library', library], work / 'native-preview.log')
    need(not (preview_state / 'installation.json').exists(), '预演意外生成安装事务')
    reconstructed = {}
    for row in release['resources']:
        check(game / row['relative'], row['input'])
        matches = sorted((preview_state / 'transactions').glob('*/after-' + row['layer']),
                         key=lambda p: p.stat().st_mtime_ns)
        need(bool(matches), '正式客户端没有生成预演输出：' + row['layer'])
        check(matches[-1], row['output'])
        reconstructed[row['layer']] = {'path': str(matches[-1]), 'fingerprint': row['output']}
    need(original == {relative: fingerprint(live / relative) for relative in original},
         '真实游戏在预演期间发生变化')
    cache.write_text(json.dumps({'status': 'PASS', 'binding': binding, 'live_files': original,
                                 'reconstructed': reconstructed}, indent=2) + '\n')
    return {'status': 'PASS', 'reused_verified_preview': False}


def execute(config, prepare_only=False):
    configure(config)
    from common import STATE, save_json, fingerprint
    from detect import stable_signature
    before = read_snapshot()
    version, build = before['client_version'], before['steam_build']
    identity = f'{version}-{build}'
    signature = hashlib.sha256(json.dumps(stable_signature(before), sort_keys=True).encode()).hexdigest()
    root = Path(config.get('output_root', str(STATE / 'maintenance'))).resolve()
    work = root / identity / signature[:16]
    work.mkdir(parents=True, exist_ok=True)
    state_path = work / 'maintenance.json'
    state = json.loads(state_path.read_text()) if state_path.exists() else {
        'schema': 'MTGA_CN_MAINTENANCE_RUN_V1', 'identity': identity,
        'input_signature': signature, 'completed': False, 'stages': [],
    }
    need(state['identity'] == identity and state['input_signature'] == signature,
         '维护接续记录不属于当前资源')

    def record(stage, **values):
        state.update(stage=stage, **values)
        if stage not in state['stages']:
            state['stages'].append(stage)
        save_json(state_path, state)

    try:
        missing_sources = source_preflight()
        save_json(work / 'blocked-inputs.json', {'sources': missing_sources})
        record('official_base', completed=False, blockers=[])
        base = diagnose(before)
        save_json(work / 'base-diagnostic.json', base)
        if not base['safe_to_rebuild']:
            layers = base.get('layers', [])
            if len(layers) == 5 and all(row.get('status') == 'verified' for row in layers):
                record('blocked_inputs', blockers=list(base.get('errors', [])) +
                       ([f'{len(missing_sources)} 项历史来源缺失或变化，详见 blocked-inputs.json']
                        if missing_sources else []),
                       next_action='五层官方底稿已核验；保留旧事务并恢复缺失备份/来源，勿重复采集或改写证明。')
                return 2
            capture = STATE / 'runs' / f'{version}_base_{build}_{signature[:16]}'
            record('base_capture', capture_run=str(capture))
            stage_capture(before, capture)
            record('base_review', blockers=['新版底稿已取证；需要独立来源审查与可信登记后续跑'] +
                   ([f'{len(missing_sources)} 项历史来源缺失或变化，详见 blocked-inputs.json']
                    if missing_sources else []),
                   next_action=f'独立审查 {capture / "base-capture.json"} 并登记受审底稿；不得自动批准。')
            return 2
        if missing_sources:
            record('source_inputs', blockers=[
                f'{len(missing_sources)} 项历史来源缺失或变化，详见 blocked-inputs.json'],
                next_action='从原备份恢复与原收据哈希相符的来源；不得跳过来源守卫或改写历史哈希。')
            return 2
        run = Path(config['run']).resolve() if config.get('run') else None
        if run is None:
            record('candidate')
            invoke([sys.executable, ROOT / 'cli.py', 'update'], work / 'build.log')
            run, report = latest()
        else:
            report = json.loads((run / 'report.json').read_text())
        need(report['client_version'] == version, '候选版本与当前游戏不符')
        candidate_snapshot = json.loads((run / 'snapshot.json').read_text())
        need(candidate_snapshot['steam_build'] == build, '候选 Steam build 与当前游戏不符')
        record('candidate_validation', run=str(run))
        from cli import validate_run
        validate_run(run, report)
        save_json(work / 'candidate-status.json', report)
        # Reuse the builder's already-verified delta rather than replaying the whole Registry.
        delta = run / 'diff.json'
        save_json(work / 'incremental-review.json', {
            'policy': '只审新增、英文/身份/用途变化、来源失效和冲突；未变化已审译文复用',
            'official_delta': json.loads(delta.read_text()) if delta.is_file() else None,
            'decision_files': [str(p) for p in (run / 'card-decisions.json',
                                                run / 'client-decisions.json',
                                                run / 'ui-decisions.json') if p.is_file()],
            'human_acceptance_created': False, 'runtime_acceptance_created': False,
        })
        blockers = list(report.get('blockers', []))
        if not report.get('safe_to_install'):
            blockers = blockers or ['当前候选尚未完成独立工程审查和真实游戏验收']
        acceptance = Path(config.get('acceptance', str(run / 'publisher-acceptance.json')))
        if not acceptance.is_file():
            blockers.append('缺少绑定当前候选的真实发布验收')
        if blockers:
            record('review_acceptance', blockers=blockers,
                   next_action='仅处理新增/英文或身份用途变化/来源失效/冲突项；补齐实际审查验收后重跑。')
            return 2
        # The exact original publisher validator checks subject binding before any signing.
        sys.path.insert(0, str(ROOT.parent / 'subscriber'))
        from publisher import subject_for, validate_acceptance
        subject = subject_for(candidate_snapshot, report,
                              json.loads((run / 'official-base.json').read_text()))
        validate_acceptance(json.loads(acceptance.read_text()), subject)
        required = ['client', 'key_file', 'public_key_file', 'installer']
        need(all(config.get(k) and Path(config[k]).is_file() for k in required),
             '发布配置缺少已有客户端、密钥路径、公钥或安装包；不自动生成替代密钥')
        network = network_gate(config, run, subject)
        need(stable_signature(read_snapshot()) == stable_signature(before),
             '准备期间游戏资源发生变化；重跑维护以使用当前版本，未开始签名发布')
        if prepare_only:
            record('ready_for_release', completed=False, blockers=[],
                   next_action='准备检查通过；去掉 --prepare-only 后继续正式发布。')
            return 0
        record('remote_preflight', blockers=[])
        index, releases = remote_state(config, work)
        if 'revision' not in state:
            revision = next_revision(index, releases, version, build)
            record('release_plan', revision=revision, release_id=f'{identity}-r{revision}')
        binding = {'subject': subject, 'acceptance': fingerprint(acceptance),
                   'client': fingerprint(config['client']), 'public_key': fingerprint(config['public_key_file']),
                   'installer': fingerprint(config['installer']), 'network_evidence': network,
                   'repo': config.get('repo', '1996Neil/mtga-chinese-updates')}
        if 'frozen_binding' in state:
            need(state['frozen_binding'] == binding, '当前候选/证据/客户端或配置与冻结发布不同，保留原包并检查')
        else:
            record('release_plan', frozen_binding=binding)
        package = work / 'package'
        receipt = work / 'publication.json'
        release_id = state['release_id']
        record('publication')
        if not package.exists():
            args = [sys.executable, ROOT / 'cli.py', 'release', '--version', version,
                    '--repo', config.get('repo', '1996Neil/mtga-chinese-updates'),
                    '--run', run, '--channel', 'stable', '--revision', state['revision'],
                    '--tag', 'mtga-' + release_id, '--output', package,
                    '--client', config['client'], '--key', config['key_file'],
                    '--public-key-file', config['public_key_file'], '--acceptance', acceptance,
                    '--minimum-client', '4', '--include-full', '--installer', config['installer'],
                    '--receipt', receipt, '--publish']
            invoke(args, work / 'release.log')
        elif not receipt.exists():
            # Reuse the same frozen ID after interrupted upload/index/finalization.
            from github_publish import publication_plan
            frozen = publication_plan(package, binding['repo'], 'mtga-' + release_id,
                                      config['client'], config['public_key_file'])
            need(frozen['release']['acceptance']['evidence_sha256'] ==
                 binding['acceptance']['sha256'], '冻结包验收与当前证据不符')
            for row in frozen['release']['resources']:
                need(row['output'] == {k: subject['output_files'][row['layer']][k]
                                      for k in ('sha256', 'bytes')} and
                     row['input'] == {k: subject['input_files'][row['layer']][k]
                                     for k in ('sha256', 'bytes')}, '冻结包资源与当前候选不符')
            invoke([sys.executable, ROOT.parent / 'subscriber/github_publish.py', 'publish',
                    '--package', package, '--repo', config.get('repo', '1996Neil/mtga-chinese-updates'),
                    '--tag', 'mtga-' + release_id, '--client', config['client'],
                    '--key', config['key_file'], '--public-key-file', config['public_key_file'],
                    '--installer', config['installer'], '--receipt', receipt], work / 'release-resume.log')
        from github_publish import publication_plan
        plan = publication_plan(package, binding['repo'], 'mtga-' + release_id,
                                config['client'], config['public_key_file'])
        record('monitor_sync')
        sys.path.insert(0, str(ROOT.parents[1] / 'mtga-watch'))
        sync_monitor(config, work, run, plan)
        record('public_verification')
        evidence = verify_publication(config, work, package, release_id)
        save_json(work / 'network-acceptance.json', evidence)
        need(stable_signature(read_snapshot()) == stable_signature(before),
             '维护过程中游戏资源发生变化，不能声称完成')
        record('complete', completed=True, blockers=[], verification=evidence)
        print(f'维护发布完成：{release_id}；stable 签名、匿名资源、安装包和监控入口核对通过。')
        return 0
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error) as error:
        record(state.get('stage', 'preflight'), completed=False, blockers=[str(error)])
        return 2
    finally:
        print(f'维护状态：{state["stage"]}；{"完成" if state["completed"] else "未完成"}')
        for blocker in state.get('blockers', []):
            print('阻塞：' + blocker)
        print(f'接续记录：{state_path}')
        print('重新运行同一 maintain 命令会重验输入并接续；未安装到游戏。')


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path,
                        default=Path(os.environ.get('MTGA_MAINTENANCE_CONFIG', str(DEFAULT_CONFIG))))
    parser.add_argument('--prepare-only', action='store_true', help='仅准备到发布门槛，不签名或写远端')
    parser.add_argument('--online', action='store_true', help='从锁定的线上源码和私有文本库准备隔离维护环境')
    args = parser.parse_args(argv)
    config = load_config(args.config) if args.config.exists() else None
    if args.online or config is None or config.get('online') is True:
        from online_sources import prepare_online
        config = prepare_online(args.config)
    need(config is not None, '维护配置未建立')
    os.environ['MTGA_MAINTENANCE_CONFIG'] = str(args.config.expanduser().resolve())
    configure(config)
    from common import STATE
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'maintenance-workflow.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return execute(config, args.prepare_only)
