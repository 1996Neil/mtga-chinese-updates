"""Reuse only exact artifacts from an authenticated same-build stable manifest."""
import json
import re
import subprocess
import base64
from pathlib import Path
from publisher import LAYERS


def asset_tag(url, repo, name):
    if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,145}', name):
        raise ValueError('复用资源名称无效。')
    prefix = f'https://github.com/{repo}/releases/download/'
    if not isinstance(url, str) or not url.startswith(prefix):
        raise ValueError('复用资源不属于既有分发仓库。')
    parts = url[len(prefix):].split('/')
    if (len(parts) != 2 or parts[1] != name or
            not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]{0,150}', parts[0])):
        raise ValueError('复用资源地址无效。')
    return parts[0]


def load_baseline(path, client, public_key, repo):
    """The original signed envelope stays unchanged; no unsigned reuse hints."""
    import tempfile
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise ValueError('资源复用需要原字节签名基准。')
    with tempfile.TemporaryDirectory(prefix='mtga-asset-key-') as directory:
        key = Path(directory) / 'public.txt'
        key.write_text(public_key)
        result = subprocess.run([str(client), 'verify-envelope', '--input', str(path),
                                 '--key', str(key)], text=True, capture_output=True)
    if result.returncode:
        raise ValueError('资源复用基准签名无效。')
    release = json.loads(base64.b64decode(json.loads(path.read_text())['payload'], validate=True))
    rows = release.get('resources', [])
    if (release.get('schema') != 'MTGA_CN_RELEASE_V1' or
            release.get('acceptance', {}).get('status') != 'accepted' or
            len(rows) != 5 or {row['layer'] for row in rows} != set(LAYERS)):
        raise ValueError('资源复用基准不是完整的正式版本。')
    for row in rows:
        for kind in ('patch', 'full'):
            if kind in row:
                asset_tag(row[kind].get('url'), repo, row[kind]['name'])
    return release


def matching_row(baseline, version, build, layer, relative, original, output):
    if (baseline['client_version'], baseline['steam_build']) != (version, build):
        raise ValueError('不能跨游戏版本或 Steam build 复用资源。')
    row = next(row for row in baseline['resources'] if row['layer'] == layer)
    return row if (row['relative'] == relative and row['input'] == original and
                   row['output'] == output) else None
