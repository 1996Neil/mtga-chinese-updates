"""Read-only workload inspection of captured inputs; no network or game writes."""
import argparse
from collections import Counter
import json
from pathlib import Path
import sqlite3

from baseline import Baseline, activate, configured
from common import STATE, database, fingerprint, read_json, require
from provenance import contexts


def official_for_run(run):
    run = Path(run).expanduser().resolve()
    snapshot = read_json(run / 'snapshot.json')
    official = {}
    for layer in ('card', 'client'):
        relative = Path(snapshot['files'][layer]['relative'])
        require(not relative.is_absolute() and '..' not in relative.parts, 'Invalid snapshot relative path')
        path = run / 'official' / relative
        expected = snapshot['official_entries'][path.name]['fingerprint']
        require(fingerprint(path) == expected, 'Captured official Raw input changed: ' + layer)
        official[layer] = path
    return snapshot, official


def inspect(run, baseline=None, registry_path=None, since=None):
    from registry import SCHEMA, decide
    from provenance import migrate, audit
    snapshot, official = official_for_run(run)
    current = contexts(official)
    previous = contexts(official_for_run(since)[1]) if since else None
    c = sqlite3.connect(':memory:')
    try:
        if registry_path is not None:
            path = Path(registry_path).expanduser().resolve()
            # SQLite mode=ro forbids creation and writes to the actual registry.
            with sqlite3.connect(path.as_uri() + '?mode=ro', uri=True) as original:
                original.backup(c)
        c.executescript(SCHEMA)
        migrate(c)
        source_failures = {}
        valid_sources = []
        for ident, path, sha, size in c.execute('SELECT id,path,sha256,bytes FROM sources'):
            try:
                require(fingerprint(Path(path)) == {'sha256': sha, 'bytes': size},
                        'Legacy source fingerprint mismatch')
                valid_sources.append((Path(path), {'sha256': sha, 'bytes': size}))
            except (OSError, RuntimeError, ValueError):
                source_failures[ident] = 'source-evidence-missing-or-changed'
        # Rebuild ephemeral admission against these exact inputs in memory.
        # No refresh: this command never fetches websites or sends notifications.
        audit(c, official, snapshot['client_version'], 'workload-report', refresh=False, activate=True)
        activate(c, baseline, current)
        rows = []
        for (layer, key), ctx in sorted(current.items()):
            value, decision = decide(c, 'workload-report', layer, key, ctx['english'],
                                     ctx['field_use'], persist=False)
            text, baseline_reason = baseline.check(layer, key, ctx) if baseline else (None, 'baseline-disabled')
            if decision['status'] == 'verified':
                classification = 'reviewed_override'
            elif decision['status'] == 'keep-english':
                classification = 'intentional_english'
            elif decision['status'] == 'baseline-adopted':
                classification = 'baseline_adopted'
            else:
                classification = 'needs_review'
            reason = decision['reason']
            if decision['source'] in source_failures:
                classification = 'needs_review'
                reason = source_failures[decision['source']]
            old = previous.get((layer, key)) if previous is not None else None
            rows.append({'layer': layer, 'key': key, 'classification': classification,
                         'reason': reason, 'baseline_reason': baseline_reason,
                         'baseline_eligible': text is not None,
                         'added': (old is None) if previous is not None else None,
                         'english_changed': (bool(old and old['english'] != ctx['english']))
                         if previous is not None else None})
        counts = Counter(r['classification'] for r in rows)
        for key in ('reviewed_override', 'intentional_english', 'baseline_adopted', 'needs_review'):
            counts.setdefault(key, 0)
        if baseline:
            baseline.guard()
        for path, expected in valid_sources:
            require(fingerprint(path) == expected, 'Legacy source changed during report')
        # Detect drift during inspection, including mutations by another process.
        require(official_for_run(run) == (snapshot, official), 'Captured inputs changed during report')
        return {'schema': 'MTGA_CN_WORKLOAD_V1', 'version': snapshot['client_version'],
                'registry_included': registry_path is not None,
                'baseline_inputs': baseline.inputs if baseline else {},
                'source_input_failures': source_failures,
                'counts': dict(counts), 'by_layer': dict(Counter(r['layer'] for r in rows)),
                'added': sum(r['added'] for r in rows) if previous is not None else None,
                'removed': len(previous.keys() - current.keys()) if previous is not None else None,
                'english_changed': sum(r['english_changed'] for r in rows) if previous is not None else None,
                'baseline_eligible': sum(r['baseline_eligible'] for r in rows),
                'baseline_rejected': dict(Counter(r['baseline_reason'] for r in rows
                                                  if not r['baseline_eligible'])),
                'rows': rows, 'human_acceptance': False, 'runtime_acceptance': False}
    finally:
        c.close()


def markdown(report):
    counts = report['counts']
    lines = [f'# MTGA {report["version"]} 维护工作量', '',
             '读取已捕获底稿；基线采用属于社区文本复用，不代表人工或游戏验收。', '',
             '| 项目 | 数量 |', '| --- | ---: |']
    labels = [('reviewed_override', '可复用的已审译文'), ('baseline_adopted', '可采用的社区基线'),
              ('intentional_english', '按现有规则保留英文'), ('needs_review', '需处理')]
    lines += [f'| {label} | {counts[key]} |' for key, label in labels]
    for key, label in [('added', '新增 key'), ('removed', '移除 key'), ('english_changed', '英文变化')]:
        lines.append(f'| {label} | {report[key] if report[key] is not None else "未提供上一版，无法判定"} |')
    if not report['registry_included']:
        lines += ['', '未提供 registry；已审覆盖与历史保留英文记录未纳入计算。']
    if report['source_input_failures']:
        lines += ['', f'另有 {len(report["source_input_failures"])} 个历史来源输入缺失或哈希不符；'
                  '相关译文不计为可复用的已审覆盖，生产构建仍会阻止继续。']
    lines += ['', '基线拒绝原因：' + json.dumps(report['baseline_rejected'], ensure_ascii=False), '',
              '以下最多列出 100 个需处理项；完整列表使用 --json。', '',
              '| 层 | key | 原因 |', '| --- | --- | --- |']
    def cell(value):
        return str(value).replace('|', '\\|').replace('\n', ' ').replace('\r', ' ')
    lines += [f'| {cell(r["layer"])} | {cell(r["key"])} | {cell(r["reason"])} |'
              for r in [r for r in report['rows'] if r['classification'] == 'needs_review'][:100]]
    return '\n'.join(lines)


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--run', required=True, type=Path, help='已捕获官方底稿的候选目录')
    p.add_argument('--since-run', type=Path, help='上一版底稿目录；省略时不猜测新增数量')
    p.add_argument('--registry', type=Path, help='只读现有 registry；省略时只计算基线')
    p.add_argument('--baseline-root', type=Path)
    p.add_argument('--baseline-version')
    p.add_argument('--json', action='store_true')
    a = p.parse_args(argv)
    snapshot, _ = official_for_run(a.run)
    baseline = (Baseline(a.baseline_root, a.baseline_version or snapshot['client_version'])
                if a.baseline_root else configured(snapshot['client_version']))
    result = inspect(a.run, baseline, a.registry, a.since_run)
    print(json.dumps(result, ensure_ascii=False, indent=2) if a.json else markdown(result))
    return 1 if result['counts']['needs_review'] or result['source_input_failures'] else 0
