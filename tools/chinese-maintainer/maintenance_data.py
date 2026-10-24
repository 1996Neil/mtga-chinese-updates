"""Export compact reviewed maintenance text; never install or approve resources."""
import argparse
from collections import Counter
import hashlib
import json
from pathlib import Path
import re
import tempfile
from urllib.parse import parse_qsl, urlsplit

SCHEMA = "MTGA_PUBLIC_MAINTENANCE_DATA_V1"
ROW_FIELDS = {
    "layer", "key", "english", "chinese", "english_sha256", "chinese_sha256",
    "field_use", "identity", "verification_level", "ai_expression_remaining",
    "disposition", "source_ids", "batch_id",
}
SOURCE_FIELDS = {"id", "website", "url", "kind", "language", "declared", "adopted"}
LEVELS = {"exact-full-text", "source-backed-concepts", "source-unavailable", "intentional-preserved"}
FORBIDDEN = re.compile(
    r"(?i:/Users/|/home/|/private/(?:tmp|var)/|/var/folders/|(?<!\w)/tmp/|[A-Z]:[\\/]Users[\\/])|"
    r"github_pat_[A-Za-z0-9_]+|gh[pousr]_[A-Za-z0-9]{20,}|-----BEGIN [^-]*PRIVATE KEY-----"
)

def require(ok, message):
    if not ok:
        raise ValueError(message)

def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)

def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()

def fingerprint(path):
    value = Path(path).read_bytes()
    return {"bytes": len(value), "sha256": hashlib.sha256(value).hexdigest()}

def public_only(value):
    if isinstance(value, str):
        require(not FORBIDDEN.search(value), "Private path or credential-shaped data in public export")
    elif isinstance(value, dict):
        for key, item in value.items():
            public_only(key)
            public_only(item)
    elif isinstance(value, list):
        for item in value:
            public_only(item)

def source_entry(source):
    url = urlsplit(source["url"])
    require(url.scheme == "https" and url.hostname and not url.username and not url.password,
            "Source must be an HTTPS address without credentials")
    require(not any(k.lower() in {"access_token", "token", "api_key", "secret", "password",
                                 "authorization", "x-amz-credential", "x-amz-signature"}
                    for k, _ in parse_qsl(url.query)), "Credential parameter in source URL")
    result = {
        "website": url.hostname, "url": source["url"], "kind": source["source_kind"],
        "language": source["language"], "declared": source.get("source_declared") or "未注明",
        "adopted": source.get("adopted") is not False,
    }
    public_only(result)
    return {"id": "src-" + digest(canonical(result))[:16], **result}

def statistics(rows, sources):
    return {
        "records": len(rows), "by_layer": dict(sorted(Counter(r["layer"] for r in rows).items())),
        "verification_levels": dict(sorted(Counter(r["verification_level"] for r in rows).items())),
        "source_entries": len(sources), "unique_source_urls": len({s["url"] for s in sources}),
        "source_websites": sorted({s["website"] for s in sources}),
    }

def validate(directory):
    """Integrity and format validation, explicitly not translation/source approval."""
    from common import safe_format
    directory = Path(directory)
    manifest = json.loads((directory / "manifest.json").read_text())
    require(manifest.get("schema") == SCHEMA, "Unknown compact data schema")
    require(manifest.get("automatic_admission") is False and manifest.get("raw_evidence_included") is False,
            "Compact data must not claim automatic admission or include raw evidence")
    require(manifest.get("human_acceptance") is False and manifest.get("runtime_acceptance") is False,
            "Export cannot manufacture human or runtime acceptance")
    require(set(manifest["files"]) == {"sources.json", "translations.jsonl"}, "Unexpected data files")
    for name, fp in manifest["files"].items():
        require(fingerprint(directory / name) == fp, "Data file fingerprint mismatch: " + name)
    catalog = json.loads((directory / "sources.json").read_text())
    require(catalog.get("schema") == "MTGA_PUBLIC_SOURCES_V1", "Unknown source catalog")
    sources = catalog["sources"]
    ids = {}
    for entry in sources:
        require(set(entry) == SOURCE_FIELDS, "Unexpected source fields")
        reconstructed = source_entry({
            "url": entry["url"], "source_kind": entry["kind"], "language": entry["language"],
            "source_declared": entry["declared"], "adopted": entry["adopted"],
        })
        require(entry == reconstructed and entry["id"] not in ids, "Invalid or duplicate source identity")
        ids[entry["id"]] = entry
    batches = manifest["source_batches"]
    require(len({b["id"] for b in batches}) == len(batches), "Duplicate source batch")
    rows, seen = [], set()
    for line in (directory / "translations.jsonl").read_text().splitlines():
        require(bool(line.strip()), "Blank record line")
        row = json.loads(line)
        require(set(row) == ROW_FIELDS and row["layer"] in {"card", "client", "ui"}, "Invalid record fields")
        key = row["layer"], row["key"]
        require(isinstance(row["key"], str) and row["key"] and key not in seen, "Invalid or duplicate slot")
        seen.add(key)
        require(all(isinstance(row[f], str) for f in ("english", "chinese")), "Translation text required")
        require(row["english_sha256"] == digest(row["english"]) and
                row["chinese_sha256"] == digest(row["chinese"]), "Translation hash mismatch")
        require(row["verification_level"] in LEVELS and isinstance(row["ai_expression_remaining"], bool),
                "Invalid source coverage")
        require(isinstance(row["source_ids"], list) and len(row["source_ids"]) == len(set(row["source_ids"]))
                and set(row["source_ids"]) <= ids.keys(), "Unknown or repeated source reference")
        require(row["batch_id"] in {b["id"] for b in batches}, "Unknown source batch")
        require(not safe_format(row["english"], row["chinese"]), "Unsafe text format: " + str(key))
        level = row["verification_level"]
        if level in {"exact-full-text", "source-backed-concepts"}:
            require(any(ids[s]["adopted"] and not re.search(
                r"gpt|claude|gemini|\bai\b", ids[s]["declared"], re.I
            ) for s in row["source_ids"]), "Source-backed record lacks an adopted non-AI source")
        if level in {"source-backed-concepts", "source-unavailable"}:
            require(row["ai_expression_remaining"], "Remaining AI expression must be disclosed")
        if level in {"exact-full-text", "intentional-preserved"}:
            require(not row["ai_expression_remaining"], "Source coverage contradicts AI disclosure")
        if level == "intentional-preserved":
            require(row["english"] == row["chinese"] and row["disposition"] in
                    {"empty-source-preserved", "technical-identifier-preserved"}, "Invalid preservation exception")
        require(isinstance(row["field_use"], list) and isinstance(row["identity"], dict),
                "Matching context missing")
        require(all(isinstance(v, str) for v in row["field_use"]) and row["disposition"] in {
            "translated", "reviewed-unreferenced-localization", "empty-source-preserved",
            "technical-identifier-preserved"
        }, "Invalid usage or disposition")
        if row["layer"] in {"client", "ui"}:
            identity_key = "client_key" if row["layer"] == "client" else "ui_key"
            require(row["identity"] == {identity_key: row["key"]}, "Wrong localization identity")
        else:
            require(set(row["identity"]) == {"cards"} and isinstance(row["identity"]["cards"], list),
                    "Card matching identity missing")
        public_only(row)
        rows.append(row)
    require(statistics(rows, sources) == manifest["statistics"], "Statistics mismatch")
    public_only(manifest)
    public_only(catalog)
    return {"status": "PASS", **manifest["statistics"], "automatic_admission": False}

def export(pack_paths, output):
    """Project already reviewed V2 records; preserve original immutable evidence."""
    from common import safe_format
    from source_check import validate_pack
    output = Path(output)
    require(not output.exists(), "Use a fresh output directory")
    rows, catalog, batches, seen, versions = [], {}, [], set(), set()
    for path in sorted(Path(p).resolve() for p in pack_paths):
        pack = json.loads(path.read_text())
        review_path = path.with_name("review.json")
        review = json.loads(review_path.read_text())
        require(pack.get("schema") == "MTGA_SOURCE_CHECKED_RESOLUTION_V2", "Export only source-checked V2")
        require(pack.get("human_acceptance") is False and pack.get("runtime_acceptance") is False,
                "Unexpected acceptance claim")
        require(review.get("review_kind") == "independent-AI-resolution-review" and
                review.get("status") == "PASS" and not review.get("blocking_findings") and
                review.get("source_checks_reviewed") is True and
                review.get("pack_fingerprint") == fingerprint(path), "Missing or stale independent source review")
        require(review.get("format_pairs_fingerprint") ==
                fingerprint(Path(__file__).with_name("source-format-reviewed-pairs.json")), "Format review changed")
        validate_pack(pack, base_dir=path.parent)
        batch_id = path.parent.name
        require(batch_id not in {b["id"] for b in batches}, "Repeated batch id")
        batches.append({"id": batch_id, "pack": fingerprint(path), "review": fingerprint(review_path)})
        versions.add(pack["source_version"])
        for original in pack["records"]:
            key = original["layer"], str(original["key"])
            require(key not in seen, "Duplicate source slot; resolve conflicts explicitly: " + str(key))
            seen.add(key)
            require(not safe_format(original["english"], original["chinese"]), "Unsafe source translation")
            check = original["source_check"]
            refs = []
            for source in check["sources"]:
                entry = source_entry(source)
                require(entry["id"] not in catalog or catalog[entry["id"]] == entry, "Source identifier collision")
                catalog[entry["id"]] = entry
                if entry["id"] not in refs:
                    refs.append(entry["id"])
            row = {
                "layer": key[0], "key": key[1], "english": original["english"], "chinese": original["chinese"],
                "english_sha256": digest(original["english"]), "chinese_sha256": digest(original["chinese"]),
                "field_use": original["field_use"], "identity": original["identity"],
                "verification_level": check["verification_level"],
                "ai_expression_remaining": bool(check.get("ai_remaining")),
                "disposition": original["disposition"], "source_ids": sorted(refs), "batch_id": batch_id,
            }
            public_only(row)
            rows.append(row)
    require(len(versions) == 1, "Export one source game version at a time")
    rows.sort(key=lambda r: (r["layer"], r["key"]))
    sources = sorted(catalog.values(), key=lambda s: s["id"])
    text = "".join(canonical(row) + "\n" for row in rows)
    source_text = json.dumps({"schema": "MTGA_PUBLIC_SOURCES_V1", "sources": sources},
                             ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    manifest = {
        "schema": SCHEMA, "source_version": versions.pop(), "statistics": statistics(rows, sources),
        "source_batches": batches, "review_scope": "Preserves independently AI-reviewed source coverage; not new human or runtime acceptance",
        "automatic_admission": False, "raw_evidence_included": False,
        "human_acceptance": False, "runtime_acceptance": False,
        "files": {
            "sources.json": {"bytes": len(source_text.encode()), "sha256": digest(source_text)},
            "translations.jsonl": {"bytes": len(text.encode()), "sha256": digest(text)},
        },
    }
    public_only(manifest)
    public_only(sources)
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".maintenance-export-", dir=output.parent) as temporary:
        stage = Path(temporary) / "data"
        stage.mkdir()
        (stage / "sources.json").write_text(source_text, encoding="utf-8")
        (stage / "translations.jsonl").write_text(text, encoding="utf-8")
        (stage / "manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        result = validate(stage)
        require(not output.exists(), "Output appeared during export")
        stage.rename(output)
    return result

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    check = sub.add_parser("validate")
    check.add_argument("directory", type=Path)
    build = sub.add_parser("export")
    build.add_argument("--pack", type=Path, action="append", required=True)
    build.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = validate(args.directory) if args.command == "validate" else export(args.pack, args.output)
    print(json.dumps(result, ensure_ascii=False, indent=2))

if __name__ == "__main__":
    main()
