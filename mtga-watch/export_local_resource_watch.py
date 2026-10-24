#!/usr/bin/env python3
"""Export only observed public metadata after a local official game update.

Read-only for the game. Never exports Player.log, player identity or game payloads.
The output is a seed for known resource probes, not a claim of global latest.
"""
import argparse
import gzip
import hashlib
import json
import plistlib
import re
from pathlib import Path
from datetime import datetime, timezone


def export(game, player_log, published_release_id):
    version = plistlib.loads((game / 'MTGA.app/Contents/Info.plist').read_bytes())['CFBundleShortVersionString']
    acf = (game.parent.parent / 'appmanifest_2141910.acf').read_text()
    build = re.search(r'"buildid"\s+"([0-9]+)"', acf)
    depot = re.search(r'"2141913"\s*\{\s*"manifest"\s+"([0-9]+)"', acf)
    if not build or not depot:
        raise ValueError('Steam official macOS identity missing')
    # Extract a public CDN filename only. The raw log never leaves this function.
    text = player_log.read_text(errors='replace')
    external = re.findall(r'https://assets\.mtgarena\.wizards\.com/(External_[0-9]+(?:\.[0-9]+){4}-macos\.mtga)', text)
    external = [x for x in external if x.startswith('External_' + version + '.')]
    if not external:
        raise ValueError('Start updated game and complete downloads first: current macOS pointer is not observed')
    downloads = game / 'MTGA_Data/Downloads'
    locs = list(downloads.glob('Manifest_Localization_*.mtga'))
    if len(locs) != 1:
        raise ValueError('Expected exactly one active localization manifest')
    raw = locs[0].read_bytes()
    plain = gzip.decompress(raw) if raw[:2] == b'\x1f\x8b' else raw
    if hashlib.md5(plain).hexdigest() != locs[0].stem.rsplit('_', 1)[-1]:
        raise ValueError('Local official manifest content-address mismatch')
    assets = json.loads(plain)['Assets']
    if len([a for a in assets if a['Name'].startswith('Raw_ClientLocalization_')]) != 1:
        raise ValueError('Official localization manifest layout changed')
    return {'client_version': version, 'steam_build': build[1], 'macos_manifest': depot[1],
            'external_name': external[-1], 'localization_manifest': locs[0].name,
            'published_release_id': published_release_id,
            'observed_at': datetime.now(timezone.utc).isoformat(),
            'provenance': 'Local official manifest and public macOS CDN pointer only; no account, log or payload uploaded.'}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--game', type=Path, default=Path.home() / 'Library/Application Support/Steam/steamapps/common/MTGA')
    p.add_argument('--player-log', type=Path, default=Path.home() / 'Library/Logs/Wizards Of The Coast/MTGA/Player.log')
    p.add_argument('--output', type=Path, required=True)
    p.add_argument('--config', type=Path, default=Path(__file__).resolve().parent / 'config.json')
    args = p.parse_args()
    from watch_mtga import baseline_target
    published = baseline_target(json.loads(args.config.read_text()), datetime.now(timezone.utc))
    result = export(args.game, args.player_log, published['release_id'])
    # Exclusive creation prevents accidental overwrite of existing config/evidence.
    with args.output.open('x') as f:
        f.write(json.dumps(result, ensure_ascii=False, indent=2) + '\n')
    print('Observed public resource seed exported. No game file changed.')


if __name__ == '__main__':
    main()
