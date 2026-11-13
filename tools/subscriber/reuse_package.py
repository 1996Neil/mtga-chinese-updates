"""Republish an accepted frozen package without rebuilding or rewriting evidence.

Only bytes covered by a valid stable signature can enter this path. A fresh
independent engineering review binds the immutable inputs and republisher code.
"""
import copy
import datetime as dt
import json
from pathlib import Path
import shutil
import time

from github_publish import publication_plan
from publisher import (LAYERS, check, dump, fingerprint, sign, subject_for,
                       validate_acceptance, verify_delta)


def need(condition, message):
    if not condition:
        raise ValueError(message)


def regular(path, parent=None):
    path = Path(path)
    need(path.is_file() and not path.is_symlink() and path.absolute() == path.resolve(),
         'Reuse input is not a regular canonical path: ' + str(path))
    if parent is not None:
        need(path.is_relative_to(parent), 'Reuse input outside candidate/package')
    return path


def code_binding():
    root = Path(__file__).resolve().parents[2]
    names = ('tools/chinese-maintainer/release_workflow.py',
             'tools/subscriber/reuse_package.py', 'tools/subscriber/publisher.py',
             'tools/subscriber/github_publish.py', 'tools/subscriber/package_installer.py')
    return {name: fingerprint(root / name) for name in names}


def validate(source, run, acceptance, repo, client, public_file, review=None):
    source, run = Path(source).resolve(), Path(run).resolve()
    acceptance = Path(acceptance).resolve()
    tracked = {}

    def track(path, expected=None, parent=None):
        path = regular(path, parent)
        if expected is not None:
            check(path, expected)
        tracked[str(path)] = fingerprint(path)
        return path

    def read(path):
        return json.loads(track(path).read_text())

    profile = read(source / 'profile.json')
    need(profile['channel'] == 'stable', 'Reuse requires an existing stable package')
    # Native Ed25519 signatures and all signed asset hashes/URLs are mandatory.
    import base64
    envelope = read(source / 'index-stable.json')
    index = json.loads(base64.b64decode(envelope['payload'], validate=True))
    need(len(index['releases']) == 1, 'Reuse needs exactly one frozen release')
    ref = index['releases'][0]
    plan = publication_plan(source, repo, 'mtga-' + ref['id'], client, public_file)
    release = plan['release']
    track(Path(plan['manifest']))
    need(release['schema'] == 'MTGA_CN_RELEASE_V1' and
         release['acceptance']['status'] == 'accepted', 'Source is not stable accepted')
    need(len(release['resources']) == 5 and
         {row['layer'] for row in release['resources']} == set(LAYERS),
         'Source must contain all five unique layers')
    snapshot = read(run / 'snapshot.json')
    report = read(run / 'report.json')
    bases = read(run / 'official-base.json')
    need(report['schema'] == 'MTGA_CN_MAINTAINER_V1' and
         set(report['candidate_files']) == set(LAYERS), 'Incomplete historical candidate')
    need(not report.get('high_risk_unknown'), 'Unresolved historical high-risk item')
    need(report['localization']['static_status'] == 'PASS' and
         report['import_compatibility']['static_status'] == 'PASS', 'Static validation failed')
    need(len(bases['layers']) == 5 and {r['layer'] for r in bases['layers']} == set(LAYERS),
         'Incomplete official bases')
    subject = subject_for(snapshot, report, bases)
    evidence = read(acceptance)
    architectures = validate_acceptance(evidence, subject)
    need(release['acceptance']['evidence_sha256'] == fingerprint(acceptance)['sha256'] and
         release['acceptance']['scope'] == evidence['scope'] and
         release['architectures'] == architectures, 'Signed acceptance does not match preserved evidence')
    for key in ('client_version', 'steam_build', 'platform'):
        need(release[key] == subject[key], 'Source/candidate identity mismatch')
    protected = [{'relative': r['relative'], 'fingerprint': {k: r[k] for k in ('sha256', 'bytes')}}
                 for r in subject['protected_files'].values()]
    protected += [{'relative': rel, 'fingerprint': fp} for rel, fp in subject['manifests'].items()]
    need(release['protected_files'] == protected, 'Protected input drift')
    for relative, expected in snapshot['manifests'].items():
        track(run / 'official' / relative, expected, run)
    for item in report.get('baseline_inputs', {}).values():
        track(Path(item['path']), item)
    for row in release['resources']:
        layer = row['layer']
        original, output = subject['input_files'][layer], subject['output_files'][layer]
        need(row['relative'] == original['relative'] == output['relative'] and
             row['input'] == {k: original[k] for k in ('sha256', 'bytes')} and
             row['output'] == {k: output[k] for k in ('sha256', 'bytes')},
             'Historical resource differs from accepted source: ' + layer)
        base = track(run / 'official' / row['relative'], original, run)
        track(Path(report['candidate_files'][layer]['path']), output, run)
        for kind in ('patch', 'full'):
            need(kind in row, 'Reuse requires complete resources and deltas')
            asset = row[kind]
            track(source / asset['name'], asset['fingerprint'], source)
        verify_delta(base, source / row['patch']['name'], output)
    # The old testing-only review is retained, never promoted or rewritten.
    track(run / 'independent-review.json')
    binding = {'schema': 'MTGA_CN_STABLE_REUSE_BINDING_V1',
               'source_id': release['id'], 'inputs': tracked,
               'republisher_code': code_binding(),
               'client': fingerprint(regular(Path(client).resolve())),
               'public_key': fingerprint(regular(Path(public_file).resolve()))}
    if review is not None:
        approval = json.loads(regular(Path(review).resolve()).read_text())
        need(approval.get('schema') == 'MTGA_CN_STABLE_REUSE_REVIEW_V1' and
             approval.get('status') == 'PASS' and approval.get('stable_republish_approved') is True and
             approval.get('blocking_findings') == [] and
             bool(approval.get('reviewer', '').strip()) and bool(approval.get('scope', '').strip()) and
             approval.get('binding') == binding, 'Missing or stale independent stable reuse review')
    return plan, binding


def package(source, run, acceptance, review, destination, repo, tag, client, public_file, key, revision):
    need(review is not None, 'Independent stable reuse review is mandatory before signing')
    plan, binding = validate(source, run, acceptance, repo, client, public_file, review)
    source, destination = Path(source).resolve(), Path(destination).resolve()
    old = plan['release']
    need(type(revision) is int and revision > old['revision'], 'Reuse revision must increase')
    release_id = old['client_version'] + '-' + old['steam_build'] + '-r' + str(revision)
    need(tag == 'mtga-' + release_id, 'Reuse requires the canonical new release tag')
    need(not destination.exists(), 'Reuse destination already exists')
    destination.mkdir(parents=True)
    release = copy.deepcopy(old)
    release.update(id=release_id, revision=revision)
    for row in release['resources']:
        for kind in ('patch', 'full'):
            asset = row[kind]
            shutil.copyfile(source / asset['name'], destination / asset['name'])
            check(destination / asset['name'], asset['fingerprint'])
            asset['url'] = f'https://github.com/{repo}/releases/download/{tag}/{asset["name"]}'
    need(validate(source, run, acceptance, repo, client, public_file, review)[1] == binding,
         'Reuse inputs drifted before signing')
    manifest = destination / (release_id + '.json')
    sign(client, key, release, manifest, destination)
    index = {'schema': 'MTGA_CN_INDEX_V1', 'sequence': time.time_ns() // 1000000,
             'expires_at': (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=30)).strftime('%Y-%m-%dT%H:%M:%SZ'),
             'channel': 'stable', 'releases': [{'id': release_id, 'revision': revision,
              'client_version': old['client_version'], 'steam_build': old['steam_build'],
              'manifest': {'name': manifest.name, 'fingerprint': fingerprint(manifest)}}]}
    sign(client, key, index, destination / 'index-stable.json', destination)
    shutil.copyfile(source / 'profile.json', destination / 'profile.json')
    dump(destination / 'package-inventory.json', {p.name: fingerprint(p) for p in destination.iterdir()})
    publication_plan(destination, repo, tag, client, public_file)
    need(validate(source, run, acceptance, repo, client, public_file, review)[1] == binding,
         'Reuse inputs drifted after signing')
    return release
