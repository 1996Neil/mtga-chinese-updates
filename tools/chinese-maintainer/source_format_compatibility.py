"""Fail-closed compatibility for individually AI-reviewed malformed Arena sources.

This helper does not replace normal format validation. Call it only after that
validator reports failures; only exactly ['unbalanced-format'] may be waived.
An exact layer/key + English SHA256 + reviewed Chinese SHA256 allowlist is
mandatory. It is intentionally invalidated whenever either text changes.
"""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re

_MANIFEST = Path(__file__).with_name('source-format-reviewed-pairs.json')

def _sha(text: str) -> str:
    return hashlib.sha256(text.encode('utf-8')).hexdigest()

def control_skeleton(text: str) -> dict:
    """Scan the WHOLE string, independently of any ICU parser early failure.

    These are additional conservative invariants, not a permissive ICU parser.
    Source/translation hashes remain the final authority for exceptional prose.
    Rich tags retain full bytes and order, including link URLs and malformed tags.
    Argument headers, braces, branches, mana tokens and line controls are scanned
    globally so malformed earlier content never hides tokens farther in the text.
    """
    header = r'\{\s*(?:\d+|[A-Za-z_]\w*(?: [A-Za-z_]\w*)*|\[[A-Za-z_]\w*\])\s*(?:[:,]\s*[A-Za-z_]\w*(?::[A-Za-z_]\w*)?)?(?=\s*[,}:])'
    return {
        'rich_tags': re.findall(r'<[^>]*>', text),
        'braces': re.findall(r'[{}]', text),
        'argument_headers': re.findall(header, text),
        # Also catches the source's known missing-{ numeric select header.
        'typed_numeric_headers': re.findall(r'\b\d+\s*,\s*[A-Za-z_]\w*(?::[A-Za-z_]\w*)?', text),
        'branches': re.findall(r'(?:=\d+|\b(?:other|zero|one|two|few|many))\s*(?=\{)', text),
        'offsets': re.findall(r'\boffset\s*:\s*\d+', text),
        'mana': re.findall(r'\{o(?:[^\s{},]+|\{\d+\s*,\s*Number\})+\}|\[(?:[WUBRGCXYZSTQEP]|\d+|[WUBRG]/[WUBRGP2])\]', text),
        'cardname_count': len(re.findall(r'\bCARDNAME\b', text)),
        'line_controls': re.findall(r'\\+n|\\+r', text),
        'nul_count': text.count('\x00'),
    }

def compatible_source_format(english: str, chinese: str, failures: list[str], *, layer: str, key: str) -> bool:
    if failures != ['unbalanced-format'] or '\x00' in english or '\x00' in chinese:
        return False
    if control_skeleton(english) != control_skeleton(chinese):
        return False
    try:
        manifest = json.loads(_MANIFEST.read_text(encoding='utf-8'))
    except (OSError, ValueError, TypeError):
        return False
    if not isinstance(manifest, dict) or not isinstance(manifest.get('pairs'), list):
        return False
    pair = (layer, str(key), _sha(english), _sha(chinese))
    return any(pair == (r.get('layer'), str(r.get('key')), r.get('english_sha256'), r.get('chinese_sha256'))
               for r in manifest['pairs'] if isinstance(r, dict))

def preserve_or_resolve_failures(english: str, chinese: str, failures: list[str], *, layer: str, key: str) -> list[str]:
    return [] if compatible_source_format(english, chinese, failures, layer=layer, key=key) else list(failures)
