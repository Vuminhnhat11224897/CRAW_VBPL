"""Read-only readiness checks before a small v2 publish on cluster 23.

python -m legal_crawler.v2.preflight --data data_es2_test/input --out data_es2_test/preflight.json
No index, bucket or document is created. --draft additionally validates the actual payload and dry-runs publish.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
from urllib.parse import urlsplit

import requests

from ..settings import load_env
from ..storage import atomic_json, digest_file
from . import export, ids, publish, safety, store
from .validate import validate_batch, validate_source


def get_json(url):
    response = requests.get(url, timeout=(4, 12), allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('Expected HTTP 200 without redirection')
    return response.json()


def safe_error(exc):
    # Requests/S3 exception messages may contain credentials or signed URLs.
    result = {'type': type(exc).__name__}
    if getattr(exc, 'response', None) is not None:
        result['http_status'] = exc.response.status_code
    if getattr(exc, 'code', None):
        result['code'] = exc.code
    return result


def es_check():
    root = get_json(publish.ES + '/')
    if not root.get('cluster_uuid'):
        raise ValueError('Missing Elasticsearch cluster identity')
    mappings = {}
    for index in (publish.DOCS, publish.CHUNKS, publish.CHANGES):
        result = get_json(f'{publish.ES}/{index}/_mapping')
        if set(result) != {index}:
            raise ValueError('Write target must be a concrete allowed index, not an alias')
        mappings[index] = result[index]['mappings']['properties']
    health = get_json(publish.ES + '/_cluster/health')
    if health.get('status') == 'red':
        raise ValueError('Elasticsearch cluster is red')
    return {'status': 'ok', 'cluster_uuid': root['cluster_uuid'],
            'version': root.get('version', {}).get('number'), 'health': health.get('status'),
            'mapping_fields': {k: len(v) for k, v in mappings.items()}, 'mappings': mappings}


def llm_check():
    base = os.getenv('LLM_BASE_URL', 'http://100.91.202.88:8012/v1').rstrip('/')
    response = requests.get(base + '/models', headers={'Authorization': 'Bearer ' + os.getenv('LLM_API_KEY', 'EMPTY')},
                            timeout=(4, 12), allow_redirects=False)
    response.raise_for_status()
    models = [m['id'] for m in response.json()['data']]
    model = os.getenv('LLM_MODEL', 'qwen3.6-35b')
    if model not in models:
        raise ValueError('Configured LLM model is unavailable')
    return {'status': 'ok', 'model': model, 'generation_tested': False}


def embedding_check():
    health = get_json(export.EMBEDDING_URL + '/health')
    if (health.get('model'), health.get('dims'), health.get('max_length')) != ('BAAI/bge-m3', 1024, 256):
        raise ValueError('Embedding model, dimension or context differs from the v2 contract')
    response = requests.post(export.EMBEDDING_URL + '/embed',
                             json={'texts': ['Kiểm tra luồng văn bản pháp luật.'], 'batch_size': 1}, timeout=(4, 30))
    response.raise_for_status()
    vectors = response.json()['embeddings']
    if (len(vectors) != 1 or len(vectors[0]) != 1024 or not any(vectors[0]) or
            any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) for v in vectors[0])):
        raise ValueError('Embedding response violates the v2 vector contract')
    return {'status': 'ok', 'model': health['model'], 'dims': 1024, 'generated_vectors': 1}


def ocr_check():
    from . import ocr
    health = get_json(ocr.base_url() + '/health')
    if health.get('status') != 'ok':
        raise ValueError('OCR service is not healthy')
    return {'status': 'ok'}


def minio_check():
    endpoint = os.getenv('MINIO_ENDPOINT', '').rstrip('/')
    name = store.bucket()
    if not endpoint or not ((os.getenv('MINIO_ACCESS_KEY') or os.getenv('MINIO_USERNAME')) and
                            (os.getenv('MINIO_SECRET_KEY') or os.getenv('MINIO_PASSWORD'))):
        raise ValueError('MinIO endpoint or credentials missing')
    response = requests.get(endpoint + '/minio/health/live', timeout=(4, 12), allow_redirects=False)
    response.raise_for_status()
    if response.status_code != 200:
        raise ValueError('MinIO health did not return HTTP 200')
    import urllib3
    http = urllib3.PoolManager(timeout=urllib3.Timeout(connect=4, read=12), retries=False)
    client = store.client(http_client=http)
    exists = client.bucket_exists(name)
    return {'status': 'ok', 'bucket': name, 'bucket_exists': exists,
            'would_create_bucket': not exists, 'write_permission_tested': False}


def input_check(data):
    if not data or not (data / 'state.sqlite3').is_file():
        raise ValueError('Source SQLite database is missing')
    with sqlite3.connect(f'file:{(data / "state.sqlite3").resolve()}?mode=ro', uri=True) as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT record FROM documents')]
    records = [r for r in records if r.get('status') == 'downloaded']
    if not records:
        raise ValueError('No downloaded candidate documents')
    count = 0
    for record in records:
        if not record.get('number') or not record.get('issued_date'):
            raise ValueError('Candidate lacks number/date reference')
        pdfs = [f for f in record.get('files', []) if f.get('format') == 'pdf']
        if not pdfs:
            raise ValueError('Candidate lacks a PDF')
        for file in pdfs:
            path = (data / file['path']).resolve()
            if not path.is_relative_to(data.resolve()) or digest_file(path) != file['sha256']:
                raise ValueError('Candidate file path or checksum mismatch')
            count += 1
    return {'status': 'ok', 'documents': len(records), 'pdfs': count}


def input_ocr_check(data):
    from .build import Quarantine, read_record
    with sqlite3.connect(f'file:{(data / "state.sqlite3").resolve()}?mode=ro', uri=True) as db:
        records = [json.loads(row[0]) for row in db.execute('SELECT record FROM documents ORDER BY id')]
    results = []
    for record in records:
        if record.get('status') != 'downloaded':
            continue
        try:
            _, digest, folder, _, report, _ = read_record(record, data, data / 'v2_work')
            results.append({'number': record['number'], 'status': 'eligible_for_metadata',
                            'sha256': digest, 'method': report['method'],
                            'identity': {k: report['identity'][k]['value'] for k in ['number', 'issued_date']},
                            'evidence': str(folder / 'ocr-review.json')})
        except Quarantine as exc:
            results.append({'number': record.get('number'), 'status': 'quarantined', 'reason': exc.reason})
    eligible = sum(r['status'] == 'eligible_for_metadata' for r in results)
    return {'status': 'ok' if eligible else 'blocked', 'eligible': eligible,
            'quarantined': len(results) - eligible, 'documents': results}


def draft_check(draft, data, mappings):
    docs = [json.loads(line) for line in (draft / 'documents.jsonl').read_text().splitlines() if line.strip()]
    chunks = [json.loads(line) for line in (draft / 'chunks.jsonl').read_text().splitlines() if line.strip()]
    changes_file = draft / 'changes.jsonl'
    changes = [json.loads(line) for line in changes_file.read_text().splitlines() if line.strip()] if changes_file.exists() else []
    if not docs or len({d['_id'] for d in docs}) != len(docs):
        raise ValueError('Draft is empty or contains duplicate document IDs')
    doc_ids = {d['_id'] for d in docs}
    if any(c['_source'].get('doc_id') not in doc_ids for c in chunks):
        raise ValueError('Draft contains orphan chunks')
    if any(str(c['_source'].get('source_id')) not in doc_ids for c in changes):
        raise ValueError('Draft contains relations of an unrelated document')
    if len({c['_id'] for c in chunks}) != len(chunks) or len({c['_id'] for c in changes}) != len(changes):
        raise ValueError('Draft contains duplicate chunk/relation IDs')
    statuses = []
    for doc in docs:
        did, source = doc['_id'], doc['_source']
        if not str(did).isdigit() or not ids.START <= int(did) < ids.LIMIT or not publish.own(did, source):
            raise ValueError('Draft ownership or ID is invalid')
        selected = [c for c in chunks if c['_source']['doc_id'] == did]
        related = [r for r in changes if str(r['_source']['source_id']) == did]
        errors = validate_batch(source, selected, mappings[publish.DOCS], mappings[publish.CHUNKS], did)
        errors.extend(e for r in related for e in validate_source(r['_source'], mappings[publish.CHANGES]))
        if errors:
            raise ValueError('Draft does not match the live v2 mappings')
        for row in selected + related:
            if row['_source'].get('origin') != 'crawler' or row['_source'].get('ingest_job_id') != source['ingest_job_id']:
                raise ValueError('Draft mixes provenance from different writers/runs')
        sha = source['content_sha256']
        pdf = next(data.glob(f'files/*/{sha}.pdf'), None)
        if pdf is None or digest_file(pdf) != sha or source['pdf_minio_path'] != f'{store.bucket()}/pdf/{sha}.pdf':
            raise ValueError('Draft PDF checksum or object path is invalid')
        status, detail = publish.publish_one(doc, selected,
                       {'docs': mappings[publish.DOCS], 'chunks': mappings[publish.CHUNKS]},
                       None, pdf, write=False, changes=related)
        statuses.append({'id': did, 'number': source['document_number'], 'status': status, 'detail': detail})
    return {'status': 'ok', 'documents': len(docs), 'chunks': len(chunks), 'changes': len(changes),
            'new_documents': sum(r['status'] == 'dry_run' for r in statuses), 'dry_run': statuses}


def run(data, draft=None, *, check_ocr=False):
    export.configure()
    publish.configure()
    checks = {'elasticsearch': es_check, 'llm': llm_check, 'embedding': embedding_check,
              'ocr': ocr_check, 'minio': minio_check, 'input': lambda: input_check(data)}
    if check_ocr:
        checks['ocr_inputs'] = lambda: input_ocr_check(data)
    report = {'checked_at': datetime.now(timezone.utc).isoformat(), 'read_only': True,
              'target_host': safety.host(publish.ES), 'indexes': [publish.DOCS, publish.CHUNKS, publish.CHANGES],
              'write_enabled_in_environment': safety.writes_enabled(), 'checks': {}}
    with ThreadPoolExecutor(max_workers=6) as pool:
        jobs = {name: pool.submit(check) for name, check in checks.items()}
        for name, job in jobs.items():
            try:
                report['checks'][name] = job.result()
            except Exception as exc:
                report['checks'][name] = {'status': 'blocked', 'error': safe_error(exc)}
    mappings = report['checks']['elasticsearch'].pop('mappings', None)
    if draft is not None:
        if mappings is None:
            report['checks']['draft'] = {'status': 'blocked', 'reason': 'live_es_mapping_unavailable'}
        else:
            try:
                report['checks']['draft'] = draft_check(draft, data, mappings)
            except Exception as exc:
                report['checks']['draft'] = {'status': 'blocked', 'error': safe_error(exc)}
    report['blockers'] = [name for name, result in report['checks'].items() if result['status'] != 'ok']
    report['ready_for_export'] = not [b for b in report['blockers'] if b not in {'minio', 'draft'}]
    report['ready_for_write_test'] = (draft is not None and not report['blockers'] and
                                    report['checks']['draft'].get('new_documents', 0) > 0)
    return report, mappings


def main():
    load_env(Path('.env'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--draft', type=Path)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--check-ocr', action='store_true', help='also read the inputs through the OCR/identity gates')
    args = parser.parse_args()
    report, mappings = run(args.data, args.draft, check_ocr=args.check_ocr)
    atomic_json(args.out, report)
    if mappings is not None:
        atomic_json(args.out.with_suffix('.mappings.json'), mappings)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 2 if report['blockers'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
