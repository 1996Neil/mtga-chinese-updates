"""Offline same-core replay on an in-memory Registry; never builds a candidate."""
from collections import Counter
import json
from pathlib import Path
import sqlite3
from common import ROOT, STATE, database, fingerprint, read_json, require, save_json, atomic_bytes, managed_path
from registry import decide, import_corrections, bind_contexts
from provenance import audit, contexts, evidence_index, evidence_fingerprint, load_rows


def replay(c, official, baseline_run, version, corrections):
    """Caller supplies a disposable Registry. Historical decisions are immutable."""
    historical = {(layer,key): {'status':status,'english':en,'field_use':json.loads(use),
                                'translation':zh,'reason':reason,'source':source}
                  for layer,key,status,en,use,zh,reason,source in c.execute(
                      'SELECT layer,key,status,english,field_use,translation,reason,source_id FROM decisions WHERE run_id=?', (baseline_run,))}
    require(historical, 'Preflight baseline decisions missing')
    imported = import_corrections(c, corrections, official, version)
    source_audit = audit(c, official, version, baseline_run, refresh=False, activate=True)
    current = contexts(official)
    if 'resources' in official:
        import UnityPy
        from unity_layers import localization, objects, row_map
        rows = row_map(json.loads(localization(objects(UnityPy.load(str(official['resources'])))).read().m_Script))
        for key, langs in rows.items():
            require('en-US' in langs, 'UI English slot missing')
            current['ui',key] = {'english':langs['en-US'], 'field_use':['LocLibraryData.en-US'], 'identity':{'ui_key':key}}
    bind_contexts(c, current)
    pending = {k for k,v in historical.items() if v['status'] in {'candidate','needs-review'}}
    added = current.keys() - historical.keys()
    removed = historical.keys() - current.keys()
    changed = {k for k in current.keys() & historical.keys()
               if current[k]['english'] != historical[k]['english'] or current[k]['field_use'] != historical[k]['field_use']}
    # Identity/source applicability can change while English and use stay equal.
    admission_keys = {(r['layer'],str(r['key'])) for _,r in load_rows(c)}
    admission_keys.update((layer,key) for layer,key in c.execute(
        "SELECT layer,key FROM localization WHERE source_id LIKE 'manual-correction-%' OR source_id LIKE 'independent-resolution-%'"))
    for path in corrections.glob('*.json'):
        admission_keys.update((r['layer'],str(r['key'])) for r in read_json(path)['records'])
    research = evidence_index(c, current)
    results = []
    for layer,key in sorted(pending | added | removed | changed | admission_keys):
        ctx = current.get((layer,key))
        if ctx is None:
            results.append({'layer':layer,'key':key,'status':'unavailable','reason':'key-removed-from-snapshot',
                            'migration_eligible':False,'historical':historical.get((layer,key))})
            continue
        text, meta = decide(c, baseline_run, layer, key, ctx['english'], ctx['field_use'], persist=False)
        results.append({'layer':layer,**meta,'translation':text,'historical':historical.get((layer,key)),
                        'research_evidence':research.get((layer,key),[])})
    counts = Counter(r['status'] for r in results)
    eligible = [r for r in results if r['migration_eligible']]
    return {'schema':'MTGA_READ_ONLY_PREFLIGHT_V1','baseline_run':baseline_run,'snapshot_version':version,
            'scope':'Frozen pending plus added/removed/English-or-use-changed and human/AI/provenance-bound keys in the specified official snapshot; not live game state.',
            'historical_pending':len(pending),'evaluated':len(results),'current_pending':counts['candidate']+counts['needs-review'],
            'migration_eligible':len(eligible),'unavailable':counts['unavailable'],
            'new_keys':[list(k) for k in sorted(added)],'removed_keys':[list(k) for k in sorted(removed)],
            'changed_keys':[list(k) for k in sorted(changed)],
            'source_discovered':sum(bool(r.get('research_evidence')) for r in results),
            'approval_reusable':sum((r.get('source') or '').startswith('manual-correction-') for r in eligible),
            'format_adapted':sum('restored-ICU-other-selector' in r['reason'] or
                                 (r.get('historical') or {}).get('reason','').startswith('format-unsafe:') for r in eligible),
            'corrections_imported_in_memory':imported,'source_audit':source_audit,
            'admission_evidence_fingerprint':evidence_fingerprint(c),'records':results,
            'network_requests':0,'candidate_built':False,'game_applied':0,'new_client_acceptances':0,
            'approval_created':False,'real_registry_modified':False}


def run(run_dir, destination):
    run_dir, destination = managed_path(run_dir).resolve(), managed_path(destination).resolve()
    require(not destination.exists(), 'Preflight output must be a new directory')
    # Do not allow reports inside an existing candidate, approval, or source tree.
    require(destination.parent == STATE / 'preflights', 'Use state/preflights/<new-name>')
    report, snapshot = read_json(run_dir/'report.json'), read_json(run_dir/'snapshot.json')
    from official_base import cached_run
    bases = cached_run(run_dir, snapshot, STATE)
    official = {layer:Path(bases[layer]['official_base_path']) for layer in ('card','client','resources')}
    registry_path = STATE/'registry.sqlite'
    before = fingerprint(registry_path)
    from reporting import code_fingerprints
    def input_fingerprints():
        return {'registry':fingerprint(registry_path),'code':code_fingerprints(),
                'official_snapshot':{k:fingerprint(v) for k,v in official.items()},
                'baseline_snapshot':fingerprint(run_dir/'snapshot.json'),
                'baseline_report':fingerprint(run_dir/'report.json'),
                'approvals':{p.name:fingerprint(p) for p in sorted((ROOT/'reviewed-corrections').glob('*.json'))},
                'reviewed_resolutions':{str(p.relative_to(ROOT)):fingerprint(p) for p in sorted((ROOT/'reviewed-resolutions').glob('*/*.json'))},
                'source_format_pairs':(fingerprint(ROOT/'source-format-reviewed-pairs.json')
                                       if (ROOT/'source-format-reviewed-pairs.json').exists() else None)}
    inputs_before = input_fingerprints()
    scratch = sqlite3.connect(':memory:')
    try:
        with database(registry_path) as original: original.backup(scratch)
        from cli import source_guard
        source_guard(scratch)
        result = replay(scratch, official, report['run_id'], report['client_version'], ROOT/'reviewed-corrections')
        source_guard(scratch)
        from provenance import guard
        from registry import correction_scope
        guard(scratch)
        for layer,key,origin,en,use in scratch.execute(
                "SELECT layer,key,source_id,english,field_use FROM localization WHERE source_id LIKE 'manual-correction-%' OR source_id LIKE 'independent-resolution-%'"):
            correction_scope(scratch, origin, layer, key, en, json.loads(use))
    finally: scratch.close()
    require(cached_run(run_dir, snapshot, STATE) == bases, 'Official base trust changed during preflight')
    require(fingerprint(registry_path) == before, 'Real Registry changed during preflight')
    require(input_fingerprints() == inputs_before, 'Preflight inputs changed during replay')
    result['inputs'] = inputs_before
    save_json(destination/'report.json',result)
    lines = ['# MTGA 只读预演', '', f"基线：{result['baseline_run']}；不是实时游戏状态。", '',
             '| 口径 | 数量 |','|---|---:|',
             *[f'| {label} | {result[key]} |' for label,key in [('历史待审','historical_pending'),('本次评估','evaluated'),
               ('当前可迁移','migration_eligible'),('仍待审','current_pending'),('无法评估／已移除','unavailable'),
               ('找到已有研究证据','source_discovered'),('用户批准可复用','approval_reusable'),('格式阻塞解除','format_adapted')]],
             '', '来源发现、批准复用和格式适配为重叠维度，不相加。逐项结果见 report.json。',
             '', '联网 0；真实候选构建 0；游戏应用 0；新增客户端验收 0。历史 Registry、验收与安装状态未改。',
             '', '官方底稿由独立来源校验，回滚备份不参与底稿选择；本报告不是安装许可。']
    atomic_bytes(destination/'REPORT.md', '\n'.join(lines).encode())
    return result
