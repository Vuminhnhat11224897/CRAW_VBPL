"""Read PDFs and save OCR evidence, without LLM/ES/MinIO calls.

python -m legal_crawler.v2.ocr_check --data data --limit 30
python -m legal_crawler.v2.ocr_check --data data_benchmark_30 --manifest data_benchmark_30/manifest.json
"""
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3
import time

from ..settings import load_env
from ..storage import atomic_json
from . import ocr, reader


def records(data, manifest=None):
    if manifest:
        rows = json.loads(manifest.read_text(encoding='utf-8'))
        for row in rows:
            yield row, data / row['file'], row['sha256']
    else:
        with sqlite3.connect(f'file:{(data / "state.sqlite3").resolve()}?mode=ro', uri=True) as db:
            for (raw,) in db.execute('SELECT record FROM documents ORDER BY id'):
                row = json.loads(raw)
                pdfs = [f for f in row.get('files', []) if f.get('format') == 'pdf']
                if row.get('status') == 'downloaded' and pdfs:
                    yield row, data / pdfs[0]['path'], pdfs[0]['sha256']


def main():
    load_env(Path('.env'))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--data', type=Path, default=Path('data'))
    parser.add_argument('--manifest', type=Path)
    parser.add_argument('--out', type=Path)
    parser.add_argument('--limit', type=int, default=0)
    args = parser.parse_args()
    if args.limit < 0:
        parser.error('--limit must be nonnegative')
    stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')
    out = args.out or args.data / 'ocr_checks' / stamp
    out.mkdir(parents=True, exist_ok=True)
    results = []
    started = time.monotonic()
    for record, path, digest in records(args.data, args.manifest):
        if args.limit and len(results) >= args.limit:
            break
        folder = args.data / 'v2_work' / digest
        result = {'id': record['id'], 'number': record.get('number'), 'file': str(path)}
        try:
            text, report, positions = reader.read(path, folder, record=record)
            text_file = out / (digest + '.txt')
            text_file.write_text(text, encoding='utf-8')
            result.update(status=report.get('quality_status', 'no_issues_detected'),
                          text_file=str(text_file), report=report, date_pages=positions)
        except (ValueError, OSError, ocr.OCRUnavailable) as exc:
            result.update(status='error', error=f'{type(exc).__name__}: {exc}')
        results.append(result)
        atomic_json(out / 'documents.json', results)
        print(json.dumps({k: result[k] for k in ('id', 'number', 'status')}, ensure_ascii=False), flush=True)
    summary = {'documents': len(results), 'errors': sum(r['status'] == 'error' for r in results),
               'needs_review': sum(r['status'] == 'needs_review' for r in results),
               'pages': sum(r.get('report', {}).get('pages', 0) or 0 for r in results),
               'ocr_pages': sum(len(r.get('report', {}).get('ocr_pages', [])) for r in results),
               'seconds': round(time.monotonic() - started, 3), 'output': str(out)}
    atomic_json(out / 'summary.json', summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 2 if summary['errors'] or summary['needs_review'] else 0


if __name__ == '__main__':
    raise SystemExit(main())
