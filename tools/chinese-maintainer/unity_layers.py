"""Rebuild UI and CJK font sources on current official Unity assets."""
import io
import json
import struct
from collections import Counter
import UnityPy
from fontTools.ttLib import TTCollection, TTFont
from common import (require, digest, fingerprint, safe_format, has_chinese, save_json)
from registry import decide
from tmp_settings import parse_settings
from bundle_crc import build_candidate, load_bundle

require(UnityPy.__version__ == '1.23.0', 'Use the pinned UnityPy runtime')

# Dynamic TMP slots whose baked glyph cache must stay empty. A baked entry keeps
# a glyph index reserved, and runtime allocation then reuses that index, so a
# generated character gets served a stale baked glyph (observed: 计 rendered に).
DYNAMIC_SLOTS = ('Font_Default_DynamicFallback', 'Font_Title_DynamicFallback',
                 'Font_Default_JP_DynamicFallback', 'Font_Title_JP_DynamicFallback',
                 'Font_Default_KR_DynamicFallback', 'Font_Title_KR_DynamicFallback',
                 'Font_Default_RU_DynamicFallback', 'Font_Title_RU_DynamicFallback')


def objects(env):
    result = {o.path_id: o for o in env.objects}
    require(len(result) == len(list(env.objects)), 'Duplicate object IDs across asset files')
    return result


def object_hashes(objs):
    return {key: digest(o.get_raw_data()) for key, o in objs.items()}


def localization(objs):
    rows = [o for o in objs.values() if o.type.name == 'TextAsset' and o.read().m_Name == 'LocLibraryData']
    require(len(rows) == 1, 'Unknown UI localization object layout')
    return rows[0]


def row_map(rows):
    result = {r['Key']: {v['Language']: v['Translation'] for v in r['Translations']} for r in rows}
    require(len(result) == len(rows), 'Duplicate UI keys')
    require(all(len(r['Translations']) == len(result[r['Key']]) for r in rows), 'Duplicate UI language slots')
    return result


def settings_object(objs):
    rows = [o for o in objs.values() if o.type.name == 'MonoBehaviour' and
            o.read_typetree(check_read=False).get('m_Name') == 'TMP Settings']
    require(len(rows) == 1, 'Unknown TMP settings layout')
    return rows[0]


def validate_resources(source, candidate, translations, fallback_refs):
    a, b = UnityPy.load(str(source)), UnityPy.load(str(candidate))
    ao, bo = objects(a), objects(b)
    require(ao.keys() == bo.keys(), 'Resource object set changed')
    al, bl = localization(ao), localization(bo)
    ar, br = json.loads(al.read().m_Script), json.loads(bl.read().m_Script)
    expected = json.loads(al.read().m_Script)
    for row in expected:
        if row['Key'] in translations:
            target = next(t for t in row['Translations'] if t['Language'] == 'en-US')
            require(not safe_format(target['Translation'], translations[row['Key']]), 'Unsafe UI formatting')
            target['Translation'] = translations[row['Key']]
    require(expected == br, 'Non-target UI key/order/language/metadata changed')
    ast, bst = settings_object(ao), settings_object(bo)
    af, _, _ = parse_settings(ast.get_raw_data())
    bf, _, _ = parse_settings(bst.get_raw_data())
    af['m_fallbackFontAssets'] = fallback_refs
    require(af == bf, 'Non-fallback TMP setting changed')
    allowed = {al.path_id, ast.path_id}
    changed = [key for key in ao if ao[key].get_raw_data() != bo[key].get_raw_data()]
    require(set(changed) <= allowed, 'Unrelated resource object changed')
    require([(e.guid, e.type, e.path) for e in al.assets_file.externals] ==
            [(e.guid, e.type, e.path) for e in bl.assets_file.externals], 'Resource external dependencies changed')
    return {'status': 'PASS', 'objects_preserved': len(ao), 'changed_objects': changed,
            'UI_keys_order_other_languages_preserved': True, 'other_TMP_fields_preserved': True}


def select_ui_translations(rows, registry, run):
    """Select current UI text without serializing assets or rebuilding fonts."""
    maps = row_map(rows)
    # The card/client source audit rebinds its own contexts. Rebind exact UI
    # identity here so independently reviewed UI rows cannot inherit stale data.
    registry.execute('CREATE TEMP TABLE IF NOT EXISTS admission_context(layer TEXT,key TEXT,payload TEXT,PRIMARY KEY(layer,key))')
    registry.executemany('INSERT OR REPLACE INTO admission_context VALUES (?,?,?)',
                        [('ui', k, json.dumps({'english': v['en-US'],
                          'field_use': ['LocLibraryData.en-US'], 'identity': {'ui_key': k}}))
                         for k, v in maps.items()])
    decisions, translated = [], {}
    for row in rows:
        key = row['Key']
        require('en-US' in maps[key], 'UI English slot missing')
        english = maps[key]['en-US']
        zh, record = decide(registry, run, 'ui', key, english, ['LocLibraryData.en-US'])
        decisions.append(record)
        if zh != english:
            next(t for t in row['Translations'] if t['Language'] == 'en-US')['Translation'] = zh
            translated[key] = zh
    return translated, decisions


def build_resources(source, bootstrap_source, output, registry, run):
    env = UnityPy.load(str(source))
    objs = objects(env)
    target = localization(objs)
    rows = json.loads(target.read().m_Script)
    translated, decisions = select_ui_translations(rows, registry, run)
    edited = target.read()
    edited.m_Script = json.dumps(rows, ensure_ascii=False, indent=2)
    edited.save()
    settings = settings_object(objs)
    raw = settings.get_raw_data()
    fields, start, end = parse_settings(raw)
    # Resolve file IDs from this version; no fixed old pointer IDs.
    af = settings.assets_file
    indexes = [i + 1 for i, e in enumerate(af.externals) if e.path == 'sharedassets0.assets']
    require(len(indexes) == 1, 'Bootstrap font external unresolved')
    bootstrap = UnityPy.load(str(bootstrap_source))
    fonts = objects(bootstrap)
    refs = list(fields['m_fallbackFontAssets'])
    for name in ('Font_Default_JP_DynamicFallback', 'Font_Title_JP_DynamicFallback'):
        matching = [o for o in fonts.values() if o.type.name == 'MonoBehaviour' and
                    o.read_typetree(check_read=False).get('m_Name') == name]
        require(len(matching) == 1, f'Unknown bootstrap TMP font: {name}')
        ref = {'m_FileID': indexes[0], 'm_PathID': matching[0].path_id}
        if ref not in refs:
            refs.append(ref)
    replacement = raw[:start] + struct.pack('<i', len(refs)) + b''.join(
        struct.pack('<iq', r['m_FileID'], r['m_PathID']) for r in refs) + raw[end:]
    fields2, _, _ = parse_settings(replacement)
    require({k: v for k, v in fields.items() if k != 'm_fallbackFontAssets'} ==
            {k: v for k, v in fields2.items() if k != 'm_fallbackFontAssets'}, 'TMP layout drift')
    settings.set_raw_data(replacement)
    output.write_bytes(af.save())
    validation = validate_resources(source, output, translated, refs)
    save_json(output.parent / 'ui-decisions.json', decisions)
    return {'validation': validation, 'changed_UI_entries': len(translated),
            'counts': dict(Counter(r['status'] for r in decisions)), 'fallback_pointers': refs,
            'translations': translated}


def system_faces():
    from pathlib import Path
    result = {}
    for name, path in [('Light', Path('/System/Library/Fonts/STHeiti Light.ttc')),
                       ('Medium', Path('/System/Library/Fonts/STHeiti Medium.ttc'))]:
        coll = TTCollection(path, lazy=True)
        choices = [font for font in coll.fonts if font['name'].getDebugName(1) == 'Heiti SC']
        require(len(choices) == 1, 'Heiti SC source face changed')
        f = choices[0]
        f.recalcTimestamp = False
        stream = io.BytesIO()
        f.save(stream)
        data = stream.getvalue()
        rt = TTFont(io.BytesIO(data))
        result[name] = {'data': data, 'cmap': set(rt.getBestCmap()), 'source': str(path),
                        'source_fingerprint': fingerprint(path), 'sha256': digest(data)}
        coll.close()
    return result


def validate_font_objects(source, candidate, changes, cleared=None):
    cleared = dict(cleared or {})
    a, b = UnityPy.load(str(source)), UnityPy.load(str(candidate))
    ao, bo = objects(a), objects(b)
    require(ao.keys() == bo.keys(), 'Font object IDs changed')
    actual = {i for i in ao if ao[i].get_raw_data() != bo[i].get_raw_data()}
    # Recognise a dynamic slot losing its baked cache as an intended change here,
    # so build-time and re-validation callers accept the same repair without
    # having to duplicate the slot list. The per-slot check below still rejects
    # any change beyond those two containers.
    for i in sorted(actual - set(changes) - set(cleared)):
        try:
            name = ao[i].read_typetree().get('m_Name')
        except (ValueError, KeyError, TypeError):
            continue
        if name in DYNAMIC_SLOTS:
            cleared[i] = True
    require(actual == set(changes) | set(cleared), 'Unexpected changed font object')
    for i, face in changes.items():
        before, after = ao[i].read_typetree(), bo[i].read_typetree()
        before['m_FontNames'] = ['Heiti SC']
        before['m_FontData'] = list(face['data'])
        require(before == after, 'Font metrics/material/TMP fields changed')
    for i in cleared:
        before, after = ao[i].read_typetree(), bo[i].read_typetree()
        # Only the two baked-glyph containers may differ; every other TMP field
        # (atlas pointers, material, face info, population mode) must survive.
        require(before['m_CharacterTable'] or before['m_GlyphTable'], 'Nothing to clear')
        require(not after['m_CharacterTable'] and not after['m_GlyphTable'], 'TMP glyph cache not cleared')
        expected = dict(before)
        expected['m_CharacterTable'] = []
        expected['m_GlyphTable'] = []
        require(expected == after, 'TMP fields changed beyond cache clearing')
    return {'status': 'PASS', 'changed_source_fonts': sorted(changes),
            'cleared_baked_glyph_slots': sorted(cleared),
            'all_other_objects_preserved': True,
            'allowed_fields': ['m_FontData', 'm_FontNames', 'm_CharacterTable', 'm_GlyphTable']}


def build_fonts(bootstrap, bundle, output_dir, snapshot, chinese_chars):
    faces = system_faces()
    needed = {ord(c) for c in chinese_chars if '\u3400' <= c <= '\u9fff'}
    missing = {style: sorted(needed - face['cmap']) for style, face in faces.items()}
    require(not any(missing.values()), 'System font lacks candidate Chinese glyphs; font review needed')
    env = UnityPy.load(str(bootstrap))
    objs = objects(env)
    # The official native asset carries the engine version omitted by bundles.
    engine_version = next(iter(objs.values())).assets_file.unity_version
    require(isinstance(engine_version, str) and engine_version.startswith('6000.'), 'Unsupported Unity engine layout')
    UnityPy.config.FALLBACK_UNITY_VERSION = engine_version
    changes = {}
    for name, style in [('DFMARUGOTHICSTD-W3', 'Light'), ('MOTOYAAPOROSTD-W6', 'Medium')]:
        matching = [o for o in objs.values() if o.type.name == 'Font' and o.read().m_Name == name]
        require(len(matching) == 1, f'Unknown bootstrap source font: {name}')
        target = matching[0]
        edit = target.read_typetree()
        edit['m_FontNames'] = ['Heiti SC']
        edit['m_FontData'] = list(faces[style]['data'])
        target.save_typetree(edit)
        changes[target.path_id] = faces[style]
    boot_output = output_dir / 'sharedassets0.assets'
    boot_output.write_bytes(next(iter(objs.values())).assets_file.save())
    bootstrap_validation = validate_font_objects(bootstrap, boot_output, changes)
    env, bf = load_bundle(bundle)
    objs = objects(env)
    entry = snapshot['official_entries'][bundle.name]
    require(f'{bf.audit_crc32:08x}' == entry['crc'], 'Official font CRC mismatch')
    changes = {}
    cleared = {}
    for name, style in [('Font_Default_JP_DynamicFallback', 'Light'), ('Font_Title_JP_DynamicFallback', 'Medium')]:
        matching = [o for o in objs.values() if o.type.name == 'MonoBehaviour' and o.read_typetree().get('m_Name') == name]
        require(len(matching) == 1, f'Unknown dynamic font slot: {name}')
        tmp = matching[0].read_typetree()
        require(tmp['m_AtlasPopulationMode'] == 1, 'Dynamic TMP population mode changed')
        # 2026.64.50 bakes seven developer-test glyphs (こんにちは plus U+25A1 and
        # U+00AD) into the default fallback. They must be CLEARED, not kept: a
        # dynamic font allocates glyph indices at runtime, and the baked entries
        # occupy those indices, so a generated character can be served a stale
        # baked glyph. Observed symptom: 计 U+8BA1 rendered as に. Clearing
        # restores a pure dynamic table whose glyphs all come from the Heiti
        # source font installed just below.
        if tmp['m_CharacterTable'] or tmp['m_GlyphTable']:
            tmp['m_CharacterTable'] = []
            tmp['m_GlyphTable'] = []
            matching[0].save_typetree(tmp)
            cleared[matching[0].path_id] = True
        pointer = tmp['m_SourceFontFile']
        require(pointer['m_FileID'] == 0 and pointer['m_PathID'] in objs, 'Dynamic font pointer unresolved')
        target = objs[pointer['m_PathID']]
        require(target.type.name == 'Font', 'Dynamic source not a Font')
        edit = target.read_typetree()
        edit['m_FontNames'] = ['Heiti SC']
        edit['m_FontData'] = list(faces[style]['data'])
        target.save_typetree(edit)
        changes[target.path_id] = faces[style]
    raw_output = output_dir / 'font-source-before-crc.mtga'
    raw_output.write_bytes(bf.save(packer='original'))
    source_validation = validate_font_objects(bundle, raw_output, changes, cleared)
    bundle_output = output_dir / bundle.name
    crc_report, _ = build_candidate(raw_output, bundle_output, int(entry['crc'], 16), reference=bundle, packer='original')
    final_validation = validate_font_objects(bundle, bundle_output, changes, cleared)
    return {'bootstrap_path': str(boot_output), 'bundle_path': str(bundle_output),
            'bootstrap_validation': bootstrap_validation, 'source_validation': source_validation,
            'bundle_validation': final_validation, 'crc_validation': crc_report,
            'glyphs_checked': len(needed), 'missing_glyphs': missing,
            'source_fonts': {style: {k: v for k, v in face.items() if k not in {'data', 'cmap'}} for style, face in faces.items()},
            'runtime_rendering': '未验证', 'official_manifest_modified': False}
