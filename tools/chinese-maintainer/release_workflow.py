"""Orchestrate existing build, validation, packaging and publication gates."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

from common import ROOT, STATE, read_json, require


def execute(args, invoke=None):
    """Dry-run validates a candidate and emits a plan without signing or publishing."""
    invoke = invoke or (lambda argv: subprocess.run(argv, check=True))
    if args.baseline_root:
        os.environ['MTGA_BASELINE_ROOT'] = str(args.baseline_root.expanduser().resolve())
    if args.baseline_version:
        os.environ['MTGA_BASELINE_VERSION'] = args.baseline_version
    if args.run is None:
        from detect import detect
        require(detect()['client_version'] == args.version, 'Installed game version differs from requested release')
        # update uses its own installation lock and never installs a candidate.
        invoke([sys.executable, str(ROOT / 'cli.py'), 'update'])
        from cli import latest
        run, _ = latest()
    else:
        run = args.run.expanduser().resolve()
    require(run.is_dir(), 'Candidate directory missing')
    # update has released this same lock before returning. Hold it throughout
    # validation/signing/publication so another maintenance command cannot alter
    # the registry or installation state between stages.
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'maintainer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return finish(args, run, invoke)


def finish(args, run, invoke):
    from cli import validate_run
    report = read_json(run / 'report.json')
    require(report['client_version'] == args.version, 'Candidate version differs from requested release')
    validate_run(run, report)
    require(report['localization']['static_status'] == 'PASS' and
            report['import_compatibility']['static_status'] == 'PASS', 'Candidate static validation failed')
    output = (args.output or run / f'release-r{args.revision}').expanduser().resolve()
    snapshot = read_json(run / 'snapshot.json')
    tag = args.tag or f'{args.version}-{snapshot["steam_build"]}-r{args.revision}'
    subscriber = ROOT.parent / 'subscriber'
    package = [sys.executable, str(subscriber / 'publisher.py'), '--run', str(run),
               '--output', str(output), '--revision', str(args.revision), '--channel', args.channel,
               '--feed', f'https://raw.githubusercontent.com/{args.repo}/main/feed',
               '--asset-base', f'https://github.com/{args.repo}/releases/download/{tag}']
    missing = [name for name in ('client', 'key', 'public_key_file') if getattr(args, name) is None]
    blockers = []
    if not report.get('static_safe_to_trial'):
        blockers.append('当前候选的静态门槛或独立工程审查未通过')
    if args.channel == 'stable':
        if not report.get('safe_to_install'):
            blockers.append('当前候选的真实游戏验收未通过')
        if args.acceptance is None:
            blockers.append('稳定渠道缺少发布验收记录 --acceptance')
    if missing:
        blockers.append('缺少签名/客户端参数：' + ', '.join('--' + n.replace('_', '-') for n in missing))
    plan = {'version': args.version, 'run': str(run), 'channel': args.channel,
            'candidate_entries': {layer: report['localization'][layer]['counts']['applied']
                                  for layer in ('card', 'client')},
            'baseline_enabled': bool(report.get('baseline_inputs')),
            'output': str(output), 'repo': args.repo, 'tag': tag,
            'steps': ['构建或复用候选', '重验输入哈希、候选与审查门槛', '既有发布器构建并验证签名差分',
                      '既有 GitHub 发布器核对发布计划'] +
                     (['上传核验后推进 feed，再发布 Release'] if args.publish else []),
            'blockers': blockers, 'dry_run': args.dry_run,
            'signing_performed': False, 'network_writes_performed': False}
    if args.dry_run:
        print(json.dumps(plan, ensure_ascii=False, indent=2))
        return 1 if blockers else 0
    require(not blockers, '; '.join(blockers))
    require(not output.exists(), 'Release output already exists; use a fresh destination/revision')
    require(args.revision > 0, 'Revision must be positive')
    # Validate destination identities before invoking a signer.
    sys.path.insert(0, str(subscriber))
    from github_publish import validate_repo
    import re
    validate_repo(args.repo)
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9-]{0,38}/[A-Za-z0-9][A-Za-z0-9_.-]{0,99}',
                              args.repo)), 'Invalid GitHub repository identity')
    require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', tag)), 'Invalid release tag')
    public_key = args.public_key_file.expanduser().read_text().strip()
    package += ['--client', str(args.client), '--key', str(args.key), '--public-key', public_key]
    if args.acceptance:
        package += ['--acceptance', str(args.acceptance)]
    if args.minimum_client:
        package += ['--minimum-client', str(args.minimum_client)]
    if args.include_full:
        package += ['--include-full']
    invoke(package)
    validate_run(run, report)
    publication = [sys.executable, str(subscriber / 'github_publish.py'), 'plan',
                   '--package', str(output), '--repo', args.repo, '--tag', tag,
                   '--client', str(args.client), '--public-key-file', str(args.public_key_file)]
    invoke(publication)
    if args.publish:
        # Every network mutation remains in the existing, tested publisher.
        publication = list(publication)
        publication[2] = 'publish'
        publication += ['--key', str(args.key)]
        if args.installer:
            publication += ['--installer', str(args.installer)]
        if args.receipt:
            publication += ['--receipt', str(args.receipt)]
        validate_run(run, report)
        invoke(publication)
    print(json.dumps({**plan, 'signing_performed': True,
                      'network_writes_performed': args.publish}, ensure_ascii=False, indent=2))
    return 0


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--version', required=True)
    p.add_argument('--run', type=Path, help='复用已有候选；省略时先执行 update')
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--baseline-version')
    p.add_argument('--dry-run', action='store_true', help='验证候选并列出计划；不签名、不写远端')
    p.add_argument('--publish', action='store_true', help='签名构建后调用既有远端发布器')
    p.add_argument('--output', type=Path)
    p.add_argument('--revision', type=int, default=1)
    p.add_argument('--channel', choices=('testing', 'stable'), default='testing')
    p.add_argument('--repo', default='1996Neil/mtga-chinese-updates')
    p.add_argument('--tag')
    p.add_argument('--client', type=Path)
    p.add_argument('--key', type=Path)
    p.add_argument('--public-key-file', type=Path)
    p.add_argument('--acceptance', type=Path)
    p.add_argument('--minimum-client', type=int, choices=(2, 3, 4))
    p.add_argument('--include-full', action='store_true')
    p.add_argument('--installer', type=Path)
    p.add_argument('--receipt', type=Path)
    a = p.parse_args(argv)
    require(a.revision > 0, 'Revision must be positive')
    require(not a.include_full or a.minimum_client == 4, 'Complete resources require protocol 4')
    return execute(a)
