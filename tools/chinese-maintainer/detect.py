"""Version/resource discovery and immutable official snapshots."""
import base64
import json
import plistlib
import re
from common import GAME, STATE, database, require, fingerprint, one, read_json, copy_checked, save_json


def detect(game=GAME):
    downloads = game / 'MTGA_Data/Downloads'
    card = one((downloads / 'Raw').glob('Raw_CardDatabase_*.mtga'), 'active CardDatabase')
    client = one((downloads / 'Raw').glob('Raw_ClientLocalization_*.mtga'), 'active ClientLocalization')
    fonts = one((downloads / 'AssetBundle').glob('Fonts_*.mtga'), 'Fonts bundle')
    manifest_paths = sorted(p for p in downloads.glob('Manifest_*.mtga') if 'Audio' not in p.name)
    require(manifest_paths, 'No official manifests')
    assets = {}
    manifests = {}
    for path in manifest_paths:
        data = read_json(path)
        manifests[str(path.relative_to(game))] = fingerprint(path)
        for entry in data.get('Assets', []):
            if entry['Name'] in {card.name, client.name, fonts.name}:
                fp = {'sha256': base64.b64decode(entry['sha256'], validate=True).hex(), 'bytes': entry['Length']}
                require(entry['Name'] not in assets or assets[entry['Name']]['fingerprint'] == fp,
                        f'Conflicting official manifests: {entry["Name"]}')
                assets[entry['Name']] = {'fingerprint': fp, 'manifest': str(path.relative_to(game)), 'crc': entry.get('crc')}
    require(all(p.name in assets for p in (card, client, fonts)), 'Raw/font missing from official manifest')
    paths = {'card': card, 'client': client, 'font_bundle': fonts,
             'resources': game / 'MTGA.app/Contents/Resources/Data/resources.assets',
             'bootstrap_fonts': game / 'MTGA.app/Contents/Resources/Data/sharedassets0.assets',
             'executable': game / 'MTGA.app/Contents/MacOS/MTGA',
             'game_assembly': game / 'MTGA.app/Contents/Frameworks/GameAssembly.dylib',
             'info_plist': game / 'MTGA.app/Contents/Info.plist'}
    for prefix in ('Bucket_Card.FieldFont_', 'Bucket_Card.FontMaterialSettings_'):
        matches = sorted((downloads / 'AssetBundle').glob(prefix + '*.mtga'))
        require(matches, f'Missing relevant font asset: {prefix}')
        for index, path in enumerate(matches):
            paths[prefix + str(index)] = path
    plist = plistlib.loads(paths['info_plist'].read_bytes())
    steam = game.parent.parent / 'appmanifest_2141910.acf'
    text = steam.read_text()
    build = one(re.findall(r'"buildid"\s+"(\d+)"', text), 'Steam build')
    with database(card) as c:
        versions = dict(c.execute('SELECT Type,Version FROM Versions'))
        primary = {r[0] for r in c.execute('SELECT DISTINCT TitleId FROM Cards WHERE IsPrimaryCard=1 AND IsToken=0')}
        f1 = dict(c.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=1'))
        f0 = dict(c.execute('SELECT LocId,Loc FROM Localizations_enUS WHERE Formatted=0'))
        from common import has_chinese
        observed = {'primary_titles': len(primary), 'chinese_primary_F1': sum(has_chinese(f1.get(i)) for i in primary),
                    'primary_F0_present': sum(i in f0 for i in primary),
                    'samples': {str(i): {'F0': f0.get(i), 'F1': f1.get(i)} for i in (1250, 16650)}}
    return {'client_version': plist['CFBundleShortVersionString'], 'steam_build': build, 'data_versions': versions,
            'game': str(game), 'files': {k: {'relative': str(p.relative_to(game)), **fingerprint(p)} for k, p in paths.items()},
            'manifests': manifests, 'official_entries': assets, 'observed': observed}


def stable_signature(snapshot):
    # Runtime observation counters are not part of an installation guard.
    return {k: snapshot[k] for k in ('client_version', 'steam_build', 'data_versions', 'files', 'manifests', 'official_entries')}


def verify_current(snapshot):
    require(stable_signature(detect()) == stable_signature(snapshot), 'MTGA update/resource drift since candidate build')


def snapshot_official(snapshot, run_dir):
    """Resolve every layer before copying; rollback_before is never a base."""
    from official_base import diagnose
    resolution = diagnose(snapshot, GAME, STATE)
    require(resolution['safe_to_rebuild'], 'Official base unresolved: ' + json.dumps(resolution, ensure_ascii=False))
    verify_current(snapshot)
    result = {}
    for row in resolution['layers']:
        destination = run_dir / 'official' / row['relative']
        destination.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(__import__('pathlib').Path(row['official_base_path']), destination, row['fingerprint'])
        result[row['layer']] = destination
    for relative, expected in snapshot['manifests'].items():
        dst = run_dir / 'official' / relative
        dst.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(GAME / relative, dst, expected)
    verify_current(snapshot)
    require(diagnose(snapshot, GAME, STATE) == resolution, 'Official base evidence drift during capture')
    save_json(run_dir / 'snapshot.json', snapshot)
    save_json(run_dir / 'official-base.json', resolution)
    return result


def comparison_official(snapshot, run_dir, historical):
    """Use the preceding distinct official release, with historical first-run fallback."""
    fields = ('client_version', 'steam_build', 'data_versions', 'official_entries')
    current = {key: snapshot[key] for key in fields}
    paths, origin = historical, 'historical-2026.63.10'
    runs = sorted((STATE / 'runs').iterdir(), key=lambda p: p.name.rsplit('_', 1)[-1], reverse=True)
    for previous in runs:
        if previous == run_dir or not (previous / 'report.json').exists():
            continue
        report = read_json(previous / 'report.json')
        if report.get('schema') != 'MTGA_CN_MAINTAINER_V1':
            continue
        before = read_json(previous / 'snapshot.json')
        if {key: before[key] for key in fields} == current:
            continue
        paths = {}
        for layer in ('card', 'client'):
            path = previous / 'official' / before['files'][layer]['relative']
            require(fingerprint(path) == before['official_entries'][path.name]['fingerprint'],
                    'Previous official comparison snapshot corrupted')
            paths[layer] = path
        origin = previous.name
        break
    result = {}
    for layer, path in paths.items():
        target = run_dir / 'comparison' / (layer + '.mtga')
        target.parent.mkdir(parents=True, exist_ok=True)
        copy_checked(path, target, fingerprint(path))
        result[layer] = target
    save_json(run_dir / 'comparison-source.json', {'origin': origin,
              'files': {layer: {'path': str(path), **fingerprint(path)} for layer, path in paths.items()}})
    return result
