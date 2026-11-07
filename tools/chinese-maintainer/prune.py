"""Keep a finished candidate's verifiable record and drop its bulk bytes.

A candidate directory holds the official inputs, a comparison copy, both
intermediate card databases, the built assets and the signed packages: roughly
1.6 GB per run, of which only a small record is needed after publication. The
signed release on GitHub is the artifact; this module keeps what explains it.

Nothing is pruned until publication has been verified, and a pruned marker
makes the operation idempotent.
"""
import datetime as dt
import gzip
import json
import shutil
from pathlib import Path

SCHEMA = 'MTGA_CN_RUN_ARCHIVE_V1'
KEEP_FILES = ('report.json', 'snapshot.json', 'REPORT.md', 'official-base.json',
              'independent-review.json', 'comparison-source.json', 'network-acceptance.json',
              'publisher-acceptance.json', 'acceptance-template.json')
COMPRESS = ('card-decisions.json', 'client-decisions.json', 'ui-decisions.json',
            'name-plan.json', 'migration-plan.json', 'diff.json', 'fonts-validation.json',
            'runtime-template.json')
BULK_SUFFIX = ('.mtga', '.assets', '.delta', '.full', '.dll', '.dylib')
BULK_PREFIX = ('package-', 'release-', 'base-', 'scratch', 'preview-')
BULK_DIRS = ('official', 'comparison', 'fixtures', 'source-audit')


def _size(path):
    if path.is_symlink():
        return 0
    if path.is_file():
        return path.stat().st_size
    return sum(_size(p) for p in path.rglob('*'))


def archive(run, *, publication=None):
    """Trim one finished run. Returns the summary written to the run."""
    run = Path(run).resolve()
    marker = run / 'archive.json'
    if marker.exists():
        return json.loads(marker.read_text())
    if publication is not None and not Path(publication).is_file():
        raise ValueError('发布回执不存在，拒绝对未完成发布的候选做归档')
    before = _size(run)
    removed = []
    compressed = {}

    def drop(path):
        nonlocal before
        size = _size(path)
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink()
        removed.append(path.name)
        return size

    freed = 0
    for entry in sorted(run.iterdir()):
        name = entry.name
        if entry.is_dir():
            if name in BULK_DIRS or name.startswith(BULK_PREFIX):
                freed += drop(entry)
            continue
        if name in KEEP_FILES or name == 'archive.json':
            continue
        if name in COMPRESS:
            target = run / (name + '.gz')
            raw = entry.read_bytes()
            with gzip.open(target, 'wb', compresslevel=9) as stream:
                stream.write(raw)
            freed += entry.stat().st_size - target.stat().st_size
            entry.unlink()
            compressed[name] = target.name
            continue
        if name.endswith(BULK_SUFFIX) or name.startswith(BULK_PREFIX):
            freed += drop(entry)
    summary = {'schema': SCHEMA,
               'archived_at': dt.datetime.now(dt.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
               'bytes_before': before, 'bytes_after': _size(run), 'bytes_freed': freed,
               'compressed': compressed, 'removed': sorted(removed)}
    marker.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + '\n')
    return summary


def protected_runs(state):
    """Runs referenced by a registered base approval must never be archived."""
    state = Path(state)
    approvals = state / 'base-approvals'
    result = set()
    if approvals.is_dir():
        for path in approvals.glob('*.json'):
            entry = json.loads(path.read_text())
            run = entry.get('run')
            if isinstance(run, str) and run:
                result.add(Path(run).resolve())
    return result


def prune_registry(state, *, keep_runs):
    """Drop decision rows whose candidate directory no longer exists.

    The registry keeps one decisions row per localization key per run; seven
    runs of 92k rows reach 376 MB for candidates that were already removed.
    """
    import sqlite3
    state = Path(state)
    path = state / 'registry.sqlite'
    if not path.is_file():
        return {'removed_rows': 0, 'bytes_before': 0, 'bytes_after': 0}
    keep = {str(name) for name in keep_runs}
    before = path.stat().st_size
    connection = sqlite3.connect(path)
    try:
        runs = [row[0] for row in connection.execute('SELECT DISTINCT run_id FROM decisions')]
        stale = [run for run in runs if run not in keep]
        for run in stale:
            connection.execute('DELETE FROM decisions WHERE run_id=?', (run,))
        connection.commit()
        removed = connection.total_changes
        connection.execute('VACUUM')
    finally:
        connection.close()
    return {'removed_rows': removed, 'removed_runs': len(stale),
            'bytes_before': before, 'bytes_after': path.stat().st_size}


def archive_state(state, *, keep=2, protected=()):
    """Archive every run except the newest keep and the protected base captures."""
    state = Path(state)
    runs = sorted((p for p in (state / 'runs').iterdir() if p.is_dir()),
                  key=lambda p: p.stat().st_mtime, reverse=True)
    protected = {Path(p).resolve() for p in protected}
    archived = []
    for run in runs[keep:]:
        if run.resolve() in protected or (run / 'archive.json').exists():
            continue
        archived.append((run.name, archive(run)))
    return archived
