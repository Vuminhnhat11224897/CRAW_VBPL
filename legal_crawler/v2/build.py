"""Turn one crawled record + its PDF into ES v2-shaped Legal and Chunk documents.

Mirrors Workflow.process/publish in data_vbpl (commit 22d9796) without PostgreSQL/MinIO and the review UI.
Metadata is extracted by the Lào Cai LLM exactly as the v2 writer does (schema + verbatim evidence checks).
Anything that cannot be produced with the same certainty raises Quarantine instead of guessing.
"""
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from .contracts import normalize_metadata, same_authority, signer_parts
from . import dates as datelib
from . import ocr
from . import reader
from .extract import extract_metadata, signature_for_metadata
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
    if record.get("issuer") and not same_authority(record["issuer"], meta.get("issuing_authority")):
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
    if report.get('identity', {}).get('issues'):
        raise Quarantine('ocr_identity_unverified',
                         f"{report['identity']['issues']}; evidence: {folder / 'ocr-review.json'}")
    if report.get('review_pages'):
        raise Quarantine('ocr_needs_review',
                         f"pages {report['review_pages']}; evidence: {folder / 'ocr-review.json'}")
    return text, report, pages


def read_record(record, root, work):
    """Select a primary PDF; an attachment listed first must not hide the actual act."""
    import json
    from ..storage import atomic_json, digest_file
    files = [f for f in record.get('files', []) if f.get('format') == 'pdf']
    if not files:
        raise Quarantine('no_pdf')
    attempts = []
    for index, item in enumerate(files):
        path, digest = Path(root) / item['path'], item['sha256']
        folder = Path(work) / digest
        folder.mkdir(parents=True, exist_ok=True)
        if digest_file(path) != digest:
            raise Quarantine('pdf_checksum_mismatch', str(path))
        if index + 1 < len(files):
            # A native appendix heading is enough to skip it without OCR-ing 40 pages of forms.
            import pymupdf
            from .text import PDF_LOCK
            from .ocr_identity import header_fields
            try:
                with PDF_LOCK, pymupdf.open(path) as pdf:
                    header = pdf[0].get_text('text', sort=True)[:1000]
                attachment = (not header_fields(header)['number'] and
                              not re.search(r'(?im)^\s*Số\s*:', header) and
                              re.search(r'(?im)^\s*(?:Phụ\s+lục\b|QCVN\s*[:\d])', header))
            except (RuntimeError, ValueError):
                attachment = False
            if attachment:
                (folder / 'attachment-header.txt').write_text(header, encoding='utf-8')
                atomic_json(folder / 'ocr-review.json', {'content_sha256': digest, 'header_only': True,
                            'quality_status': 'needs_review', 'identity': {'status': 'needs_review',
                            'issues': ['primary_act_missing']}})
                attempts.append({'sha256': digest, 'reason': 'primary_act_missing',
                                 'evidence': str(folder / 'ocr-review.json')})
                atomic_json(folder / 'pdf-selection.json', {'candidate_count': len(files),
                            'status': 'attachment_skipped', 'attempts': attempts})
                continue
        try:
            text, report, pages = read_text(path, folder, record=record)
        except Quarantine as exc:
            attempts.append({'sha256': digest, 'reason': exc.reason,
                             'evidence': str(folder / 'ocr-review.json')})
            atomic_json(folder / 'pdf-selection.json', {'candidate_count': len(files),
                        'candidate_sha256': digest, 'status': 'quarantined', 'attempts': attempts})
            # Only a positively identified attachment allows trying the next PDF.
            # Wrong numbers/dates or body-quality failures are never bypassed this way.
            try:
                saved = json.loads((folder / 'ocr-review.json').read_text())
            except (OSError, ValueError):
                saved = {}
            attachment = 'primary_act_missing' in saved.get('identity', {}).get('issues', [])
            if exc.reason == 'ocr_identity_unverified' and attachment and index + 1 < len(files):
                continue
            raise
        if report.get('content_sha256') != digest:
            raise Quarantine('pdf_checksum_mismatch', str(path))
        report['pdf_selection'] = {'status': 'selected', 'selected_sha256': digest, 'selected_file': str(path),
                                   'candidate_count': len(files), 'skipped': attempts}
        atomic_json(folder / 'pdf-selection.json', report['pdf_selection'])
        return path, digest, folder, text, report, pages
    raise Quarantine('no_pdf')


def resolve_dates(record, meta, pages, warnings, identity=None):
    """Web dates win; the PDF corner rules are the fallback for the effective date and a cross-check for the rest."""
    found = datelib.locate(pages)
    if identity is not None:
        own_issue = identity.get('issued_date', {})
        if own_issue.get('status') == 'source_agreement':
            found['issue'] = {'date': own_issue['value'], 'page': 1,
                              'line': own_issue['evidence']['quote'], 'source': 'verified_identity'}
        found['sign'] = identity.get('signing_date')
        derived = (found.get('effective') or {}).get('derived_from')
        if derived:
            verified = found.get(derived)
            found['effective'] = ({**found['effective'], 'date': verified['date'],
                                   'date_source': 'verified_identity'} if verified else None)
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
    path, digest, folder, text, report, pages = read_record(record, root, work)
    from .reviewed_text import apply_review, text_hash
    from .structure import point_sequence_issues
    try:
        text, review = apply_review(text, digest, folder)
    except (ValueError, KeyError, TypeError) as exc:
        raise Quarantine('ocr_review_invalid', str(exc)) from None
    if review:
        report['reviewed_corrections'] = review
    from .reviewed_metadata import load_review
    try:
        metadata_review = load_review(folder, digest, text)
    except (ValueError, KeyError, TypeError, OSError) as exc:
        raise Quarantine('metadata_review_invalid', str(exc)) from None
    quote_resets = metadata_review.get('quote_resets', [])
    issues = point_sequence_issues(text, quote_resets)
    accepted_points = metadata_review.get('accepted_point_issues', [])
    if any(item not in issues for item in accepted_points):
        raise Quarantine('metadata_review_invalid', 'Reviewed point issue no longer matches')
    issues = [item for item in issues if item not in accepted_points]
    if issues:
        raise Quarantine('ocr_structure_needs_review', '; '.join(issues))
    # Metadata and sector caches must not survive a correction to source text.
    metadata_folder = folder / ('metadata-' + text_hash(text)[:24])
    metadata_folder.mkdir(parents=True, exist_ok=True)
    try:
        meta, evidence, warnings = extract_metadata(text, catalog, metadata_folder,
            signature=metadata_review.get('signature') or signature_for_metadata(report.get('identity',{}),pages))
    except Exception as exc:  # LLM down, schema/evidence rejected three times, ...
        raise Quarantine("llm_extraction_failed", f"{type(exc).__name__}: {str(exc)[:300]}") from None
    try:  # strict re-validation: extract_metadata only warns, the nightly writer must not
        meta = normalize_metadata(meta, catalog, no_number=False)
    except ValueError as exc:
        raise Quarantine("metadata_invalid", str(exc)) from None
    if meta["issuing_authority"] not in catalog["issuing_authority"]:
        reviewed_authority = metadata_review.get('new_authority')
        if reviewed_authority != meta['issuing_authority'] or not same_authority(record.get('issuer'), reviewed_authority):
            raise Quarantine("authority_not_in_catalog", meta["issuing_authority"])
        warnings.append('new_authority_visually_reviewed: ' + reviewed_authority)
    cross_check(meta, record)
    if os.getenv("LEGAL_SECTORS_MODE", "empty") != "llm":
        # Measured against reviewed v2 documents the model over-selects sectors (see ADR 0001), so default to [].
        meta["legal_sectors"] = []
        warnings = [w for w in warnings if not w.startswith("legal_sectors:")]
    dates = resolve_dates(record, meta, pages, warnings, report.get('identity'))
    meta["url"] = record["source_url"]
    parsed, stats = parse(text, quote_resets)
    if metadata_review:
        report['metadata_review'] = {'file': metadata_review['file'], 'reviewer': metadata_review['reviewer'],
                                    'quote_resets': len(quote_resets)}
    # Interleaved PDF columns can place standalone signature lines among recipients.
    signature_lines = {part.casefold() for part in signer_parts(meta.get('signers'))}
    parsed['noi_nhan'] = [value for value in parsed['noi_nhan'] if value.casefold() not in signature_lines]
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
