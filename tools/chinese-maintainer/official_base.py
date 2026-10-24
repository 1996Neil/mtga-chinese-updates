"""Read-only base resolution. Rollback bytes are never a source of official bytes.

Manifest validation retains the existing local publisher-manifest trust boundary.
Opaque assets use an explicitly pinned, previously reviewed LOCAL capture only;
this is not a publisher signature and does not authorize another release.
"""
from pathlib import Path
from common import ROOT, STATE, GAME, fingerprint, read_json, require, managed_path, evidence_path

LAYERS = ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')
MANIFEST_LAYERS = ('card', 'client', 'font_bundle')
OPAQUE = ('resources', 'bootstrap_fonts')
# Generation 2 (2026.64.50, Steam 25919252), captured 2026-10-21 after the
# official update. Evidence: state/base-captures/20261020T202202.7577/probe.json
# (nine automated criteria) and the same directory's independent review by
# state/base-captures/20261020T201156.9171/independent-review.json.
# Generation 1 was 2026.63.20_20261009T143707774535Z under
# audit-releases/admission-preflight/ + state/pro-handoff-20261010/.
PIN_RUN = '2026.64.50_20261020T202537160084Z'
PIN_REVIEW_PATH = ROOT / 'audit-releases/base-capture-20261021/independent-review.json'
PIN_RECEIPT_PATH = STATE / 'base-handoff-20261021/protected-before-next.json'
PIN_REVIEW = {'sha256': '69e16518c7ffacf3ffaa2500a343b79751aeed508f4b225dd3a25910bf6fa0c5', 'bytes': 15547}
PIN_RECEIPT = {'sha256': '065a3fbed9e76c7837a15974fb6572fb0660194e52b51ef57d40c0237a03af75', 'bytes': 1863}
PIN_SNAPSHOT = {'sha256': 'ae01697addf1920e7b22ee5ce8b2cf7d10f595f2c7caffd6170ff549ffc3db88', 'bytes': 4200}


def fp(record):
    return {k: record[k] for k in ('sha256', 'bytes')}


def relative_path(value):
    path = Path(value)
    require(value and not path.is_absolute() and '..' not in path.parts and str(path) == value,
            'Unsafe resource relative path')
    return path


def release(snapshot):
    """Same release includes Data/GRP, paths, manifests and untouched binaries."""
    return {**{k: snapshot[k] for k in ('client_version', 'steam_build', 'data_versions', 'manifests', 'official_entries')},
            'targets': {k: snapshot['files'][k]['relative'] for k in LAYERS},
            'non_targets': {k: v for k, v in snapshot['files'].items() if k not in LAYERS}}


def opaque_baseline(snapshot, state=STATE):
    state = managed_path(state)
    review_path = PIN_REVIEW_PATH
    receipt_path = PIN_RECEIPT_PATH
    run = state / 'runs' / PIN_RUN
    require(fingerprint(review_path) == PIN_REVIEW, 'Pinned independent review changed')
    review = read_json(review_path)
    require(review['validation_run_evidence']['evidence_files'].get(evidence_path(receipt_path)) == PIN_RECEIPT,
            'Pinned receipt not bound by independent review')
    require(fingerprint(receipt_path) == PIN_RECEIPT, 'Pinned historical receipt changed')
    receipt = read_json(receipt_path)
    snapshot_path = run / 'snapshot.json'
    require(receipt.get(evidence_path(snapshot_path)) == PIN_SNAPSHOT and fingerprint(snapshot_path) == PIN_SNAPSHOT,
            'Pinned historical snapshot changed')
    baseline = read_json(snapshot_path)
    require(release(baseline) == release(snapshot), 'No trusted opaque base for this exact release')
    rows = {}
    for layer in OPAQUE:
        path = run / 'official' / relative_path(baseline['files'][layer]['relative'])
        expected = receipt.get(evidence_path(path))
        require(expected == fp(baseline['files'][layer]), 'Opaque capture receipt mismatch')
        require(fingerprint(path) == expected, 'Pinned opaque original missing/corrupted')
        rows[layer] = {'official_base_path': str(path), 'fingerprint': expected,
                       'authority': 'reviewed-local-capture-not-publisher-signature',
                       'evidence': {'review': PIN_REVIEW, 'receipt': PIN_RECEIPT, 'snapshot': PIN_SNAPSHOT}}
    return rows


def cached_run(run, snapshot, state=STATE):
    """Validate a declared official copy against roots, never report/self hashes.

    No pointer following: exactly one run/official/<relative> per layer. This
    excludes candidate files, before backups, traversal and cyclic provenance.
    """
    run, state = managed_path(run).absolute(), managed_path(state)
    require(run == run.resolve() and run.parent == (state / 'runs').absolute(), 'Official run outside managed runs')
    old = read_json(run / 'snapshot.json')
    require(release(old) == release(snapshot), 'Official run belongs to another release')
    for rel, expected in old['manifests'].items():
        require(fingerprint(run / 'official' / relative_path(rel)) == expected, 'Cached official manifest corrupted')
    pins = opaque_baseline(snapshot, state)
    rows = {}
    for layer in LAYERS:
        rel = relative_path(snapshot['files'][layer]['relative'])
        expected = (snapshot['official_entries'][rel.name]['fingerprint'] if layer in MANIFEST_LAYERS
                    else pins[layer]['fingerprint'])
        path = run / 'official' / rel
        require(fingerprint(path) == expected, 'Official snapshot corrupted: ' + layer)
        rows[layer] = {'official_base_path': str(path), 'fingerprint': expected,
                       'authority': 'current-manifest' if layer in MANIFEST_LAYERS else pins[layer]['authority'],
                       'evidence': {'manifest': snapshot['official_entries'][rel.name]} if layer in MANIFEST_LAYERS else pins[layer]['evidence']}
    return rows


def rollback_info(transaction):
    """Read legacy and V2 without migration; do not consult official_base."""
    if transaction is None:
        return {'status': 'no_transaction', 'files': [], 'valid': True}
    errors = []
    try:
        from installer import validate_transaction_shape
        validate_transaction_shape(transaction)
    except (RuntimeError, TypeError) as error:
        return {'status':'invalid_transaction', 'files':[], 'errors':[str(error)], 'valid':False}
    rows = []
    for row in transaction.get('files', []):
        path = row.get('rollback_before_path', row.get('before_path'))
        item = {'layer': row.get('layer'), 'rollback_before_path': path, 'fingerprint': row.get('before')}
        try:
            require(path == row.get('before_path'), 'Rollback path alias mismatch')
            require(row.get('rollback_before', row.get('before')) == row.get('before'), 'Rollback hash alias mismatch')
            require(fingerprint(Path(path)) == row['before'], 'Rollback backup missing/corrupted')
            item['valid'] = True
        except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
            item.update(valid=False, error=str(error))
        rows.append(item)
    errors = []
    if len(rows) != len(LAYERS) or {r['layer'] for r in rows} != set(LAYERS):
        errors.append('Rollback layer set incomplete/duplicate')
    try:
        require(set(transaction['saved_manifests']) == set(transaction['snapshot']['manifests']), 'Rollback manifest set mismatch')
        for rel, path in transaction['saved_manifests'].items():
            require(fingerprint(Path(path)) == transaction['snapshot']['manifests'][rel], 'Rollback manifest missing/corrupted')
    except (OSError, RuntimeError, KeyError, TypeError, ValueError) as error:
        errors.append(str(error))
    return {'status': ('explicit_separate_bases' if transaction.get('schema') == 'MTGA_CN_TRANSACTION_V2'
                       else 'legacy_transaction_without_official_base'),
            'files': rows, 'errors': errors, 'valid': bool(rows) and all(r['valid'] for r in rows) and not errors}


def diagnose(snapshot, game=GAME, state=STATE):
    """No writes, no SQLite writer, no network, no recursive backup search."""
    state = managed_path(state)
    transaction_path = state / 'installation.json'
    transaction = read_json(transaction_path) if transaction_path.exists() else None
    inputs = {str(transaction_path): fingerprint(transaction_path)} if transaction else {}
    errors = []
    rollback = rollback_info(transaction)
    if transaction and transaction.get('state') in {'installing', 'rolling-back', 'backed-up'}:
        errors.append('Unfinished installation transaction')
    if not rollback['valid']:
        errors.append('Rollback backup verification failed')
    try:
        pins = opaque_baseline(snapshot, state)
    except (OSError, RuntimeError, KeyError, ValueError) as error:
        pins = {}; errors.append(str(error))
    # Only the active transaction's run plus the fixed audited capture. No scan,
    # latest pointer, report promotion, before_path or official_base path chasing.
    runs = []
    if transaction and transaction.get('run_dir'):
        runs.append(managed_path(transaction['run_dir']))
    pinned_run = state / 'runs' / PIN_RUN
    if pinned_run not in runs:
        runs.append(pinned_run)
    caches, rejected = [], []
    for run in runs:
        if not run.exists():
            continue
        try:
            rows = cached_run(run, snapshot, state)
            inputs[str(run/'snapshot.json')] = fingerprint(run/'snapshot.json')
            caches.append(rows)
        except (OSError, RuntimeError, KeyError, ValueError) as error:
            rejected.append({'run': str(run), 'reason': str(error)})
    result = []
    for layer in LAYERS:
        record = snapshot['files'][layer]
        item = {'layer': layer, 'relative': record['relative'], 'current': fp(record), 'status': 'unknown'}
        try:
            rel = relative_path(record['relative']); current = game / rel
            require(fingerprint(current) == fp(record), 'Current resource drift: ' + layer)
            expected = (snapshot['official_entries'][rel.name]['fingerprint'] if layer in MANIFEST_LAYERS
                        else pins[layer]['fingerprint'])
            item['manifest_hash'] = expected if layer in MANIFEST_LAYERS else None
            item['expected_official'] = expected
            source = None
            if fp(record) == expected:
                source = {'official_base_path': str(current), 'fingerprint': expected,
                          'authority': 'current-manifest' if layer in MANIFEST_LAYERS else pins[layer]['authority'],
                          'evidence': {'manifest': snapshot['official_entries'][rel.name]} if layer in MANIFEST_LAYERS else pins[layer]['evidence']}
            else:
                require(transaction is not None and transaction.get('state') in {'installed', 'trial', 'rolled-back'},
                        'Unknown current patched resource: ' + layer)
                require(release(transaction['snapshot']) == release(snapshot), 'Update retained old patch or release drift: ' + layer)
                old = [r for r in transaction['files'] if r['layer'] == layer and r['relative'] == str(rel)]
                expected_current = 'before' if transaction['state'] == 'rolled-back' else 'candidate'
                require(len(old) == 1 and old[0][expected_current] == fp(record), 'Unrecognized current patch: ' + layer)
                source = next((c[layer] for c in caches if c[layer]['fingerprint'] == expected), None)
                require(source is not None, 'No independently verified official copy: ' + layer)
            item.update(source, status='verified')
        except (OSError, RuntimeError, KeyError, ValueError) as error:
            item['reason'] = str(error)
        result.append(item)
    for rel, expected in snapshot['manifests'].items():
        try:
            require(fingerprint(game / relative_path(rel)) == expected, 'Current manifest changed')
        except (OSError, RuntimeError, KeyError, ValueError) as error:
            errors.append(str(error))
    for path, expected in inputs.items():
        require(fingerprint(Path(path)) == expected, 'Base diagnostic input drift')
    return {'schema': 'MTGA_CN_BASE_DIAGNOSTIC_V1', 'release': release(snapshot), 'layers': result,
            'rollback': rollback, 'errors': errors, 'rejected_snapshots': rejected, 'inputs': inputs,
            'safe_to_rebuild': not errors and all(r['status'] == 'verified' for r in result),
            'writes': 0, 'vendor_authentication': False,
            'trust_note': 'Local current manifests plus explicitly pinned reviewed local capture; no publisher signature claimed.'}
