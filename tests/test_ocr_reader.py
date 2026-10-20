import concurrent.futures
import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from unittest.mock import MagicMock, patch

try:
    import pymupdf
except ImportError:
    pymupdf = None

from legal_crawler.v2 import ocr, reader
from legal_crawler.v2.build import Quarantine, read_text
from legal_crawler.v2.ocr_quality import comparison_text, page_issues
from legal_crawler.v2.text import NeedsOCR, read_document

BODY = 'This is readable document content with enough words to test reliable page handling. '
OCR_BODY = 'Recognized scan content. This complete sentence preserves the text of the scanned page for testing.'
POSITIONS = [{'page': 1, 'items': [(.5, .1, .3, .03, 'ngày 01 tháng 10 năm 2026')]}]


@unittest.skipUnless(pymupdf, 'PyMuPDF is required for real PDF routing tests')
class ReaderTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = patch.dict(os.environ, {'OCR_REVIEW_RETRY': 'yes', 'OCR_PDF_SCALE': '2', 'OCR_REVIEW_SCALE': '3'})
        self.env.start(); self.addCleanup(self.env.stop)

    def pdf(self, kinds=('native', 'scan', 'native')):
        path = self.root / 'document.pdf'
        with pymupdf.open() as scanned:
            p = scanned.new_page()
            p.insert_textbox((60, 100, 530, 400), 'Scanned image content. ' * 15)
            image = p.get_pixmap().tobytes('png')
        with pymupdf.open() as doc:
            for i, kind in enumerate(kinds):
                p = doc.new_page()
                if kind == 'native':
                    p.insert_textbox((60, 100, 530, 400), f'Native page {i+1}. ' + BODY * 5)
                elif kind == 'scan':
                    p.insert_image(p.rect, stream=image)
            doc.save(path)
        return path

    def test_native_only_never_calls_ocr_and_keeps_corner_positions(self):
        path = self.pdf(('native', 'native'))
        with patch.object(ocr, 'health', side_effect=AssertionError('no network')):
            text, report, pages = reader.read(path, self.root / 'work')
        self.assertEqual(report['method'], 'text_layer')
        self.assertEqual(report['ocr_pages'], [])
        self.assertIn('Native page 1', text)
        self.assertIn('Native page 2', text)
        self.assertEqual([p['page'] for p in pages], [1, 2])

    def test_bad_ocr_configuration_does_not_affect_native_documents(self):
        path = self.pdf(('native',))
        with patch.dict(os.environ, {'OCR_PDF_SCALE': 'bad'}), \
             patch.object(ocr, 'health', side_effect=AssertionError('native needs no OCR')):
            _, report, _ = reader.read(path, self.root / 'work')
        self.assertEqual(report['method'], 'text_layer')

    def test_bad_ocr_configuration_does_not_permanently_reject_a_scan(self):
        from legal_crawler.v2.export import PERMANENT
        path = self.pdf(('scan',))
        with patch.dict(os.environ, {'OCR_PDF_SCALE': 'bad'}):
            with self.assertRaises(Quarantine) as caught:
                read_text(path, self.root / 'work')
        self.assertEqual(caught.exception.reason, 'ocr_config_invalid')
        self.assertNotIn(caught.exception.reason, PERMANENT)

    def test_mixed_pdf_reads_only_the_scan_and_does_not_box_middle_pages(self):
        path = self.pdf()
        with self.assertRaises(NeedsOCR):
            read_document(path)
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})) as raw, \
             patch.object(ocr, 'ocr_boxes', side_effect=AssertionError('middle page needs no dates')):
            text, report, positions = reader.read(path, self.root / 'work')
        self.assertEqual(raw.call_count, 1)
        self.assertEqual(report['ocr_pages'], [2])
        self.assertEqual(report['method'], 'mixed')
        self.assertEqual(report['review_pages'], [])
        self.assertLess(text.index('Native page 1'), text.index(OCR_BODY))
        self.assertLess(text.index(OCR_BODY), text.index('Native page 3'))
        self.assertEqual([p['page'] for p in positions], [1, 3])

    def test_all_scans_get_boxes_only_at_corners_with_original_page_numbers(self):
        path = self.pdf(('scan', 'scan', 'scan'))
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})) as raw, \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS) as boxes:
            _, report, positions = reader.read(path, self.root / 'work')
        self.assertEqual(raw.call_count, 3)
        self.assertEqual(boxes.call_count, 2)
        self.assertEqual([p['page'] for p in positions], [1, 3])
        self.assertEqual(report['method'], 'ocr')
        self.assertEqual(report['ocr_pages'], [1, 2, 3])

    def test_cache_survives_new_reader_and_is_rechecked_against_source_record(self):
        path = self.pdf(('scan',))
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})), \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            first, _, _ = reader.read(path, self.root / 'work')
        with patch.object(ocr, 'health', side_effect=AssertionError('cached')), \
             patch.object(ocr, 'ocr_text', side_effect=AssertionError('cached')):
            second, report, _ = reader.read(path, self.root / 'work')
        self.assertEqual(first, second)
        self.assertTrue(report['page_reports'][0]['cached'])
        with patch.dict(os.environ, {'OCR_REVIEW_RETRY': 'no'}), patch.object(ocr, 'health'), \
             patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})), patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            _, report, _ = reader.read(path, self.root / 'work', record={'number': '123/2026/QD'})
        self.assertIn('source_number_missing', report['page_reports'][0]['issues'])

    def test_changed_input_does_not_reuse_cached_page_text(self):
        path = self.pdf(('scan',))
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})) as raw, \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            _, before, _ = reader.read(path, self.root / 'work')
            updated = self.root / 'changed.pdf'
            with pymupdf.open(path) as doc:
                doc.set_metadata({'title': 'Different PDF content'})
                doc.save(updated)
            _, after, _ = reader.read(updated, self.root / 'work')
        self.assertEqual(raw.call_count, 2)
        self.assertNotEqual(before['evidence_dir'], after['evidence_dir'])

    def test_cached_text_is_not_implicitly_approved_for_changed_source_metadata(self):
        path = self.pdf(('scan',))
        with patch.dict(os.environ, {'OCR_REVIEW_RETRY': 'no'}):
            with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})), \
                 patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
                reader.read(path, self.root / 'work')
            with patch.object(ocr, 'health', side_effect=AssertionError('cached read')), \
                 patch.object(ocr, 'ocr_text', side_effect=AssertionError('cached text')):
                _, report, _ = reader.read(path, self.root / 'work', record={'number': '123/2026/QD'})
        self.assertTrue(report['page_reports'][0]['cached'])
        self.assertEqual(report['review_pages'], [1])
        self.assertIn('source_number_missing', report['page_reports'][0]['issues'])

    def test_two_documents_can_wait_on_ocr_without_holding_the_pdf_lock(self):
        first = self.pdf().rename(self.root / 'first.pdf')
        second = self.pdf().rename(self.root / 'second.pdf')
        rendezvous = threading.Barrier(2)
        def raw(*args, **kwargs):
            rendezvous.wait(3)
            return OCR_BODY, {}
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', side_effect=raw), \
             concurrent.futures.ThreadPoolExecutor(2) as pool:
            jobs = [pool.submit(reader.read, path, self.root / f'work-{i}') for i, path in enumerate([first, second])]
            results = [job.result(5) for job in jobs]
        self.assertTrue(all(report['method'] == 'mixed' for _, report, _ in results))

    def test_blank_page_has_no_ocr_request_and_is_accounted_for(self):
        path = self.pdf(('native', 'blank', 'native'))
        with patch.object(ocr, 'health', side_effect=AssertionError('blank is not a scan')):
            _, report, _ = reader.read(path, self.root / 'work')
        self.assertEqual(len(report['page_reports']), 3)
        self.assertEqual(report['page_reports'][1]['method'], 'blank')
        self.assertEqual(report['ocr_pages'], [])

    def test_disagreement_retains_baseline_and_saves_evidence_for_quarantine(self):
        path = self.pdf(('scan',))
        suspicious = 'Repeated paragraph with enough meaningful content to exceed the duplicate check length. ' * 2
        suspicious = suspicious + '\n\n' + suspicious
        with patch.object(ocr, 'health'), \
             patch.object(ocr, 'ocr_text', side_effect=[(suspicious, {}), (OCR_BODY, {})]), \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            text, report, _ = reader.read(path, self.root / 'work')
        self.assertEqual(comparison_text(text), comparison_text(suspicious))
        self.assertEqual(report['review_pages'], [1])
        detail = report['page_reports'][0]
        self.assertIn('verification_disagrees', detail['issues'])
        self.assertFalse(detail['verification']['accepted_automatically'])
        self.assertTrue(Path(detail['image_file']).is_file())
        self.assertEqual(Path(detail['verification']['text_file']).read_text(), OCR_BODY)
        with patch.object(ocr, 'health', side_effect=AssertionError('cache must be reusable')):
            with self.assertRaises(Quarantine) as caught:
                read_text(path, self.root / 'work')
        self.assertEqual(caught.exception.reason, 'ocr_needs_review')
        self.assertTrue((self.root / 'work' / 'ocr-review.json').exists())

    def test_header_retry_is_a_region_and_never_overwrites_whole_page(self):
        path = self.pdf(('scan',))
        calls = []
        def raw(file, **kw):
            with pymupdf.open(file) as doc:
                calls.append((doc[0].rect.height, len(doc)))
            return (OCR_BODY if len(calls) == 1 else 'Number: 123/2026/QD'), {}
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', side_effect=raw), \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            text, report, _ = reader.read(path, self.root / 'work', record={'number': '123/2026/QD'})
        self.assertEqual(text, OCR_BODY)
        self.assertLess(calls[1][0], calls[0][0])
        self.assertEqual(report['page_reports'][0]['verification']['scope'], 'region')
        self.assertEqual(report['review_pages'], [1])

    def test_ocr_outage_stays_pending_instead_of_dropping_a_scan(self):
        path = self.pdf()
        with patch.object(ocr, 'health', side_effect=ocr.OCRUnavailable('offline')):
            with self.assertRaises(Quarantine) as caught:
                read_text(path, self.root / 'work')
        self.assertEqual(caught.exception.reason, 'ocr_unavailable')

    def test_empty_ocr_preserves_a_review_report_instead_of_losing_evidence(self):
        path = self.pdf(('scan',))
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=('', {})), \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            with self.assertRaises(Quarantine) as caught:
                read_text(path, self.root / 'work')
        self.assertEqual(caught.exception.reason, 'ocr_needs_review')
        report = json.loads((self.root / 'work' / 'ocr-review.json').read_text())
        self.assertEqual(report['review_pages'], [1])
        self.assertIn('insufficient_page_text', report['page_reports'][0]['issues'])

    def test_partial_corner_failure_reuses_text_on_the_next_attempt(self):
        path = self.pdf(('scan',))
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', return_value=(OCR_BODY, {})) as raw, \
             patch.object(ocr, 'ocr_boxes', side_effect=ocr.OCRUnavailable('box service down')):
            with self.assertRaises(ocr.OCRUnavailable):
                reader.read(path, self.root / 'work')
        self.assertEqual(raw.call_count, 1)
        with patch.object(ocr, 'health'), patch.object(ocr, 'ocr_text', side_effect=AssertionError('text is cached')), \
             patch.object(ocr, 'ocr_boxes', return_value=POSITIONS):
            _, report, _ = reader.read(path, self.root / 'work')
        self.assertTrue(report['page_reports'][0]['cached'])


class QualityTest(unittest.TestCase):
    def test_formulas_and_source_mismatches_require_review(self):
        text = OCR_BODY + '\nCong thuc = 100%'
        self.assertIn('source_number_missing', page_issues(text, record={'number': '123/2026/QD'}, first=True))
        self.assertIn('formula_layout', page_issues(OCR_BODY + '\nCông thức: x = y / z * 100%'))
        self.assertIn('table_layout', page_issues(OCR_BODY + '\n| A | B |\n| 1 | 2 |'))


class ClientTest(unittest.TestCase):
    def test_invalid_timeout_is_a_configuration_error_before_network_io(self):
        with patch.dict(os.environ, {'OCR_TIMEOUT': 'nan'}), patch.object(ocr.requests, 'post') as post:
            with self.assertRaises(ocr.OCRConfigurationError):
                ocr.call('/ocr/raw', Path('missing.pdf'))
        post.assert_not_called()

    def test_invalid_json_does_not_sleep_after_the_last_attempt(self):
        response = MagicMock(status_code=200)
        response.json.side_effect = ValueError('not json')
        with patch('builtins.open', unittest.mock.mock_open(read_data=b'%PDF')), \
             patch.object(ocr.requests, 'post', return_value=response), patch.object(ocr.time, 'sleep') as sleep:
            with self.assertRaises(ocr.OCRUnavailable):
                ocr.call('/ocr/raw', Path('x.pdf'), retries=1)
        sleep.assert_not_called()

    def test_invalid_coordinates_are_transient_service_failure(self):
        with patch.object(ocr, 'call', return_value={'pages': [{'page': 1, 'boxes': [{'x': 2, 'y': 0, 'w': .1, 'h': .1, 'text': 'x'}]}]}):
            with self.assertRaises(ocr.OCRUnavailable):
                ocr.ocr_boxes(Path('x.pdf'))

    def test_gate_bounds_simultaneous_requests_across_threads(self):
        gate = ocr.Gate(); release = threading.Event(); lock = threading.Lock()
        active = 0; maximum = 0; entered = threading.Event()
        def request():
            nonlocal active, maximum
            with gate:
                with lock:
                    active += 1; maximum = max(maximum, active)
                    if active == 2: entered.set()
                release.wait(2)
                with lock: active -= 1
        with patch.dict(os.environ, {'OCR_WORKERS': '2'}), concurrent.futures.ThreadPoolExecutor(3) as pool:
            jobs = [pool.submit(request) for _ in range(3)]
            self.assertTrue(entered.wait(2)); time.sleep(.05)
            self.assertEqual(maximum, 2)
            release.set()
            for job in jobs: job.result(2)
        self.assertEqual(gate.active, 0)


if __name__ == '__main__':
    unittest.main()
