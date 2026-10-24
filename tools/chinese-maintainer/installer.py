"""Journaled, guarded local installation and exact last-transaction rollback."""
from pathlib import Path
import datetime
from common import (STATE, BACKUPS, GAME, read_json, save_json, require, fingerprint,
                    copy_checked, game_stopped, managed_path)
from detect import detect, stable_signature

LAYERS = ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')


def validate_transaction_shape(transaction):
    """Shared fail-closed structural checks; official bytes are NOT needed to roll back."""
    try:
        require(transaction.get('schema') in {'MTGA_CN_TRANSACTION_V1', 'MTGA_CN_TRANSACTION_V2'}, 'Unknown transaction schema')
        require(transaction.get('state') in {'backed-up', 'installing', 'trial', 'installed', 'rolling-back', 'rolled-back'}, 'Unknown transaction state')
        rows = transaction['files']
        require(len(rows) == len(LAYERS) and {r['layer'] for r in rows} == set(LAYERS), 'Transaction layer set invalid')
        for row in rows:
            from official_base import relative_path
            relative_path(row['relative'])
            require(row['relative'] == transaction['snapshot']['files'][row['layer']]['relative'], 'Transaction target mismatch')
            require(row['before'] == {k: transaction['snapshot']['files'][row['layer']][k] for k in ('sha256', 'bytes')}, 'Rollback snapshot mismatch')
            require(Path(row['before_path']).is_absolute(), 'Rollback path must be absolute')
            if transaction['schema'] == 'MTGA_CN_TRANSACTION_V2':
                require(row['rollback_before_path'] == row['before_path'] and row['rollback_before'] == row['before'], 'Rollback aliases disagree')
                base = row['official_base']
                expected_path = Path(transaction['run_dir']) / 'official' / row['relative']
                require(Path(row['official_base_path']) == expected_path and
                        base['official_base_path'] == row['official_base_path'], 'Official base path metadata invalid')
                require(set(base['fingerprint']) == {'sha256', 'bytes'} and
                        isinstance(base['fingerprint']['bytes'], int) and base['fingerprint']['bytes'] >= 0 and
                        __import__('re').fullmatch('[0-9a-f]{64}', base['fingerprint']['sha256']) and
                        isinstance(base['authority'], str) and isinstance(base['evidence'], dict), 'Official base metadata incomplete')
                # Do not test existence of official_base: loss of build input
                # must not prevent recovery from intact rollback_before bytes.
        require(set(transaction['saved_manifests']) == set(transaction['snapshot']['manifests']), 'Transaction manifest set invalid')
    except (KeyError, TypeError, ValueError, AttributeError) as error:
        raise RuntimeError('Malformed transaction: ' + str(error)) from error


def expected_signature(transaction, after=False):
    snapshot = __import__('copy').deepcopy(transaction['snapshot'])
    if after:
        for row in transaction['files']:
            snapshot['files'][row['layer']].update(row['candidate'])
    return stable_signature(snapshot)


def guard_transaction(transaction, allow_partial=False, game=GAME, inspect=detect):
    validate_transaction_shape(transaction)
    current = inspect(game)
    base = __import__('copy').deepcopy(transaction['snapshot'])
    changed = {row['layer']: row for row in transaction['files']}
    for layer, actual in current['files'].items():
        row = changed.get(layer)
        if row:
            fp = {k: actual[k] for k in ('sha256', 'bytes')}
            allowed = [row['before'], row['candidate']] if allow_partial else [row['candidate']]
            require(fp in allowed and actual['relative'] == row['relative'], f'Unknown current {layer}; refuse overwrite')
            base['files'][layer].update(fp)
    require(stable_signature(current) == stable_signature(base), 'Official update/non-target resource changed; refuse rollback/install')
    return current


def restore_transaction(transaction, journal_path, game=GAME, inspect=detect, stopped=game_stopped):
    stopped()
    guard_transaction(transaction, allow_partial=True, game=game, inspect=inspect)
    # Validate every backup before changing any destination.
    for row in transaction['files']:
        require(fingerprint(Path(row['before_path'])) == row['before'], 'Rollback backup corrupted')
    for relative, saved in transaction['saved_manifests'].items():
        require(fingerprint(Path(saved)) == transaction['snapshot']['manifests'][relative], 'Saved manifest corrupted')
    transaction['state'] = 'rolling-back'
    save_json(journal_path, transaction)
    for row in reversed(transaction['files']):
        target = game / row['relative']
        if fingerprint(target) == row['before']:
            continue
        def precommit():
            stopped()
            guard_transaction(transaction, allow_partial=True, game=game, inspect=inspect)
            require(fingerprint(target) == row['candidate'], 'Target changed while staging rollback')
        copy_checked(Path(row['before_path']), target, row['before'], precommit)
        row['restored'] = True
        save_json(journal_path, transaction)
    require(stable_signature(inspect(game)) == expected_signature(transaction), 'Rollback final verification failed')
    transaction['state'] = 'rolled-back'
    transaction['rolled_back_at'] = datetime.datetime.now(datetime.timezone.utc).isoformat()
    save_json(journal_path, transaction)
    return transaction


def create_transaction(report, run_dir, game=GAME, backup_root=BACKUPS, inspect=detect, stopped=game_stopped, state=STATE):
    run_dir = managed_path(run_dir)
    stopped()
    snapshot = read_json(run_dir / 'snapshot.json')
    from official_base import cached_run
    bases = cached_run(run_dir, snapshot, state)
    snapshot_fp = fingerprint(run_dir / 'snapshot.json')
    require(stable_signature(inspect(game)) == stable_signature(snapshot), 'Resources drifted since update')
    stamp = datetime.datetime.now(datetime.timezone.utc).strftime('%Y%m%dT%H%M%S%fZ')
    backup = backup_root / (stamp + '-maintainer-v1')
    backup.mkdir(parents=True, exist_ok=False)
    rows = []
    for layer in LAYERS:
        item = report['candidate_files'][layer]
        require(snapshot['files'][layer]['relative'] == item['relative'], 'Unknown installation target')
        require(not Path(item['relative']).is_absolute() and '..' not in Path(item['relative']).parts, 'Target path escape')
        candidate = managed_path(item['path'])
        require(run_dir.resolve() in candidate.resolve().parents, 'Candidate outside selected run')
        expected = {k: item[k] for k in ('sha256', 'bytes')}
        require(fingerprint(candidate) == expected, 'Candidate changed')
        target = game / item['relative']
        before = {k: snapshot['files'][layer][k] for k in ('sha256', 'bytes')}
        saved = backup / 'before' / item['relative']
        saved.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(target, saved, before)
        rows.append({'layer': layer, 'relative': item['relative'], 'candidate_path': str(candidate),
                     'candidate': expected, 'before_path': str(saved), 'before': before,
                     'rollback_before_path': str(saved), 'rollback_before': before,
                     'official_base_path': bases[layer]['official_base_path'],
                     'official_base': bases[layer]})
    saved_manifests = {}
    for relative, fp in snapshot['manifests'].items():
        saved = backup / 'before' / relative
        saved.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(game / relative, saved, fp)
        saved_manifests[relative] = str(saved)
    require(stable_signature(inspect(game)) == stable_signature(snapshot), 'Game changed during backup')
    require(fingerprint(run_dir / 'snapshot.json') == snapshot_fp, 'Run snapshot drift during backup')
    require(cached_run(run_dir, snapshot, state) == bases, 'Official base drift during backup')
    for row in rows:
        require(fingerprint(Path(row['candidate_path'])) == row['candidate'], 'Candidate drift during backup')
    transaction = {'schema': 'MTGA_CN_TRANSACTION_V2', 'state': 'backed-up', 'run_id': report['run_id'],
                   'run_dir': str(run_dir), 'snapshot': snapshot, 'files': rows,
                   'saved_manifests': saved_manifests, 'backup_dir': str(backup)}
    validate_transaction_shape(transaction)
    save_json(backup / 'manifest.json', transaction)
    return transaction


def apply_transaction(transaction, journal_path, mode, game=GAME, inspect=detect, stopped=game_stopped, after_write=None):
    require(mode in {'trial', 'installed'}, 'Unknown install mode')
    stopped()
    guard_transaction(transaction, allow_partial=True, game=game, inspect=inspect)
    # The durable intent precedes every resource mutation; crash recovery reads it.
    transaction['state'] = 'installing'
    transaction['intended_state'] = mode
    save_json(journal_path, transaction)
    try:
        for row in transaction['files']:
            target = game / row['relative']
            if fingerprint(target) == row['candidate']:
                continue
            def precommit():
                stopped()
                guard_transaction(transaction, allow_partial=True, game=game, inspect=inspect)
                require(fingerprint(target) == row['before'], 'Target drift during staging')
            copy_checked(Path(row['candidate_path']), target, row['candidate'], precommit)
            row['written'] = True
            save_json(journal_path, transaction)
            if after_write:
                after_write(row)
        guard_transaction(transaction, game=game, inspect=inspect)
        transaction['state'] = mode
        save_json(journal_path, transaction)
        return transaction
    except Exception:
        # Do not swallow a recovery failure; the journal remains retryable.
        restore_transaction(transaction, journal_path, game, inspect, stopped)
        raise


def load_transaction():
    path = STATE / 'installation.json'
    return read_json(path) if path.exists() else None


def persist(transaction):
    save_json(STATE / 'installation.json', transaction)
    save_json(Path(transaction['backup_dir']) / 'manifest.json', transaction)
