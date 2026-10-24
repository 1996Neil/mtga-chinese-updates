"""Local resource guards. No network access and no account data."""
from pathlib import Path
from collections import Counter
import contextlib
import gzip
import hashlib
import json
import os
import re
import sqlite3
import stat
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parent
STATE = ROOT / 'state'
GAME = Path(os.environ.get('MTGA_GAME_PATH', str(Path.home() / 'Library/Application Support/Steam/steamapps/common/MTGA'))).expanduser()
BACKUPS = Path(os.environ.get('MTGA_BACKUP_ROOT', str(Path.home() / 'Library/Application Support/MTGAChineseMaintainer/backups'))).expanduser()
OLD = BACKUPS / '2026-09-30-221411-english-chinese'
HISTORY = Path(os.environ.get('MTGA_HISTORY_ROOT', str(ROOT.parents[1] / 'compatibility/native-import-2026-10-02'))).expanduser()
DONORS = [Path.home() / 'Downloads' / f'MTGA Mod(MacOS) {v}' for v in ('26.9.9', '26.9.30')]

if sys.flags.optimize:
    raise RuntimeError('Refuse optimized Python: legacy resource-parser assertions must remain enabled')


def require(condition, message):
    if not condition:
        raise RuntimeError(message)


LEGACY_PROJECT = Path('/Users/neil/Documents/ChatGPT/mtga')
MOVED_ROOTS = ((LEGACY_PROJECT / 'tools/chinese-maintainer', ROOT),
               (LEGACY_PROJECT / 'compatibility', ROOT.parents[1] / 'compatibility'))


def managed_path(path):
    """Resolve only the two relocated trees; never rewrite historical payloads.

    Absolute evidence identifiers remain immutable. Files are opened at their
    new physical locations, with the same traversal/symlink refusal as before.
    Unrelated paths (game, system fonts, external rollback backups) stay intact.
    """
    path = Path(path)
    for old, current in MOVED_ROOTS:
        if path.is_relative_to(old):
            require('..' not in path.parts, 'Traversal in relocated path')
            path = current / path.relative_to(old)
            break
    if any(path.is_relative_to(current) for _, current in MOVED_ROOTS):
        require('..' not in path.parts and path.absolute() == path.resolve(),
                f'Refuse symlink/traversal in managed path: {path}')
    return path


def evidence_path(path):
    """Original identifier for a pinned historical receipt key, not an I/O path."""
    path = managed_path(path)
    for old, current in MOVED_ROOTS:
        if path.is_relative_to(current):
            return str(old / path.relative_to(current))
    return str(path)


def regular(path):
    path = managed_path(path)
    require(path.absolute() == path.resolve() and stat.S_ISREG(path.lstat().st_mode),
            f'Refuse symlink/nonregular file: {path}')
    for suffix in ('-wal', '-shm', '-journal'):
        require(not Path(str(path) + suffix).exists(), f'Active SQLite sidecar: {path}')
    return path


def fingerprint(path):
    path = regular(path)
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return {'sha256': h.hexdigest(), 'bytes': path.stat().st_size}


def digest(data):
    return hashlib.sha256(data).hexdigest()


def read_json(path):
    """Read project JSON or an official .mtga manifest.

    MTGA manifests are JSON text through 2026.63.x. From 2026.64.x the file
    arrives gzip-compressed (wrapped in a JSON payload by the newer updater),
    so the magic bytes decide the decoding. A JSON document can never start
    with 1f 8b, so this changes nothing for the text form.
    """
    raw = regular(path).read_bytes()
    if raw[:2] == b'\x1f\x8b':
        raw = gzip.decompress(raw)
    return json.loads(raw.decode('utf-8'))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_bytes(path, data, mode=0o600):
    path = managed_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    require(not path.is_symlink(), f'Refuse output symlink: {path}')
    fd, temp = tempfile.mkstemp(prefix='.mtga-cn-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as f:
            f.write(data)
            os.fchmod(f.fileno(), mode)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temp, path)
        sync_dir(path.parent)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def save_json(path, data):
    atomic_bytes(path, (json.dumps(data, ensure_ascii=False, indent=2) + '\n').encode())


def copy_checked(source, target, expected, before_replace=None):
    source, target = regular(source), managed_path(target)
    require(fingerprint(source) == expected, f'Source changed: {source}')
    mode = stat.S_IMODE(regular(source).stat().st_mode)
    # Staged bytes are checked before the target can change.
    fd, tmp = tempfile.mkstemp(prefix='.mtga-cn-copy-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as f, source.open('rb') as src:
            for block in iter(lambda: src.read(1024 * 1024), b''):
                f.write(block)
            os.fchmod(f.fileno(), mode)
            f.flush()
            os.fsync(f.fileno())
        require(fingerprint(Path(tmp)) == expected, 'Staged copy hash mismatch')
        if before_replace:
            before_replace()
        else:
            require(not target.exists(), f'Refuse artifact overwrite: {target}')
        os.replace(tmp, target)
        sync_dir(target.parent)
        require(fingerprint(target) == expected, 'Final copy hash mismatch')
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


@contextlib.contextmanager
def database(path):
    path = regular(path)
    c = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    try:
        yield c
    finally:
        c.close()


def one(paths, label):
    paths = list(paths)
    require(len(paths) == 1, f'Expected one {label}, found {len(paths)}; inspect active resources')
    return paths[0]


def game_stopped():
    r = subprocess.run(['/usr/bin/pgrep', '-x', 'MTGA'], capture_output=True)
    require(r.returncode == 1 and not r.stdout.strip(), 'MTGA is running or process check failed; quit MTGA first')


def has_chinese(text):
    return bool(re.search('[\u3400-\u9fff]', text or ''))


def argument_metadata(text):
    """Parse the supported ICU/.NET skeleton; branch prose is never a token.

    Unknown compound grammar fails closed. This deliberately does not attempt
    to interpret arbitrary ICU custom formatters or quoted brace syntax.
    """
    arguments, selectors, symbols = Counter(), Counter(), Counter()

    def signature(parts):
        return json.dumps(parts, ensure_ascii=False, separators=(',', ':'))

    def enclosed(message, start):
        depth = 1
        end = start + 1
        while end < len(message) and depth:
            depth += (message[end] == '{') - (message[end] == '}')
            end += 1
        if depth:
            raise ValueError('Unclosed argument')
        return message[start + 1:end - 1], end

    def parse(message, context=(), depth=0):
        if depth > 32:
            raise ValueError('Argument nesting too deep')
        pos = 0
        while pos < len(message):
            char = message[pos]
            if char == '}':
                raise ValueError('Unexpected closing brace')
            if char != '{':
                pos += 1
                continue
            body, pos = enclosed(message, pos)
            # Arena uses both plain mana tokens and a numeric formatter inside
            # a dynamic cost, e.g. {o{2, Number}oR}. Preserve exact cost bytes.
            if re.fullmatch(r'o(?:[^\s{},]+|\{\d+\s*,\s*Number\})+', body):
                symbols[signature([*context, '{' + body + '}'])] += 1
                continue
            match = re.fullmatch(r'\s*(\d+|[A-Za-z_]\w*(?: [A-Za-z_]\w*)*|\[[A-Za-z_]\w*\])\s*(?:([:,])(.*))?', body, re.S)
            if not match:
                raise ValueError('Unknown argument grammar')
            name, separator, rest = match.groups()
            if separator == ':':
                if any(c in rest for c in '{}'):
                    raise ValueError('Unknown .NET formatter')
                arguments[signature([*context, name, ':', rest])] += 1
                continue
            if separator is None:
                arguments[signature([*context, name])] += 1
                continue
            kind, comma, style = rest.strip().partition(',')
            kind = kind.strip()
            if kind.lower() not in {'plural', 'select', 'selectordinal'}:
                # Enum:CounterType is an observed Arena typed argument.
                if not re.fullmatch(r'[A-Za-z]+(?::[A-Za-z_]\w*)?', kind) or any(c in style for c in '{}'):
                    raise ValueError('Unknown typed formatter')
                arguments[signature([*context, name, kind, style.strip() if comma else ''])] += 1
                continue
            if not comma:
                raise ValueError('Missing ICU branches')
            argument = signature([*context, name, kind])
            arguments[argument] += 1
            cursor = 0
            offset = re.match(r'\s*offset\s*:\s*(\d+)\s*', style)
            if offset:
                if kind.lower() == 'select':
                    raise ValueError('Offset on select')
                selectors[signature([argument, 'offset', offset[1]])] += 1
                cursor = offset.end()
            seen = set()
            while cursor < len(style):
                branch = re.match(r'\s*(=\d+|[A-Za-z_]\w*)\s*\{', style[cursor:])
                if not branch:
                    if style[cursor:].strip():
                        raise ValueError('Unknown ICU branch')
                    break
                selector = branch[1]
                if selector in seen:
                    raise ValueError('Duplicate ICU selector')
                seen.add(selector)
                start = cursor + branch.end() - 1
                prose, cursor = enclosed(style, start)
                selectors[signature([argument, selector])] += 1
                parse(prose, (*context, name, kind, selector), depth + 1)
            if 'other' not in seen:
                raise ValueError('Missing ICU other branch')

    try:
        parse(text)
        valid = '\x00' not in text
    except ValueError:
        valid = False
    return arguments, selectors, symbols, valid


def format_metadata(text, *, tmp_indent_state=False):
    text = text or ''
    placeholders, selectors, symbols, valid = argument_metadata(text)
    stack = []
    indent_seen = False
    tags = re.findall(r'<[^>]*>', text)
    critical = []
    optional = {'i', 'b', 'u', 'nobr', 'indent', 'color', 'size', 'alpha', 'cspace', 'voffset', 'line-height', 'align', 'font-weight'}
    for tag in tags:
        if re.fullmatch(r'<#[0-9a-fA-F]{6,8}>', tag):
            stack.append('color')
            continue
        match = re.match(r'<(/?)([A-Za-z][\w-]*)(?:\s|=|/?>)', tag)
        if not match:
            valid = False
            continue
        closing, name = match.groups()
        name = name.lower()
        if tmp_indent_state and name == 'indent':
            # TMP indentation changes caret state across lines, not the bold/italic stack.
            # Only observed numeric syntax is admitted; both languages must retain
            # the identical full tag sequence before this mode can affect validation.
            if closing:
                valid &= indent_seen and tag == '</indent>'
            else:
                valid &= bool(re.fullmatch(r'<indent=[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:%|em|px)?>', tag))
                indent_seen = True
            continue
        if name not in optional:
            critical.append(tag)
        if name in {'sprite', 'br', 'space', 'page'}:
            continue
        if closing:
            valid &= bool(stack) and stack[-1] == name
            if stack:
                stack.pop()
        else:
            stack.append(name)
    # TMP permits a style opener to continue to the end of a text field.
    valid &= all(name in optional for name in stack)
    return {'placeholders': dict(placeholders), 'selectors': dict(selectors),
            'mana_symbols': dict(symbols), 'critical_tags': dict(Counter(critical)),
            'balanced': bool(valid), 'rich_tags': tags,
            'cardname_tokens': len(re.findall(r'\bCARDNAME\b', text))}


def safe_format(english, chinese):
    a, b = format_metadata(english), format_metadata(chinese)
    failures = [k for k in ('placeholders', 'selectors', 'mana_symbols', 'critical_tags') if a[k] != b[k]]
    if not a['balanced'] or not b['balanced']:
        failures.append('unbalanced-format')
    if failures == ['unbalanced-format'] and a['rich_tags'] == b['rich_tags'] and any(
            tag.startswith('<indent=') for tag in a['rich_tags']):
        # Never waive argument validity or other nesting failures. This is an
        # invariant-preserving parser correction, not a text/markup repair.
        corrected_a = format_metadata(english, tmp_indent_state=True)
        corrected_b = format_metadata(chinese, tmp_indent_state=True)
        if corrected_a['balanced'] and corrected_b['balanced']:
            failures = []
    if failures == ['unbalanced-format']:
        # Exact independently reviewed source/translation pairs only. A global
        # token scan remains mandatory when the official ICU parser stops early.
        from source_format_compatibility import compatible_source_format, _MANIFEST
        try:
            pairs = json.loads(_MANIFEST.read_text()).get('pairs', [])
        except (OSError, ValueError, TypeError):
            pairs = []
        if any(compatible_source_format(english or '', chinese or '', failures,
                                       layer=r.get('layer'), key=r.get('key'))
               for r in pairs if isinstance(r, dict)):
            failures = []
    # Historical translations sometimes expand CARDNAME to the verified name;
    # adding new CARDNAME tokens is unsafe.
    if b['cardname_tokens'] > a['cardname_tokens']:
        failures.append('introduced-CARDNAME')
    return failures


def quoted(name):
    return '"' + name.replace('"', '""') + '"'


def adapt_verified_format(english, chinese):
    """Restore a demonstrated mistranslated ICU syntax word, never branch prose.

    The existing reviewed translation remains the source. Only a selector in
    a fully traversable ICU branch list is considered; final metadata must
    match the current English exactly. All other text bytes are preserved.
    """
    if not argument_metadata(english)[3] or argument_metadata(chinese)[3]:
        return chinese, None
    edits = set()
    header = re.compile(r'\{(?:\d+|[A-Za-z_]\w*)\s*,\s*(?:plural|select|selectordinal)\s*,')
    for match in header.finditer(chinese):
        cursor = match.end()
        local = []
        complete = False
        while cursor < len(chinese):
            if chinese[cursor:].lstrip().startswith('}'):
                complete = True
                break
            branch = re.match(r'\s*(=\d+|[A-Za-z_]\w*|另外)\s*\{', chinese[cursor:])
            if not branch:
                break
            if branch[1] == '另外':
                local.append((cursor + branch.start(1), cursor + branch.end(1)))
            pos = cursor + branch.end()
            depth = 1
            while pos < len(chinese) and depth:
                depth += (chinese[pos] == '{') - (chinese[pos] == '}')
                pos += 1
            if depth:
                break
            cursor = pos
        if complete:
            edits.update(local)
    adapted = chinese
    for start, end in sorted(edits, reverse=True):
        adapted = adapted[:start] + 'other' + adapted[end:]
    if edits and not safe_format(english, adapted):
        return adapted, 'restored-ICU-other-selector'
    return chinese, None
