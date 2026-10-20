"""Compare whole-page OCR with fixed crops, without LLM, publishing or source edits.

python -m legal_crawler.v2.ocr_benchmark --data data_benchmark_30 \
    --manifest data_benchmark_30/manifest.json --out data_ocr_regions_30
"""
import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
from pathlib import Path
import re
import tempfile
import time

from ..settings import load_env
from . import ocr
from .ocr_identity import choose, header_fields, read_signature, signature_page_hint
from .ocr_quality import comparison_text
from .ocr_regions import RegionOCR
from .reader import _write_json
from .text import PDF_LOCK


def word_error(reference, actual):
    """Exact token edit distance, linear memory; 1-WER is a reference agreement proxy."""
    a = re.findall(r'\w+', comparison_text(reference))
    b = re.findall(r'\w+', comparison_text(actual))
    previous = list(range(len(b) + 1))
    for i, word in enumerate(a, 1):
        row = [i]
        for j, other in enumerate(b, 1):
            row.append(min(row[-1] + 1, previous[j] + 1, previous[j - 1] + (word != other)))
        previous = row
    edits = previous[-1]
    return {'reference_tokens': len(a), 'token_edits': edits, 'WER': edits / max(1, len(a))}


class Benchmark:
    def __init__(self, data, out):
        self.data, self.out = Path(data), Path(out)

    def whole(self, record, number):
        import pymupdf
        folder = self.out / 'whole' / record['sha256'][:16]
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f'{number:04d}.json'
        profile = {'digest': record['sha256'], 'page': number, 'scale': 2,
                   'url': ocr.base_url(), 'version': 1}
        try:
            saved = json.loads(output.read_text())
            if saved.get('profile') == profile and isinstance(saved.get('text'), str):
                return {**saved, 'cached': True}
        except (OSError, ValueError, AttributeError):
            pass
        with tempfile.TemporaryDirectory(prefix='ocr-bench-') as temp:
            path = Path(temp) / 'page.pdf'
            with PDF_LOCK, pymupdf.open(self.data / record['file']) as src, pymupdf.open() as dst:
                dst.insert_pdf(src, from_page=number - 1, to_page=number - 1)
                dst.save(path)
            started = time.monotonic()
            text, _ = ocr.ocr_text(path, scale=2, min_chars=0)
        result = {'profile': profile, 'text': text, 'seconds': round(time.monotonic() - started, 3), 'cached': False}
        _write_json(output, result)
        (folder / f'{number:04d}.txt').write_text(text, encoding='utf-8')
        return result

    def signature_page(self, record):
        """Optional saved page text can locate a signature before an appendix."""
        import pymupdf
        folder = self.data / 'documents' / record['sha256'][:16]
        with PDF_LOCK:
            doc = pymupdf.open(self.data / record['file'])
        try:
            for i in range(len(doc)):
                candidates = [folder / f'{i+1:04d}.native.txt', folder / f'{i+1:04d}.baseline.txt']
                with PDF_LOCK:
                    native = doc[i].get_text('text')
                text = native + '\n' + '\n'.join(p.read_text() for p in candidates if p.exists())
                if signature_page_hint(text):
                    return i + 1
        finally:
            with PDF_LOCK:
                doc.close()
        return None

    def header(self, record):
        import pymupdf
        folder = self.out / 'documents' / record['sha256'][:16]
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / 'header.json'
        # Recompute decisions against today's reference; only model outputs are cached.
        started = time.monotonic()
        result = {k: record.get(k) for k in ('rank', 'number', 'issued_date')}
        result.update(file=str(self.data / record['file']), digest=record['sha256'], status='error')
        try:
            baseline = self.whole(record, 1)
            result['whole'] = baseline
            fields = header_fields(baseline['text'])
            result['whole_fields'] = fields
            regional = RegionOCR(self.out / 'regions', record['sha256'])
            with PDF_LOCK:
                doc = pymupdf.open(self.data / record['file'])
                page = doc[0]
            try:
                crops = []
                observations = {f: [{**o, 'origin': 'whole'} for o in fields[f]] for f in fields}
                for name, field in [('number', 'number'), ('issue_date', 'issued_date')]:
                    crop = regional.read(page, name)
                    crops.append(crop)
                    extracted = header_fields(crop['text'])[field]
                    observations[field].extend({**o, 'origin': name} for o in extracted)
                    decision = choose(field, [{**o, 'origin': name} for o in extracted], record.get(field))
                    if decision['status'] != 'source_agreement':
                        name = 'number_line' if field == 'number' else 'date_line'
                        crop = regional.read(page, name)
                        crops.append(crop)
                        observations[field].extend({**o, 'origin': name} for o in header_fields(crop['text'])[field])
                number = self.signature_page(record) if fields['number'] else None
                result['signature_page'] = number
                if number:
                    with PDF_LOCK:
                        signature = doc[number - 1]
                    result['signature'], result['signature_reads'] = read_signature(regional, signature)
                result.update(crops=crops, identity={f: choose(f, observations[f], record.get(f)) for f in observations}, status='ok')
            finally:
                with PDF_LOCK:
                    doc.close()
        except (ValueError, OSError, ocr.OCRUnavailable) as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
        result['seconds'] = round(time.monotonic() - started, 3)
        _write_json(output, result)
        return result

    def body(self, record, sample):
        import pymupdf
        page_number = sample['page']
        result = {'number': record['number'], 'page': page_number, 'kind': sample.get('kind', 'unscored'), 'status': 'error'}
        folder = self.out / 'documents' / record['sha256'][:16]
        folder.mkdir(parents=True, exist_ok=True)
        try:
            baseline = self.whole(record, page_number)
            with PDF_LOCK:
                doc = pymupdf.open(self.data / record['file'])
                page = doc[page_number - 1]
                vertical = page.rect.width > page.rect.height
            try:
                regional = RegionOCR(self.out / 'regions', record['sha256'])
                names = ['left', 'right'] if vertical else ['top', 'bottom']
                crops = [regional.read(page, name) for name in names]
            finally:
                with PDF_LOCK:
                    doc.close()
            text = '\n\n'.join(c['text'] for c in crops)
            (folder / f'body-{page_number:04d}.split.txt').write_text(text, encoding='utf-8')
            result.update(status='ok', strategy=names, whole=baseline, crops=crops, split_text=text)
            if sample.get('reference_file'):
                reference = Path(sample['reference_file']).read_text(encoding='utf-8')
                result['whole_vs_native'] = word_error(reference, baseline['text'])
                result['split_vs_native'] = word_error(reference, text)
                result['reference_file'] = sample['reference_file']
        except (ValueError, OSError, ocr.OCRUnavailable) as exc:
            result['error'] = f'{type(exc).__name__}: {exc}'
        _write_json(folder / f'body-{page_number:04d}.json', result)
        return result


def main():
    load_env(Path('.env'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, required=True)
    parser.add_argument('--manifest', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--body-samples', type=Path)
    parser.add_argument('--limit', type=int, default=30)
    parser.add_argument('--workers', type=int, default=2)
    args = parser.parse_args()
    if args.limit < 1 or args.workers < 1:
        parser.error('limit and workers must be positive')
    manifest = json.loads(args.manifest.read_text())[:args.limit]
    for i, record in enumerate(manifest, 1):
        record.setdefault('rank', i)
        with (args.data / record['file']).open('rb') as stream:
            if hashlib.file_digest(stream, 'sha256').hexdigest() != record['sha256']:
                raise ValueError('PDF SHA-256 mismatch: ' + record['file'])
    benchmark = Benchmark(args.data, args.out)
    started, headers, bodies = time.monotonic(), [], []
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for future in as_completed([pool.submit(benchmark.header, r) for r in manifest]):
            item = future.result()
            headers.append(item)
            _write_json(args.out / 'headers.json', sorted(headers, key=lambda r: r['rank']))
            print(json.dumps({'stage': 'header', 'done': len(headers), 'number': item['number'], 'status': item['status']}, ensure_ascii=False), flush=True)
        if args.body_samples:
            records = {r['number']: r for r in manifest}
            samples = json.loads(args.body_samples.read_text())
            jobs = [pool.submit(benchmark.body, records[s['number']], s) for s in samples if s['number'] in records]
            for future in as_completed(jobs):
                item = future.result()
                bodies.append(item)
                _write_json(args.out / 'bodies.json', bodies)
                print(json.dumps({'stage': 'body', 'done': len(bodies), 'number': item['number'], 'page': item['page'], 'status': item['status']}, ensure_ascii=False), flush=True)
    scored = [r for r in bodies if 'whole_vs_native' in r and r['kind'] == 'native_prose_reference']
    summary = {'documents': len(headers), 'body_pages': len(bodies),
               'errors': sum(r['status'] == 'error' for r in headers + bodies),
               'identity_source_agreement': sum(all(v['status'] == 'source_agreement' for v in r.get('identity', {}).values()) and bool(r.get('identity')) for r in headers),
               'reference_pages': len(scored), 'seconds': round(time.monotonic() - started, 3),
               'accuracy_warning': 'Token difference from supplied references; not a full human-certified accuracy rate.'}
    for name in ('whole', 'split'):
        n = sum(r[name + '_vs_native']['reference_tokens'] for r in scored)
        e = sum(r[name + '_vs_native']['token_edits'] for r in scored)
        summary[name] = {'reference_tokens': n, 'token_edits': e, 'WER': e / max(1, n),
                         'pages_within_5_percent': sum(r[name + '_vs_native']['WER'] <= .05 for r in scored)}
    _write_json(args.out / 'benchmark-summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary['errors'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
