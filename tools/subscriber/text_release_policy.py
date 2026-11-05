"""Owner-authorized text-only stable release checks; never fabricate runtime PASS."""
import base64
import hashlib
import json
from pathlib import Path
import subprocess

from publisher import LAYERS, canonical, fingerprint

SCHEMA = 'MTGA_CN_AUTOMATIC_TEXT_RELEASE_V1'
POLICY_PATH = Path(__file__).resolve().parents[2] / 'release-policy.json'
SCOPE = ('按维护者授权自动批准同游戏版本、同 Steam build 的常规文本修订；'
         '官方输入、资源路径、受保护文件及字体输出不变，静态完整性与独立工程审查通过。'
         '本次未执行新的真实游戏验收，不代表人工审校或游戏测试通过。')
POLICY_FLAGS = {
    'enabled': True, 'runtime_acceptance_required': False,
    'requires_same_client_version': True, 'requires_same_steam_build': True,
    'requires_unchanged_fonts': True, 'requires_independent_review': True,
}


def need(condition, message):
    if not condition:
        raise ValueError(message)


def read_policy(path=None):
    path = Path(path or POLICY_PATH).resolve()
    value = json.loads(path.read_text())
    need(value.get('schema') == 'MTGA_CN_RELEASE_POLICY_V1' and
         value.get('routine_text_updates') == POLICY_FLAGS and
         isinstance(value.get('owner_authorization'), str) and
         bool(value['owner_authorization'].strip()), '常规文本自动发布政策未明确启用或范围变化')
    return value, {'path': str(path), **fingerprint(path)}


def native_baseline(path, client, public_file):
    path, public_file = Path(path).resolve(), Path(public_file).resolve()
    need(path.is_file() and not path.is_symlink() and public_file.is_file(),
         '缺少已有 stable 签名基线或公钥文件')
    known = json.loads((Path(__file__).parent / 'profile.json').read_text())['public_key']
    need(public_file.read_text().strip() == known, '自动文本发布不能切换既有公钥')
    result = subprocess.run([str(client), 'verify-envelope', '--input', str(path),
                             '--key', str(public_file)], capture_output=True, text=True)
    need(result.returncode == 0, '自动文本发布的历史基线签名无效')
    envelope = json.loads(path.read_text())
    release = json.loads(base64.b64decode(envelope['payload'], validate=True))
    need(release.get('schema') == 'MTGA_CN_RELEASE_V1' and
         release.get('acceptance', {}).get('status') == 'accepted',
         '自动文本发布只允许复用已有 accepted stable 基线')
    return release


def evaluate(subject, report, review, baseline, policy):
    """Return every structural blocker; no caller booleans can override them."""
    blockers = []
    if policy.get('routine_text_updates') != POLICY_FLAGS:
        blockers.append('routine-text-policy-not-enabled')
    for field in ('client_version', 'steam_build', 'platform'):
        if baseline.get(field) != subject.get(field):
            blockers.append('baseline-' + field + '-mismatch')
    if (report.get('localization', {}).get('static_status') != 'PASS' or
            report.get('import_compatibility', {}).get('static_status') != 'PASS'):
        blockers.append('candidate-static-validation-not-pass')
    if report.get('static_safe_to_trial') is not True:
        blockers.append('candidate-not-statically-safe')
    if report.get('high_risk_unknown') != []:
        blockers.append('candidate-high-risk-unknown')
    if (review.get('status') != 'PASS' or
            review.get('candidate_files') != report.get('candidate_files') or
            review.get('code_fingerprints') != report.get('code_fingerprints')):
        blockers.append('independent-engineering-review-mismatch')
    names = report.get('import_compatibility', {}).get('names', {})
    validation = names.get('validation', {})
    canonical_check = validation.get('canonical_binding_validation', {})
    if (validation.get('status') != 'PASS' or canonical_check.get('status') != 'PASS' or
            canonical_check.get('all_primary_non_token_F0_bound_to_official_titles') is not True or
            canonical_check.get('all_display_F1_preserved') is not True or
            canonical_check.get('token_only_secondary_only_titles_not_added') is not True or
            canonical_check.get('canonical_exact_cross_title_collisions') != 0):
        blockers.append('name-structure-validation-incomplete')
    # New-group counts refer to the portable text dataset, which can be older
    # than the signed stable baseline. Actual baseline identity is established
    # below by the exact official card input and full structural validator.
    if not isinstance(names.get('new_card_groups'), list):
        blockers.append('historical-new-card-count-unknown')
    for field in ('changed_card_identities', 'changed_canonical_names',
                  'canonical_conflicts', 'uncovered'):
        if field not in names or names[field]:
            blockers.append('name-import-structure-changed-or-unknown:' + field)
    resources = baseline.get('resources', [])
    if len(resources) != 5 or {r.get('layer') for r in resources} != set(LAYERS):
        blockers.append('baseline-layer-set-invalid')
    else:
        for row in resources:
            layer = row['layer']
            current = subject.get('input_files', {}).get(layer, {})
            output = subject.get('output_files', {}).get(layer, {})
            if (row.get('relative') != current.get('relative') or
                    row.get('relative') != output.get('relative') or
                    row.get('input') != {k: current.get(k) for k in ('sha256', 'bytes')}):
                blockers.append('official-input-or-resource-path-changed:' + layer)
            if layer in {'bootstrap_fonts', 'font_bundle'} and (
                    row.get('output') != {k: output.get(k) for k in ('sha256', 'bytes')}):
                blockers.append('font-output-changed:' + layer)
    protected = [{'relative': row['relative'],
                  'fingerprint': {key: row[key] for key in ('sha256', 'bytes')}}
                 for row in subject.get('protected_files', {}).values()]
    protected += [{'relative': relative, 'fingerprint': fp}
                  for relative, fp in subject.get('manifests', {}).items()]
    if baseline.get('protected_files') != protected:
        blockers.append('protected-file-or-manifest-changed')
    localization = report.get('localization', {})
    for layer in ('card', 'client'):
        check = localization.get(layer, {}).get('validation', {})
        tables = check.get('all_tables_expected_rows', {})
        if (check.get('status') != 'PASS' or check.get('integrity') != 'ok' or
                check.get('schema_and_indexes_preserved') is not True or not tables or
                any(value is not True for value in tables.values())):
            blockers.append('raw-preservation-validation-incomplete:' + layer)
    resources_check = localization.get('resources', {}).get('validation', {})
    if (resources_check.get('status') != 'PASS' or
            resources_check.get('UI_keys_order_other_languages_preserved') is not True or
            resources_check.get('other_TMP_fields_preserved') is not True):
        blockers.append('UI-preservation-validation-incomplete')
    fonts = localization.get('fonts', {})
    if (fonts.get('status') != 'PASS' or fonts.get('official_manifest_modified') is not False or
            set(fonts.get('missing_glyphs', {})) != {'Light', 'Medium'} or
            any(fonts['missing_glyphs'].values()) or any(
                fonts.get(kind, {}).get('status') != 'PASS' or
                fonts.get(kind, {}).get('all_other_objects_preserved') is not True
                for kind in ('bootstrap_validation', 'bundle_validation'))):
        blockers.append('font-validation-incomplete')
    fixtures = report.get('import_compatibility', {}).get('fixtures', {})
    expected_fixtures = {'english-60', 'chinese-60', 'mixed-60', 'qualified-60', 'special-english-60'}
    historical_new = fixtures.get('new_importable_cards')
    if (fixtures.get('status') != 'PASS' or type(historical_new) is not int or historical_new < 0 or
            set(fixtures.get('fixtures', {})) != expected_fixtures or any(
                row.get('static_status') != 'PASS' or row.get('cards') != 60 or row.get('errors') != []
                for row in fixtures.get('fixtures', {}).values())):
        blockers.append('import-fixture-validation-incomplete')
    return blockers


def _binding(subject, report, review, baseline_manifest, policy_record):
    return {
        'subject_sha256': hashlib.sha256(canonical(subject)).hexdigest(),
        'report_sha256': hashlib.sha256(canonical(report)).hexdigest(),
        'independent_review_sha256': hashlib.sha256(canonical(review)).hexdigest(),
        'baseline_manifest': {'path': str(Path(baseline_manifest).resolve()),
                              **fingerprint(baseline_manifest)},
        'policy': policy_record,
        'policy_code': fingerprint(Path(__file__)),
    }


def create(subject, report, review, baseline_manifest, client, public_key_file, policy_path=None):
    policy, policy_record = read_policy(policy_path)
    baseline = native_baseline(baseline_manifest, client, public_key_file)
    blockers = evaluate(subject, report, review, baseline, policy)
    need(not blockers, '常规文本自动发布不适用：' + '; '.join(blockers))
    arches = baseline.get('architectures')
    need(isinstance(arches, list) and arches and set(arches) <= {'arm64', 'x86_64'},
         '基线架构范围无效')
    return {
        'schema': SCHEMA, **_binding(subject, report, review, baseline_manifest, policy_record),
        'architectures': arches, 'scope': SCOPE,
        'decision': 'owner-authorized-routine-text-release',
        'runtime_acceptance': False, 'human_acceptance': False,
        'runtime_validation': 'NOT_RUN', 'human_validation': 'NOT_RUN',
        'machine_checks': {
            'same_game_and_steam_build': True, 'official_inputs_and_paths_unchanged': True,
            'protected_files_and_manifests_unchanged': True, 'font_outputs_unchanged': True,
            'official_card_identity_and_import_structure_unchanged_against_stable': True,
            'static_integrity_and_independent_engineering_review': True,
        },
    }


def validate(evidence, subject, report, review, client, public_key, policy_path=None):
    """Recompute the decision and native-verification result before signing."""
    need(evidence.get('schema') == SCHEMA and evidence.get('runtime_acceptance') is False and
         evidence.get('human_acceptance') is False and evidence.get('runtime_validation') == 'NOT_RUN',
         '自动文本发布证明不可声称人工或运行时通过')
    baseline = evidence.get('baseline_manifest', {})
    need(isinstance(baseline.get('path'), str), '自动文本发布缺少基线清单路径')
    known = json.loads((Path(__file__).parent / 'profile.json').read_text())['public_key']
    need(public_key == known, '自动文本发布不能切换既有公钥')
    # The public key is already explicit to publisher.package; materialize it only
    # for the native verifier, never read or duplicate a private key.
    import tempfile
    with tempfile.TemporaryDirectory(prefix='mtga-text-policy-') as directory:
        public_file = Path(directory) / 'public.txt'
        public_file.write_text(public_key)
        actual = create(subject, report, review, baseline['path'], client, public_file, policy_path)
    need(evidence == actual, '自动文本发布证明已过期或不属于当前精确输入')
    return actual['architectures']
