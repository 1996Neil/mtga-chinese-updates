#!/usr/bin/env python3
"""mtga-cn: one version migration, two independent capability gates."""
import argparse
import datetime
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
from collections import Counter
from common import (ROOT, STATE, GAME, require, fingerprint, read_json, save_json,
                    database, game_stopped, has_chinese, managed_path)
from detect import detect, snapshot_official, comparison_official, stable_signature, verify_current
from registry import open_registry, bootstrap, import_donor_review, import_corrections
from raw import build_localization, build_names, regressions, validate_db, validate_names, deck_lines
from unity_layers import (build_resources, build_fonts, validate_resources,
                          validate_font_objects, system_faces)
from reporting import write_report, code_fingerprints, runtime_for, RUNTIME_CHECKS
from installer import (create_transaction, apply_transaction, restore_transaction,
                       load_transaction, persist, guard_transaction, expected_signature)
from user_messages import label, base_message, error_message, GUIDE


def latest():
    p = managed_path(read_json(STATE / 'latest.json')['run_dir'])
    require(p.parent == STATE / 'runs' and p.resolve() == p, 'Invalid run pointer')
    return p, read_json(p / 'report.json')


def source_guard(registry):
    for path, sha, size in registry.execute('SELECT path,sha256,bytes FROM sources'):
        require(fingerprint(Path(path)) == {'sha256': sha, 'bytes': size}, f'Registry source changed: {path}')


def review_input_fingerprints():
    paths = list((ROOT / 'reviewed-corrections').glob('*.json'))
    paths += list((ROOT / 'reviewed-resolutions').glob('*/*.json'))
    paths += [ROOT / 'source-format-reviewed-pairs.json']
    paths += [p for p in (STATE / 'donor-review-packet.json', STATE / 'donor-independent-review.json') if p.exists()]
    result = {str(p): fingerprint(p) for p in sorted(paths)}
    from provenance import evidence_fingerprint, guard
    registry = open_registry()
    try:
        guard(registry)
        result['provenance-inputs-v1.1'] = evidence_fingerprint(registry)
    finally:
        registry.close()
    return result


def diff(official, old):
    result = {}
    for layer in ('card', 'client'):
        query = ('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1' if layer == 'card'
                 else 'SELECT Key,enUS FROM Loc')
        with database(official[layer]) as n, database(old[layer]) as o:
            before, after = dict(o.execute(query)), dict(n.execute(query))
            result[layer] = {'added_keys': sorted(map(str, after.keys() - before.keys())),
                             'removed_keys': sorted(map(str, before.keys() - after.keys())),
                             'changed_english': [{'key': str(k), 'before': before[k], 'after': after[k]}
                                                 for k in sorted(before.keys() & after.keys()) if before[k] != after[k]],
                             'unchanged_english': sum(before[k] == after[k] for k in before.keys() & after.keys())}
    return result


def refresh_gates(report, run_dir):
    loc, imp = report['localization'], report['import_compatibility']
    runtime = runtime_for(run_dir, report)
    loc['client_status'] = 'PASS' if runtime and runtime['localization_status'] == 'PASS' else '未验证'
    imp['client_status'] = 'PASS' if runtime and runtime['import_status'] == 'PASS' else '未验证'
    for kind, result in imp['fixtures']['fixtures'].items():
        result['native_client_status'] = runtime['imports'][kind] if runtime else '未验证'
    imp['fixtures']['special_client_status'] = 'PASS' if runtime else '未验证'
    report['blockers'] = []
    if loc['static_status'] != 'PASS':
        report['blockers'].append('中文显示静态检查未通过')
    if imp['static_status'] != 'PASS':
        report['blockers'].append('双语导入静态检查未通过')
    if report['high_risk_unknown']:
        report['blockers'].append('高风险未知项未处理')
    review = run_dir / 'independent-review.json'
    review_pass = False
    if review.exists():
        r = read_json(review)
        review_pass = (r.get('status') == 'PASS' and r.get('code_fingerprints') == code_fingerprints()
                       and r.get('candidate_files') == report['candidate_files'])
    report['independent_review'] = 'PASS' if review_pass else 'PENDING'
    if not review_pass:
        report['blockers'].append('独立工程审查未通过或证据已过期')
    report['static_safe_to_trial'] = not report['blockers']
    if loc['client_status'] != 'PASS' or imp['client_status'] != 'PASS':
        report['blockers'].append('当前候选的中文显示与原生英/中/混合导入待验收')
    report['safe_to_install'] = not report['blockers']


def update():
    from baseline import configured, activate
    from provenance import contexts
    build_code = code_fingerprints()
    transaction = load_transaction()
    require(not transaction or transaction['state'] not in {'trial', 'installing', 'rolling-back'},
            'A test/recovery transaction is active; record results or rollback before update')
    s = detect()
    baseline = configured(s['client_version'])
    baseline_inputs = baseline.inputs if baseline else {}
    if (STATE / 'latest.json').exists():
        previous_dir, previous = latest()
        if (previous.get('schema') == 'MTGA_CN_MAINTAINER_V1' and previous.get('code_fingerprints') == code_fingerprints()
                and previous.get('review_inputs') == review_input_fingerprints()
                and previous.get('baseline_inputs', {}) == baseline_inputs):
            same_before = stable_signature(s) == stable_signature(read_json(previous_dir / 'snapshot.json'))
            same_installed = (transaction and transaction['state'] == 'installed' and
                              transaction['run_id'] == previous['run_id'] and stable_signature(s) == expected_signature(transaction, after=True))
            if same_before or same_installed:
                validate_run(previous_dir, previous)
                write_report(previous_dir, previous)
                print('未发现新的官方更新；已重新验证现有候选／安装。', flush=True)
                print_summary(previous_dir, previous)
                return
    prior_run_id = latest()[1]['run_id'] if (STATE / 'latest.json').exists() else None
    run = s['client_version'] + '_' + datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    p = STATE / 'runs' / run
    p.mkdir(parents=True)
    save_json(STATE / 'latest.json', {'run_dir': str(p), 'state': 'building'})
    save_json(p / 'report.json', {'state': 'building', 'run_id': run, 'safe_to_install': False})
    try:
        print('正在核验官方底稿、版本与文件完整性；通过后保存构建用副本……', flush=True)
        official = snapshot_official(s, p)
        registry = open_registry()
        try:
            old = bootstrap(registry)
            if (STATE / 'donor-independent-review.json').exists():
                import_donor_review(registry, STATE / 'donor-review-packet.json',
                                    STATE / 'donor-independent-review.json', s['client_version'])
            import_corrections(registry, ROOT / 'reviewed-corrections', official, s['client_version'])
            source_guard(registry)
            old = comparison_official(s, p, old)
            delta = diff(official, old)
            save_json(p / 'diff.json', delta)
            from provenance import audit, write_audit
            prior_run = prior_run_id or run
            source_audit = audit(registry, official, s['client_version'], prior_run, refresh=True, activate=True)
            activate(registry, baseline, contexts(official) if baseline else {})
            write_audit(p / 'source-audit', source_audit)
            loc = {}
            migration = {}
            print('正在迁移已核验译文，并检查卡牌数据库和客户端文本……', flush=True)
            for layer in ('card', 'client'):
                r = build_localization(official[layer], p / (layer + '-localized.mtga'), layer, registry, run)
                migration[layer] = r.pop('updates')
                loc[layer] = r
            save_json(p / 'migration-plan.json', migration)
            print('正在生成中文显示、英文导入兼容名称及测试牌表……', flush=True)
            names = build_names(official['card'], p / 'card-localized.mtga', p / 'card-final.mtga', registry, run, old['card'])
            fixtures = regressions(official['card'], p / 'card-final.mtga', p / 'fixtures', names)
            print('正在基于官方资源构建中文界面和字体……', flush=True)
            resources = build_resources(official['resources'], official['bootstrap_fonts'], p / 'resources.assets', registry, run)
            migration['ui'] = resources.pop('translations')
            loc['resources'] = resources
            save_json(p / 'migration-plan.json', migration)
            chars = ''.join(migration['ui'].values())
            for file, query in [('card-final.mtga', 'SELECT Loc FROM Localizations_enUS WHERE Formatted=1'),
                                ('client-localized.mtga', 'SELECT enUS FROM Loc')]:
                with database(p / file) as db:
                    chars += ''.join(text or '' for text, in db.execute(query))
            fonts = build_fonts(official['bootstrap_fonts'], official['font_bundle'], p, s, chars)
            save_json(p / 'fonts-validation.json', fonts)
            loc['fonts'] = {'status': 'PASS', 'glyphs_checked': fonts['glyphs_checked'],
                            'missing_glyphs': fonts['missing_glyphs'], 'runtime_rendering': '未验证',
                            'bootstrap_validation': fonts['bootstrap_validation'], 'bundle_validation': fonts['bundle_validation'],
                            'official_manifest_modified': False}
            registry.commit()
            # Report the actual completed decisions, not the prior run's queue.
            source_audit = audit(registry, official, s['client_version'], run, refresh=False, activate=False)
            source_audit['queue_basis'] = 'actual-current-build-decisions'
            write_audit(p / 'source-audit', source_audit)
            source_guard(registry)
            if baseline:
                baseline.guard()
        finally:
            registry.close()
        candidate_paths = {'card': p / 'card-final.mtga', 'client': p / 'client-localized.mtga',
                           'resources': p / 'resources.assets', 'bootstrap_fonts': p / 'sharedassets0.assets',
                           'font_bundle': Path(fonts['bundle_path'])}
        candidates = {layer: {'path': str(path), 'relative': s['files'][layer]['relative'], **fingerprint(path)}
                      for layer, path in candidate_paths.items()}
        baseline = '未验证'
        if (STATE / 'official-baseline-runtime.json').exists():
            b = read_json(STATE / 'official-baseline-runtime.json')
            if b.get('snapshot') == stable_signature(s):
                baseline = b['status'] + '（用户实测；未独立核对导出身份/数量）'
        report = {'schema': 'MTGA_CN_MAINTAINER_V1', 'run_id': run, 'client_version': s['client_version'],
                  'steam_build': s['steam_build'], 'data_versions': s['data_versions'], 'state': 'candidate generated; not installed',
                  'observed': s['observed'], 'diff': delta, 'official_english_baseline': baseline,
                  'localization': {**loc, 'static_status': 'PASS', 'client_status': '未验证'},
                  'import_compatibility': {'names': names, 'fixtures': fixtures,
                    'static_status': 'PASS' if names['validation']['status'] == fixtures['status'] == 'PASS' else 'FAIL', 'client_status': '未验证'},
                  'candidate_files': candidates, 'code_fingerprints': build_code,
                  'official_asset_fingerprints': {layer: fingerprint(official[layer]) for layer in ('resources', 'bootstrap_fonts')},
                  'registry_fingerprint': fingerprint(STATE / 'registry.sqlite'),
                  'review_inputs': review_input_fingerprints(), 'source_audit': source_audit,
                  'baseline_inputs': baseline_inputs,
                  'high_risk_unknown': ([{'changed_card_identities': names['changed_card_identities']}] if names['changed_card_identities'] else []) +
                                       ([{'changed_canonical_names': names['changed_canonical_names']}] if names['changed_canonical_names'] else []),
                  'blockers': [], 'safe_to_install': False}
        # Proof files bind all validation plans and sources to this candidate.
        proof_names = ['snapshot.json', 'migration-plan.json', 'name-plan.json', 'fonts-validation.json',
                       'diff.json', 'fixtures/expected-60.json', 'fixtures/expected-fixtures.json',
                       'fixtures/new-cards.json', 'fixtures/special-structures.json',
                       'comparison-source.json', 'comparison/card.mtga', 'comparison/client.mtga']
        proof_names += ['source-audit/report.json', 'source-audit/REPORT.md']
        proof_names += ['fixtures/' + kind + '.txt' for kind in fixtures['fixtures']]
        proof_names.append('official-base.json')
        report['proof_files'] = {name: fingerprint(p / name) for name in proof_names}
        template = {'evidence_kind': 'direct_user_confirmation', 'user_quote': '',
                    'candidate_files': candidates,
                    'checks': {key: False for key in RUNTIME_CHECKS},
                    'imports': {key: '未验证' for key in fixtures['fixtures']},
                    'exports': {key: str(p / ('native-export-' + key + '.txt')) for key in fixtures['fixtures']}}
        save_json(p / 'runtime-template.json', template)
        require(code_fingerprints() == build_code, 'Maintainer code changed during build; rerun update')
        verify_current(s)
        refresh_gates(report, p)
        write_report(p, report)
        save_json(STATE / 'latest.json', {'run_dir': str(p), 'state': 'candidate'})
        print_summary(p, report)
    except Exception as error:
        save_json(p / 'report.json', {'state': 'BUILD FAILED; not installed', 'run_id': run,
                                     'safe_to_install': False, 'error': str(error)})
        raise


def print_summary(p, report):
    print(f'游戏版本：{report["client_version"]}')
    for name, key in (('中文显示', 'localization'), ('双语导入', 'import_compatibility')):
        row = report[key]
        print(f'{name}：静态检查 {label(row["static_status"])}；游戏内验收 {label(row["client_status"])}')
    print(f'是否允许正式安装：{"是（请先退出 MTGA，再按 4）" if report["safe_to_install"] else "否"}')
    print(f'状态：{label(report["state"])}\n报告：{p / "REPORT.md"}')
    for blocker in report.get('blockers', []):
        print(f'待处理：{blocker}')
    if not report['safe_to_install']:
        print('下一步：先完成上述阻塞项；临时测试与验收操作见菜单 6 或 mtga-cn guide。')


def validate_run(p, report):
    require(report.get('schema') == 'MTGA_CN_MAINTAINER_V1', 'Incomplete/unknown candidate')
    from baseline import guard_inputs
    guard_inputs(report.get('baseline_inputs', {}))
    require(report['code_fingerprints'] == code_fingerprints(), 'Maintainer code changed; run update again')
    require(report['review_inputs'] == review_input_fingerprints(), 'Reviewed translation inputs changed; run update again')
    for name, fp in report['proof_files'].items():
        require(fingerprint(p / name) == fp, 'Candidate proof/fixture changed: ' + name)
    for row in report['candidate_files'].values():
        require(fingerprint(Path(row['path'])) == {k: row[k] for k in ('sha256', 'bytes')}, 'Candidate hash changed')
    registry = open_registry()
    try:
        source_guard(registry)
    finally:
        registry.close()
    snapshot = read_json(p / 'snapshot.json')
    from official_base import cached_run
    bases = cached_run(p, snapshot)
    official = {layer: Path(row['official_base_path']) for layer, row in bases.items()}
    migration = read_json(p / 'migration-plan.json')
    validate_db(official['card'], p / 'card-localized.mtga', 'card', migration['card'])
    validate_db(official['client'], p / 'client-localized.mtga', 'client', migration['client'])
    plan = read_json(p / 'name-plan.json')
    validate_db(p / 'card-localized.mtga', p / 'card-final.mtga', 'card', {}, plan['added'])
    validate_names(official['card'], p / 'card-localized.mtga', p / 'card-final.mtga', plan['added'])
    validate_resources(official['resources'], p / 'resources.assets', migration['ui'], report['localization']['resources']['fallback_pointers'])
    f = read_json(p / 'fonts-validation.json')
    faces = system_faces()
    for face in f['source_fonts'].values():
        require(fingerprint(Path(face['source'])) == face['source_fingerprint'], 'System font changed')
    # Resolve source IDs by names for this version rather than carrying old IDs.
    import UnityPy
    env = UnityPy.load(str(official['bootstrap_fonts']))
    boot_changes = {o.path_id: faces['Light' if o.read().m_Name == 'DFMARUGOTHICSTD-W3' else 'Medium']
                    for o in env.objects if o.type.name == 'Font' and o.read().m_Name in {'DFMARUGOTHICSTD-W3', 'MOTOYAAPOROSTD-W6'}}
    validate_font_objects(official['bootstrap_fonts'], p / 'sharedassets0.assets', boot_changes)
    from bundle_crc import load_bundle, manual_unityfs_crc
    engine = next(iter(env.objects)).assets_file.unity_version
    UnityPy.config.FALLBACK_UNITY_VERSION = engine
    env, _ = load_bundle(official['font_bundle'])
    objs = {o.path_id: o for o in env.objects}
    bundle_changes = {}
    for o in objs.values():
        if o.type.name != 'MonoBehaviour':
            continue
        tree = o.read_typetree()
        name = tree.get('m_Name')
        if name in {'Font_Default_JP_DynamicFallback', 'Font_Title_JP_DynamicFallback'}:
            bundle_changes[tree['m_SourceFontFile']['m_PathID']] = faces['Light' if name == 'Font_Default_JP_DynamicFallback' else 'Medium']
    bundle_path = managed_path(f['bundle_path'])
    validate_font_objects(official['font_bundle'], bundle_path, bundle_changes)
    require(manual_unityfs_crc(bundle_path)['crc32'] == int(snapshot['official_entries'][official['font_bundle'].name]['crc'], 16), 'Font candidate CRC mismatch')
    refresh_gates(report, p)
    return report


def status():
    s = detect()
    print(f'游戏版本：{s["client_version"]}；Steam 构建：{s["steam_build"]}；数据版本：{s["data_versions"]}')
    transaction = load_transaction()
    if transaction and transaction['state'] in {'trial', 'installed', 'installing', 'rolling-back'}:
        try:
            guard_transaction(transaction, allow_partial=transaction['state'] in {'installing', 'rolling-back'})
            print(f'安装状态：{label(transaction["state"])}')
            p = managed_path(transaction['run_dir'])
            report = read_json(p / 'report.json')
            if transaction['state'] == 'installed' and report['code_fingerprints'] != code_fingerprints():
                runtime = runtime_for(p, report)
                print('已安装中文显示的历史验收：' + ('通过' if runtime else '未验证'))
                print('已安装双语导入的历史验收：' + ('通过' if runtime else '未验证'))
                print('历史验收仍对应当前安装文件；维护程序已更新，再次安装前需重新构建并审查候选。')
                return
            refresh_gates(report, p)
            print_summary(p, report)
            return
        except RuntimeError as error:
            print(error_message(error))
    print('中文显示与双语导入：当前版本需要重新检查，不能沿用旧版本验收。')
    if (STATE / 'latest.json').exists():
        p, r = latest()
        if r.get('schema') == 'MTGA_CN_MAINTAINER_V1' and stable_signature(s) == stable_signature(read_json(p / 'snapshot.json')):
            refresh_gates(r, p)
            print_summary(p, r)
        else:
            print(f'最近构建：{label(r.get("state", "未知"))}\n记录：{p / "report.json"}')
            if r.get('error'):
                print(error_message(RuntimeError(r['error'])))
            else:
                print('下一步：先按 2 检查底稿，检查通过后按 3 构建。')
    else:
        print('尚无构建记录。下一步：先按 2 检查底稿，通过后按 3 构建。')


def install(trial=False):
    p, report = latest()
    validate_run(p, report)
    require(report['static_safe_to_trial'] if trial else report['safe_to_install'],
            'SAFE TO INSTALL: NO — ' + '; '.join(report['blockers']))
    game_stopped()
    transaction = load_transaction()
    if transaction and transaction['state'] in {'trial', 'installed'} and transaction['run_id'] == report['run_id']:
        guard_transaction(transaction)
        require(not trial, 'Test session already active')
        transaction['state'] = 'installed'
        persist(transaction)
    else:
        require(not transaction or transaction['state'] not in {'trial', 'installing', 'rolling-back'}, 'Active transaction; rollback first')
        transaction = create_transaction(report, p)
        persist(transaction)
        apply_transaction(transaction, STATE / 'installation.json', 'trial' if trial else 'installed')
        persist(transaction)
    report['state'] = 'temporary test session; client validation pending' if trial else 'installed; client validated'
    write_report(p, report)
    print_summary(p, report)
    print('需要恢复本次安装前状态时：先退出 MTGA，再按 5，或执行 mtga-cn rollback。')


def rollback():
    transaction = load_transaction()
    require(transaction is not None, 'No Maintainer installation/test session exists; nothing changed')
    if transaction['state'] == 'rolled-back':
        print('此前已完成回滚，无需重复恢复。')
        return
    restore_transaction(transaction, STATE / 'installation.json')
    persist(transaction)
    p = managed_path(transaction['run_dir'])
    report = read_json(p / 'report.json')
    report['state'] = 'rolled back to exact pre-transaction resources; not installed'
    write_report(p, report)
    print('已恢复到本次安装／临时测试开始前的实际文件状态。')


def record_runtime(path):
    p, report = latest()
    validate_run(p, report)
    transaction = load_transaction()
    require(transaction and transaction['run_id'] == report['run_id'] and transaction['state'] == 'trial', 'Start a current candidate test-session first')
    guard_transaction(transaction)
    evidence = read_json(path)
    require(evidence.get('evidence_kind') == 'direct_user_confirmation' and
            isinstance(evidence.get('user_quote'), str) and evidence['user_quote'].strip(), 'Actual user confirmation required')
    require(evidence.get('candidate_files') == report['candidate_files'], 'Runtime evidence must bind this candidate')
    require(all(evidence.get('checks', {}).get(k) is True for k in RUNTIME_CHECKS), 'Chinese/special-structure checks incomplete')
    kinds = tuple(report['import_compatibility']['fixtures']['fixtures'])
    require(all(evidence.get('imports', {}).get(k) == 'PASS' for k in kinds), 'One or more native import tests did not pass')
    new_cards = read_json(p / 'fixtures/new-cards.json')
    if new_cards:
        accepted_new = evidence.get('new_cards', {})
        for card in new_cards:
            result = accepted_new.get(str(card['arena_grp_id']), {})
            require(result.get('english_import') == 'PASS' and result.get('canonical') == card['english'] and
                    result.get('display') == ('PASS' if has_chinese(card['display']) else 'KEEP_ENGLISH'),
                    'New-card canonical/display/native-import check incomplete')
    # Each native export must resolve to the exact trusted TitleIds and quantities.
    expected = read_json(p / 'fixtures/expected-fixtures.json')
    with database(p / 'card-final.mtga') as c:
        titles = {i for i, in c.execute('SELECT DISTINCT TitleId FROM Cards WHERE IsPrimaryCard=1 AND IsToken=0')}
        variants = {}
        for i, _, name in c.execute('SELECT * FROM Localizations_enUS'):
            if i in titles:
                variants.setdefault(name, set()).add(i)
        cards = list(c.execute('SELECT TitleId,ExpansionCode,CollectorNumber FROM Cards WHERE IsPrimaryCard=1 AND IsToken=0'))
        exports = {}
        for kind in kinds:
            expected_counts = Counter()
            for row in expected[kind]:
                expected_counts[row['title_id']] += row['quantity']
            source = managed_path(Path(evidence['exports'][kind]).expanduser()).resolve()
            text = source.read_text()
            got = Counter()
            for quantity, name, expansion, collector in deck_lines(text):
                ids = variants.get(name, set())
                if expansion:
                    ids = ids & {i for i, e, n in cards if e == expansion and str(n) == collector}
                require(len(ids) == 1, f'Native export identity unresolved: {kind}/{name}')
                got[next(iter(ids))] += quantity
            require(got == expected_counts and sum(got.values()) == 60, f'Native export identity/quantity mismatch: {kind}')
            destination = p / ('native-export-' + kind + '.txt')
            from common import atomic_bytes
            atomic_bytes(destination, text.encode())
            exports[kind] = {'path': str(destination), **fingerprint(destination), 'TitleIds_and_60_quantity_verified': True}
    evidence['exports'] = exports
    evidence['localization_status'] = 'PASS'
    evidence['import_status'] = 'PASS'
    evidence['accepted_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_json(p / 'runtime-verification.json', evidence)
    registry = open_registry()
    try:
        registry.execute('UPDATE card_names SET last_verified_version=?,compatibility_status=? WHERE run_id=?',
                         (report['client_version'], 'native-regression-passed;unsampled-cards-static-only', report['run_id']))
        registry.commit()
    finally:
        registry.close()
    refresh_gates(report, p)
    report['localization']['fonts']['runtime_rendering'] = 'PASS'
    write_report(p, report)
    print_summary(p, report)


class ChineseHelp(argparse.HelpFormatter):
    def _format_usage(self, usage, actions, groups, prefix):
        return super()._format_usage(usage, actions, groups, '用法：' if prefix is None else prefix)


class ChineseParser(argparse.ArgumentParser):
    def error(self, message):
        self.print_usage(sys.stderr)
        self.exit(2, f'参数有误，请使用 --help 查看命令说明，或执行 mtga-cn guide 查看中文操作指南。\n技术详情：{message}\n')


def main():
    # These commands have their own options and return before creating state or
    # taking the installation lock. Reports remain strictly read-only.
    if len(sys.argv) > 1 and sys.argv[1] in {'report', 'release'}:
        if sys.argv[1] == 'report':
            from workload import main as command_main
        else:
            from release_workflow import main as command_main
        result = command_main(sys.argv[2:])
        if result:
            raise SystemExit(result)
        return
    parser = ChineseParser(description='MTGA 中文汉化与双语导入维护。首次使用请执行：mtga-cn guide',
                           prog='mtga-cn', formatter_class=ChineseHelp,
                           add_help=False, usage='%(prog)s 命令 [选项]')
    parser._positionals.title = '命令'
    parser._optionals.title = '选项'
    parser.add_argument('-h', '--help', action='help', help='显示命令帮助并退出')
    parser.add_argument('command', choices=('status', 'update', 'validate', 'install', 'rollback', 'test-session', 'record-runtime', 'audit-sources', 'import-sources', 'preflight', 'diagnose-base', 'guide', 'report', 'release'),
                        help='status 查看状态；diagnose-base 检查底稿；update 构建；validate 验证；install 安装；rollback 回滚；guide 操作指南。临时测试与来源维护详见 README。')
    parser.add_argument('--human', action='store_true', help='底稿诊断使用中文摘要；省略时保持原有 JSON 输出')
    parser.add_argument('--details', action='store_true', help='失败时补充原始技术错误，供维护者排查')
    parser.add_argument('--run', type=Path, help='离线预演使用的官方快照目录；默认最近候选')
    parser.add_argument('--begin', action='store_true', help='开始可回滚的临时测试；要求静态检查及独立审查通过，会替换游戏资源')
    parser.add_argument('--evidence', type=Path, help='真实游戏内验收记录的 JSON 文件路径')
    parser.add_argument('--pack', type=Path, help='译文来源包路径')
    parser.add_argument('--review', type=Path, help='独立来源审查记录路径')
    parser.add_argument('--limit', type=int, default=20, help='来源审计最多展示的工作项数量，默认 20')
    parser.add_argument('--refresh', action='store_true', help='刷新已登记的官方规则来源，最多读取两个地址，不批量抓取卡牌')
    parser.add_argument('--baseline-root', type=Path, help='私有基线库绝对路径；不设置则保持旧流程')
    parser.add_argument('--baseline-version', help='显式复用的基线版本；仍逐项核对当前英文、身份和用途')
    args = parser.parse_args()
    if args.baseline_root:
        os.environ['MTGA_BASELINE_ROOT'] = str(args.baseline_root.expanduser().resolve())
    if args.baseline_version:
        os.environ['MTGA_BASELINE_VERSION'] = args.baseline_version
    if args.command == 'guide':
        print(GUIDE)
        print(f'完整验收清单与维护文档：{ROOT / "README.md"}')
        return
    if args.command == 'diagnose-base':
        require(not args.refresh, 'Base diagnosis never uses network')
        from official_base import diagnose
        before = detect()
        result = diagnose(before)
        require(stable_signature(detect()) == stable_signature(before), 'Resources changed during diagnosis')
        print(base_message(result) if args.human else json.dumps(result, ensure_ascii=False, indent=2))
        return
    STATE.mkdir(parents=True, exist_ok=True)
    with (STATE / 'maintainer.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'preflight':
            require(not args.refresh, 'Preflight never uses network')
            from preflight import run
            destination = STATE / 'preflights' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
            result = run(args.run or latest()[0], destination)
            print('离线预演报告：', destination / 'REPORT.md')
            print('历史待审：', result['historical_pending'], '当前待审：', result['current_pending'],
                  '可迁移：', result['migration_eligible'], '无法评估：', result['unavailable'])
            print('本次未联网、未构建或安装候选，也未新增游戏内验收。')
        elif args.command in {'audit-sources', 'import-sources'}:
            from provenance import import_pack, audit, write_audit
            registry = open_registry()
            try:
                if args.command == 'import-sources':
                    require(args.pack is not None and args.review is not None, '--pack and --review required')
                    print('已导入的来源记录数：', import_pack(registry, args.pack, args.review))
                p, report = latest()
                snapshot = read_json(p / 'snapshot.json')
                official = {layer: p / 'official' / snapshot['files'][layer]['relative'] for layer in ('card','client')}
                for layer,path in official.items():
                    require(fingerprint(path) == snapshot['official_entries'][path.name]['fingerprint'], 'Official source snapshot changed')
                audit_report = audit(registry, official, report['client_version'], report['run_id'], args.limit, args.refresh)
                destination = STATE / 'source-audits' / datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
                write_audit(destination, audit_report)
                print('译文来源审计报告：', destination / 'REPORT.md')
            finally:
                registry.close()
        elif args.command == 'status':
            status()
        elif args.command == 'update':
            update()
        elif args.command == 'validate':
            p, report = latest()
            validate_run(p, report)
            write_report(p, report)
            print_summary(p, report)
        elif args.command == 'install':
            install()
        elif args.command == 'rollback':
            rollback()
        elif args.command == 'test-session':
            require(args.begin, 'test-session requires --begin after accepting temporary testing')
            install(trial=True)
        elif args.command == 'record-runtime':
            require(args.evidence is not None, '--evidence required')
            record_runtime(args.evidence)


if __name__ == '__main__':
    try:
        main()
    except (RuntimeError, OSError, ValueError, KeyError, sqlite3.Error, StopIteration) as error:
        print(error_message(error), file=sys.stderr)
        if '--details' in sys.argv:
            print(f'技术详情（原文）：{error}', file=sys.stderr)
        sys.exit(1)
