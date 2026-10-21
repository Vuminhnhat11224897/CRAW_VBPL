import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

from legal_crawler.v2.contracts import same_authority
from legal_crawler.v2.reviewed_pages import reviewed_callback
from legal_crawler.v2.reviewed_text import text_hash
from legal_crawler.v2.structure import point_sequence_issues, parse
from legal_crawler.v2.reviewed_metadata import load_review


class LatestReviewTest(unittest.TestCase):
    def test_reviewed_quote_boundary_recovers_only_named_source_heading(self):
        text = ('Điều 1. Sửa đổi\n“Điều 5. Phần trích dẫn chưa đóng do OCR\n1. Nội dung.\n'
                'Điều 2. Thi hành\n1. Thực hiện.\nNơi nhận:\n- Cơ quan nhận;')
        before, _ = parse(text)
        after, _ = parse(text, ['Điều 2. Thi hành'])
        self.assertEqual(len(before['dieu_luat']), 1)
        self.assertEqual([a['so_dieu'] for a in after['dieu_luat']], [1, 2])
        self.assertIn('Phần trích dẫn', after['dieu_luat'][0]['noi_dung'])

    def test_metadata_review_requires_exact_text_and_heading_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            image = folder / 'image.png'
            image.write_bytes(b'PDF image')
            text = 'Điều 1. Nội dung'
            review = dict(pdf_sha256='pdf', text_sha256=text_hash(text), reviewer='visual', reason='PDF',
                          images=[dict(path=str(image), sha256=hashlib.sha256(image.read_bytes()).hexdigest())],
                          quote_resets=[text])
            path = folder / 'reviewed-metadata.json'
            path.write_text(json.dumps(review))
            self.assertEqual(load_review(folder, 'pdf', text)['quote_resets'], [text])
            for digest, source in [('other', text), ('pdf', text+' changed')]:
                with self.assertRaises(ValueError): load_review(folder, digest, source)
            review['quote_resets'] = ['Điều 2. Missing']
            path.write_text(json.dumps(review))
            with self.assertRaises(ValueError): load_review(folder, 'pdf', text)

    def test_legacy_locality_alias_does_not_equate_different_bodies(self):
        self.assertTrue(same_authority('UBND Tỉnh Thái Nguyên', 'Tỉnh Thái Nguyên'))
        self.assertTrue(same_authority('CT UBND Tỉnh Sơn La', 'Tỉnh Sơn La'))
        self.assertFalse(same_authority('UBND Tỉnh Thái Nguyên', 'Tỉnh Sơn La'))
        self.assertFalse(same_authority('UBND Tỉnh Thái Nguyên', 'HĐND Tỉnh Thái Nguyên'))

    def test_article_reference_does_not_reset_an_actual_point_list(self):
        text = ('Điều 1. Nội dung\n1. Các trường hợp:\na) Trường hợp một;\n'
                'b) Theo quy định tại\nĐiều 22 Nghị định số 168/2025/NĐ-CP;\nc) Trường hợp ba;\nd) Trường hợp bốn.')
        self.assertEqual(point_sequence_issues(text), [])
        self.assertTrue(point_sequence_issues(text.replace('c) Trường hợp ba;\n', '')))

    def test_review_is_bound_to_pdf_raw_text_and_evidence(self):
        with tempfile.TemporaryDirectory() as temp:
            folder = Path(temp)
            image = folder / 'page.png'
            image.write_bytes(b'review evidence')
            raw = ('Nội dung dài có trong bản gốc. ' * 5 + '\n\n') * 2
            corrected = raw + '\nChữ đã đối chiếu.'
            entry = dict(page=2, raw_text_sha256=text_hash(raw), text=corrected,
                         text_sha256=text_hash(corrected), image=str(image),
                         image_sha256=hashlib.sha256(image.read_bytes()).hexdigest(),
                         accepted_issues=['repeated_paragraph', 'repeated_line'], reason='PDF repeats this paragraph')
            review = dict(pdf_sha256='pdf', reviewer='visual review', pages=[entry])
            path = folder / 'reviewed-pages.json'
            def save(): path.write_text(json.dumps(review))
            save()
            page = Mock()
            page.get_text.return_value = ''
            callback = lambda *a: (raw, {'issues': ['repeated_paragraph', 'repeated_line']}, [])
            text, detail, _ = reviewed_callback(callback, folder, 'pdf')(page, 2, [], False)
            self.assertEqual(text, corrected)
            self.assertEqual(detail['issues'], [])
            self.assertIn('manual_review', detail)
            for key, value in [('raw_text_sha256', 'wrong'), ('text_sha256', 'wrong'), ('image_sha256', 'wrong')]:
                original = entry[key]
                entry[key] = value
                save()
                with self.assertRaises(ValueError): reviewed_callback(callback, folder, 'pdf')(page, 2, [], False)
                entry[key] = original
            review['pdf_sha256'] = 'different'
            save()
            with self.assertRaises(ValueError): reviewed_callback(callback, folder, 'pdf')

    def test_unreviewed_page_keeps_all_quality_flags(self):
        with tempfile.TemporaryDirectory() as temp:
            callback = Mock(return_value=('raw', {'issues': ['native_numbers_missing']}, []))
            actual = reviewed_callback(callback, temp, 'pdf')(Mock(), 2, [], False)
            self.assertEqual(actual[1]['issues'], ['native_numbers_missing'])
