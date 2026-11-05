"""Context-bound community text from a pinned Git dataset; never legacy approval."""
import json
import os
from pathlib import Path
import re
import subprocess

from baseline import context_hash
from common import ROOT, fingerprint, has_chinese, read_json, require, safe_format, save_json, copy_checked

SCHEMA = 'MTGA_CN_PORTABLE_BASELINE_V1'
IDENTITY_FIELDS = ('TitleId', 'ExpansionCode', 'CollectorNumber', 'IsToken',
                   'IsPrimaryCard', 'LinkedFaceType', 'LinkedFaceGrpIds')
_CACHE = {}


def online():
    return os.environ.get('MTGA_ONLINE') == '1'


def configured():
    require(online(), 'Portable baseline requires explicit online mode')
    root = os.environ.get('MTGA_PORTABLE_ROOT')
    require(root, 'Online mode requires a pinned portable text dataset')
    root = str(Path(root).expanduser().resolve())
    expected = os.environ.get('MTGA_PORTABLE_MANIFEST_SHA256')
    require(isinstance(expected, str) and bool(re.fullmatch(r'[0-9a-f]{64}', expected)),
            'Online portable manifest must be pinned by the Git source lock')
    cache_key = root, expected
    if cache_key not in _CACHE:
        _CACHE[cache_key] = PortableBaseline(Path(root), expected)
    _CACHE[cache_key].guard()
    return _CACHE[cache_key]


def verify_source(path, manifest):
    """Verify the original release envelope with the unchanged production key."""
    client = os.environ.get('MTGA_PUBLISHER_CLIENT')
    public = os.environ.get('MTGA_PUBLIC_KEY_FILE')
    require(client and public, 'Online text import requires native client and fixed public key file')
    expected = read_json(ROOT.parent / 'subscriber/profile.json')['public_key']
    require(Path(public).read_text().strip() == expected == manifest['public_key'],
            'Portable dataset public key differs from the existing publisher key')
    result = subprocess.run([client, 'verify-envelope', '--input', str(path), '--key', public],
                            capture_output=True, text=True)
    require(result.returncode == 0, 'Portable source release signature verification failed')
    import base64
    release = json.loads(base64.b64decode(read_json(path)['payload'], validate=True))
    require(release['id'] == manifest['source_release_id'] and
            release['client_version'] == manifest['version'] and
            release['steam_build'] == manifest['steam_build'] and
            release['acceptance']['status'] == 'accepted',
            'Portable dataset is not bound to its accepted signed source release')
    return release


class PortableBaseline:
    def __init__(self, root, expected_manifest_sha256):
        self.root = Path(root).expanduser().resolve()
        self.path = self.root / 'manifest.json'
        require(fingerprint(self.path)['sha256'] == expected_manifest_sha256,
                'Portable manifest differs from the pinned Git source lock')
        self.manifest = read_json(self.path)
        manifest = self.manifest
        require(manifest.get('schema') == SCHEMA and
                manifest.get('source_kind') == 'community-localization' and
                manifest.get('human_acceptance') is False and
                manifest.get('runtime_acceptance') is False and
                {'card', 'client', 'ui', 'titles'} <= set(manifest['files']) <=
                {'card', 'client', 'ui', 'titles', 'prior_sources'},
                'Invalid portable community baseline manifest')
        self.inputs = {'portable_manifest': {'path': str(self.path), **fingerprint(self.path)}}
        self.rows = {}

        def entry(name, record):
            require(isinstance(record['name'], str) and Path(record['name']).name == record['name'] and
                    record['name'] not in {'.', '..'}, 'Unsafe portable data filename')
            path = self.root / record['name']
            require(fingerprint(path) == record['fingerprint'], 'Portable data hash mismatch: ' + name)
            self.inputs[name] = {'path': str(path), **record['fingerprint']}
            return path

        source = entry('portable_source_manifest', manifest['source_manifest'])
        release = verify_source(source, manifest)
        outputs = {row['layer']: row['output']['sha256'] for row in release['resources']}
        require(len(release['resources']) == 5 and
                set(outputs) == {'card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle'},
                'Portable signed source release has an incomplete resource set')
        signed_outputs = {row['layer']: row['output'] for row in release['resources']}
        signed_raw = {row['layer']: row['input'] for row in release['resources']
                      if row['layer'] in {'card', 'client'}}
        require(manifest['source_output_fingerprints'] == signed_outputs and
                manifest['source_official_raw_fingerprints'] == signed_raw,
                'Portable declared inputs/outputs differ from signed release')
        for layer, item in manifest['files'].items():
            path = entry('portable_' + layer, item)
            if layer == 'prior_sources':
                continue  # Informational source links never grant adoption or review.
            records = [json.loads(line) for line in path.read_text().splitlines()]
            require(len(records) == item['records'], 'Portable record count mismatch: ' + layer)
            if layer == 'titles':
                self.titles = records
                continue
            rows = {}
            for row in records:
                key = row['key']
                require(row['layer'] == layer and isinstance(key, str) and key not in rows and
                        isinstance(row['english'], str) and isinstance(row['chinese'], str) and
                        isinstance(row['field_use'], list) and isinstance(row['identity'], dict) and
                        row.get('human_acceptance') is False and row.get('runtime_acceptance') is False,
                        'Invalid or duplicate portable text context')
                from common import digest
                require(row['english_sha256'] == digest(row['english'].encode()) and
                        row['chinese_sha256'] == digest(row['chinese'].encode()) and
                        row['context_sha256'] == context_hash(row),
                        'Portable text/context hash mismatch')
                origin = row['source']
                resource = 'resources' if layer == 'ui' else layer
                require(origin['kind'] == 'community-localization' and
                        origin['release_id'] == release['id'] and origin['resource_layer'] == resource and
                        origin['output_sha256'] == outputs[resource],
                        'Portable row not bound to its signed source output')
                require(row['disposition'] in {'translated', 'keep-english', 'empty-source-preserved'},
                        'Invalid portable disposition')
                if layer == 'ui':
                    require(row['field_use'] == ['LocLibraryData.en-US'] and
                            row['identity'] == {'ui_key': key}, 'Invalid portable UI identity')
                rows[key] = row
            self.rows[layer] = rows
        self.title_rows = {}
        for row in getattr(self, 'titles', []):
            self.title_rows.setdefault(int(row['title_id']), row)
        self.text_index = {}
        for row in self.rows.get('card', {}).values():
            if row['disposition'] != 'translated' or not has_chinese(row['chinese']):
                continue
            signature = ('card', row['english'], json.dumps(row['field_use'], sort_keys=True))
            self.text_index.setdefault(signature, set()).add(row['chinese'])
        self.source_id = 'portable-community:' + self.inputs['portable_manifest']['sha256']
        self.guard()

    def fallback(self, layer, context):
        """Recover a translation when only the LocId moved between builds.

        Two narrow matches only: the same card title by identity, or the exact
        same English text in the exact same field use with one agreed value.
        Every other case keeps English rather than guessing where it renders.
        """
        english, use = context.get('english'), context.get('field_use') or []
        if not use or not isinstance(english, str) or not english.strip():
            return None, 'portable-unknown-field-use'
        if layer == 'card':
            cards = (context.get('identity') or {}).get('cards') or []
            titles = {int(card['TitleId']) for card in cards if card.get('TitleId')}
            printings = {int(card['GrpId']) for card in cards if card.get('GrpId')}
            if len(titles) == 1:
                row = self.title_rows.get(next(iter(titles)))
                if (row is not None and row['english'] == english and
                        int(row['grp_id']) in printings and has_chinese(row['chinese']) and
                        not safe_format(english, row['chinese'])):
                    return row['chinese'], 'portable-title-identity-match'
        values = self.text_index.get((layer, english, json.dumps(use, sort_keys=True)))
        if layer == 'card' and values and len(values) == 1:
            chinese = next(iter(values))
            if not safe_format(english, chinese):
                return chinese, 'portable-text-context-match'
        return None, 'portable-new-context'

    def check(self, layer, key, context):
        row = self.rows.get(layer, {}).get(str(key))
        if row is None:
            return self.fallback(layer, context)
        if row['english'] != context['english']:
            return None, 'portable-english-changed'
        if row['context_sha256'] != context_hash(context):
            return None, 'portable-identity-or-use-changed'
        if not context['field_use']:
            return None, 'portable-unknown-field-use'
        failures = safe_format(context['english'], row['chinese'])
        if failures:
            return None, 'portable-format-unsafe:' + ','.join(failures)
        if row['disposition'] != 'translated':
            if row['chinese'] == context['english']:
                return context['english'], 'portable-existing-English-retained'
            return None, 'portable-preserved-source-mismatch'
        if not has_chinese(row['chinese']):
            return None, 'portable-no-chinese'
        return row['chinese'], 'portable-community-context-and-format-match'

    def guard(self):
        for item in self.inputs.values():
            require(fingerprint(Path(item['path'])) == {k: item[k] for k in ('sha256', 'bytes')},
                    'Portable baseline changed during maintenance')

    def register(self, registry):
        """Register immutable source files only, never verified localization rows."""
        from registry import source
        marker = registry.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='portable_imports'").fetchone()
        require(marker or (registry.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0 and
                           registry.execute('SELECT COUNT(*) FROM localization').fetchone()[0] == 0),
                'Online mode requires a new isolated Registry; legacy sources are preserved')
        registry.execute('CREATE TABLE IF NOT EXISTS portable_imports('
                         'manifest_sha256 TEXT PRIMARY KEY, source_release_id TEXT NOT NULL)')
        count = registry.execute('SELECT COUNT(*) FROM portable_imports').fetchone()[0]
        require(count or registry.execute('SELECT COUNT(*) FROM sources').fetchone()[0] == 0,
                'Online mode requires a new isolated Registry; legacy sources are preserved')
        for name, item in self.inputs.items():
            ident = self.source_id if name == 'portable_manifest' else self.source_id + ':' + name
            source(registry, ident, Path(item['path']), self.manifest['version'],
                   {'kind': 'community-localization', 'source_release_id': self.manifest['source_release_id'],
                    'human_acceptance': False, 'runtime_acceptance': False},
                   {key: item[key] for key in ('sha256', 'bytes')})
        registry.execute('INSERT OR IGNORE INTO portable_imports VALUES (?,?)',
                         (self.inputs['portable_manifest']['sha256'], self.manifest['source_release_id']))
        registry.commit()

    def diff(self, current):
        result = {}
        for layer in ('card', 'client', 'ui'):
            before = self.rows[layer]
            after = {key: value for (kind, key), value in current.items() if kind == layer}
            shared = before.keys() & after.keys()
            result[layer] = {
                'added_keys': sorted(after.keys() - before.keys()),
                'removed_keys': sorted(before.keys() - after.keys()),
                'changed_english': [{'key': key, 'before': before[key]['english'],
                                     'after': after[key]['english']} for key in sorted(shared)
                                    if before[key]['english'] != after[key]['english']],
                'identity_or_use_changed': [key for key in sorted(shared)
                                           if before[key]['field_use'] != after[key]['field_use'] or
                                           before[key]['identity'] != after[key]['identity']],
                'unchanged_english': sum(before[key]['english'] == after[key]['english'] for key in shared),
            }
        return result

    def name_history(self):
        cards = {}
        for row in self.rows['card'].values():
            for card in row['identity'].get('cards', []):
                ident = int(card['GrpId'])
                values = tuple(card[key] for key in IDENTITY_FIELDS)
                require(ident not in cards or cards[ident] == values, 'Conflicting portable card identities')
                cards[ident] = values
        names = {}
        for row in self.titles:
            title = int(row['title_id'])
            english = row.get('canonical_english', row['english'])
            require(english and not has_chinese(english) and
                    not any(char in english for char in '<>\r\n\x00'), 'Portable canonical name not plain English')
            require(title not in names or names[title] == english, 'Portable canonical title collision')
            names[title] = english
        return {'cards': cards, 'titles': names}


def current_contexts(official):
    from provenance import contexts
    from capture_base import ui_text
    result = contexts(official)
    for key, english in ui_text(official['resources']).items():
        result['ui', key] = {'english': english, 'field_use': ['LocLibraryData.en-US'],
                             'identity': {'ui_key': key}}
    return result


def comparison_inputs(official, run, baseline):
    """Keep verifier file guards explicit; historical diff comes from portable text."""
    result = {}
    for layer in ('card', 'client'):
        target = run / 'comparison' / (layer + '.mtga')
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(official[layer], target, fingerprint(official[layer]))
        result[layer] = target
    save_json(run / 'comparison-source.json', {
        'origin': baseline.manifest['source_release_id'], 'comparison_kind': 'portable-text-contexts',
        'scope': 'Historical English/identity comparison uses portable JSONL, not these current-input copies.',
        'files': {layer: {'path': str(path), **fingerprint(path)} for layer, path in result.items()},
        'portable_inputs': baseline.inputs,
    })
    return result
