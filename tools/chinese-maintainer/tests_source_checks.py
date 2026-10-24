"""Evidence integrity and importer admission tests; no game or registry writes."""
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / 'tools/chinese-maintainer'))
import source_check
FIXTURE_BASE = Path(__file__).resolve().parent
FIXTURE = FIXTURE_BASE / 'fixtures/source-fixture.txt'
import reviewed_resolution
from common import fingerprint


def row(level='exact-full-text'):
    quote = '中文对应规则全文。'
    return {'layer': 'card', 'key': '123', 'english': 'Draw a card.',
            'chinese': '抓一张牌。', 'field_use': ['test'], 'identity': {'name': 'Fixture'},
            'disposition': 'reviewed-localization',
            'source_check': {'verification_level': level,
                             'audit_note': 'Test fixture: exact English, no rule changes; translation adapted to the slot.',
                             'english_alignment': 'Fixture exact English matches current slot.',
                             'adaptation_note': 'No changed rule meaning; fixture-only quotation.',
                             'ai_remaining': ['Fixture AI expression remains for concept-only rows.'] if level != 'exact-full-text' else [],
                             'sources': [{'url': 'https://example.org/card/123',
                                          'source_kind': 'official-print', 'language': 'zh-Hans-CN',
                                          'excerpt': quote, 'excerpt_sha256': source_check.excerpt_hash(quote),
                                          'retrieved_at': '2026-10-24T00:00:00Z',
                                          'snapshot': {'path': 'fixtures/source-fixture.txt', 'sha256': hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), 'bytes': FIXTURE.stat().st_size},
                                          'match_note': 'Fixture card identity and face match; no English differences.'}]}}


def pack(records):
    return {'schema': 'MTGA_SOURCE_CHECKED_RESOLUTION_V2', 'source_version': 'test',
            'human_acceptance': False, 'runtime_acceptance': False, 'records': records}


class SourceEvidenceTests(unittest.TestCase):
    def test_full_text_and_concept_categories_stay_distinct(self):
        records = [row(), row('source-backed-concepts')]
        records[1]['key'] = '124'
        result = source_check.validate_pack(pack(records), base_dir=FIXTURE_BASE)
        self.assertEqual(result['verification_levels'], {'exact-full-text': 1, 'source-backed-concepts': 1})
        self.assertFalse(result['runtime_acceptance'])

    def test_cannot_claim_full_text_without_sources(self):
        r = row(); r['source_check']['sources'] = []
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_tampered_quotation_fails(self):
        r = row(); r['source_check']['sources'][0]['excerpt'] += 'changed'
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_raw_snapshot_hash_and_size_cannot_be_forged(self):
        for field, value in [('sha256', '0'*64), ('bytes', 1)]:
            r = row(); r['source_check']['sources'][0]['snapshot'][field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, 'snapshot fingerprint'):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_snapshot_path_escape_missing_and_symlink_fail(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root/'source-link.txt').symlink_to(FIXTURE)
            for value in [str(FIXTURE), '../source-fixture.txt', 'source-link.txt', 'missing.txt']:
                r = row(); r['source_check']['sources'][0]['snapshot']['path'] = value
                with self.subTest(path=value), self.assertRaises(ValueError):
                    source_check.validate_record(r, base_dir=root)

    def test_only_dates_and_non_utc_timestamps_fail(self):
        for value in ['2026-10-24', '2026-10-24T08:00:00+08:00', '2026-10-24T00:00:00']:
            r = row(); r['source_check']['sources'][0]['retrieved_at'] = value
            with self.subTest(timestamp=value), self.assertRaises(ValueError):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_recorded_time_can_preserve_unknown_network_time_without_fabricating_retrieval(self):
        r = row(); source = r['source_check']['sources'][0]
        source.pop('retrieved_at')
        source.update(recorded_at='2026-10-24T00:00:00Z', timestamp_basis='snapshot-write-time',
                      capture_time_basis='Local snapshot write time; network retrieval timestamp unavailable')
        self.assertEqual(source_check.validate_record(r, base_dir=FIXTURE_BASE), 'exact-full-text')
        self.assertNotIn('retrieved_at', source)
        for field in ['recorded_at', 'timestamp_basis', 'capture_time_basis']:
            bad = row(); bad['source_check']['sources'][0] = dict(source)
            del bad['source_check']['sources'][0][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                source_check.validate_record(bad, base_dir=FIXTURE_BASE)

    def test_invalid_retrieval_time_cannot_fall_back_to_recorded_time(self):
        r = row(); source = r['source_check']['sources'][0]
        source.update(retrieved_at='not-a-date', recorded_at='2026-10-24T00:00:00Z',
                      timestamp_basis='snapshot-write-time', capture_time_basis='Local write time')
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_ai_remaining_and_explicit_alignment_required(self):
        for field in ['english_alignment', 'adaptation_note']:
            r = row(); del r['source_check'][field]
            with self.subTest(field=field), self.assertRaises(ValueError):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)
        r = row('source-backed-concepts'); r['source_check']['ai_remaining'] = []
        with self.assertRaisesRegex(ValueError, 'remaining AI'):
            source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_unstructured_lookup_attempts_fail(self):
        for value in [True, 'searched online', [{}], [{'url': 'https://example.org/'}]]:
            r = row('source-unavailable'); r['source_check']['sources'] = []
            r['source_check']['lookup_attempts'] = value
            with self.subTest(attempts=value), self.assertRaises(ValueError):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_source_unavailable_needs_attempts(self):
        r = row('source-unavailable'); r['source_check']['sources'] = []
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_missing_sources_is_explicit_gap_not_exact_text(self):
        r = row('source-unavailable'); r['source_check']['sources'] = []
        r['source_check']['lookup_attempts'] = [{'url': 'https://example.org/search?q=Fixture',
                                               'retrieved_at': '2026-10-24T00:00:00Z',
                                               'query': 'Fixture exact card identity', 'outcome': 'No corresponding full text'}]
        self.assertEqual(source_check.validate_record(r, base_dir=FIXTURE_BASE), 'source-unavailable')

    def test_empty_source_can_only_preserve_exact_empty_value(self):
        r = row('intentional-preserved'); r.update(english='', chinese='', disposition='empty-source-preserved')
        r['source_check']['sources'] = []
        self.assertEqual(source_check.validate_record(r, base_dir=FIXTURE_BASE), 'intentional-preserved')
        r['chinese'] = '添加文字'
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_ordinary_translation_cannot_borrow_preserved_level(self):
        r = row('intentional-preserved')
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_full_text_cannot_disclose_unmatched_ai_segments(self):
        r = row(); r['source_check']['ai_remaining'] = ['This entire unmatched sentence is AI translated.']
        with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_online_ai_translations_cannot_be_laundered_as_reliable_sources(self):
        for level in ['exact-full-text', 'source-backed-concepts']:
            for declared in ['gpt', 'AI', 'claude', 'gemini', 'chatgpt']:
                r = row(level); r['source_check']['sources'][0]['source_declared'] = declared
                with self.subTest(level=level, declared=declared), self.assertRaisesRegex(ValueError, 'AI-generated website'):
                    source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_rejected_sources_cannot_establish_a_source_match(self):
        for level in ['exact-full-text', 'source-backed-concepts']:
            r = row(level); r['source_check']['sources'][0]['adopted'] = False
            with self.subTest(level=level), self.assertRaises(ValueError):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_preserved_dispositions_cannot_hide_ordinary_english(self):
        for disposition in ['empty-source-preserved', 'technical-identifier-preserved']:
            r = row('intentional-preserved')
            r.update(english='Draw a card.', chinese='Draw a card.', disposition=disposition)
            r['source_check']['sources'] = []
            with self.subTest(disposition=disposition), self.assertRaises(ValueError):
                source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_missing_url_language_date_or_matching_note_fails(self):
        for field in ['url', 'language', 'retrieved_at', 'match_note']:
            with self.subTest(field=field):
                r = row(); del r['source_check']['sources'][0][field]
                with self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)

    def test_duplicate_slots_cannot_inflate_coverage(self):
        with self.assertRaises(ValueError): source_check.validate_pack(pack([row(), row()]), base_dir=FIXTURE_BASE)

    def test_unknown_schema_enum_cannot_look_approved(self):
        for level in ['exact-text', 'pending', 'AI-reviewed']:
            r = row(level)
            with self.subTest(level=level), self.assertRaises(ValueError): source_check.validate_record(r, base_dir=FIXTURE_BASE)


class ImporterReviewGateTests(unittest.TestCase):
    def run_import(self, source_reviewed, evidence=None, matching_context=False, schema=None):
        # Empty real context is enough: V2 source review must fail before translations are admitted.
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve(); one = root/'test'; one.mkdir()
            (one/'fixtures').mkdir(); (one/'fixtures/source-fixture.txt').write_bytes(FIXTURE.read_bytes())
            data = pack([evidence or row()])
            if schema: data['schema'] = schema
            p = one/'pack.json'; p.write_text(json.dumps(data))
            review = {'review_kind': 'independent-AI-resolution-review', 'status': 'PASS',
                      'blocking_findings': [], 'pack_fingerprint': fingerprint(p),
                      'format_pairs_fingerprint': fingerprint(ROOT/'tools/chinese-maintainer/source-format-reviewed-pairs.json')}
            if source_reviewed is not None: review['source_checks_reviewed'] = source_reviewed
            (one/'review.json').write_text(json.dumps(review))
            record = evidence or row()
            contexts = {('card', '123'): {k: record[k] for k in ('english', 'field_use', 'identity')}} if matching_context else {}
            with patch.object(reviewed_resolution, 'official_contexts', return_value=contexts), \
                 patch('registry.bind_contexts'), patch('registry.source') as register, \
                 patch('provenance.append'), patch('registry.add') as add:
                with sqlite3.connect(':memory:') as c:
                    count = reviewed_resolution.import_resolutions(c, {}, 'test', root)
                    self.assertEqual(add.call_count, 1 if matching_context else 0)
                    self.assertEqual(register.call_count, 3)  # pack, review, actual snapshot
                    self.assertEqual(register.call_args_list[-1].args[2], one/'fixtures/source-fixture.txt')
                    return count

    def test_hash_review_without_source_review_cannot_admit_v2(self):
        for value in [None, False, 'true', 1]:
            with self.subTest(value=value), self.assertRaisesRegex(RuntimeError, 'independent source/adaptation review'): self.run_import(value)

    def test_new_v1_pack_cannot_bypass_source_checks(self):
        with self.assertRaisesRegex(RuntimeError, 'per-slot source-checked V2'):
            self.run_import(True, schema='MTGA_REVIEWED_RESOLUTION_V1')

    def test_valid_review_and_evidence_pass_gate_but_missing_context_is_not_admitted(self):
        self.assertEqual(self.run_import(True), 0)

    def test_reviewed_v2_matching_context_admits_translation_and_registers_snapshot(self):
        self.assertEqual(self.run_import(True, matching_context=True), 1)

    def test_source_review_boolean_does_not_bypass_bad_evidence(self):
        r = row(); r['source_check']['sources'] = []
        with self.assertRaisesRegex(ValueError, 'needs evidence'): self.run_import(True, r)


if __name__ == '__main__': unittest.main()
