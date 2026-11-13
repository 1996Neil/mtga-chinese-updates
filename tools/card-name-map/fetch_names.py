#!/usr/bin/env python3
"""Fetch the pinned names-only card index into tools/card-name-map/data/.

Source: https://github.com/1996Neil/mtga-card-names

Usage:
    python3 fetch_names.py --check
    python3 fetch_names.py --apply
    python3 fetch_names.py --input /path/to/cards.jsonl.gz --apply
"""
from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PIN = HERE / "RELEASE.json"
DATA = HERE / "data"
GZ = DATA / "cards.jsonl.gz"
MANIFEST = DATA / "manifest.json"
UA = "mtga-card-name-map/2.0 (names-only release sync)"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_pin() -> dict:
    try:
        pin = json.loads(PIN.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        sys.exit(f"[error] cannot read {PIN}: {exc}")
    for key in ("package", "release", "data_version", "cards_jsonl_sha256",
                "cards_jsonl_gz_sha256", "manifest_sha256", "rows"):
        if key not in pin:
            sys.exit(f"[error] {PIN} missing {key}")
    return pin


def download(url: str, expected: str, timeout: int = 60, attempts: int = 3) -> bytes:
    last = None
    for attempt in range(1, attempts + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read()
            if sha256(data) != expected:
                raise ValueError(f"sha256 mismatch for {url}")
            return data
        except (OSError, ValueError, urllib.error.URLError) as exc:
            last = exc
            if attempt < attempts:
                time.sleep(1.5 * attempt)
    sys.exit(f"[error] download failed after {attempts} attempts: {url}: {last}")


def chinese_rows(raw: bytes) -> int:
    count = 0
    for line in raw.decode("utf-8").splitlines():
        if not line.strip():
            continue
        if json.loads(line).get("zh"):
            count += 1
    return count


def verify(gz: bytes, manifest_bytes: bytes, pin: dict) -> dict:
    problems = []
    if sha256(gz) != pin["cards_jsonl_gz_sha256"]:
        problems.append("cards.jsonl.gz sha256 mismatch")
    if sha256(manifest_bytes) != pin["manifest_sha256"]:
        problems.append("manifest.json sha256 mismatch")
    try:
        manifest = json.loads(manifest_bytes)
    except ValueError as exc:
        sys.exit(f"[error] invalid manifest: {exc}")
    if manifest.get("schema") != "MTGA_CARD_NAMES_V1":
        problems.append("unexpected manifest schema")
    if manifest.get("data_version") != pin["data_version"]:
        problems.append("manifest data_version mismatch")
    files = manifest.get("files") or {}
    if files.get("cards.jsonl.gz", {}).get("sha256") != pin["cards_jsonl_gz_sha256"]:
        problems.append("manifest gz sha256 mismatch")
    if files.get("cards.jsonl", {}).get("sha256") != pin["cards_jsonl_sha256"]:
        problems.append("manifest jsonl sha256 mismatch")
    raw = gzip.decompress(gz)
    if sha256(raw) != pin["cards_jsonl_sha256"]:
        problems.append("decompressed cards.jsonl sha256 mismatch")
    rows = chinese_rows(raw)
    if rows != pin["rows"]:
        problems.append(f"Chinese rows {rows} != {pin['rows']}")
    if problems:
        sys.exit("[error] " + "; ".join(problems))
    return {"data_version": pin["data_version"], "rows": rows}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, help="local cards.jsonl.gz")
    parser.add_argument("--manifest", type=Path, help="local manifest.json (with --input; avoids network)")
    parser.add_argument("--apply", action="store_true", help="write data/cards.jsonl.gz and data/manifest.json")
    parser.add_argument("--check", action="store_true", help="verify the local cache")
    args = parser.parse_args()

    pin = read_pin()
    base = f"https://github.com/{pin['package']}/releases/download/{pin['release']}"

    if args.check:
        if not GZ.is_file() or not MANIFEST.is_file():
            sys.exit("[error] data cache missing; run `python3 fetch_names.py --apply`")
        result = verify(GZ.read_bytes(), MANIFEST.read_bytes(), pin)
        print(json.dumps({"ok": True, **result}))
        return

    if not args.apply:
        sys.exit("pass --check or --apply (and optional --input cards.jsonl.gz)")

    if args.input:
        gz = args.input.read_bytes()
        manifest_bytes = (args.manifest.read_bytes() if args.manifest
                          else download(f"{base}/manifest.json", pin["manifest_sha256"]))
    else:
        manifest_bytes = download(f"{base}/manifest.json", pin["manifest_sha256"])
        gz = download(f"{base}/cards.jsonl.gz", pin["cards_jsonl_gz_sha256"])

    result = verify(gz, manifest_bytes, pin)
    DATA.mkdir(parents=True, exist_ok=True)
    GZ.write_bytes(gz)
    MANIFEST.write_bytes(manifest_bytes)
    print(json.dumps({"ok": True, **result, "path": str(DATA)}))


if __name__ == "__main__":
    main()
