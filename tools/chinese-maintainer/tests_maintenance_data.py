"""Compact public data checks; synthetic evidence and no game access."""
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

import maintenance_data as data
from source_check import excerpt_hash

class CompactMaintenanceDataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.batch = self.root / "reviewed-fixture"
        self.batch.mkdir()
        quote = "抓指定数量的牌。"
        (self.batch / "source.json").write_text(json.dumps({"text": quote}, ensure_ascii=False), encoding="utf-8")
        self.row = {
            "layer": "client", "key": "Events/Event_Desc_Fixture", "english": "Draw {count} cards.",
            "chinese": "抓{count}张牌。", "field_use": ["Loc.enUS"],
            "identity": {"client_key": "Events/Event_Desc_Fixture"}, "disposition": "translated",
            "source_check": {
                "verification_level": "exact-full-text", "audit_note": "Synthetic full text fixture.",
                "english_alignment": "Synthetic exact English.", "adaptation_note": "Synthetic unchanged template.",
                "ai_remaining": [], "sources": [{
                    "url": "https://example.org/fixture/1", "source_kind": "community-localization",
                    "language": "zh-Hans", "source_declared": None, "excerpt": quote,
                    "excerpt_sha256": excerpt_hash(quote), "match_note": "Synthetic exact fixture.",
                    "retrieved_at": "2026-10-24T00:00:00Z",
                    "snapshot": {"path": "source.json", **data.fingerprint(self.batch / "source.json")},
                }],
            },
        }
        self.pack = {"schema": "MTGA_SOURCE_CHECKED_RESOLUTION_V2", "source_version": "fixture",
                     "human_acceptance": False, "runtime_acceptance": False, "records": [self.row]}
        self.write_pack()
        self.output = self.root / "public"

    def tearDown(self):
        self.temp.cleanup()

    def write_pack(self):
        path = self.batch / "pack.json"
        path.write_text(json.dumps(self.pack, ensure_ascii=False), encoding="utf-8")
        review = {
            "review_kind": "independent-AI-resolution-review", "status": "PASS", "blocking_findings": [],
            "source_checks_reviewed": True, "pack_fingerprint": data.fingerprint(path),
            "format_pairs_fingerprint": data.fingerprint(Path(data.__file__).with_name("source-format-reviewed-pairs.json")),
        }
        (self.batch / "review.json").write_text(json.dumps(review), encoding="utf-8")

    def exported(self):
        return data.export([self.batch / "pack.json"], self.output)

    def rewrite_rows(self, rows):
        (self.output / "translations.jsonl").write_text("".join(data.canonical(r) + "\n" for r in rows), encoding="utf-8")
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["files"]["translations.jsonl"] = data.fingerprint(self.output / "translations.jsonl")
        path.write_text(json.dumps(manifest), encoding="utf-8")

    def test_export_preserves_text_context_and_sources_without_raw_evidence(self):
        result = self.exported()
        self.assertEqual(result["records"], 1)
        row = json.loads((self.output / "translations.jsonl").read_text())
        for field in ("english", "chinese", "identity", "field_use"):
            self.assertEqual(row[field], self.row[field])
        self.assertEqual(set(p.name for p in self.output.iterdir()),
                         {"translations.jsonl", "sources.json", "manifest.json"})
        for path in self.output.iterdir():
            self.assertNotIn(str(self.root), path.read_text())
            self.assertNotIn('"snapshot"', path.read_text())
        catalog = json.loads((self.output / "sources.json").read_text())
        self.assertEqual(catalog["sources"][0]["declared"], "未注明")
        self.assertFalse(result["automatic_admission"])

    def test_stale_review_does_not_create_output(self):
        self.pack["records"][0]["chinese"] = "抓取{count}张牌。"
        (self.batch / "pack.json").write_text(json.dumps(self.pack), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "stale"):
            self.exported()
        self.assertFalse(self.output.exists())

    def test_duplicate_slots_rejected_before_any_public_output(self):
        self.pack["records"].append(copy.deepcopy(self.row))
        self.write_pack()
        with self.assertRaises(ValueError):
            self.exported()
        self.assertFalse(self.output.exists())

    def test_existing_export_is_never_overwritten(self):
        self.exported()
        before = data.fingerprint(self.output / "manifest.json")
        with self.assertRaisesRegex(ValueError, "fresh"):
            self.exported()
        self.assertEqual(before, data.fingerprint(self.output / "manifest.json"))

    def test_file_tampering_detected(self):
        self.exported()
        path = self.output / "translations.jsonl"
        path.write_text(path.read_text() + "\n", encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "fingerprint"):
            data.validate(self.output)

    def test_unknown_source_reference_rejected_even_with_updated_file_hash(self):
        self.exported()
        row = json.loads((self.output / "translations.jsonl").read_text())
        row["source_ids"] = ["src-missing"]
        self.rewrite_rows([row])
        with self.assertRaisesRegex(ValueError, "source reference"):
            data.validate(self.output)

    def test_parameter_loss_rejected_even_with_updated_hashes(self):
        self.exported()
        row = json.loads((self.output / "translations.jsonl").read_text())
        row["chinese"] = "抓牌。"
        row["chinese_sha256"] = data.digest(row["chinese"])
        self.rewrite_rows([row])
        with self.assertRaisesRegex(ValueError, "Unsafe"):
            data.validate(self.output)

    def test_private_paths_and_credentials_are_rejected(self):
        for value in ("/Users/example/source.json", r"C:\Users\example\source.json",
                      r"c:\users\example\source.json", "c:/users/example/source.json",
                      "/home/example/source.json", "/private/tmp/source.json", "ghp_" + "a" * 36):
            with self.subTest(value=value), self.assertRaises(ValueError):
                data.public_only({"nested": [value]})
        for url in ("https://person:password@example.org/source", "https://example.org/?access_token=secret"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                data.source_entry({"url": url, "source_kind": "community-localization", "language": "zh-Hans"})

    def test_ai_expression_disclosure_cannot_be_dropped(self):
        self.row["source_check"]["verification_level"] = "source-backed-concepts"
        self.row["source_check"]["ai_remaining"] = ["Synthetic adapted wording."]
        self.write_pack()
        self.exported()
        row = json.loads((self.output / "translations.jsonl").read_text())
        self.assertTrue(row["ai_expression_remaining"])
        row["ai_expression_remaining"] = False
        self.rewrite_rows([row])
        with self.assertRaisesRegex(ValueError, "AI expression"):
            data.validate(self.output)

    def test_wrong_client_identity_rejected(self):
        self.exported()
        row = json.loads((self.output / "translations.jsonl").read_text())
        row["identity"] = {"client_key": "a-different-slot"}
        self.rewrite_rows([row])
        with self.assertRaisesRegex(ValueError, "identity"):
            data.validate(self.output)

    def test_export_cannot_claim_runtime_or_automatic_admission(self):
        self.exported()
        path = self.output / "manifest.json"
        manifest = json.loads(path.read_text())
        manifest["automatic_admission"] = True
        path.write_text(json.dumps(manifest), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "automatic admission"):
            data.validate(self.output)

    def test_compact_manifest_is_not_an_approved_resolution_pack(self):
        import reviewed_resolution
        self.exported()
        directory = self.root / "pretend-resolution"
        batch = directory / "fixture"
        batch.mkdir(parents=True)
        path = batch / "pack.json"
        path.write_bytes((self.output / "manifest.json").read_bytes())
        review = {"review_kind": "independent-AI-resolution-review", "status": "PASS",
                  "blocking_findings": [], "pack_fingerprint": data.fingerprint(path)}
        (batch / "review.json").write_text(json.dumps(review), encoding="utf-8")
        with sqlite3.connect(":memory:") as connection, patch.object(
                reviewed_resolution, "official_contexts", return_value={}):
            with self.assertRaises(RuntimeError):
                reviewed_resolution.import_resolutions(connection, {}, "fixture", directory)

class CommittedMaintenanceDataTests(unittest.TestCase):
    def test_all_committed_version_exports_validate_without_original_evidence(self):
        root = Path(__file__).resolve().parents[2] / "maintenance-data"
        manifests = sorted(root.glob("*/manifest.json"))
        self.assertTrue(manifests, "Expected at least one public maintenance version")
        for path in manifests:
            with self.subTest(version=path.parent.name):
                result = data.validate(path.parent)
                self.assertGreater(result["records"], 0)
                self.assertFalse(result["automatic_admission"])

if __name__ == "__main__":
    unittest.main()
