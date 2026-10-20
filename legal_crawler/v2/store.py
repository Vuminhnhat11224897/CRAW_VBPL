"""PDF object storage in its own MinIO bucket (separate from data_vbpl's legal-upload)."""
import os
from urllib.parse import urlsplit


def client(*, http_client=None):
    from minio import Minio
    endpoint = urlsplit(os.environ["MINIO_ENDPOINT"])
    if endpoint.scheme not in {"http", "https"} or not endpoint.netloc:
        raise ValueError("MINIO_ENDPOINT must be an HTTP(S) URL")
    access = os.getenv("MINIO_ACCESS_KEY") or os.environ["MINIO_USERNAME"]  # data_vbpl's variable names also work
    secret = os.getenv("MINIO_SECRET_KEY") or os.environ["MINIO_PASSWORD"]
    return Minio(endpoint.netloc, access_key=access, secret_key=secret, secure=endpoint.scheme == "https",
                 **({'http_client': http_client} if http_client is not None else {}))


def bucket():
    name = os.getenv("CRAWLER_BUCKET", "legal-crawler")
    if not name or name == 'legal-upload':
        raise ValueError('Crawler needs its own bucket; legal-upload is protected')
    return name


def upload_pdf(minio, path, sha256, create_bucket=False):
    """Upload as <bucket>/pdf/<sha256>.pdf, verify size and signature, return the pdf_minio_path value."""
    name = bucket()
    key = f"pdf/{sha256}.pdf"
    size = path.stat().st_size
    with path.open("rb") as stream:
        if stream.read(5) != b"%PDF-":
            raise ValueError("Not a PDF")
    from ..storage import digest_file
    if digest_file(path) != sha256:
        raise ValueError('PDF SHA-256 mismatch')
    if create_bucket and not minio.bucket_exists(name):
        minio.make_bucket(name)
    try:
        if minio.stat_object(name, key).size == size:
            return f"{name}/{key}"  # same content hash already stored
    except Exception:  # noqa: BLE001 - missing object raises S3Error
        pass
    minio.fput_object(name, key, str(path), content_type="application/pdf")
    if minio.stat_object(name, key).size != size:
        raise RuntimeError("Uploaded PDF size mismatch")
    return f"{name}/{key}"
