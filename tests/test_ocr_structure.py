import unittest

from legal_crawler.v2.relations import detect
from legal_crawler.v2.structure import build_chunks, normalize, parse


class OCRStructureTest(unittest.TestCase):
    def test_mixed_markdown_headings_keep_all_articles_and_their_own_bodies(self):
        lines = []
        for number in range(1, 14):
            heading = f'Điều {number}. Quy định {number}'
            if number in (5, 6, 10, 11):
                heading = '# ' + heading
            elif number >= 7:
                heading = '**' + heading + '**'
            lines.extend([heading, f'1. Nội dung riêng của điều {number}.'])
        text = '\n'.join([*lines, 'Nơi nhận:', '- Cơ quan nhận;'])
        parsed, stats = parse(text)
        self.assertEqual(stats['n_dieu'], 13)
        self.assertTrue(stats['dieu_seq_ok'])
        for number, article in enumerate(parsed['dieu_luat'], 1):
            self.assertEqual(article['so_dieu'], number)
            self.assertEqual(article['tieu_de'], f'Quy định {number}')
            self.assertEqual(article['khoan'][0]['noi_dung'], f'Nội dung riêng của điều {number}.')
        chunks = build_chunks('10100001', {}, parsed)
        self.assertEqual({c['_source']['so_dieu'] for c in chunks}, set(range(1, 14)))
        # Parsing may interpret formatting; the stored OCR text stays untouched.
        self.assertIn('**Điều 13.', normalize(text))

    def test_heading_and_label_formatting_does_not_absorb_the_tail(self):
        parsed, stats = parse('''## **Điều 1. Quy định chung** ##
**1.** Nội dung khoản một.
__2.__ Các trường hợp:
**a)** Trường hợp đầu.
__b)__ Trường hợp sau.
## **Điều 2. Thi hành**
1. Trách nhiệm thực hiện.
**Nơi nhận:**
- Cơ quan nhận;
''')
        self.assertEqual(stats['n_dieu'], 2)
        self.assertEqual(stats['n_khoan'], 3)
        self.assertEqual(stats['n_diem'], 2)
        self.assertNotIn('Nơi nhận', parsed['dieu_luat'][-1]['noi_dung'])
        self.assertIn('Cơ quan nhận', parsed['noi_nhan'])

    def test_quoted_markdown_articles_and_table_rows_are_not_source_articles(self):
        parsed, stats = parse('''# Điều 1. Sửa đổi quy định
1. Thay nội dung như sau:
“
## **Điều 1. Nội dung được trích**
1. Phần trích dẫn.
## **Điều 2. Nội dung được trích tiếp**
1. Phần trích dẫn tiếp.
”
| **Điều 3. Một hàng bảng** | Nội dung |
# Điều 2. Thi hành
1. Trách nhiệm thi hành.
Nơi nhận:
- Cơ quan nhận;
''')
        self.assertEqual(stats['n_dieu'], 2)
        self.assertIn('Nội dung được trích tiếp', parsed['dieu_luat'][0]['noi_dung'])
        self.assertEqual(parsed['dieu_luat'][1]['tieu_de'], 'Thi hành')

    def test_markdown_effective_article_reaches_relation_detection(self):
        title = ('hướng dẫn về chế độ tài chính đối với chương trình, dự án tài chính vi mô '
                 'của tổ chức chính trị, tổ chức chính trị - xã hội, tổ chức phi chính phủ')
        declaration = ('Thông tư số 37/2019/TT-BTC ngày 25 tháng 06 năm 2019 của Bộ trưởng Bộ Tài chính '
                       + title + ' hết hiệu lực thi hành kể từ ngày Thông tư này có hiệu lực thi hành.')
        text = ('## **Điều 1. Hiệu lực thi hành**\n1. Thông tư có hiệu lực từ ngày 01 tháng 12 năm 2026.\n'
                '2. ' + declaration + '\nNơi nhận:\n- Cơ quan nhận;')
        parsed, _ = parse(text)
        meta = {'document_number': '153/2026/TT-BTC', 'title': 'Thông tư về chế độ tài chính',
                'legal_type': 'Thông tư', 'action': 'other', 'issuing_authority': 'Bộ Tài chính',
                'issuance_date': '2026-10-12'}
        candidates = detect(10100001, meta, parsed)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]['target_doc'], '37/2019/TT-BTC')
        self.assertEqual(candidates[0]['source_path'], 'Điều 1 Khoản 2')
        self.assertEqual(candidates[0]['scope'], 'toàn bộ')


if __name__ == '__main__':
    unittest.main()
