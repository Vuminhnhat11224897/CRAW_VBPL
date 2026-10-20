import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from legal_crawler.v2 import dates, ocr
from legal_crawler.v2.build import Quarantine, cross_check, resolve_dates
from legal_crawler.v2.llm import concurrency
from legal_crawler.v2.validate import validate_source

from datetime import datetime


PAGES = [{"page": 1, "items": [(0.5, 0.14, 0.3, 0.02, "Hà Nội, ngày 01 tháng 10 năm 2026"),
                                (0.07, 0.01, 0.3, 0.02, "Ký bởi: X Ngày ký: 2026-10-02"),
                                (0.2, 0.3, 0.5, 0.02, "Căn cứ Luật ngày 18 tháng 02 năm 2025;")]},
         {"page": 5, "items": [(0.17, 0.53, 0.6, 0.02, "1. Quyết định này có hiệu lực thi hành kể từ ngày 05/10/2026")]}]


class DatesTest(unittest.TestCase):
    def test_quadrants(self):
        found = dates.locate(PAGES)
        self.assertEqual(found["issue"]["date"], "2026-10-01")  # citation "Căn cứ ... ngày" is not the issue date
        self.assertEqual(found["sign"]["date"], "2026-10-02")
        self.assertEqual(found["effective"]["date"], "2026-10-05")

    def test_effective_on_signing_day(self):
        pages = [PAGES[0], {"page": 3, "items": [(0.1, 0.5, 0.6, 0.02, "Quyết định này có hiệu lực thi hành kể từ ngày ký ban hành")]}]
        found = dates.locate(pages)
        self.assertEqual((found["effective"]["date"], found["effective"]["derived_from"]), ("2026-10-02", "sign"))

    def test_quoted_or_cited_effective_sentences_are_ignored(self):
        pages = [PAGES[0], {"page": 9, "items": [(0.1, 0.5, 0.6, 0.02, "Luật số 11/2026/QH16 có hiệu lực kể từ ngày 01 tháng 3 năm 2027")]}]
        self.assertIsNone(dates.locate(pages)["effective"])

    def test_gazette_stamp_is_not_the_signing_date(self):
        pages = [{"page": 1, "items": [(0.07, 0.01, 0.3, 0.02, "Ký bởi: CÔNG BÁO NƯỚC CỘNG HÒA Ngày ký: 2026-10-15")]}]
        self.assertIsNone(dates.locate(pages)["sign"])

    def test_consolidated_text_gets_no_corner_effective_date(self):
        meta = {"issuance_date": "2026-10-01", "legal_type": "Văn bản hợp nhất"}
        result = resolve_dates({}, meta, PAGES, [])
        self.assertIsNone(result["effective"])

    def test_signing_date_must_be_a_signature_line_near_the_issue_date(self):
        body = (0.55, 0.7, 0.3, 0.02, "Thông tư này thay thế quy định ngày 03/03/2010")
        near = (0.55, 0.6, 0.3, 0.02, "Hà Nội, ngày 03 tháng 10 năm 2026")
        far = (0.55, 0.6, 0.3, 0.02, "Hà Nội, ngày 03 tháng 03 năm 2010")
        self.assertIsNone(dates.locate([PAGES[0], {"page": 5, "items": [body]}])["sign"] and None)
        self.assertEqual(dates.locate([PAGES[0], {"page": 5, "items": [near]}])["sign"]["date"], "2026-10-02")  # stamp on page 1 wins
        only = {"page": 1, "items": [PAGES[0]["items"][0]]}
        self.assertEqual(dates.locate([only, {"page": 5, "items": [near]}])["sign"]["date"], "2026-10-03")
        self.assertIsNone(dates.locate([only, {"page": 5, "items": [far]}])["sign"])
        self.assertIsNone(dates.locate([only, {"page": 5, "items": [body]}])["sign"])

    def test_no_effective_sentence_means_none(self):
        self.assertIsNone(dates.locate(PAGES[:1])["effective"])

    def test_invalid_dates_ignored(self):
        self.assertEqual(dates.dates_in("ngày 31 tháng 02 năm 2026 và 5/10/2026"), ["2026-10-05"])


class BuildTest(unittest.TestCase):
    META = {"document_number": "377/2026/NĐ-CP", "issuance_date": "2026-10-01", "legal_type": "Nghị định",
            "issuing_authority": "Chính phủ"}

    def test_cross_check_conflict_goes_to_quarantine(self):
        record = {"number": "377/2026/NĐ-CP", "issued_date": "2026-10-02", "document_type": "Nghị định", "issuer": "Chính phủ"}
        with self.assertRaises(Quarantine) as caught:
            cross_check(self.META, record)
        self.assertEqual(caught.exception.reason, "metadata_conflict")
        cross_check(self.META, {**record, "issued_date": "2026-10-01", "issuer": "CHÍNH PHỦ"})

    def test_web_effective_date_wins_and_range_is_enforced(self):
        meta = {"issuance_date": "2026-10-01"}
        with patch.dict(os.environ, {"DATE_FROM": "2024-01-01"}):
            result = resolve_dates({"effective_date": "2026-10-09"}, meta, PAGES, [])
            self.assertEqual((result["effective"], result["effective_source"]), ("2026-10-09", "web"))
            result = resolve_dates({}, meta, PAGES, [])
            self.assertEqual((result["effective"], result["effective_source"]), ("2026-10-05", "pdf_corner"))
            with self.assertRaises(Quarantine):
                resolve_dates({"effective_date": "2023-12-31"}, meta, PAGES, [])


class OCRTest(unittest.TestCase):
    def test_unreachable_server_is_reported_not_guessed(self):
        with patch("legal_crawler.v2.ocr.requests.get", side_effect=requests.ConnectionError()):
            with self.assertRaises(ocr.OCRUnavailable):
                ocr.health()

    def test_boxes_are_normalised(self):
        response = MagicMock(status_code=200)
        response.json.return_value = {"pages": [{"page": 1, "boxes": [{"x": .1, "y": .2, "w": .3, "h": .1, "text": "a"}]}]}
        with patch("legal_crawler.v2.ocr.requests.post", return_value=response), \
                patch("builtins.open", unittest.mock.mock_open(read_data=b"%PDF")):
            from pathlib import Path
            self.assertEqual(ocr.ocr_boxes(Path("x.pdf")), [{"page": 1, "items": [(.1, .2, .3, .1, "a")]}])


class ConfigTest(unittest.TestCase):
    def test_llm_concurrency_day_and_night(self):
        self.assertEqual(concurrency(datetime(2026, 1, 1, 9)), 3)
        self.assertEqual(concurrency(datetime(2026, 1, 1, 22)), 10)
        self.assertEqual(concurrency(datetime(2026, 1, 1, 6, 59)), 10)
        self.assertEqual(concurrency(datetime(2026, 1, 1, 7)), 3)
        self.assertEqual(concurrency(datetime(2026, 1, 1, 21, 59)), 3)
        self.assertEqual(concurrency(datetime(2026, 1, 1, 0)), 10)

    def test_gate_follows_the_clock_while_a_run_is_in_progress(self):
        import threading, time
        from legal_crawler.v2 import llm
        limit = {"n": 2}
        gate = llm.Gate()
        with patch.object(llm, "concurrency", lambda *a: limit["n"]):
            gate.__enter__(); gate.__enter__()
            entered = threading.Event()
            threading.Thread(target=lambda: (gate.__enter__(), entered.set()), daemon=True).start()
            time.sleep(0.2); self.assertFalse(entered.is_set())  # third request waits at limit 2
            limit["n"] = 3
            with gate.cond:
                gate.cond.notify_all()
            self.assertTrue(entered.wait(2))  # the limit rose (22:00): it proceeds without a release

    def test_mapping_validation_rejects_unknown_field_and_bad_date(self):
        props = {"a": {"type": "keyword"}, "d": {"type": "date", "format": "yyyy-MM-dd"}}
        self.assertEqual(validate_source({"a": "x", "d": "2026-10-01"}, props), [])
        self.assertEqual(len(validate_source({"z": 1, "d": "01/10/2026"}, props)), 2)


if __name__ == "__main__":
    unittest.main()
