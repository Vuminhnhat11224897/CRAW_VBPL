"""PDF object storage in its own MinIO bucket (separate from data_vbpl's legal-upload)."""
import os
from urllib.parse import urlsplit


def client():
    from minio import Minio
    endpoint = urlsplit(os.environ["MINIO_ENDPOINT"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("MINIO_ENDPOINT must be an HTTP(S) URL")
    access = os.getenv("MINIO_ACCESS_KEY") or os.environ["MINIO_USERNAME"]  # data_vbpl's variable names also work
    secret = os.getenv("MINIO_SECRET_KEY") or os.environ["MINIO_PASSWORD"]
    return Minio(endpoint.netloc, access_key=access, secret_key=secret, secure=endpoint.scheme == "https")


def bucket():
    return os.getenv("CRAWLER_BUCKET", "legal-crawler")


def upload_pdf(minio, path, sha256, create_bucket=False):
    """Upload as <bucket>/pdf/<sha256>.pdf, verify size and signature, return the pdf_minio_path value."""
    name = bucket()
    key = f"pdf/{sha256}.pdf"
    if create_bucket and not minio.bucket_exists(name):
        minio.make_bucket(name)
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Not a PDF")
    try:
        if minio.stat_object(name, key).size == size:
            return f"{name}/{key}"  # same content hash already stored
    except Exception:  # noqa: BLE001 - missing object raises S3Error
        pass
    minio.fput_object(name, key, str(path), content_type="application/pdf")
    if minio.stat_object(name, key).size != size:
        raise RuntimeError("Uploaded PDF size mismatch")
    return f"{name}/{key}"
