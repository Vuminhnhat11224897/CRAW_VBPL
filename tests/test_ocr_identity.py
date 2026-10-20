import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from legal_crawler.v2 import ocr, reader
from legal_crawler.v2.build import Quarantine, read_text, resolve_dates
from legal_crawler.v2.ocr_identity import choose, header_fields, read_signature, signature_page_hint
from legal_crawler.v2.ocr_regions import RegionOCR, crop_pdf
from legal_crawler.v2.ocr_quality import page_issues

try:
    import pymupdf
except ImportError:
    pymupdf = None

GOOD_NUMBER = '153/2026/TT-BTC'
GOOD_DATE = '2026-10-12'
WRONG = ('BỘ TÀI CHÍNH\nSố: A53 /2026/TT-BTC\n'
         'Hà Nội, ngày 22 tháng 10 năm 2026\nTHÔNG TƯ\n'
         'Căn cứ Nghị định số 153/2026/NĐ-CP ngày 12 tháng 10 năm 2026;\n'
         'Điều 1. Nội dung phải được giữ nguyên sau khi sửa đúng phần đầu văn bản.')
RECORD = {'number': GOOD_NUMBER, 'issued_date': GOOD_DATE}


class IdentityParsingTest(unittest.TestCase):
    def test_own_header_cannot_be_verified_by_a_citation_or_gazette_date(self):
        fields = header_fields(WRONG)
        self.assertEqual([x['value'] for x in fields['number']], ['A53/2026/TT-BTC'])
        self.assertEqual([x['value'] for x in fields['issued_date']], ['2026-10-22'])
        self.assertEqual(choose('number', fields['number'], GOOD_NUMBER)['status'], 'conflict')
        text = 'Ngày ký: 2026-10-19\nCăn cứ Thông tư số: 153/2026/TT-BTC; Hà Nội, ngày 12 tháng 10 năm 2026'
        self.assertEqual(header_fields(text), {'number': [], 'issued_date': []})

    def test_no_fuzzy_digit_correction_or_implicit_reference(self):
        self.assertEqual(choose('number', [{'value': 'A53/2026/TT-BTC'}], GOOD_NUMBER)['value'], None)
        self.assertEqual(choose('number', [{'value': GOOD_NUMBER}], None)['status'], 'reference_missing')
        self.assertEqual(choose('number', [{'value': '1153/2026/TT-BTC'}], GOOD_NUMBER)['status'], 'conflict')

    def test_missing_more_than_five_percent_native_words_is_flagged(self):
        words = ['word' + str(i) for i in range(100)]
        self.assertNotIn('native_text_missing', page_issues(' '.join(words[:95]), ' '.join(words)))
        self.assertIn('native_text_missing', page_issues(' '.join(words[:94]), ' '.join(words)))
        self.assertIn('native_text_added', page_issues(' '.join(words + ['invented'] * 6), ' '.join(words)))

    def test_a_matching_candidate_does_not_clear_ambiguity_within_the_same_read(self):
        observations = [{'value': GOOD_NUMBER, 'origin': 'number'},
                        {'value': '154/2026/TT-BTC', 'origin': 'number'}]
        self.assertEqual(choose('number', observations, GOOD_NUMBER)['status'], 'ambiguous')

    def test_short_act_signature_uses_upper_fallback_and_rejects_instruction_echo(self):
        regional = Mock()
        regional.read.side_effect = [
            {'name': 'signature', 'text': 'Chỉ trả về nguyên văn chữ trong ảnh.'},
            {'name': 'signature_upper', 'text': 'KT. THỦ TƯỚNG\nPHÓ THỦ TƯỚNG\nNguyễn Văn An'},
        ]
        chosen, reads = read_signature(regional, Mock())
        self.assertTrue(chosen['usable'])
        self.assertEqual(chosen['name'], 'signature_upper')
        self.assertEqual(len(reads), 2)
        self.assertTrue(signature_page_hint('XÁC THỰC VĂN BẢN HỢP NHẤT\nKT. THỐNG ĐỐC'))

    def test_publishing_signature_is_not_canonical_or_an_effective_date_fallback(self):
        pages = [{'page': 1, 'items': [(.6, .1, .3, .05, 'Hà Nội, ngày 12 tháng 10 năm 2026'),
                                      (.6, .2, .3, .05, 'Thời gian ký: 19/10/2026'),
                                      (.1, .8, .3, .05, 'Thông tư này có hiệu lực kể từ ngày ký.')] }]
        identity = {'issued_date': {'status': 'source_agreement', 'value': GOOD_DATE,
                                   'evidence': {'quote': 'Hà Nội, ngày 12 tháng 10 năm 2026'}}, 'signing_date': None}
        with patch.dict(os.environ, {'DATE_FROM': ''}):
            result = resolve_dates(RECORD, {'issuance_date': GOOD_DATE, 'legal_type': 'Thông tư'}, pages, [], identity)
        self.assertIsNone(result['pdf']['sign'])
        self.assertIsNone(result['effective'])

    def test_effective_alias_uses_repaired_issue_date_instead_of_stale_ocr_boxes(self):
        pages = [{'page': 1, 'items': [(.6, .1, .3, .05, 'Hà Nội, ngày 22 tháng 10 năm 2026'),
                                      (.1, .8, .3, .05, 'Thông tư này có hiệu lực kể từ ngày ban hành.')] }]
        identity = {'issued_date': {'status': 'source_agreement', 'value': GOOD_DATE,
                                   'evidence': {'quote': 'Hà Nội, ngày 12 tháng 10 năm 2026'}}, 'signing_date': None}
        with patch.dict(os.environ, {'DATE_FROM': ''}):
            result = resolve_dates(RECORD, {'issuance_date': GOOD_DATE, 'legal_type': 'Thông tư'}, pages, [], identity)
        self.assertEqual(result['effective'], GOOD_DATE)


@unittest.skipUnless(pymupdf, 'PyMuPDF is required')
class RegionalReadTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'OCR_REGION_SCALE': '3', 'OCR_REVIEW_RETRY': 'yes'})
        self.env.start(); self.addCleanup(self.env.stop)
        self.pdf = self.root / 'scan.pdf'
        with pymupdf.open() as source:
            p = source.new_page(width=600, height=800)
            p.draw_rect(p.rect, color=(1, 1, 1), fill=(1, 1, 1))
            p.draw_rect((0, 0, 300, 400), color=(1, 0, 0), fill=(1, 0, 0))
            p.draw_rect((300, 0, 600, 400), color=(0, 0, 1), fill=(0, 0, 1))
            image = p.get_pixmap().tobytes('png')
        with pymupdf.open() as doc:
            doc.new_page(width=600, height=800).insert_image((0, 0, 600, 800), stream=image)
            doc.save(self.pdf)

    def test_crop_excludes_other_quadrant_and_all_hidden_native_text(self):
        out = self.root / 'crop.pdf'
        with pymupdf.open(self.pdf) as src:
            crop_pdf(src[0], (0, 0, .5, .5), out, scale=1)
        with pymupdf.open(out) as doc:
            self.assertEqual(tuple(doc[0].rect), (0, 0, 600, 800))
            self.assertEqual(doc[0].get_text(), '')
            pix = doc[0].get_pixmap()
            center = (pix.height // 2 * pix.width + pix.width // 2) * pix.n
            self.assertGreater(pix.samples[center], 240)
            self.assertLess(pix.samples[center + 2], 15)

    def test_crop_coordinates_follow_displayed_rotation(self):
        out = self.root / 'rotated-crop.pdf'
        with pymupdf.open(self.pdf) as src:
            src[0].set_rotation(90)
            page = src[0]
            pix = page.get_pixmap()
            center = (pix.height // 4 * pix.width + pix.width // 4) * pix.n
            expected = pix.samples[center:center + 3]
            crop_pdf(page, (0, 0, .5, .5), out, scale=1)
        with pymupdf.open(out) as doc:
            pix = doc[0].get_pixmap()
            center = (pix.height // 2 * pix.width + pix.width // 2) * pix.n
            self.assertEqual(pix.samples[center:center + 3], expected)

    def test_region_cache_reused_and_invalidated_by_scale_and_content(self):
        with pymupdf.open(self.pdf) as doc, patch.object(ocr, 'ocr_text', return_value=('Số: 153/2026/TT-BTC', {})) as call:
            a = RegionOCR(self.root / 'cache', 'digest-one').read(doc[0], 'number')
            b = RegionOCR(self.root / 'cache', 'digest-one').read(doc[0], 'number')
            self.assertTrue(b['cached']); self.assertEqual(call.call_count, 1)
            c = RegionOCR(self.root / 'cache', 'digest-one', scale=4).read(doc[0], 'number')
            d = RegionOCR(self.root / 'cache', 'digest-two').read(doc[0], 'number')
        self.assertEqual(call.call_count, 3)
        self.assertEqual(a['image_file'], b['image_file'])
        self.assertNotEqual(a['image_file'], c['image_file'])
        self.assertNotEqual(a['image_file'], d['image_file'])

    def test_tighter_number_crop_and_date_quarter_repair_only_verified_header(self):
        responses = [(WRONG, {}), ('Số: A53/2026/TT-BTC', {}),
                     ('Số: 153/2026/TT-BTC', {}), ('Hà Nội, ngày 12 tháng 10 năm 2026', {})]
        work = self.root / 'work'
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', side_effect=responses), \
             patch.object(ocr, 'ocr_boxes', return_value=[{'page': 1, 'items': []}]):
            text, report, _ = reader.read(self.pdf, work, record=RECORD)
        self.assertEqual(report['identity']['status'], 'source_agreement')
        self.assertEqual(report['review_pages'], [])
        self.assertEqual([r['name'] for r in report['identity']['reads']], ['number', 'number_line', 'issue_date'])
        self.assertIn('Số: 153/2026/TT-BTC', text)
        self.assertIn('ngày 12 tháng 10 năm 2026', text)
        self.assertNotIn('ngày 22 tháng 10 năm 2026', text)
        self.assertIn('Căn cứ Nghị định số 153/2026/NĐ-CP ngày 12 tháng 10 năm 2026', text)
        self.assertEqual(len(report['header_repairs']), 2)
        self.assertIn('A53', Path(report['page_reports'][0]['text_file']).read_text())
        with patch.object(ocr, 'health', side_effect=AssertionError('cache')), \
             patch.object(ocr, 'ocr_text', side_effect=AssertionError('cache')):
            again, cached_report, _ = read_text(self.pdf, work, record=RECORD)
        self.assertEqual(text, again)
        self.assertTrue(all(x['cached'] for x in cached_report['identity']['reads']))

    def test_crop_failure_blocks_identity_and_preserves_whole_page(self):
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', side_effect=[(WRONG, {}), ocr.OCRUnavailable('offline'), ocr.OCRUnavailable('offline')]), \
             patch.object(ocr, 'ocr_boxes', return_value=[{'page': 1, 'items': []}]):
            with self.assertRaises(Quarantine) as caught:
                read_text(self.pdf, self.root / 'work', record=RECORD)
        self.assertEqual(caught.exception.reason, 'ocr_identity_unverified')
        report = json.loads((self.root / 'work' / 'ocr-review.json').read_text())
        self.assertEqual(report['identity']['number']['value'], None)
        self.assertTrue(Path(report['page_reports'][0]['text_file']).is_file())

    def test_attachment_citation_cannot_be_promoted_to_primary_identity(self):
        text = 'Phụ lục I\n(Ban hành kèm theo Thông tư số 153/2026/TT-BTC ngày 12 tháng 10 năm 2026)\n' + 'Nội dung mẫu. ' * 10
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(text, {})) as call, \
             patch.object(ocr, 'ocr_boxes', return_value=[{'page': 1, 'items': []}]):
            _, report, _ = reader.read(self.pdf, self.root / 'work', record=RECORD)
        self.assertIn('primary_act_missing', report['identity']['issues'])
        self.assertIsNone(report['identity']['number']['value'])
        self.assertEqual(call.call_count, 1)

    def test_requested_signing_date_is_blocked_when_no_signature_date_can_be_verified(self):
        whole = WRONG.replace('A53', '153').replace('ngày 22 tháng', 'ngày 12 tháng')
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(whole, {})), \
             patch.object(ocr, 'ocr_boxes', return_value=[{'page': 1, 'items': []}]):
            with self.assertRaises(Quarantine) as caught:
                read_text(self.pdf, self.root / 'work', record={**RECORD, 'signing_date': GOOD_DATE})
        self.assertEqual(caught.exception.reason, 'ocr_identity_unverified')

    def test_changed_web_metadata_rechecks_cached_reads_and_never_injects_the_new_date(self):
        whole = WRONG.replace('A53', '153').replace('ngày 22 tháng', 'ngày 12 tháng')
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(whole, {})), \
             patch.object(ocr, 'ocr_boxes', return_value=[{'page': 1, 'items': []}]):
            reader.read(self.pdf, self.root / 'work', record=RECORD)
            text, report, _ = reader.read(self.pdf, self.root / 'work', record={**RECORD, 'issued_date': '2026-10-13'})
        self.assertIsNone(report['identity']['issued_date']['value'])
        self.assertEqual(report['identity']['status'], 'needs_review')
        self.assertNotIn('ngày 13 tháng', text)


if __name__ == '__main__':
    unittest.main()
