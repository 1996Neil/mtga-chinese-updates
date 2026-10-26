"""Explicit, immutable community baseline inputs; never a source approval channel."""
import hashlib
import json
import os
from pathlib import Path
import re

from common import fingerprint, read_json, require, safe_format, has_chinese

SCHEMA = 'MTGA_CN_BASELINE_V1'


def context_hash(context):
    payload = {k: context[k] for k in ('english', 'field_use', 'identity')}
    return hashlib.sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False,
                                    separators=(',', ':')).encode()).hexdigest()


def configured(version):
    root = os.environ.get('MTGA_BASELINE_ROOT')
    return Baseline(Path(root), os.environ.get('MTGA_BASELINE_VERSION') or version) if root else None


class Baseline:
    def __init__(self, root, version):
        require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,100}', version)),
                'Invalid baseline version')
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / 'baseline' / ('manifest-' + version + '.json')
        self.manifest = read_json(self.path)
        require(self.manifest.get('schema') == SCHEMA and self.manifest.get('version') == version
                and self.manifest.get('source_kind') == 'community-localization'
                and self.manifest.get('human_acceptance') is False
                and self.manifest.get('runtime_acceptance') is False, 'Invalid baseline manifest')
        require(set(self.manifest['files']) == {'client', 'card', 'bindings'},
                'Incomplete baseline manifest')
        self.inputs = {'manifest': {'path': str(self.path), **fingerprint(self.path)}}
        self.rows = {}
        for layer in ('client', 'card', 'bindings'):
            entry = self.manifest['files'][layer]
            name = entry['name']
            require(isinstance(name, str) and Path(name).name == name and name not in {'.', '..'},
                    'Invalid baseline filename')
            path = self.path.parent / name
            require(fingerprint(path) == entry['fingerprint'], 'Baseline file changed: ' + name)
            self.inputs[layer] = {'path': str(path), **entry['fingerprint']}
            rows = [json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            require(len(rows) == entry['records'], 'Baseline row count mismatch')
            indexed = {}
            for row in rows:
                if layer == 'bindings':
                    require(row['layer'] in {'card', 'client'} and isinstance(row['key'], str)
                            and bool(re.fullmatch(r'[0-9a-f]{64}', row['context_sha256'])),
                            'Invalid baseline binding')
                    key = row['layer'], row['key']
                else:
                    require(isinstance(row['en'], str) and isinstance(row['zh'], str),
                            'Invalid baseline text')
                    key = str(row['locid'] if layer == 'card' else row['key'])
                require(key not in indexed, 'Duplicate baseline key')
                indexed[key] = row
            self.rows[layer] = indexed
        self.source_id = 'community-baseline:' + self.inputs['manifest']['sha256']

    def check(self, layer, key, context):
        row = self.rows.get(layer, {}).get(str(key))
        if row is None:
            return None, 'baseline-missing'
        if row['en'] != context['english']:
            return None, 'baseline-english-changed'
        binding = self.rows['bindings'].get((layer, str(key)))
        if not binding or binding['context_sha256'] != context_hash(context):
            return None, 'baseline-identity-or-use-changed'
        if not context['field_use']:
            return None, 'baseline-unknown-field-use'
        if not has_chinese(row['zh']):
            return None, 'baseline-no-chinese'
        # Preserve imported text byte for byte. Existing syntax repairs remain
        # available to reviewed overrides; baseline adoption never edits wording.
        failures = safe_format(context['english'], row['zh'])
        if failures:
            return None, 'baseline-format-unsafe:' + ','.join(failures)
        return row['zh'], 'baseline-context-and-format-match'

    def guard(self):
        for entry in self.inputs.values():
            require(fingerprint(Path(entry['path'])) ==
                    {k: entry[k] for k in ('sha256', 'bytes')}, 'Baseline inputs changed')


def activate(c, baseline, contexts):
    """Prepare a connection-local selection; persistent review tables stay intact."""
    c.execute('CREATE TEMP TABLE IF NOT EXISTS baseline_selection('
              'layer TEXT,key TEXT,english TEXT,field_use TEXT,translation TEXT,'
              'source TEXT,reason TEXT,PRIMARY KEY(layer,key))')
    c.execute('DELETE FROM baseline_selection')
    if baseline is None:
        return
    for (layer, key), context in contexts.items():
        text, reason = baseline.check(layer, key, context)
        if text is not None:
            c.execute('INSERT INTO baseline_selection VALUES (?,?,?,?,?,?,?)',
                      (layer, str(key), context['english'], json.dumps(context['field_use']),
                       text, baseline.source_id, reason))
    baseline.guard()


def select(c, layer, key, english, use):
    if not c.execute("SELECT 1 FROM sqlite_temp_master WHERE name='baseline_selection'").fetchone():
        return None
    return c.execute('SELECT translation,source,reason FROM baseline_selection '
                     'WHERE layer=? AND key=? AND english=? AND field_use=?',
                     (layer, str(key), english, json.dumps(use))).fetchone()


def guard_inputs(inputs):
    for entry in inputs.values():
        require(fingerprint(Path(entry['path'])) ==
                {k: entry[k] for k in ('sha256', 'bytes')}, 'Candidate baseline inputs changed')
