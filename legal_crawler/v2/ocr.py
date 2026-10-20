"""Vintern OCR API client (/ocr/raw for text, /ocr/boxes for line coordinates).

The server only listens on 127.0.0.1 of its own host and has no authentication, so OCR_API_URL must be
reachable from where this runs (same host, or an SSH tunnel). One shared GPU: keep OCR_WORKERS small.
"""
import os
import time

import requests


class OCRUnavailable(RuntimeError):
    """OCR server cannot be reached or is degraded: the document stays pending, it is not guessed."""


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
    data = {"pdf_scale": str(scale or os.getenv("OCR_PDF_SCALE", "2.0"))}
    if max_px:
        data["max_px"] = str(max_px)
    last = None
    for attempt in range(retries):
        try:
            with open(path, "rb") as stream:
                response = requests.post(base_url() + endpoint, data=data, files={"file": (path.name, stream, "application/pdf")},
                                         timeout=(5, timeout or float(os.getenv("OCR_TIMEOUT", "900"))))
            if response.status_code == 200:
                return response.json()
            last = f"HTTP {response.status_code}: {response.text[:160]}"
            if response.status_code in {400, 413, 422}:  # permanent: damaged / oversized PDF
                raise ValueError("OCR rejected the file: " + last)
        except requests.RequestException as exc:
            last = type(exc).__name__
        time.sleep(min(30, 2 ** (attempt + 1)))
    raise OCRUnavailable("OCR failed after retries: " + str(last))


def ocr_text(path):
    """Whole-page text in reading order (best quality per the API notes)."""
    result = call("/ocr/raw", path)
    text = result["raw_text"].strip()
    if len(text) < 50:
        raise ValueError("OCR returned insufficient text")
    return text, {"method": "ocr", "pages": None, "ocr_pages": "all", "warnings": [],
                  "ocr_seconds": result["metadata"].get("elapsed_seconds")}


def ocr_boxes(path):
    """[{'page': n, 'items': [(x, y, w, h, text)]}] with coordinates normalised 0..1 from the top-left."""
    result = call("/ocr/boxes", path, max_px=1600)
    return [{"page": p["page"], "items": [(b["x"], b["y"], b["w"], b["h"], b["text"]) for b in p["boxes"]]}
            for p in result["pages"]]
