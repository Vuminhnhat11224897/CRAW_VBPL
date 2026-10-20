"""Vintern OCR API client (/ocr/raw for text, /ocr/boxes for line coordinates).

The server only listens on 127.0.0.1 of its own host and has no authentication, so OCR_API_URL must be
reachable from where this runs (same host, or an SSH tunnel). One shared GPU: keep OCR_WORKERS small.
"""
import os
import math
import threading
import time

import requests


class OCRUnavailable(RuntimeError):
    """OCR server cannot be reached or is degraded: the document stays pending, it is not guessed."""


class OCRRejected(ValueError):
    """The service rejected this input or returned insufficient document text."""


class OCRConfigurationError(ValueError):
    """Invalid local settings must not permanently reject a document."""


class Gate:
    """Limit HTTP requests across the exporter's document threads (one GPU)."""

    def __init__(self):
        self.condition = threading.Condition()
        self.active = 0

    def __enter__(self):
        try:
            limit = int(os.getenv("OCR_WORKERS", "2"))
        except ValueError:
            raise OCRConfigurationError("OCR_WORKERS must be a positive integer") from None
        if limit < 1:
            raise OCRConfigurationError("OCR_WORKERS must be positive")
        with self.condition:
            while self.active >= limit:
                self.condition.wait()
            self.active += 1

    def __exit__(self, *exc):
        with self.condition:
            self.active -= 1
            self.condition.notify_all()


GATE = Gate()


def base_url():
    return os.getenv("OCR_API_URL", "http://127.0.0.1:8000").rstrip("/")


def health():
    try:
        response = requests.get(base_url() + "/health", timeout=(3, 10))
    except requests.RequestException as exc:
        raise OCRUnavailable(f"OCR server unreachable at {base_url()}: {type(exc).__name__}") from None
    if response.status_code != 200:
        raise OCRUnavailable(f"OCR server degraded: HTTP {response.status_code}")


def call(endpoint, path, *, scale=None, max_px=None, retries=3, timeout=None):
    if retries < 1:
        raise ValueError("OCR retries must be positive")
    try:
        scale = float(scale if scale is not None else os.getenv("OCR_PDF_SCALE", "2.0"))
        read_timeout = float(timeout if timeout is not None else os.getenv("OCR_TIMEOUT", "900"))
    except (TypeError, ValueError):
        raise OCRConfigurationError("OCR scale and timeout must be numbers") from None
    if not .5 <= scale <= 4:
        raise OCRConfigurationError("OCR_PDF_SCALE must be between 0.5 and 4")
    if not math.isfinite(read_timeout) or read_timeout <= 0:
        raise OCRConfigurationError("OCR_TIMEOUT must be positive and finite")
    data = {"pdf_scale": str(scale)}
    if max_px:
        data["max_px"] = str(max_px)
    last = None
    for attempt in range(retries):
        try:
            with GATE, open(path, "rb") as stream:
                response = requests.post(base_url() + endpoint, data=data, files={"file": (path.name, stream, "application/pdf")},
                                         timeout=(5, read_timeout))
            if response.status_code == 200:
                try:
                    result = response.json()
                    if not isinstance(result, dict):
                        raise ValueError("expected object")
                    return result
                except ValueError:
                    last = "invalid JSON response"
                    if attempt + 1 < retries:
                        time.sleep(min(30, 2 ** (attempt + 1)))
                    continue
            last = f"HTTP {response.status_code}: {response.text[:160]}"
            if response.status_code in {400, 413, 422}:  # permanent: damaged / oversized PDF
                raise OCRRejected("OCR rejected the file: " + last)
        except requests.RequestException as exc:
            last = type(exc).__name__
        if attempt + 1 < retries:
            time.sleep(min(30, 2 ** (attempt + 1)))
    raise OCRUnavailable("OCR failed after retries: " + str(last))


def ocr_text(path, *, scale=None, min_chars=50):
    """Whole-page text in reading order (best quality per the API notes)."""
    result = call("/ocr/raw", path, scale=scale)
    if not isinstance(result.get("raw_text"), str):
        raise OCRUnavailable("OCR response is missing raw_text")
    text = result["raw_text"].strip()
    if len(text) < min_chars:
        raise OCRRejected("OCR returned insufficient text")
    metadata = result.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise OCRUnavailable("OCR response contains invalid metadata")
    return text, {"method": "ocr", "pages": None, "ocr_pages": "all", "warnings": [],
                  "ocr_seconds": (metadata or {}).get("elapsed_seconds")}


def ocr_boxes(path):
    """[{'page': n, 'items': [(x, y, w, h, text)]}] with coordinates normalised 0..1 from the top-left."""
    result = call("/ocr/boxes", path, max_px=1600)
    try:
        pages = []
        for p in result["pages"]:
            if not isinstance(p["page"], int) or p["page"] < 1:
                raise ValueError("invalid page")
            items = []
            for b in p["boxes"]:
                xywh = tuple(b[k] for k in ("x", "y", "w", "h"))
                if (not isinstance(b["text"], str) or
                    any(not isinstance(v, (float, int)) or not math.isfinite(v) or not 0 <= v <= 1 for v in xywh) or
                    xywh[0] + xywh[2] > 1.001 or xywh[1] + xywh[3] > 1.001):
                    raise ValueError("invalid normalized coordinates")
                items.append((*xywh, b["text"]))
            pages.append({"page": p["page"], "items": items})
        return pages
    except (KeyError, TypeError, ValueError):
        raise OCRUnavailable("OCR response contains invalid page/box data") from None
