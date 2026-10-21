import copy
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from legal_crawler.v2 import build, extract, llm


TEXT = ('BỘ TÀI CHÍNH\nSố: 153/2026/TT-BTC\n'
        'Hà Nội, ngày 12 tháng 10 năm 2026\nTHÔNG TƯ\nHướng dẫn chế độ tài chính\n'
        'Điều 1. Phạm vi\n1. Quy định chế độ tài chính.\nNơi nhận:\n- Nguyễn Văn Nhận;')
SIGNATURE = 'KT. BỘ TRƯỞNG\nTHỨ TRƯỞNG\nNguyễn Đức Chi'
CATALOG = {'action': ['other', 'huongdan'], 'legal_type': ['Thông tư'],
           'legal_sectors': [], 'issuing_authority': ['Bộ Tài chính']}


def payload(signature=True):
    return {'metadata': {'document_number': '153/2026/TT-BTC',
            'title': 'Thông tư Hướng dẫn chế độ tài chính', 'legal_type': 'Thông tư',
            'legal_sectors': [], 'issuing_authority': 'Bộ Tài chính',
            'issuance_date': '2026-10-12',
            'signers': SIGNATURE.splitlines() if signature else [],
            'tom_tat': 'Quy định chế độ tài chính.'},
            'evidence': {'title': ['THÔNG TƯ\nHướng dẫn chế độ tài chính'],
            'document_number': ['Số: 153/2026/TT-BTC'],
            'issuance_date': ['Hà Nội, ngày 12 tháng 10 năm 2026'],
            'issuing_authority': ['BỘ TÀI CHÍNH'],
            'signers': [SIGNATURE] if signature else [], 'legal_sectors': []}}


def primary_signature():
    return {'status': 'source_agreement', 'signature': {'usable': True, 'page': 8,
            'bounds': [.45, 0, 1, 1],
            'text': 'KT. BỘ TRƯỞNG\nTHỦ TRƯỞNG\nNguyễn Đức Chi'}}


POSITIONS = [{'page': 8, 'items': [
    (.13, .29, .77, .02, 'Nội dung toàn chiều ngang, không thuộc riêng cột chữ ký'),
    (.63, .43, .18, .02, 'KT. BỘ TRƯỞNG'),
    (.64, .45, .16, .02, 'THỨ TRƯỞNG'),
    (.63, .55, .18, .02, 'Nguyễn Đức Chi'),
]}]


class SignatureMetadataTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.folder = Path(self.temp.name)
        self.signature = extract.signature_for_metadata(primary_signature(), POSITIONS)

    def test_positioned_primary_signature_recovers_lines_omitted_by_whole_page_ocr(self):
        self.assertNotIn('Nguyễn Đức Chi', TEXT)
        self.assertEqual(self.signature['text'], SIGNATURE)
        self.assertEqual(self.signature['method'], 'positioned_text')
        self.assertIsNone(extract.signature_for_metadata(
            {**primary_signature(), 'status': 'needs_review'}, POSITIONS))
        identity = primary_signature()
        identity['signature']['usable'] = False
        self.assertIsNone(extract.signature_for_metadata(identity, POSITIONS))

    def test_regional_evidence_reaches_model_and_invalidates_old_empty_signer_cache(self):
        (self.folder / 'metadata_response_v6.json').write_text(json.dumps(payload(False)))
        def respond(prompt, source, *args, **kwargs):
            supplied = json.loads(source)
            self.assertEqual(supplied['signature_region']['text'], SIGNATURE)
            self.assertEqual(supplied['signature_region']['page'], 8)
            return json.dumps(payload()), {}
        with patch.object(llm, 'call_llm', side_effect=respond) as call:
            meta, evidence, _ = extract.extract_metadata(TEXT, CATALOG, self.folder, signature=self.signature)
            again, _, _ = extract.extract_metadata(TEXT, CATALOG, self.folder, signature=self.signature)
        self.assertEqual(call.call_count, 1)
        self.assertEqual(meta['signers'], 'KT. BỘ TRƯỞNG, THỨ TRƯỞNG, Nguyễn Đức Chi')
        self.assertEqual(again['signers'], meta['signers'])
        self.assertEqual(evidence['signers'], [SIGNATURE])
        saved = json.loads((self.folder / 'signature-metadata-source.json').read_text())
        self.assertEqual(saved['text'], SIGNATURE)

    def test_changed_signature_evidence_does_not_reuse_previous_signer(self):
        with patch.object(llm, 'call_llm', return_value=(json.dumps(payload()), {})):
            extract.extract_metadata(TEXT, CATALOG, self.folder, signature=self.signature)
        changed = {**self.signature, 'text': SIGNATURE.replace('Nguyễn Đức Chi', 'Nguyễn Văn Hai')}
        response = payload()
        response['metadata']['signers'][-1] = 'Nguyễn Văn Hai'
        response['evidence']['signers'] = [changed['text']]
        with patch.object(llm, 'call_llm', return_value=(json.dumps(response), {})) as call:
            meta, _, _ = extract.extract_metadata(TEXT, CATALOG, self.folder, signature=changed)
        self.assertEqual(call.call_count, 1)
        self.assertIn('Nguyễn Văn Hai', meta['signers'])
        self.assertNotIn('Nguyễn Đức Chi', meta['signers'])

    def test_recipient_cannot_become_a_signer_when_missing_from_the_primary_region(self):
        response = payload()
        response['metadata']['signers'] = ['Nguyễn Văn Nhận']
        response['evidence']['signers'] = ['Nguyễn Văn Nhận']
        with patch.object(llm, 'call_llm', return_value=(json.dumps(response), {})), \
             patch.object(llm.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'primary signature region'):
                extract.extract_metadata(TEXT, CATALOG, self.folder, signature=self.signature)

    def test_regional_text_cannot_supply_an_issuer_absent_from_the_main_document(self):
        response = payload()
        response['metadata']['issuing_authority'] = 'Bộ Tư pháp'
        response['evidence']['issuing_authority'] = ['Bộ Tư pháp']
        signature = {**self.signature, 'text': SIGNATURE + '\nBộ Tư pháp'}
        with patch.object(llm, 'call_llm', return_value=(json.dumps(response), {})), \
             patch.object(llm.time, 'sleep'):
            with self.assertRaisesRegex(ValueError, 'cannot supply evidence for issuing_authority'):
                extract.extract_metadata(TEXT, CATALOG, self.folder, signature=signature)

    def test_prepare_and_chunks_receive_signature_without_mutating_full_content(self):
        report = {'identity': primary_signature(), 'method': 'ocr'}
        record = {'number': '153/2026/TT-BTC', 'issued_date': '2026-10-12',
                  'document_type': 'Thông tư', 'issuer': 'Bộ Tài chính',
                  'source_url': 'https://vanban.chinhphu.vn/example'}
        original_positions = copy.deepcopy(POSITIONS)
        with patch.object(build, 'read_record', return_value=(None, 'sha', self.folder, TEXT, report, POSITIONS)), \
             patch.object(llm, 'call_llm', return_value=(json.dumps(payload()), {})), \
             patch.dict(os.environ, {'DATE_FROM': '', 'LEGAL_SECTORS_MODE': 'empty'}):
            prepared = build.prepare(record, self.folder, CATALOG, self.folder)
        self.assertEqual(prepared['text'], TEXT)
        self.assertEqual(POSITIONS, original_positions)
        self.assertNotIn('signers_missing', prepared['warnings'])
        assembled = build.assemble(prepared, '10100000')
        self.assertIn('Nguyễn Đức Chi', assembled['document']['signers'])
        self.assertTrue(all(c['_source']['signers'] == assembled['document']['signers']
                            for c in assembled['chunks']))

    def test_standalone_signature_columns_are_not_recipients(self):
        text = TEXT + '\nTHỨ TRƯỞNG\n- Văn phòng Thứ trưởng;\nNguyễn Đức Chi'
        report = {'identity': primary_signature(), 'method': 'ocr'}
        record = {'number': '153/2026/TT-BTC', 'issued_date': '2026-10-12',
                  'document_type': 'Thông tư', 'issuer': 'Bộ Tài chính', 'source_url': 'https://example.test'}
        with patch.object(build, 'read_record', return_value=(None, 'sha', self.folder, text, report, POSITIONS)), \
             patch.object(llm, 'call_llm', return_value=(json.dumps(payload()), {})), \
             patch.dict(os.environ, {'DATE_FROM': '', 'LEGAL_SECTORS_MODE': 'empty'}):
            prepared = build.prepare(record, self.folder, CATALOG, self.folder)
        self.assertEqual(prepared['text'], text)
        self.assertEqual(prepared['parsed']['noi_nhan'], ['Nguyễn Văn Nhận', 'Văn phòng Thứ trưởng'])


if __name__ == '__main__':
    unittest.main()
