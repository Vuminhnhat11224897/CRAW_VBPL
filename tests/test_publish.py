import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from legal_crawler.v2 import publish, safety

PROPS = {"docs": {"document_number": {"type": "keyword"}, "issuance_date": {"type": "date", "format": "yyyy-MM-dd"},
                  "title": {"type": "text"}, "legal_type": {"type": "keyword"}, "issuing_authority": {"type": "keyword"},
                  "full_content": {"type": "text"}, "origin": {"type": "keyword"}, "ingest_job_id": {"type": "keyword"}, "content_sha256": {"type": "keyword"},
                  "pdf_minio_path": {"type": "keyword"}, "embedding": {"type": "dense_vector", "dims": 1024}},
         "chunks": {"doc_id": {"type": "keyword"}, "origin": {"type": "keyword"},
                    "embedding": {"type": "dense_vector", "dims": 1024}}}
DOC = {"_id": "100014", "_source": {"document_number": "1/2026/NĐ-CP", "issuance_date": "2026-10-01", "title": "t",
       "legal_type": "Nghị định", "issuing_authority": "Chính phủ", "full_content": "x", "origin": "crawler", "ingest_job_id": "crawler:20261019T000000Z",
       "content_sha256": "ab", "pdf_minio_path": "legal-crawler/pdf/ab.pdf", "embedding": [0.1] * 1024}}
CHUNKS = [{"_id": "100014_d1", "_source": {"doc_id": "100014", "origin": "crawler", "embedding": [0.1] * 1024}}]


def response(status=200, body=None):
    r = MagicMock(status_code=status, text=json.dumps(body or {}))
    r.json.return_value = body or {}
    return r


class GuardTest(unittest.TestCase):
    def test_cluster_10_is_forbidden_everywhere(self):
        for url in ("http://10.1.10.10:9200", "http://10.1.10.10:9200/legal-docs/_search"):
            with self.assertRaises(safety.WriteForbidden):
                safety.forbid_cluster_10(url)
            with self.assertRaises(safety.WriteForbidden):
                safety.check_write(url, "legal-docs")

    def test_only_cluster_23_and_v2_indexes_may_be_written(self):
        for url in ("http://10.1.10.23:9200", "http://100.77.4.35:9200"):
            with self.subTest(url=url):
                safety.check_write(url, "legal-docs", "legal-chunk-section-v2", "legal-changed-v2")
                for index in ("legal-docs-v3", "legal-chunk-section-v3", "legal-changed-v3", "legal-docs-v4", "legal-changed"):
                    with self.assertRaises(safety.WriteForbidden):
                        safety.check_write(url, index)
        with self.assertRaises(safety.WriteForbidden):
            safety.check_write("http://10.9.9.9:9200", "legal-docs")

    def test_dedupe_client_refuses_cluster_10(self):
        from legal_crawler.es import ESDedupe
        with self.assertRaises(safety.WriteForbidden):
            ESDedupe(MagicMock(es_url="http://10.1.10.10:9200", es_index="legal-docs", timeout=5))


@patch("legal_crawler.v2.publish.ids._taken", return_value=set())
@patch("legal_crawler.v2.publish.find", return_value=[])
class PublishTest(unittest.TestCase):
    def test_unembedded_draft_is_refused_before_any_es_request(self, _find, _taken):
        chunk = {'_id': CHUNKS[0]['_id'], '_source': {k: v for k, v in CHUNKS[0]['_source'].items() if k != 'embedding'}}
        with patch('legal_crawler.v2.publish.req') as request:
            with self.assertRaises(ValueError):
                publish.publish_one(DOC, [chunk], PROPS, None, None, write=True)
        request.assert_not_called()

    def test_zero_chunk_draft_is_refused_before_any_es_request(self, _find, _taken):
        with patch('legal_crawler.v2.publish.req') as request:
            with self.assertRaises(ValueError):
                publish.publish_one(DOC, [], PROPS, None, None, write=True)
        request.assert_not_called()

    def test_dry_run_never_writes(self, _find, _taken):
        calls = []
        with patch("legal_crawler.v2.publish.req", side_effect=lambda m, p, **k: calls.append(m) or response(404)):
            status, _ = publish.publish_one(DOC, CHUNKS, PROPS, None, None, write=False)
        self.assertEqual((status, calls), ("dry_run", ["GET"]))

    def test_existing_id_or_duplicate_is_never_overwritten(self, find, _taken):
        with patch("legal_crawler.v2.publish.req", return_value=response(200)):
            self.assertEqual(publish.publish_one(DOC, CHUNKS, PROPS, None, None, write=True)[0], "exists")
        find.return_value = [{"_id": "5"}]
        with patch("legal_crawler.v2.publish.req", return_value=response(404)):
            self.assertEqual(publish.publish_one(DOC, CHUNKS, PROPS, None, None, write=True)[0], "duplicate")

    def test_id_with_leftovers_in_any_index_is_refused(self, find, taken):
        taken.return_value = {100014}
        with patch("legal_crawler.v2.publish.req", return_value=response(404)):
            status, detail = publish.publish_one(DOC, CHUNKS, PROPS, None, None, write=True)
        self.assertEqual(status, "exists")

    def test_other_writers_documents_are_refused(self, _find, _taken):
        other = {"_id": "900000001", "_source": {**DOC["_source"], "origin": "user_upload"}}
        with self.assertRaises(safety.WriteForbidden):
            publish.publish_one(other, CHUNKS, PROPS, None, None, write=True)

    def test_bulk_failure_rolls_back_only_what_was_created(self, _find, _taken):
        sent = []

        def fake(method, path, **kw):
            sent.append((method, path))
            if path == "/_bulk" and "create" in kw["data"].decode():
                return response(200, {"errors": True, "items": [{"create": {"_id": "100014_d1", "status": 201}},
                                                                  {"create": {"_id": "x", "status": 400, "error": {"type": "bad"}}}]})
            return response(404 if method == "GET" else 200, {})
        with patch("legal_crawler.v2.publish.req", side_effect=fake), \
                patch("legal_crawler.v2.publish.store.upload_pdf", return_value="legal-crawler/pdf/ab.pdf"):
            with self.assertRaises(RuntimeError):
                publish.publish_one(DOC, CHUNKS, PROPS, None, Path("x.pdf"), write=True)
        self.assertNotIn(("DELETE", "/legal-docs/_doc/100014"), sent)  # the document was never created, so it is never deleted
        self.assertFalse(any(p.startswith("/legal-docs/_create") for _, p in sent))

    def test_relation_failure_rolls_back_document_chunks_and_relations(self, _find, _taken):
        sent = []
        change = {"_id": "100014_0", "_source": {"source_id": 100014, "origin": "crawler"}}

        def fake(method, path, **kw):
            body = kw.get("data", b"").decode() if kw.get("data") else ""
            sent.append((method, path, body[:60]))
            if path == "/_bulk" and '"_index": "legal-changed-v2"' in body and '"create"' in body:
                return response(200, {"errors": True, "items": [{"create": {"_id": "100014_0", "status": 400, "error": {"type": "bad"}}}]})
            if path == "/_bulk" and "create" in body:
                return response(200, {"errors": False, "items": [{"create": {"_id": "100014_d1", "status": 201}}]})
            if method == "PUT":
                return response(201, {})
            return response(404 if method == "GET" else 200, {})
        with patch("legal_crawler.v2.publish.req", side_effect=fake), \
                patch("legal_crawler.v2.publish.store.upload_pdf", return_value="legal-crawler/pdf/ab.pdf"):
            with self.assertRaises(RuntimeError):
                publish.publish_one(DOC, CHUNKS, PROPS, None, Path("x.pdf"), write=True, changes=[change])
        deletes = [b for m, p, b in sent if p == "/_bulk" and '"delete"' in b]
        self.assertTrue(any("legal-chunk-section-v2" in d for d in deletes))
        self.assertIn(("DELETE", "/legal-docs/_doc/100014"), [(m, p) for m, p, _ in sent])

    def test_relations_of_another_source_are_refused(self, _find, _taken):
        bad = {"_id": "x_0", "_source": {"source_id": 5, "origin": "crawler"}}

        def fake(method, path, **kw):
            body = kw.get("data", b"").decode() if kw.get("data") else ""
            if path == "/_bulk" and "create" in body:
                return response(200, {"errors": False, "items": [{"create": {"_id": "100014_d1", "status": 201}}]})
            return response(201 if method == "PUT" else 404 if method == "GET" else 200, {})
        with patch("legal_crawler.v2.publish.req", side_effect=fake), \
                patch("legal_crawler.v2.publish.store.upload_pdf", return_value="legal-crawler/pdf/ab.pdf"):
            with self.assertRaises(RuntimeError):
                publish.publish_one(DOC, CHUNKS, PROPS, None, Path("x.pdf"), write=True, changes=[bad])


class AllocatorTest(unittest.TestCase):
    def test_smallest_free_id_from_start_and_state_advances(self):
        from legal_crawler.v2 import ids
        with patch("legal_crawler.v2.ids._taken", return_value={10100000, 10100001, 10100003}):
            state = {"next_id": 10100000, "ids": {"a": 10100002}}
            self.assertEqual(ids.next_free("http://10.1.10.23:9200", state), 10100004)  # ...02 is reserved by an earlier draft
            self.assertEqual(state["next_id"], 10100005)

    def test_allocator_refuses_cluster_10(self):
        from legal_crawler.v2 import ids
        with self.assertRaises(safety.WriteForbidden):
            ids.next_free("http://10.1.10.10:9200", {})

    def test_ids_never_start_below_the_configured_floor(self):
        from legal_crawler.v2 import ids
        with patch("legal_crawler.v2.ids._taken", return_value=set()), patch.object(ids, "START", 10100000):
            self.assertEqual(ids.next_free("http://10.1.10.23:9200", {"next_id": 5}), 10100000)


class ReceiptTest(unittest.TestCase):
    def test_only_confirmed_success_records_created_ids(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'documents.jsonl').write_text(json.dumps(DOC) + '\n')
            (root / 'chunks.jsonl').write_text(json.dumps(CHUNKS[0]) + '\n')
            change = {'_id': DOC['_id'] + '_0', '_source': {'source_id': int(DOC['_id'])}}
            (root / 'changes.jsonl').write_text(json.dumps(change) + '\n')
            for status in ['dry_run', 'duplicate', 'exists', 'written']:
                args = ['publish', '--draft', str(root), '--data', str(root)] + (['--write'] if status == 'written' else [])
                def mapping(url, **kw):
                    key = 'docs' if '/legal-docs/' in url else 'chunks'
                    return response(200, {'index': {'mappings': {'properties': PROPS[key]}}})
                with patch('sys.argv', args), patch.dict(os.environ, {'ES_WRITE_ENABLED': 'yes'}), \
                     patch.object(publish.requests, 'get', side_effect=mapping), \
                     patch.object(publish.store, 'client'), patch.object(publish, 'publish_one', return_value=(status, 'fixture')):
                    self.assertEqual(publish.main(), 0)
                result = json.loads((root / 'publish-report.json').read_text())
                self.assertTrue(result['completed'])
                self.assertIn('started_at', result)
                receipt = result['documents'][0]
                self.assertEqual(receipt['created_document'], status == 'written')
                self.assertEqual(receipt['created_chunks'], [CHUNKS[0]['_id']] if status == 'written' else [])
                self.assertEqual(receipt['created_changes'], [change['_id']] if status == 'written' else [])

    def test_unknown_only_id_cannot_reuse_a_previous_success_receipt(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'documents.jsonl').write_text(json.dumps(DOC) + '\n')
            (root / 'chunks.jsonl').write_text(json.dumps(CHUNKS[0]) + '\n')
            (root / 'publish-report.json').write_text(json.dumps({'documents': [{'status': 'written'}]}))
            args = ['publish', '--draft', str(root), '--only', 'missing']
            mapping = {'index': {'mappings': {'properties': {}}}}
            with patch('sys.argv', args), patch.object(publish.requests, 'get', return_value=response(200, mapping)), \
                 patch.object(publish, 'publish_one') as call:
                self.assertEqual(publish.main(), 1)
            call.assert_not_called()
            receipt = json.loads((root / 'publish-report.json').read_text())
            self.assertEqual(receipt['documents'], [])
            self.assertEqual(receipt['error'], 'requested_ids_not_in_draft')


if __name__ == "__main__":
    unittest.main()
