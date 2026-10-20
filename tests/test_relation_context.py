import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from legal_crawler.v2 import changes


class RelationContextTest(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.key = ('Điều 13 Khoản 2', '37/2019/TT-BTC', 'thay-the')
        self.document = {'document_number': '153/2026/TT-BTC', 'issuance_date': '2026-10-12',
                         'title': 'Thông tư hướng dẫn chế độ tài chính', 'origin': 'crawler'}
        self.candidate = {'_id': 'hhl_10100001_0', 'target_doc': self.key[1], 'action': self.key[2],
                          'source_id': 10100001, 'source_issuance_date': self.document['issuance_date'],
                          'source_path': self.key[0], 'scope': 'toàn bộ', 'unresolved': False,
                          '_evidence': 'Thông tư số 37/2019/TT-BTC hết hiệu lực kể từ ngày Thông tư này có hiệu lực.'}
        lookup = MagicMock()
        lookup.by_number.return_value = []
        self.ctx = {'meter': changes.Meter(), 'folder': Path(temp.name), 'qa_tokens': 512,
                    'scope_tokens': 1024, 'provenance': self.document, 'lookup': lookup}

    def test_verdict_sees_source_identity_and_cache_changes_when_source_changes(self):
        calls = []

        def verdict(prompt, text, folder, cache, validate, **kwargs):
            calls.append((json.loads(text), folder))
            return {'valid': True, 'reason': 'Văn bản nguồn quy định văn bản đích hết hiệu lực.'}

        with patch.object(changes, 'validated_json', side_effect=verdict):
            rows, status = changes.qa_group(self.key, [self.candidate], self.ctx)
            self.document['document_number'] = '154/2026/TT-BTC'
            changes.qa_group(self.key, [self.candidate], self.ctx)
        self.assertEqual(status, 'accepted')
        self.assertEqual(rows[0]['_source']['target_doc'], '37/2019/TT-BTC')
        self.assertEqual(calls[0][0]['source_document']['document_number'], '153/2026/TT-BTC')
        self.assertEqual(calls[0][0]['source_document']['issuance_date'], '2026-10-12')
        self.assertEqual(calls[0][0]['target'], '37/2019/TT-BTC')
        self.assertNotEqual(calls[0][1], calls[1][1])

    def test_explicit_context_still_respects_a_rejected_verdict(self):
        with patch.object(changes, 'validated_json', return_value={'valid': False, 'reason': 'Chỉ trích dẫn.'}):
            rows, status = changes.qa_group(self.key, [self.candidate], self.ctx)
        self.assertEqual((rows, status), ([], 'rejected'))
        self.ctx['lookup'].by_number.assert_not_called()


if __name__ == '__main__':
    unittest.main()
