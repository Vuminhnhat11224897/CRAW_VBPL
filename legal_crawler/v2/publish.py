"""Write reviewed drafts to Elasticsearch (v2 indexes on cluster 23). Dry-run unless --write AND ES_WRITE_ENABLED=yes.

Order per document: re-check duplicate -> PDF to MinIO -> bulk-create chunks -> create the document last
(the document is what search lists, so a half-written document is never visible) -> read back and verify.
Any failure removes only what this run created (IDs >= CRAWLER_ID_FLOOR, origin=crawler).
"""
import argparse
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import ids, safety, store
from .compare import find
from .validate import validate_batch
from ..storage import atomic_json

log = logging.getLogger("publish")
ES = os.getenv("TARGET_ES_URL", "http://10.1.10.23:9200").rstrip("/")
DOCS = os.getenv("TARGET_DOCUMENT_INDEX", "legal-docs")
CHUNKS = os.getenv("TARGET_CHUNK_INDEX", "legal-chunk-section-v2")
CHANGES = os.getenv("TARGET_CHANGE_INDEX", "legal-changed-v2")


def configure():
    """CLI loads .env after imports; apply the actual settings before checking targets."""
    global ES, DOCS, CHUNKS, CHANGES
    ES = os.getenv('TARGET_ES_URL', 'http://10.1.10.23:9200').rstrip('/')
    DOCS = os.getenv('TARGET_DOCUMENT_INDEX', 'legal-docs')
    CHUNKS = os.getenv('TARGET_CHUNK_INDEX', 'legal-chunk-section-v2')
    CHANGES = os.getenv('TARGET_CHANGE_INDEX', 'legal-changed-v2')
    safety.check_write(ES, DOCS, CHUNKS, CHANGES)


def req(method, path, **kw):
    response = requests.request(method, ES + path, timeout=120, **kw)
    return response


def own(doc_id, source):
    """Crawler documents live among legacy IDs, so ownership is by provenance, never by ID range."""
    return source.get("origin") == "crawler" and str(source.get("ingest_job_id", "")).startswith("crawler:") and str(doc_id).isdigit()


def bulk_create(index, rows):
    body = "".join(json.dumps({"create": {"_index": index, "_id": r["_id"]}}) + "\n" + json.dumps(r["_source"], ensure_ascii=False) + "\n" for r in rows)
    result = req("POST", "/_bulk", data=body.encode(), headers={"Content-Type": "application/x-ndjson"}).json()
    created = [i["create"]["_id"] for i in result["items"] if i["create"].get("status") in (200, 201)]
    errors = [i["create"].get("error") for i in result["items"] if "error" in i["create"]]
    return created, errors


def rollback(doc_id, chunk_ids, change_ids=(), doc_created=False):
    """Remove only what THIS run created: IDs whose _create returned 201. Never a search- or range-based delete."""
    safety.check_write(ES, DOCS, CHUNKS, CHANGES)
    for index, items in ((CHANGES, change_ids), (CHUNKS, chunk_ids)):
        if items:
            body = "".join(json.dumps({"delete": {"_index": index, "_id": i}}) + "\n" for i in items)
            req("POST", "/_bulk", data=body.encode(), headers={"Content-Type": "application/x-ndjson"})
    if doc_created:
        req("DELETE", f"/{DOCS}/_doc/{doc_id}")


def publish_one(doc, chunks, props, minio, pdf_path, write, changes=()):
    """Returns ('written'|'dry_run'|'duplicate'|'exists', detail). Raises on failure after rolling back."""
    doc_id, source = doc["_id"], doc["_source"]
    safety.check_write(ES, DOCS, CHUNKS, CHANGES)
    if not own(doc_id, source):
        raise safety.WriteForbidden(f"{doc_id}: not a crawler document")
    errors = validate_batch(source, chunks, props["docs"], props["chunks"], doc_id)
    if not chunks:
        errors.append('document has no chunks')
    if not source.get('embedding') or any(not c['_source'].get('embedding') for c in chunks):
        errors.append('document and every chunk require embeddings before publish')
    if errors:
        raise ValueError(f"{doc_id}: schema errors: {errors[:3]}")
    if req("GET", f"/{DOCS}/_doc/{doc_id}", params={"_source": "false"}).status_code == 200:
        return "exists", f"ID {doc_id} already present"
    in_use = ids._taken(ES, (DOCS, CHUNKS, CHANGES), [int(doc_id)])  # legacy IDs share this space: refuse any ID with leftovers
    if in_use:
        return "exists", f"ID {doc_id} is already used in documents, chunks or relations"
    match = find(ES, DOCS, source["document_number"], source["issuance_date"])  # race with other writers
    if match:
        return "duplicate", f"same number+date as {match[0]['_id']}"
    if not write:
        return "dry_run", f"would write {len(chunks)} chunks + document + {len(changes)} relations, PDF {source['pdf_minio_path']}"
    created, created_changes, doc_created = [], [], False
    try:
        path = store.upload_pdf(minio, pdf_path, source["content_sha256"], create_bucket=True)
        if path != source["pdf_minio_path"]:
            raise RuntimeError(f"pdf_minio_path mismatch: {path} != {source['pdf_minio_path']}")
        body = "".join(json.dumps({"create": {"_index": CHUNKS, "_id": c["_id"]}}) + "\n" + json.dumps(c["_source"], ensure_ascii=False) + "\n"
                       for c in chunks)
        result = req("POST", "/_bulk", data=body.encode(), headers={"Content-Type": "application/x-ndjson"}).json()
        created = [i["create"]["_id"] for i in result["items"] if i["create"].get("status") in (200, 201)]
        if result["errors"]:
            raise RuntimeError("bulk errors: " + json.dumps([i["create"].get("error") for i in result["items"] if "error" in i["create"]][:2], ensure_ascii=False))
        response = req("PUT", f"/{DOCS}/_create/{doc_id}", json=source)
        if response.status_code not in (200, 201):
            raise RuntimeError(f"document create failed: HTTP {response.status_code} {response.text[:200]}")
        doc_created = True
        if changes:  # relations last: they point at a document that already exists
            if any(c["_source"].get("source_id") != int(doc_id) for c in changes):
                raise RuntimeError("relation source_id does not match the document")
            created_changes, change_errors = bulk_create(CHANGES, changes)
            if change_errors:
                raise RuntimeError("relation bulk errors: " + json.dumps(change_errors[:2], ensure_ascii=False))
        req("POST", f"/{CHUNKS},{DOCS},{CHANGES}/_refresh")
        stored = req("GET", f"/{DOCS}/_doc/{doc_id}").json()["_source"]
        count = req("POST", f"/{CHUNKS}/_count", json={"query": {"term": {"doc_id": str(doc_id)}}}).json()["count"]
        related = req("POST", f"/{CHANGES}/_count", json={"query": {"term": {"source_id": int(doc_id)}}}).json()["count"] if changes else 0
        if related != len(changes):
            raise RuntimeError(f"verification failed: relations {related}/{len(changes)}")
        if count != len(chunks) or stored.get("content_sha256") != source["content_sha256"] or len(stored.get("embedding", [])) != 1024:
            raise RuntimeError(f"verification failed: chunks {count}/{len(chunks)}")
    except Exception:
        rollback(doc_id, created, created_changes, doc_created)
        raise
    return "written", f"{len(chunks)} chunks"


def main():
    from ..settings import load_env
    load_env(Path(".env"))
    configure()
    parser = argparse.ArgumentParser()
    parser.add_argument("--draft", type=Path, required=True, help="a data/v2_draft/<run> folder")
    parser.add_argument("--data", type=Path, default=Path("data"))
    parser.add_argument("--write", action="store_true")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", action="append", default=[], help="write only these document IDs")
    parser.add_argument('--report', type=Path, help='receipt with statuses and exact IDs; defaults inside the draft')
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    safety.check_write(ES, DOCS, CHUNKS)
    write = args.write and safety.writes_enabled()
    if args.write and not write:
        log.error("--write given but ES_WRITE_ENABLED is not 'yes': staying in dry-run")
    receipt_path = args.report or args.draft / 'publish-report.json'
    receipt_report = {'started_at': datetime.now(timezone.utc).isoformat(), 'completed': False,
                      'write': write, 'host': safety.host(ES), 'indexes': [DOCS, CHUNKS, CHANGES],
                      'summary': {}, 'documents': []}
    atomic_json(receipt_path, receipt_report)
    props = {"docs": list(requests.get(f"{ES}/{DOCS}/_mapping", timeout=60).json().values())[0]["mappings"]["properties"],
             "chunks": list(requests.get(f"{ES}/{CHUNKS}/_mapping", timeout=60).json().values())[0]["mappings"]["properties"]}
    docs = [json.loads(line) for line in (args.draft / "documents.jsonl").open(encoding="utf-8")]
    changes_by_doc = {}
    changes_file = args.draft / "changes.jsonl"
    if changes_file.exists():
        for line in changes_file.open(encoding="utf-8"):
            row = json.loads(line)
            changes_by_doc.setdefault(str(row["_source"]["source_id"]), []).append(row)
    by_doc = {}
    for line in (args.draft / "chunks.jsonl").open(encoding="utf-8"):
        chunk = json.loads(line)
        by_doc.setdefault(chunk["_source"]["doc_id"], []).append(chunk)
    if args.only:
        docs = [d for d in docs if d["_id"] in set(args.only)]
        if not docs:
            receipt_report.update(completed=True, error='requested_ids_not_in_draft')
            atomic_json(receipt_path, receipt_report)
            log.error('Requested IDs do not exist in this draft')
            return 1
    if args.limit:
        docs = docs[:args.limit]
    minio = store.client() if write else None
    tally, failed, receipts = {}, 0, []
    for doc in docs:
        sha = doc["_source"]["content_sha256"]
        pdf = next(Path(args.data).glob(f"files/*/{sha}.pdf"), None)
        try:
            status, detail = publish_one(doc, by_doc.get(doc["_id"], []), props, minio, pdf, write, changes_by_doc.get(doc["_id"], []))
        except Exception as exc:  # noqa: BLE001 - keep going, report
            status, detail, failed = "failed", f"{type(exc).__name__}: {exc}"[:300], failed + 1
        tally[status] = tally.get(status, 0) + 1
        receipt = {'id': doc['_id'], 'number': doc['_source']['document_number'], 'status': status,
                   'detail': detail, 'ingest_job_id': doc['_source'].get('ingest_job_id'),
                   'content_sha256': sha, 'created_document': status == 'written',
                   'created_chunks': [c['_id'] for c in by_doc.get(doc['_id'], [])] if status == 'written' else [],
                   'created_changes': [c['_id'] for c in changes_by_doc.get(doc['_id'], [])] if status == 'written' else []}
        receipts.append(receipt)
        receipt_report.update(summary=tally, documents=receipts)
        atomic_json(receipt_path, receipt_report)
        log.info("%s %s %s: %s", doc["_id"], doc["_source"]["document_number"], status, detail)
    log.info("Summary %s (write=%s)", tally, write)
    receipt_report['completed'] = True
    atomic_json(receipt_path, receipt_report)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
