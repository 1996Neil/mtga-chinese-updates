#!/usr/bin/env python3
"""One-time offline migration of stable watch keys, never private issue bodies.

Input is a complete local JSON array exported from old issues?state=all including
closed issues. This command performs no network request, message, or game write.
The maintainer must verify pagination completeness before using it.
"""
import argparse
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from watch_mtga import REPO, STATE_SCHEMA, validate_state

LEGACY_REPO = '1996Neil/mtga-resource-acquisition-probe'


def migrate(issues, migrated_at):
    if not isinstance(issues, list):
        raise ValueError('Expected complete all-state issue array')
    keys, count = set(), 0
    for issue in issues:
        if not isinstance(issue, dict):
            raise ValueError('Invalid legacy issue')
        if 'pull_request' in issue or not str(issue.get('title', '')).startswith('[MTGA]'):
            continue
        count += 1
        body = issue.get('body') or ''
        if not isinstance(body, str):
            raise ValueError('Invalid issue body')
        matches = re.findall(r'<!-- mtga-watch:([A-Za-z0-9._-]{1,180}) -->', body)
        keys.update(matches)
        # Pre-marker Steam notifications used a deterministic build in the title.
        if not matches:
            match = re.search(r'\[MTGA\] 新版本 build ([0-9]+)\b', issue.get('title', ''))
            if match:
                keys.add('steam-build-' + match[1])
    state = {'schema': STATE_SCHEMA, 'repository': REPO, 'seen_keys': sorted(keys),
             'migration': {'from_repository': LEGACY_REPO, 'migrated_at': migrated_at,
                           'legacy_watch_issues_examined': count,
                           'scope': 'Stable event keys only; no private issue bodies, numbers, users, or credentials copied'},
             'status': 'MIGRATED_NOT_YET_CHECKED', 'human_notifications_sent': False,
             'localization_published': False, 'findings': [], 'errors': []}
    return validate_state(state)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--issues', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--confirm-complete-history', action='store_true', required=True,
                        help='Confirm source includes every page and both open/closed issues')
    args = parser.parse_args()
    raw = args.issues.read_bytes()
    state = migrate(json.loads(raw), datetime.now(timezone.utc).isoformat())
    # Digest binds the operator's input but deliberately not its private content.
    state['migration']['input_sha256'] = hashlib.sha256(raw).hexdigest()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x') as f:
        f.write(json.dumps(state, ensure_ascii=False, indent=2) + '\n')
    print(f'Migrated {len(state["seen_keys"])} stable event keys; no notifications sent')


if __name__ == '__main__':
    main()
