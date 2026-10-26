#!/usr/bin/env python3
"""Publisher-only signed, data-only release packaging. No game writes or upload."""
import argparse
import datetime as dt
import hashlib
import json
import shutil
from pathlib import Path
import struct
import subprocess
import time
import zlib

LAYERS = ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')
CHECKS = ('official_source_review', 'independent_engineering_review', 'chinese_display',
          'font_rendering', 'english_import', 'chinese_import', 'mixed_import',
          'qualified_import', 'special_structures', 'new_cards', 'restart_repeat')


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()


def fingerprint(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return {'sha256': h.hexdigest(), 'bytes': Path(path).stat().st_size}


def check(path, expected):
    if fingerprint(path) != {k: expected[k] for k in ('sha256', 'bytes')}:
        raise ValueError(f'文件已变化：{path}')


def dump(path, value):
    Path(path).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n')


def make_delta(base, candidate, destination):
    """Copy unchanged aligned ranges; compress changed bytes, never execute code."""
    block = 65536
    operations = []
    with Path(base).open('rb') as a, Path(candidate).open('rb') as b:
        offset = 0
        while True:
            target = b.read(block)
            if not target:
                break
            original = a.read(block)
            if original == target:
                if operations and operations[-1][0] == 'copy' and operations[-1][1] + operations[-1][2] == offset:
                    kind, start, size = operations[-1]; operations[-1] = kind, start, size + len(target)
                else:
                    operations.append(('copy', offset, len(target)))
            elif operations and operations[-1][0] == 'literal' and len(operations[-1][1]) + len(target) <= 1024*1024:
                operations[-1] = ('literal', operations[-1][1] + target)
            else:
                operations.append(('literal', target))
            offset += len(target)
    with Path(destination).open('xb') as out:
        out.write(b'MTGADL01' + struct.pack('<QI', offset, len(operations)))
        for op in operations:
            if op[0] == 'copy':
                out.write(b'\x00' + struct.pack('<QI', op[1], op[2]))
            else:
                packed = zlib.compress(op[1], 9)
                out.write(b'\x01' + struct.pack('<II', len(op[1]), len(packed)) + packed)
    return fingerprint(destination)


def verify_delta(base, patch, expected):
    """Reconstruct and hash delta output before signing; bounded, streaming verifier."""
    h = hashlib.sha256(); produced = 0
    with Path(base).open('rb') as src, Path(patch).open('rb') as p:
        def read(n):
            data = p.read(n)
            if len(data) != n: raise ValueError('差分截断。')
            return data
        if read(8) != b'MTGADL01': raise ValueError('差分格式无效。')
        size, count = struct.unpack('<QI', read(12))
        if size != expected['bytes'] or count > 1000000: raise ValueError('差分头无效。')
        for _ in range(count):
            kind = read(1)
            if kind == b'\x00':
                offset, length = struct.unpack('<QI', read(12))
                if not length or offset + length > Path(base).stat().st_size: raise ValueError('复制越界。')
                src.seek(offset); left = length
                while left:
                    data = src.read(min(left, 1024*1024))
                    if not data: raise ValueError('底稿截断。')
                    h.update(data); left -= len(data)
            elif kind == b'\x01':
                length, packed = struct.unpack('<II', read(8))
                if not 0 < length <= 4194304 or not 0 < packed <= 8388608: raise ValueError('解压范围无效。')
                decoder = zlib.decompressobj(); data = decoder.decompress(read(packed), length + 1)
                if len(data) != length or not decoder.eof or decoder.unused_data or decoder.unconsumed_tail: raise ValueError('差分解压失败。')
                h.update(data)
            else: raise ValueError('未知差分操作。')
            produced += length
            if produced > size: raise ValueError('差分输出越界。')
        if p.read(1) or produced != size or h.hexdigest() != expected['sha256']: raise ValueError('差分重建与已验收候选不一致。')


def subject_for(snapshot, report, base_meta):
    return {
        'client_version': snapshot['client_version'], 'steam_build': snapshot['steam_build'],
        'data_versions': snapshot['data_versions'], 'platform': 'macOS-Steam',
        'input_files': {r['layer']: {'relative': r['relative'], **{x: r['fingerprint'][x] for x in ('sha256', 'bytes')}} for r in base_meta['layers']},
        'output_files': {k: {x: report['candidate_files'][k][x] for x in ('relative', 'sha256', 'bytes')} for k in LAYERS},
        'protected_files': {k: v for k, v in snapshot['files'].items() if k not in LAYERS},
        'manifests': snapshot['manifests'], 'builder_code': report['code_fingerprints'],
    }


def validate_acceptance(evidence, subject):
    if evidence.get('schema') != 'MTGA_CN_PUBLISHER_ACCEPTANCE_V1':
        raise ValueError('发布验收格式无效。')
    expected = hashlib.sha256(canonical(subject)).hexdigest()
    if evidence.get('subject_sha256') != expected:
        raise ValueError('发布验收不属于这一组原始资源、候选、构建程序和版本。')
    if not all(evidence.get('checks', {}).get(k) is True for k in CHECKS):
        raise ValueError('发布验收尚未全部通过。')
    if not evidence.get('tester_quote', '').strip() or not evidence.get('scope', '').strip():
        raise ValueError('缺少真实测试者确认和明确验收范围。')
    arches = evidence.get('architectures')
    if not isinstance(arches, list) or not arches or not set(arches) <= {'arm64', 'x86_64'}:
        raise ValueError('需明确实际测试覆盖的处理器架构。')
    return arches


def sign(client, key, value, out, scratch):
    payload = scratch / 'payload.json'
    payload.write_bytes(canonical(value))
    subprocess.run([str(client), 'sign', '--key', str(key), '--input', str(payload), '--output', str(out)], check=True)
    payload.unlink()


def package(run, destination, client, key, public_key, revision, channel='testing', acceptance=None, sequence=None, feed=None, asset_base=None, local_acceptance=None, minimum_client=None, include_full=False):
    run, destination = Path(run).resolve(), Path(destination).resolve()
    snapshot = json.loads((run / 'snapshot.json').read_text())
    report = json.loads((run / 'report.json').read_text())
    if report.get('schema') != 'MTGA_CN_MAINTAINER_V1' or set(report.get('candidate_files', {})) != set(LAYERS):
        raise ValueError('缺少完整候选。')
    if report['localization']['static_status'] != 'PASS' or report['import_compatibility']['static_status'] != 'PASS':
        raise ValueError('静态检查未通过。')
    if channel not in {'stable', 'testing'} or revision < 1 or (sequence is not None and sequence < 1):
        raise ValueError('渠道或序号无效。')
    if minimum_client is not None and (type(minimum_client) is not int or minimum_client not in (2,3,4)):
        raise ValueError('显式最低客户端版本只能为2、3或4。')
    if include_full and minimum_client != 4:
        raise ValueError('完整资源覆盖需要最低客户端协议4。')
    base_meta = json.loads((run / 'official-base.json').read_text())
    if len(base_meta['layers']) != 5 or {r['layer'] for r in base_meta['layers']} != set(LAYERS):
        raise ValueError('官方底稿层集合不完整。')
    subject = subject_for(snapshot, report, base_meta)
    # Freeze accepted metadata before generating any delta; never sign newly observed bytes.
    sources = {}
    for layer in LAYERS:
        row = report['candidate_files'][layer]
        base = run / 'official' / snapshot['files'][layer]['relative']
        output = Path(row['path'])
        if run not in output.resolve().parents or output.is_symlink() or base.is_symlink():
            raise ValueError('构建输入不在候选目录内或含符号链接。')
        if row['relative'] != subject['input_files'][layer]['relative']:
            raise ValueError('候选与官方底稿路径不一致。')
        check(base, subject['input_files'][layer]); check(output, subject['output_files'][layer])
        sources[layer] = (base, output)
    # Any builder change requires a fresh build before either testing or stable packaging.
    builder_root = Path(__file__).resolve().parents[1] / 'chinese-maintainer'
    # A signed package must bind the same private baseline used by its candidate.
    # Do this here as well as in CLI validation, so direct packaging cannot skip it.
    for item in report.get('baseline_inputs', {}).values():
        check(Path(item['path']), {k: item[k] for k in ('sha256', 'bytes')})
    for name, fp in report['code_fingerprints'].items():
        path = Path(name) if Path(name).is_absolute() else builder_root / name
        check(path, fp)
    arches = ['arm64']
    status, evidence_hash, scope = 'testing', None, '离线构建及文件验证；真实游戏内验收尚未完成'
    if channel == 'stable':
        if report.get('high_risk_unknown'):
            raise ValueError('候选仍有高风险未知项，不能发布稳定包。')
        if not acceptance:
            raise ValueError('稳定包必须提供匹配的真实验收记录。')
        review = json.loads((run / 'independent-review.json').read_text())
        if review.get('status') != 'PASS' or review.get('code_fingerprints') != report['code_fingerprints'] or review.get('candidate_files') != report['candidate_files']:
            raise ValueError('独立工程审查不匹配。')
        evidence = json.loads(Path(acceptance).read_text())
        arches = validate_acceptance(evidence, subject)
        status, evidence_hash, scope = 'accepted', fingerprint(acceptance)['sha256'], evidence['scope']
    if local_acceptance is not None:
        if channel != 'testing': raise ValueError('简化本机验收仅适用于测试渠道。')
        local = json.loads(Path(local_acceptance).read_text())
        expected = {k: {x: report['candidate_files'][k][x] for x in ('relative','sha256','bytes')} for k in LAYERS}
        if (local.get('status') != 'PASS_USER_REPORTED_LOCAL_USE' or not local.get('user_quote') or
            local.get('client_version') != snapshot['client_version'] or local.get('steam_build') != snapshot['steam_build'] or
            local.get('run_id') != report.get('run_id') or local.get('installed_candidate_files') != expected):
            raise ValueError('本机验收不属于这一候选。')
        scope = local.get('scope')
        if not isinstance(scope, str) or not scope.strip():
            raise ValueError('本机验收需准确记录实际测试范围。')
        scope = scope.strip() + '；其他机器及完整发布验收待验证'
        evidence_hash = fingerprint(local_acceptance)['sha256']
    if asset_base is not None:
        import re
        if not re.fullmatch(r'https://github\.com/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+/releases/download/[A-Za-z0-9_.-]+', asset_base):
            raise ValueError('需要精确 GitHub Release HTTPS 资产目录。')
    destination.mkdir(parents=True, exist_ok=False)
    resources = []
    for layer in LAYERS:
        row = report['candidate_files'][layer]
        base, output = sources[layer]
        artifact_id = hashlib.sha256(canonical({'input':subject['input_files'][layer], 'output':subject['output_files'][layer]})).hexdigest()
        patch = destination / (layer + '-' + artifact_id + '.delta')
        fp = make_delta(base, output, patch)
        verify_delta(base, patch, subject['output_files'][layer])
        check(base, subject['input_files'][layer]); check(output, subject['output_files'][layer])
        resources.append({'layer': layer, 'relative': row['relative'],
                          'input': {k: subject['input_files'][layer][k] for k in ('sha256', 'bytes')},
                          'output': {k: subject['output_files'][layer][k] for k in ('sha256', 'bytes')},
                          'patch': {'name': patch.name, 'fingerprint': fp, **({'url': asset_base + '/' + patch.name} if asset_base else {})}})
        if include_full:
            full = destination / (layer + '-' + subject['output_files'][layer]['sha256'] + '.full')
            shutil.copyfile(output, full)
            full_fp = {k: subject['output_files'][layer][k] for k in ('sha256', 'bytes')}
            check(full, full_fp)
            resources[-1]['full'] = {'name': full.name, 'fingerprint': full_fp, **({'url': asset_base + '/' + full.name} if asset_base else {})}
    protected = [{'relative': row['relative'], 'fingerprint': {k: row[k] for k in ('sha256', 'bytes')}}
                 for layer, row in snapshot['files'].items() if layer not in LAYERS]
    protected += [{'relative': rel, 'fingerprint': fp} for rel, fp in snapshot['manifests'].items()]
    release_id = snapshot['client_version'] + '-' + snapshot['steam_build'] + '-r' + str(revision)
    release = {'schema': 'MTGA_CN_RELEASE_V1', 'id': release_id, 'revision': revision,
               'client_version': snapshot['client_version'], 'steam_build': snapshot['steam_build'],
               'minimum_client': minimum_client if minimum_client is not None else (2 if asset_base else 1),
               'platform': 'macOS-Steam', 'architectures': arches,
               'acceptance': {'status': status, 'evidence_sha256': evidence_hash, 'scope': scope},
               'resources': resources, 'protected_files': protected}
    for layer, (base, output) in sources.items():
        check(base, subject['input_files'][layer]); check(output, subject['output_files'][layer])
    manifest = destination / (release_id + '.json')
    sign(client, key, release, manifest, destination)
    index = {'schema': 'MTGA_CN_INDEX_V1', 'sequence': sequence if sequence is not None else time.time_ns() // 1000000,
             'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'channel': channel, 'releases': [{'id': release_id, 'revision': revision,
                 'client_version': snapshot['client_version'], 'steam_build': snapshot['steam_build'],
                 'manifest': {'name': manifest.name, 'fingerprint': fingerprint(manifest)}}]}
    sign(client, key, index, destination / ('index-' + channel + '.json'), destination)
    dump(destination / 'profile.json', {'schema': 'MTGA_CN_PROFILE_V1', 'feed': feed or str(destination),
                                      'public_key': public_key, 'channel': channel})
    template = {'schema': 'MTGA_CN_PUBLISHER_ACCEPTANCE_V1', 'subject_sha256': hashlib.sha256(canonical(subject)).hexdigest(),
                'checks': {k: False for k in CHECKS}, 'tester_quote': '', 'scope': '', 'architectures': []}
    dump(destination / 'acceptance-template.json', template)
    dump(destination / 'package-inventory.json', {p.name: fingerprint(p) for p in destination.iterdir() if p.is_file()})
    return release


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True, type=Path); p.add_argument('--output', required=True, type=Path)
    p.add_argument('--client', required=True, type=Path); p.add_argument('--key', required=True, type=Path)
    p.add_argument('--public-key', required=True); p.add_argument('--revision', required=True, type=int)
    p.add_argument('--sequence', type=int); p.add_argument('--channel', choices=('stable','testing'), default='testing')
    p.add_argument('--acceptance', type=Path); p.add_argument('--feed')
    p.add_argument('--asset-base'); p.add_argument('--local-acceptance', type=Path)
    p.add_argument('--minimum-client', type=int, choices=(2,3,4),
                   help='Require a client protocol version; version 3 enables authenticated-output adoption')
    p.add_argument('--include-full', action='store_true', help='Add signed complete resources for replacing unknown patches')
    args = p.parse_args()
    result = package(args.run,args.output,args.client,args.key,args.public_key,args.revision,args.channel,args.acceptance,args.sequence,args.feed,args.asset_base,args.local_acceptance,args.minimum_client,args.include_full)
    print('本地发布包：',args.output,'；验收状态：',result['acceptance']['status'])


if __name__ == '__main__':
    main()
