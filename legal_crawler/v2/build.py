"""Turn one crawled record + its PDF into ES v2-shaped Legal and Chunk documents.

Mirrors Workflow.process/publish in data_vbpl (commit 22d9796) without PostgreSQL/MinIO and the review UI.
Metadata is extracted by the Lào Cai LLM exactly as the v2 writer does (schema + verbatim evidence checks).
Anything that cannot be produced with the same certainty raises Quarantine instead of guessing.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .contracts import authority_key, normalize_metadata
from . import dates as datelib
from . import ocr
from . import reader
from .extract import extract_metadata
from .structure import build_chunks, parse

ORIGIN = "crawler"


class Quarantine(ValueError):
    """Record is not written: the reason is kept so a person (or a later step) can handle it."""

    def __init__(self, reason, detail=""):
        super().__init__(f"{reason}: {detail}" if detail else reason)
        self.reason = reason


def pick_pdf(record, root):
    files = [f for f in record.get("files", []) if f.get("format") == "pdf"]
    if not files:
        raise Quarantine("no_pdf", ",".join(f.get("format", "?") for f in record.get("files", [])) or "no files")
    return root / files[0]["path"], files[0]["sha256"]


def cross_check(meta, record):
    """The PDF (read by the LLM) and the source web page must agree on the identifying fields."""
    problems = []
    if (record.get("number") or "").replace(" ", "") != (meta.get("document_number") or ""):
        from .contracts import number_key
        if number_key(record.get("number")) != number_key(meta.get("document_number")):
            problems.append(f"number web={record.get('number')!r} pdf={meta.get('document_number')!r}")
    if record.get("issued_date") != meta.get("issuance_date"):
        problems.append(f"issued_date web={record.get('issued_date')!r} pdf={meta.get('issuance_date')!r}")
    if record.get("document_type") and record["document_type"].casefold() != (meta.get("legal_type") or "").casefold():
        problems.append(f"legal_type web={record['document_type']!r} pdf={meta.get('legal_type')!r}")
    if record.get("issuer") and authority_key(record["issuer"]) != authority_key(meta.get("issuing_authority")):
        problems.append(f"issuer web={record['issuer']!r} pdf={meta.get('issuing_authority')!r}")
    if problems:
        raise Quarantine("metadata_conflict", "; ".join(problems))


def read_text(path, folder=None, *, record=None):
    """Choose native/OCR per page and stop before extraction if review is needed."""
    path = Path(path)
    folder = Path(folder) if folder is not None else path.parent / 'ocr_work' / path.stem
    try:
        text, report, pages = reader.read(path, folder, record=record)
    except ocr.OCRUnavailable as exc:
        raise Quarantine("ocr_unavailable", str(exc)) from None
    except ocr.OCRConfigurationError as exc:
        raise Quarantine("ocr_config_invalid", str(exc)) from None
    except ocr.OCRRejected as exc:
        raise Quarantine("ocr_failed", str(exc)) from None
    except ValueError as exc:
        raise Quarantine("unreadable", str(exc)) from None
    if report.get('review_pages'):
        raise Quarantine('ocr_needs_review',
                         f"pages {report['review_pages']}; evidence: {folder / 'ocr-review.json'}")
    return text, report, pages


def resolve_dates(record, meta, pages, warnings):
    """Web dates win; the PDF corner rules are the fallback for the effective date and a cross-check for the rest."""
    found = datelib.locate(pages)
    issue = found["issue"]["date"] if found["issue"] else None
    if issue and issue != meta["issuance_date"]:
        warnings.append(f"issue_date_corner_mismatch: pdf corner {issue} vs extracted {meta['issuance_date']}")
    effective, source = record.get("effective_date"), "web"
    if not effective and meta.get("legal_type") == "Văn bản hợp nhất":
        found["effective"] = None  # a consolidated text quotes the original act's dates; it has no effective date of its own
    if not effective and found["effective"]:
        effective, source = found["effective"]["date"], "pdf_corner"
    elif not effective:
        source = None
    start = os.getenv("DATE_FROM", "")
    if start and ((meta["issuance_date"] < start) or (effective and effective < start)):
        raise Quarantine("outside_date_range", f"issued {meta['issuance_date']}, effective {effective} before {start}")
    return {"web": {"issued": record.get("issued_date"), "effective": record.get("effective_date")},
            "pdf": found, "effective": effective, "effective_source": source}


def prepare(record, root, catalog, work):
    """Read text, extract metadata with the LLM, parse structure. No ID needed yet; safe to run in parallel."""
    path, digest = pick_pdf(record, root)
    folder = work / digest
    folder.mkdir(parents=True, exist_ok=True)
    text, report, pages = read_text(path, folder, record=record)
    try:
        meta, evidence, warnings = extract_metadata(text, catalog, folder)
    except Exception as exc:  # LLM down, schema/evidence rejected three times, ...
        raise Quarantine("llm_extraction_failed", f"{type(exc).__name__}: {str(exc)[:300]}") from None
    try:  # strict re-validation: extract_metadata only warns, the nightly writer must not
        meta = normalize_metadata(meta, catalog, no_number=False)
    except ValueError as exc:
        raise Quarantine("metadata_invalid", str(exc)) from None
    if meta["issuing_authority"] not in catalog["issuing_authority"]:
        raise Quarantine("authority_not_in_catalog", meta["issuing_authority"])
    cross_check(meta, record)
    if os.getenv("LEGAL_SECTORS_MODE", "empty") != "llm":
        # Measured against reviewed v2 documents the model over-selects sectors (see ADR 0001), so default to [].
        meta["legal_sectors"] = []
        warnings = [w for w in warnings if not w.startswith("legal_sectors:")]
    dates = resolve_dates(record, meta, pages, warnings)
    meta["url"] = record["source_url"]
    parsed, stats = parse(text)
    if stats["grade"] != "full":
        warnings.append("grade_" + stats["grade"])
    if not meta.get("signers"):
        warnings.append("signers_missing")
    return {"meta": meta, "text": text, "report": report, "parsed": parsed, "stats": stats,
            "digest": digest, "warnings": warnings, "dates": dates}


def assemble(prepared, doc_id, bucket=None, run_id="", now=None):
    """Return {'document': source, 'chunks': [{'_id','_source'}], ...} (no embeddings yet)."""
    meta, parsed, stats = prepared["meta"], prepared["parsed"], prepared["stats"]
    provenance = {"origin": ORIGIN, "ingest_job_id": run_id, "ingest_revision": 1,
                  "ingested_at": (now or datetime.now(timezone.utc)).isoformat(), "uploaded_by": ORIGIN}
    digest = prepared["digest"]
    bucket = bucket or os.getenv("CRAWLER_BUCKET", "legal-crawler")  # never data_vbpl's legal-upload bucket
    document = {**meta,
                **{k: parsed[k] for k in ("can_cu", "can_cu_refs", "intro", "noi_nhan", "phu_luc", "has_tail")},
                "full_content": prepared["text"], "n_dieu": stats["n_dieu"], "grade": stats["grade"],
                "text_source": prepared["report"]["method"], "content_sha256": digest,
                "pdf_minio_path": f"{bucket}/pdf/{digest}.pdf", "pdf_vbpl_id": None, **provenance}
    chunks = build_chunks(doc_id, meta, parsed)
    if not chunks:
        raise Quarantine("no_chunks", "parser produced no useful chunk")
    for chunk in chunks:
        chunk["_source"].update(provenance)
    return {"document": document, "chunks": chunks, "warnings": prepared["warnings"], "dates": prepared["dates"],
            "report": {"stats": stats, "text": {k: v for k, v in prepared["report"].items() if k != "warnings"}}}


def embedding_inputs(document, chunks):
    return [document["title"]] + [" ".join((c["_source"].get("tieu_de", "") + " " + c["_source"].get("noi_dung", "")).split())
                                  for c in chunks]


def attach_embeddings(document, chunks, vectors):
    rows = [document] + [c["_source"] for c in chunks]
    for row, vector in zip(rows, vectors, strict=True):
        row["embedding"] = vector
