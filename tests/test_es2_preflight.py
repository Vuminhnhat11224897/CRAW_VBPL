import hashlib
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import requests

from legal_crawler.storage import Store
from legal_crawler.v2 import build, compare, preflight, publish, store
from legal_crawler.v2.safety import WriteForbidden

try:
    import pymupdf
except ImportError:
    pymupdf = None


def response(payload):
    result = MagicMock(status_code=200)
    result.json.return_value = payload
    return result


@unittest.skipUnless(pymupdf, 'PyMuPDF is required')
class FileSelectionTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.files = []
        for index in range(2):
            path = self.root / f'{index}.pdf'
            with pymupdf.open() as doc:
                doc.new_page().insert_text((50, 60), f'Candidate document {index}')
                doc.save(path)
            self.files.append({'format': 'pdf', 'path': path.name,
                               'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        self.record = {'id': 'source:test', 'number': '1/2026/TT-BTC', 'issued_date': '2026-10-01', 'files': self.files}

    def failing(self, path, folder, *, record):
        (folder / 'ocr-review.json').write_text(json.dumps({'identity': {'issues': ['primary_act_missing']}}))
        raise build.Quarantine('ocr_identity_unverified')

    def good(self, path, folder, *, record):
        return 'text', {'content_sha256': self.files[1]['sha256']}, []

    def test_attachment_does_not_hide_primary_file_and_evidence_is_preserved(self):
        def read(path, folder, **kw):
            return self.failing(path, folder, **kw) if path.name == '0.pdf' else self.good(path, folder, **kw)
        with patch.object(build, 'read_text', side_effect=read) as read:
            path, digest, folder, text, report, _ = build.read_record(self.record, self.root, self.root / 'work')
        self.assertEqual(path.name, '1.pdf')
        self.assertEqual(read.call_count, 2)
        self.assertEqual(len(report['pdf_selection']['skipped']), 1)
        self.assertTrue((folder / 'pdf-selection.json').is_file())

    def test_wrong_date_is_not_bypassed_by_another_pdf(self):
        def fail(path, folder, **kw):
            (folder / 'ocr-review.json').write_text(json.dumps({'identity': {'issues': ['identity_issued_date_conflict']}}))
            raise build.Quarantine('ocr_identity_unverified')
        with patch.object(build, 'read_text', side_effect=fail) as read:
            with self.assertRaises(build.Quarantine):
                build.read_record(self.record, self.root, self.root / 'work')
        self.assertEqual(read.call_count, 1)

    def test_body_review_is_not_bypassed_by_another_pdf(self):
        with patch.object(build, 'read_text', side_effect=build.Quarantine('ocr_needs_review')) as read:
            with self.assertRaises(build.Quarantine):
                build.read_record(self.record, self.root, self.root / 'work')
        self.assertEqual(read.call_count, 1)

    def test_corrupt_pdf_is_rejected_before_ocr_or_llm(self):
        (self.root / '0.pdf').write_bytes(b'changed')
        with patch.object(build, 'read_text') as read:
            with self.assertRaises(build.Quarantine) as caught:
                build.read_record(self.record, self.root, self.root / 'work')
        self.assertEqual(caught.exception.reason, 'pdf_checksum_mismatch')
        read.assert_not_called()

    def test_native_attachment_is_skipped_without_body_ocr(self):
        path = self.root / '0.pdf'
        path.unlink()
        with pymupdf.open() as doc:
            doc.new_page().insert_text((50, 60), 'QCVN 31 : 2026/BYT')
            doc.save(path)
        self.files[0]['sha256'] = hashlib.sha256(path.read_bytes()).hexdigest()
        with patch.object(build, 'read_text', side_effect=self.good) as read:
            _, _, _, _, report, _ = build.read_record(self.record, self.root, self.root / 'work')
        self.assertEqual(read.call_count, 1)
        self.assertEqual(report['pdf_selection']['skipped'][0]['reason'], 'primary_act_missing')


class PreflightTest(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        path = self.root / 'files/source/a.pdf'
        path.parent.mkdir(parents=True)
        path.write_bytes(b'%PDF-1.7\nfixture\n%%EOF')
        self.sha = hashlib.sha256(path.read_bytes()).hexdigest()
        data = Store(self.root)
        data.save_document({'id': 'source:test', 'source': 'source', 'status': 'downloaded',
                            'number': '1/2026/TT-BTC', 'issued_date': '2026-10-01',
                            'files': [{'format': 'pdf', 'path': 'files/source/a.pdf', 'sha256': self.sha}]})
        data.db.close()
        self.env = patch.dict(os.environ, {
            'SOURCE_ES_URL': 'http://10.1.10.23:9200', 'TARGET_ES_URL': 'http://10.1.10.23:9200',
            'TARGET_DOCUMENT_INDEX': 'legal-docs', 'TARGET_CHUNK_INDEX': 'legal-chunk-section-v2',
            'TARGET_CHANGE_INDEX': 'legal-changed-v2', 'LLM_MODEL': 'qwen3.6-35b',
            'MINIO_ENDPOINT': 'http://10.1.10.10:9000', 'MINIO_ACCESS_KEY': 'fixture', 'MINIO_SECRET_KEY': 'fixture',
            'CRAWLER_BUCKET': 'legal-crawler', 'CRAWLER_ID_START': '10100000', 'ES_WRITE_ENABLED': 'yes'})
        self.env.start(); self.addCleanup(self.env.stop)

    def get(self, url, **kw):
        if url == 'http://10.1.10.23:9200/':
            return response({'cluster_uuid': 'fixture-cluster-23', 'version': {'number': '9'}})
        if url.endswith('/_mapping'):
            index = url.split('/')[-2]
            self.assertIn(index, ['legal-docs', 'legal-chunk-section-v2', 'legal-changed-v2'])
            return response({index: {'mappings': {'properties': {'embedding': {'type': 'dense_vector', 'dims': 1024}}}}})
        if url.endswith('/_cluster/health'):
            return response({'status': 'yellow'})
        if url.endswith('/models'):
            return response({'data': [{'id': 'qwen3.6-35b'}]})
        if url.endswith(':8989/health'):
            return response({'status': 'ok', 'model': 'BAAI/bge-m3', 'dims': 1024, 'max_length': 256})
        return response({'status': 'ok'})

    def test_readiness_check_never_mutates_es_or_minio(self):
        minio = MagicMock()
        minio.bucket_exists.return_value = True
        def post(url, **kw):
            self.assertTrue(url.endswith(':8989/embed'))
            return response({'embeddings': [[.1] * 1024]})
        with patch('requests.get', side_effect=self.get), patch('requests.post', side_effect=post), \
             patch('requests.put') as put, patch('requests.delete') as delete, \
             patch.object(store, 'client', return_value=minio):
            report, mappings = preflight.run(self.root)
        self.assertTrue(report['ready_for_export'])
        self.assertFalse(report['ready_for_write_test'])
        self.assertEqual(len(mappings), 3)
        put.assert_not_called(); delete.assert_not_called()
        minio.make_bucket.assert_not_called(); minio.fput_object.assert_not_called()

    def test_unreachable_cluster_blocks_draft_verification_without_faking_a_pass(self):
        patches = {name: patch.object(preflight, name, return_value={'status': 'ok'}) for name in
                   ['llm_check', 'embedding_check', 'ocr_check', 'minio_check']}
        for p in patches.values():p.start(); self.addCleanup(p.stop)
        with patch.object(preflight, 'es_check', side_effect=requests.ConnectTimeout()), \
             patch.object(preflight, 'draft_check') as check:
            report, mappings = preflight.run(self.root, self.root / 'missing-draft')
        self.assertIn('elasticsearch', report['blockers'])
        self.assertFalse(report['ready_for_export'])
        self.assertFalse(report['ready_for_write_test'])
        self.assertIsNone(mappings)
        check.assert_not_called()

    def test_error_report_never_serializes_credentials_from_exception_messages(self):
        error = requests.ConnectionError('http://user:secret@10.1.10.23:9200?token=secret')
        self.assertNotIn('secret', json.dumps(preflight.safe_error(error)))

    def test_input_checksum_mismatch_is_not_ready(self):
        (self.root / 'files/source/a.pdf').write_bytes(b'corrupt')
        with self.assertRaises(ValueError):preflight.input_check(self.root)

    def test_alias_cannot_be_used_in_place_of_a_concrete_index(self):
        def fake(url):
            if url.endswith('/'):
                return {'cluster_uuid': 'cluster-23'}
            return {'unapproved-backing-index': {'mappings': {'properties': {}}}}
        with patch.object(preflight, 'get_json', side_effect=fake):
            with self.assertRaises(ValueError):preflight.es_check()

    def test_publisher_reloads_env_before_guarding_the_target(self):
        original = {k: getattr(publish, k) for k in ['ES', 'DOCS', 'CHUNKS', 'CHANGES']}
        with patch.multiple(publish, **original), patch.dict(os.environ, {'TARGET_ES_URL': 'http://10.1.10.10:9200'}):
            with self.assertRaises(WriteForbidden):publish.configure()

    def test_v2_comparison_does_not_depend_on_v3_indexes(self):
        result = {'document': {'document_number': '1/2026/TT-BTC', 'issuance_date': '2026-10-01'},
                  'chunks': [], 'warnings': [], 'dates': {}}
        with patch.object(compare, 'find', return_value=[]) as find:
            rows = compare.compare_all([({'source': 'source'}, 10100000, result)], 'http://10.1.10.23:9200', 'http://10.1.10.23:9200')
        self.assertEqual(find.call_count, 1)
        self.assertEqual(find.call_args.args[1], 'legal-docs')
        self.assertEqual(rows[0]['in_v3'], [])

    def draft(self):
        did = '10100000'
        job = 'crawler:fixture'
        source = {'document_number': '1/2026/TT-BTC', 'title': 'Fixture', 'legal_type': 'Thông tư',
                  'issuing_authority': 'Bộ Tài chính', 'issuance_date': '2026-10-01', 'full_content': 'Fixture text',
                  'content_sha256': self.sha, 'pdf_minio_path': f'legal-crawler/pdf/{self.sha}.pdf',
                  'origin': 'crawler', 'ingest_job_id': job, 'embedding': [.1] * 1024}
        chunk = {'_id': did + '_d1', '_source': {'doc_id': did, 'origin': 'crawler',
                 'ingest_job_id': job, 'embedding': [.1] * 1024}}
        doc_properties = {k: {'type': 'keyword'} for k in source if k != 'embedding'}
        doc_properties['embedding'] = {'type': 'dense_vector', 'dims': 1024}
        chunk_properties = {k: {'type': 'keyword'} for k in chunk['_source'] if k != 'embedding'}
        chunk_properties['embedding'] = {'type': 'dense_vector', 'dims': 1024}
        (self.root / f'files/source/{self.sha}.pdf').write_bytes((self.root / 'files/source/a.pdf').read_bytes())
        folder = self.root / 'draft'
        folder.mkdir(exist_ok=True)
        (folder / 'documents.jsonl').write_text(json.dumps({'_id': did, '_source': source}) + '\n')
        (folder / 'chunks.jsonl').write_text(json.dumps(chunk) + '\n')
        return folder, {'legal-docs': doc_properties, 'legal-chunk-section-v2': chunk_properties, 'legal-changed-v2': {}}

    def test_live_mapping_validation_and_dry_run_always_use_write_false(self):
        folder, mappings = self.draft()
        with patch.object(publish, 'publish_one', return_value=('dry_run', 'fixture')) as call:
            report = preflight.draft_check(folder, self.root, mappings)
        self.assertEqual(report['new_documents'], 1)
        self.assertFalse(call.call_args.kwargs['write'])
        self.assertIsNone(call.call_args.args[3])

    def test_bad_vector_is_rejected_before_duplicate_lookup_or_publish(self):
        folder, mappings = self.draft()
        path = folder / 'documents.jsonl'
        row = json.loads(path.read_text())
        row['_source']['embedding'] = [True] * 1024
        path.write_text(json.dumps(row) + '\n')
        with patch.object(publish, 'publish_one') as call:
            with self.assertRaises(ValueError):preflight.draft_check(folder, self.root, mappings)
        call.assert_not_called()

    def test_chunk_from_a_different_writer_run_is_rejected_before_publish(self):
        folder, mappings = self.draft()
        path = folder / 'chunks.jsonl'
        row = json.loads(path.read_text())
        row['_source']['ingest_job_id'] = 'crawler:another-run'
        path.write_text(json.dumps(row) + '\n')
        with patch.object(publish, 'publish_one') as call:
            with self.assertRaises(ValueError):preflight.draft_check(folder, self.root, mappings)
        call.assert_not_called()


class UploadGuardTest(unittest.TestCase):
    def test_protected_bucket_is_never_created_or_written(self):
        minio = MagicMock()
        with patch.dict(os.environ, {'CRAWLER_BUCKET': 'legal-upload'}):
            with self.assertRaises(ValueError):store.upload_pdf(minio, Path('missing.pdf'), 'a', create_bucket=True)
        self.assertEqual(minio.mock_calls, [])

    def test_pdf_digest_is_checked_before_any_minio_mutation(self):
        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / 'a.pdf'
            path.write_bytes(b'%PDF-1.7\nfixture\n%%EOF')
            minio = MagicMock()
            with patch.dict(os.environ, {'CRAWLER_BUCKET': 'legal-crawler'}):
                with self.assertRaises(ValueError):store.upload_pdf(minio, path, 'wrong', create_bucket=True)
            self.assertEqual(minio.mock_calls, [])


if __name__ == '__main__':
    unittest.main()
