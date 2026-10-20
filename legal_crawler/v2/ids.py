"""Document ID allocation: smallest ID >= CRAWLER_ID_START that is free in all three v2 indexes.

An ID is free only if no document has it AND no chunk carries it as doc_id AND no relation has it as source_id
(a deleted document can leave orphans behind; reusing its ID would silently attach them to a new document).
"""
import os

import requests

from . import safety

START = int(os.getenv("CRAWLER_ID_START", "10100000"))
LIMIT = 2_147_483_647  # source_id / target_matched_id are 32-bit integers in legal-changed-v2
BATCH = 500


def _taken(base, indexes, candidates):
    docs_index, chunks_index, changes_index = indexes
    names = [str(c) for c in candidates]
    taken = set()
    found = requests.post(f"{base}/{docs_index}/_search", json={"size": len(names), "_source": False, "query": {"ids": {"values": names}}}, timeout=60).json()
    taken |= {int(h["_id"]) for h in found["hits"]["hits"]}
    chunks = requests.post(f"{base}/{chunks_index}/_search", json={"size": 0, "query": {"terms": {"doc_id": names}},
                           "aggs": {"d": {"terms": {"field": "doc_id", "size": len(names)}}}}, timeout=60).json()
    taken |= {int(b["key"]) for b in chunks["aggregations"]["d"]["buckets"]}
    relations = requests.post(f"{base}/{changes_index}/_search", json={"size": 0, "query": {"terms": {"source_id": candidates}},
                              "aggs": {"d": {"terms": {"field": "source_id", "size": len(names)}}}}, timeout=60).json()
    taken |= {int(b["key"]) for b in relations["aggregations"]["d"]["buckets"]}
    return taken


def next_free(base, state, indexes=("legal-docs", "legal-chunk-section-v2", "legal-changed-v2")):
    """Allocate one ID and advance state['next_id'] past it. IDs only ever increase within a state file."""
    safety.forbid_cluster_10(base)
    base = base.rstrip("/")
    cursor = max(int(state.get("next_id", START)), START)
    reserved = {int(v) for v in state.get("ids", {}).values()}
    while cursor < LIMIT:
        candidates = list(range(cursor, min(cursor + BATCH, LIMIT)))
        taken = _taken(base, indexes, candidates) | reserved
        for candidate in candidates:
            if candidate not in taken:
                state["next_id"] = candidate + 1
                return candidate
        cursor += BATCH
    raise RuntimeError("No free document ID left")
