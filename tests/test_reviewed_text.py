import json
from pathlib import Path
import tempfile
import unittest
from legal_crawler.v2.reviewed_text import apply_review, text_hash
from legal_crawler.v2.structure import point_sequence_issues, parse, build_chunks

class ReviewedTextTest(unittest.TestCase):
    def test_review_is_bound_to_pdf_input_and_output(self):
        with tempfile.TemporaryDirectory() as directory:
            folder=Path(directory)
            self.assertEqual(apply_review('đàm phán','pdf',folder),('đàm phán',None))
            review=dict(pdf_sha256='pdf',input_text_sha256=text_hash('đàm phán'),
                        output_text_sha256=text_hash('đài thọ'),reviewer='reviewer',evidence='page 6',
                        patches=[dict(old='đàm phán',new='đài thọ',count=1,page=6,reason='PDF')])
            def save(): (folder/'reviewed-corrections.json').write_text(json.dumps(review))
            save()
            self.assertEqual(apply_review('đàm phán','pdf',folder)[0],'đài thọ')
            for text,digest in [('đàm phán','other'),('different','pdf')]:
                with self.assertRaises(ValueError):apply_review(text,digest,folder)
            review['patches'][0]['count']=2;save()
            with self.assertRaises(ValueError):apply_review('đàm phán','pdf',folder)
            review['patches'][0]['count']=1;review['output_text_sha256']='wrong';save()
            with self.assertRaises(ValueError):apply_review('đàm phán','pdf',folder)

    def test_missing_and_gapped_points_are_rejected(self):
        for points in ['b) Hai\nc) Ba', 'a) Một\nc) Ba']:
            self.assertTrue(point_sequence_issues('Điều 1. Vốn\n1. Vốn:\n'+points))
        self.assertFalse(point_sequence_issues('Điều 1. Vốn\n1. Vốn:\na) Một\nb) Hai\n2. Khác:\na) Ba\nb) Bốn'))
        self.assertFalse(point_sequence_issues('Điều 1. Sửa đổi\n“b) Nội dung trích dẫn\nc) Tiếp theo”'))

    def test_restored_points_get_individual_chunks(self):
        text='Điều 1. Vốn\n1. Vốn:\na) Một\nb) Hai\nc) Ba\nd) Bốn\nNơi nhận:\n- Lưu.'
        parsed,_=parse(text)
        chunks=build_chunks('123',{},parsed)
        points=[c['_source']['so_diem'] for c in chunks if c['_source']['chunk_type']=='diem']
        self.assertEqual(points,['a','b','c','d'])
