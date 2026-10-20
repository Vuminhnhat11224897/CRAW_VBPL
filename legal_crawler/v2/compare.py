"""Compare drafts with what already exists in Elasticsearch (read-only) and with the source web page."""
import re
import unicodedata

import requests

from .contracts import number_key

FIELDS = ["title", "legal_type", "issuing_authority", "issuance_date", "signers", "action", "n_dieu", "grade",
          "has_tail", "can_cu", "can_cu_refs", "noi_nhan", "intro", "phu_luc"]
SKIP_CHUNK = {"doc_id", "origin", "ingest_job_id", "ingest_revision", "ingested_at", "uploaded_by", "embedding", "url",
              "dieu_uid", "khoan_uid"}


def search(base, index, body):
    from . import safety
    safety.forbid_cluster_10(base)
    response = requests.post(f"{base}/{index}/_search", json=body, timeout=60)
    response.raise_for_status()
    return response.json()["hits"]["hits"]


def find(base, index, number, issued):
    variants = list(dict.fromkeys(v for n in (number, number.upper(), number.lower()) for v in (n, re.sub(r"\s+", "", n))))
    hits = search(base, index, {"size": 20, "_source": {"excludes": ["embedding"]}, "query": {"bool": {"filter": [
        {"terms": {"document_number": variants}},
        {"range": {"issuance_date": {"gte": issued, "lte": issued, "format": "yyyy-MM-dd"}}}]}}})
    return [h for h in hits if number_key(h["_source"].get("document_number")) == number_key(number)]


def chunks_of(base, index, doc_id):
    out, after = [], None
    while True:
        body = {"size": 1000, "query": {"term": {"doc_id": str(doc_id)}}, "sort": [{"_doc": "asc"}],
                "_source": {"excludes": ["embedding"]}}
        if after:
            body["search_after"] = after
        hits = search(base, index, body)
        out += hits
        if len(hits) < 1000:
            return out
        after = hits[-1]["sort"]


def strip_id(chunk_id, doc_id):
    return chunk_id[len(str(doc_id)):] if chunk_id.startswith(str(doc_id)) else chunk_id


def diff_against(document, chunks, doc_id, hit, base, chunk_index):
    source = hit["_source"]
    fields = [f for f in FIELDS if document.get(f) != source.get(f)]
    mine = {strip_id(c["_id"], doc_id): c["_source"] for c in chunks}
    theirs = {strip_id(c["_id"], hit["_id"]): c["_source"] for c in chunks_of(base, chunk_index, hit["_id"])}
    differing = sorted({k for i in mine.keys() & theirs.keys() for k in (mine[i].keys() | theirs[i].keys())
                        if k not in SKIP_CHUNK and mine[i].get(k) != theirs[i].get(k)})
    return {"es_id": hit["_id"], "fields_differ": fields, "chunks": {"draft": len(mine), "es": len(theirs),
            "only_in_draft": sorted(mine.keys() - theirs.keys())[:10], "only_in_es": sorted(theirs.keys() - mine.keys())[:10],
            "chunk_fields_differ": differing}}


def compare_all(items, source_es, target_es, *, include_v3=False):
    """items: [(record, doc_id, result)] -> per-document report."""
    report = []
    for record, doc_id, result in items:
        document = result["document"]
        number, issued = document["document_number"], document["issuance_date"]
        row = {"number": number, "issued": issued, "draft_id": str(doc_id), "source": record["source"],
               "warnings": result["warnings"], "dates": result["dates"],
               "in_v2": [], "in_v3": []}
        targets = [("in_v2", source_es, "legal-docs", "legal-chunk-section-v2")]
        if include_v3:
            targets.append(("in_v3", target_es, "legal-docs-v3", "legal-chunk-section-v3"))
        for key, base, index, cindex in targets:
            for hit in find(base, index, number, issued):
                row[key].append(diff_against(document, result["chunks"], doc_id, hit, base, cindex))
        row["would_skip"] = bool(row["in_v2"] or row["in_v3"])
        report.append(row)
    return report


def summarize(report):
    n = len(report)
    skip = [r for r in report if r["would_skip"]]
    fresh = [r for r in report if not r["would_skip"]]
    web_vs_pdf = {"issue_corner_match": 0, "issue_corner_found": 0, "effective_web": 0, "effective_corner_found": 0,
                  "effective_corner_equals_web": 0, "sign_found": 0}
    for r in report:
        d = r["dates"]
        corner = d["pdf"]
        if corner["issue"]:
            web_vs_pdf["issue_corner_found"] += 1
            web_vs_pdf["issue_corner_match"] += corner["issue"]["date"] == r["issued"]
        if corner["sign"]:
            web_vs_pdf["sign_found"] += 1
        if d["web"]["effective"]:
            web_vs_pdf["effective_web"] += 1
        if corner["effective"]:
            web_vs_pdf["effective_corner_found"] += 1
            web_vs_pdf["effective_corner_equals_web"] += corner["effective"]["date"] == d["web"]["effective"]
    differs = [r for r in skip for m in r["in_v2"] + r["in_v3"] if m["fields_differ"] or m["chunks"]["only_in_draft"]
               or m["chunks"]["only_in_es"] or m["chunks"]["chunk_fields_differ"]]
    return {"drafts": n, "new_would_be_written": len(fresh), "already_in_es_would_skip": len(skip),
            "already_in_es_but_differs_from_draft": len(differs), "dates": web_vs_pdf}
