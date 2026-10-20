"""Build DRAFT ES v2-shaped JSONL under <data>/v2_draft/<run>/, validate and compare with ES. Never writes to Elasticsearch.

    python -m legal_crawler.v2.export --limit 20            # build + embed + validate
    python -m legal_crawler.v2.export --no-embed            # skip BGE-M3 (structure checks only)
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import json
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests

from . import build as builder
from . import ids, safety
from .compare import compare_all, summarize
from .changes import Lookup, Meter, build_changes
from .llm import concurrency, max_workers
from .validate import validate_batch

SOURCE_ES = os.getenv("SOURCE_ES_URL", "http://10.1.10.23:9200")
TARGET_ES = os.getenv("TARGET_ES_URL", "http://10.1.10.23:9200")
EMBEDDING_URL = os.getenv("EMBEDDING_URL", "http://100.119.143.66:8989")
PERMANENT = {"metadata_conflict", "metadata_invalid", "authority_not_in_catalog", "no_pdf", "outside_date_range", "unreadable", "no_chunks"}


def es_json(url, body=None):
    safety.forbid_cluster_10(url)
    response = requests.post(url, json=body, timeout=60) if body is not None else requests.get(url, timeout=60)
    response.raise_for_status()
    return response.json()


def catalog(index="legal-docs"):
    result = es_json(f"{SOURCE_ES}/{index}/_search", {"size": 0, "aggs": {
        key: {"terms": {"field": key, "size": 10000}} for key in ("action", "legal_type", "legal_sectors", "issuing_authority")}})
    if any(agg["sum_other_doc_count"] for agg in result["aggregations"].values()):
        raise RuntimeError("ES catalog was truncated")
    return {key: [b["key"] for b in agg["buckets"]] for key, agg in result["aggregations"].items()}


def properties(base, index):
    return list(es_json(f"{base}/{index}/_mapping").values())[0]["mappings"]["properties"]


def embed(texts):
    health = requests.get(EMBEDDING_URL + "/health", timeout=30).json()
    if (health.get("model"), health.get("dims"), health.get("max_length")) != ("BAAI/bge-m3", 1024, 256):
        raise RuntimeError(f"Unexpected embedding service: {health}")
    vectors = []
    for start in range(0, len(texts), 64):
        response = requests.post(EMBEDDING_URL + "/embed", json={"texts": texts[start:start + 64], "batch_size": 64}, timeout=(10, 180))
        response.raise_for_status()
        vectors += response.json()["embeddings"]
    return vectors


def main():
    from ..settings import load_env
    load_env(Path(".env"))
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", type=Path, default=Path("data_trial"))
    parser.add_argument("--out", type=Path)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--no-embed", action="store_true")
    parser.add_argument("--new-only", action="store_true", help="skip documents already drafted or permanently rejected")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    import sqlite3
    db = sqlite3.connect(args.data / "state.sqlite3")
    records = [json.loads(r) for (r,) in db.execute("SELECT record FROM documents ORDER BY id")]
    records = [r for r in records if r.get("status") == "downloaded"]
    state_path = args.data / "v2_draft" / "state.json"
    state = json.loads(state_path.read_text()) if state_path.exists() else {"next_id": ids.START, "ids": {}, "rejected": {}}
    if args.new_only:
        records = [r for r in records if r["id"] not in state["ids"] and r["id"] not in state["rejected"]]
    if args.limit:
        records = records[:args.limit]
    cat = catalog()
    doc_props = {"v2": properties(SOURCE_ES, "legal-docs"), "v3": properties(TARGET_ES, "legal-docs-v3")}
    chunk_props = {"v2": properties(SOURCE_ES, "legal-chunk-section-v2"), "v3": properties(TARGET_ES, "legal-chunk-section-v3")}
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out = args.out or args.data / "v2_draft" / stamp
    out.mkdir(parents=True, exist_ok=True)
    run_id = "crawler:" + stamp
    ok, quarantined, bad = [], [], []
    work = args.data / "v2_work"

    def prepare(record):
        try:
            return record, builder.prepare(record, args.data, cat, work), None
        except builder.Quarantine as exc:
            return record, None, exc

    workers = max_workers()
    logging.info("LLM concurrency now: %d (pool %d)", concurrency(), workers)
    with ThreadPoolExecutor(workers) as pool:
        prepared = list(pool.map(prepare, records))
    meter = Meter()
    lookup = Lookup(SOURCE_ES, "legal-docs", meter)
    change_props = properties(SOURCE_ES, "legal-changed-v2")
    all_changes = []
    for record, item, failure in prepared:
        if failure:
            quarantined.append({"id": record["id"], "number": record.get("number"), "source": record["source"],
                                "reason": failure.reason, "detail": str(failure)})
            if failure.reason in PERMANENT:  # transient reasons (ocr_unavailable, llm failure) are retried next run
                state["rejected"][record["id"]] = failure.reason
            continue
        doc_id = state["ids"].get(record["id"]) or ids.next_free(SOURCE_ES, state)  # reuse the ID of a record drafted before; smallest free ID >= CRAWLER_ID_START, free in docs, chunks and relations
        result = builder.assemble(item, doc_id, run_id=run_id)
        if not args.no_embed:
            builder.attach_embeddings(result["document"], result["chunks"],
                                      embed(builder.embedding_inputs(result["document"], result["chunks"])))
        changes = build_changes(doc_id, result["document"], item["parsed"], work / "changes" / str(doc_id), lookup, meter, clarify=True)
        result["changes"] = changes
        from .validate import validate_source
        change_errors = [f"change {c['_id']}.{e}" for c in changes["rows"] for e in validate_source(c["_source"], change_props)]
        errors = {}
        for version in ("v2", "v3"):
            if args.no_embed:  # vectors are validated only when generated
                props = {k: v for k, v in doc_props[version].items() if k != "embedding"}
                cprops = {k: v for k, v in chunk_props[version].items() if k != "embedding"}
            else:
                props, cprops = doc_props[version], chunk_props[version]
            errors[version] = validate_batch(result["document"], result["chunks"], props, cprops, doc_id)
        if change_errors:
            errors["changes"] = change_errors
        if any(errors.values()):
            bad.append({"id": record["id"], "number": record.get("number"), "errors": errors})
            continue
        ok.append((record, doc_id, result))
        all_changes += changes["rows"]
        state["ids"][record["id"]] = doc_id  # kept so a rerun never hands out a second ID for the same record

    with (out / "documents.jsonl").open("w", encoding="utf-8") as docs, (out / "chunks.jsonl").open("w", encoding="utf-8") as chunks:
        for record, did, result in ok:
            docs.write(json.dumps({"_id": str(did), "_source": result["document"], "crawl_id": record["id"]}, ensure_ascii=False) + "\n")
            for chunk in result["chunks"]:
                chunks.write(json.dumps(chunk, ensure_ascii=False) + "\n")
    warned = [{"number": r["document"]["document_number"], "warnings": r["warnings"]} for _, _, r in ok if r["warnings"]]
    (out / "warnings.json").write_text(json.dumps(warned, ensure_ascii=False, indent=1), encoding="utf-8")
    compared = compare_all(ok, SOURCE_ES, TARGET_ES) if ok else []
    (out / "compare.json").write_text(json.dumps({"summary": summarize(compared), "documents": compared}, ensure_ascii=False, indent=1), encoding="utf-8")
    with (out / "changes.jsonl").open("w", encoding="utf-8") as handle:
        for row in all_changes:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    summary = {"relations": {"rows": len(all_changes), "groups": {r["document"]["document_number"]: r["changes"]["groups"] for _, _, r in ok if r["changes"]["groups"]}, "cost": meter.report()}, "run_id": run_id, "candidates": len(records), "valid": len(ok), "invalid": len(bad),
               "quarantined": len(quarantined), "quarantine_reasons": {},
               "chunks": sum(len(r["chunks"]) for _, _, r in ok),
               "with_warnings": len(warned), "compare": summarize(compared), "grades": {}, "text_sources": {}}
    for item in quarantined:
        summary["quarantine_reasons"][item["reason"]] = summary["quarantine_reasons"].get(item["reason"], 0) + 1
    for _, _, result in ok:
        for key, field in (("grades", "grade"), ("text_sources", "text_source")):
            summary[key][result["document"][field]] = summary[key].get(result["document"][field], 0) + 1
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "quarantine.json").write_text(json.dumps(quarantined, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "invalid.json").write_text(json.dumps(bad, ensure_ascii=False, indent=1), encoding="utf-8")
    (out / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
