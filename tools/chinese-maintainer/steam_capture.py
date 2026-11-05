"""Capture local Steam-depot-bound official bytes without approving them.

The depot cache is local metadata, not a verified Valve signature. A separate
independent review must bind the resulting capture before official_base uses it.
No account fields, game writes, installation or network access are involved.
"""
import datetime
import hashlib
from pathlib import Path
import plistlib
import re
import struct

from common import STATE, copy_checked, fingerprint, regular, require, save_json
from detect import detect, stable_signature

DEPOT = 2141913
LAYERS = ('card', 'client', 'resources', 'bootstrap_fonts', 'font_bundle')
OPAQUE = ('resources', 'bootstrap_fonts')
CODE_RESOURCES = 'MTGA.app/Contents/_CodeSignature/CodeResources'
MAX_MANIFEST = 32 * 1024 * 1024


def protobuf(data):
    """Bounded protobuf wire reader; no groups or recursively decoded inputs."""
    require(len(data) <= MAX_MANIFEST, 'Steam protobuf exceeds local limit')
    cursor = 0

    def varint():
        nonlocal cursor
        value = 0
        for shift in range(0, 70, 7):
            require(cursor < len(data), 'Truncated Steam protobuf')
            byte = data[cursor]
            cursor += 1
            require(shift < 63 or byte <= 1, 'Steam protobuf varint overflow')
            value |= (byte & 127) << shift
            if byte < 128:
                return value
        raise RuntimeError('Steam protobuf varint overflow')

    fields = []
    while cursor < len(data):
        tag = varint()
        field, wire = tag >> 3, tag & 7
        require(field > 0, 'Invalid Steam protobuf field')
        if wire == 0:
            value = varint()
        else:
            require(wire in (1, 2, 5), 'Unsupported Steam protobuf wire type')
            size = varint() if wire == 2 else (8 if wire == 1 else 4)
            require(size <= len(data) - cursor, 'Truncated Steam protobuf body')
            value = data[cursor:cursor + size]
            cursor += size
        fields.append((field, wire, value))
        require(len(fields) <= 100000, 'Steam protobuf field count exceeds limit')
    return fields


def unique(fields, number, wire):
    values = [(kind, value) for field, kind, value in fields if field == number]
    require(len(values) == 1 and values[0][0] == wire, 'Missing/duplicate Steam manifest field')
    return values[0][1]


def depot_manifest(path, manifest_id):
    path = regular(Path(path).absolute())
    require(path.stat().st_size <= MAX_MANIFEST, 'Steam manifest exceeds local limit')
    data = path.read_bytes()
    sections = {}
    cursor = 0
    for magic in (0x71F617D0, 0x1F4812BE, 0x1B81B817):
        require(cursor + 8 <= len(data), 'Truncated Steam manifest section')
        found, size = struct.unpack_from('<II', data, cursor)
        cursor += 8
        require(found == magic and size <= len(data) - cursor, 'Invalid Steam manifest section')
        sections[magic] = data[cursor:cursor + size]
        cursor += size
    require(data[cursor:] == struct.pack('<I', 0x32C415AB), 'Invalid Steam manifest trailer')
    metadata = protobuf(sections[0x1F4812BE])
    require(unique(metadata, 1, 0) == DEPOT and unique(metadata, 2, 0) == int(manifest_id),
            'Steam depot metadata differs from installed manifest')
    require(unique(metadata, 4, 0) == 0, 'Encrypted Steam filenames are unsupported')
    files = {}
    for field, wire, value in protobuf(sections[0x71F617D0]):
        require(field == 1 and wire == 2, 'Unsupported Steam payload field')
        row = protobuf(value)
        name = unique(row, 1, 2).decode('utf-8').replace('\\', '/')
        rel = Path(name)
        require(name and not rel.is_absolute() and '..' not in rel.parts and
                '.' not in name.split('/') and str(rel) == name, 'Unsafe Steam manifest path')
        require(name not in files, 'Duplicate Steam manifest filename')
        size, sha = unique(row, 2, 0), unique(row, 5, 2)
        require(len(sha) == 20, 'Invalid Steam file SHA1')
        files[name] = {'bytes': size, 'sha1': sha.hex()}
    return files


def steam_install(game):
    acf = regular(Path(game).parent.parent / 'appmanifest_2141910.acf')
    text = acf.read_text()
    require(len(text) <= 2 * 1024 * 1024, 'Steam install metadata exceeds limit')
    tokens = re.findall(r'"([^"]*)"|([{}])', text)
    cursor = 0

    def object_body(depth=0):
        nonlocal cursor
        require(depth < 16, 'Steam install nesting exceeds limit')
        values = {}
        while cursor < len(tokens):
            key, punctuation = tokens[cursor]
            cursor += 1
            if punctuation == '}':
                require(depth > 0, 'Unexpected Steam install closing brace')
                return values
            require(not punctuation and key not in values and cursor < len(tokens),
                    'Invalid/duplicate Steam install key')
            value, punctuation = tokens[cursor]
            cursor += 1
            if punctuation == '{':
                value = object_body(depth + 1)
            else:
                require(not punctuation, 'Invalid Steam install value')
            values[key] = value
        require(depth == 0, 'Truncated Steam install object')
        return values

    outer = object_body()
    require(set(outer) == {'AppState'}, 'Invalid Steam appmanifest root')
    state = outer['AppState']
    require(state.get('appid') == '2141910' and state.get('StateFlags') == '4' and
            state.get('UpdateResult') == '0', 'Steam install is not complete')
    build = state.get('buildid', '')
    require(build.isdecimal() and state.get('TargetBuildID') == build, 'Steam target build differs')
    manifest_id = state.get('InstalledDepots', {}).get(str(DEPOT), {}).get('manifest', '')
    require(isinstance(manifest_id, str) and manifest_id.isdecimal(), 'Missing macOS Steam depot')
    return {'appmanifest': str(acf), 'fingerprint': fingerprint(acf),
            'steam_build': build, 'depot_id': DEPOT, 'manifest_id': manifest_id}


def sha1(path):
    digest = hashlib.sha1()
    with regular(path).open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(block)
    return digest.hexdigest()


def collect(snapshot, *, depotcache=None, inspect=None):
    """Return exact local evidence, never approval; inspect is injected in tests."""
    game = Path(snapshot['game']).absolute()
    inspect = inspect or (lambda: detect(game))
    require(stable_signature(inspect()) == stable_signature(snapshot), 'Game drift before Steam capture')
    steam = steam_install(game)
    require(steam['steam_build'] == snapshot['steam_build'], 'Steam build differs from snapshot')
    depotcache = Path(depotcache) if depotcache else game.parents[2] / 'depotcache'
    manifest = regular(depotcache / f'{DEPOT}_{steam["manifest_id"]}.manifest')
    depot_fp = fingerprint(manifest)
    files = depot_manifest(manifest, steam['manifest_id'])
    checks = {}
    # Bind the same app binary generation as the two opaque resources. Current
    # CDN Raw manifests cannot establish the Steam application binary identity.
    for layer in (*OPAQUE, 'info_plist', 'executable', 'game_assembly'):
        row = snapshot['files'][layer]
        path = regular(game / row['relative'])
        expected = files.get(row['relative'])
        require(expected is not None and path.stat().st_size == expected['bytes'] and
                sha1(path) == expected['sha1'], 'Steam depot file mismatch: ' + layer)
        require(fingerprint(path) == {key: row[key] for key in ('bytes', 'sha256')},
                'Game file changed: ' + layer)
        checks[layer] = {'relative': row['relative'], **expected,
                         'fingerprint': fingerprint(path)}
    seal_path = regular(game / CODE_RESOURCES)
    seal_expected = files.get(CODE_RESOURCES)
    require(seal_expected is not None and seal_path.stat().st_size == seal_expected['bytes'] and
            sha1(seal_path) == seal_expected['sha1'], 'Steam CodeResources mismatch')
    seal_fp = fingerprint(seal_path)
    seal = plistlib.loads(seal_path.read_bytes())
    for layer in OPAQUE:
        relative = snapshot['files'][layer]['relative'].removeprefix('MTGA.app/Contents/')
        expected = seal.get('files2', {}).get(relative, {}).get('hash2')
        require(isinstance(expected, bytes) and expected.hex() ==
                snapshot['files'][layer]['sha256'], 'CodeResources SHA256 mismatch: ' + layer)
    for layer in ('card', 'client', 'font_bundle'):
        row = snapshot['files'][layer]
        expected = snapshot['official_entries'][Path(row['relative']).name]['fingerprint']
        require(fingerprint(game / row['relative']) == expected, 'Current manifest mismatch: ' + layer)
    from capture_base import ui_text, ui_oracle, tmp_fallback_pointers, font_payload
    ui = ui_text(game / snapshot['files']['resources']['relative'])
    oracle = ui_oracle(snapshot, game)
    require(ui and all(key in oracle and oracle[key] == value for key, value in ui.items()),
            'UI differs from manifest-verified client English')
    pointers = tmp_fallback_pointers(game / snapshot['files']['resources']['relative'])
    require(pointers == [], 'Unexpected UI fallback font pointers')
    fonts = font_payload(game / snapshot['files']['bootstrap_fonts']['relative'])
    require(fingerprint(manifest) == depot_fp and fingerprint(seal_path) == seal_fp and
            steam_install(game) == steam and stable_signature(inspect()) == stable_signature(snapshot),
            'Inputs drifted during Steam capture')
    return {'schema': 'MTGA_CN_STEAM_BASE_CAPTURE_V1',
            'at': datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'client_version': snapshot['client_version'], 'steam_build': snapshot['steam_build'],
            'snapshot': snapshot, 'steam': steam, 'depot_manifest': {'path': str(manifest), **depot_fp},
            'depot_files': checks,
            'code_resources': {'relative': CODE_RESOURCES, **seal_expected, 'fingerprint': seal_fp},
            'ui_oracle': {'ui_keys': len(ui), 'oracle_keys': len(oracle),
                          'missing': 0, 'differing': 0, 'tmp_fallback_pointers': pointers},
            'bootstrap_fonts': fonts, 'all_pass': True, 'independent_review': 'PENDING',
            'vendor_authentication': False, 'runtime_acceptance': False,
            'trust_note': 'Local Steam cache SHA1 and matching CodeResources SHA256; '
                          'not a verified Valve/Apple signature. Separate independent review required.'}


def stage(snapshot, destination):
    """Copy newly verified originals only to a fresh managed run, never game paths."""
    destination = Path(destination).absolute()
    require(destination == destination.resolve() and destination.parent == STATE / 'runs' and
            not destination.exists(), 'Steam capture requires a fresh state/runs directory')
    evidence = collect(snapshot)
    destination.mkdir(parents=True, exist_ok=False)
    game = Path(snapshot['game'])
    for layer in LAYERS:
        row = snapshot['files'][layer]
        target = destination / 'official' / row['relative']
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(game / row['relative'], target, {key: row[key] for key in ('bytes', 'sha256')})
    for relative, expected in snapshot['manifests'].items():
        target = destination / 'official' / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(game / relative, target, expected)
    seal = destination / 'official' / CODE_RESOURCES
    seal.parent.mkdir(parents=True, exist_ok=True)
    copy_checked(game / CODE_RESOURCES, seal, evidence['code_resources']['fingerprint'])
    require(stable_signature(detect(game)) == stable_signature(snapshot),
            'Game changed while staging Steam capture')
    save_json(destination / 'snapshot.json', snapshot)
    metadata = destination / 'evidence' / 'depot.manifest'
    metadata.parent.mkdir(parents=True, exist_ok=True)
    copy_checked(Path(evidence['depot_manifest']['path']), metadata,
                 {key: evidence['depot_manifest'][key] for key in ('bytes', 'sha256')})
    evidence['depot_manifest']['staged_path'] = str(metadata)
    save_json(destination / 'base-capture.json', evidence)
    return evidence
