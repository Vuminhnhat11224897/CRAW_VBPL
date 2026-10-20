import json
import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path


def load_env(path):
    """Small literal KEY=value reader: no shell expansion or code execution."""
    if not path.exists():
        return
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key, value = key.strip(), value.strip()
        if not sep or not re.fullmatch(r"[A-Z][A-Z0-9_]*", key):
            raise ValueError(f"Invalid .env line {number}")
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        os.environ.setdefault(key, value)


def number(key, default, minimum=0, kind=int):
    value = kind(os.getenv(key, str(default)))
    if value < minimum or value != value or value == float("inf"):
        raise ValueError(f"{key} must be >= {minimum}")
    return value


def boolean(key, default):
    value = os.getenv(key, str(default)).lower()
    if value not in {"true", "false"}:
        raise ValueError(f"{key} must be true or false")
    return value == "true"


@dataclass
class Settings:
    data: Path
    configs: dict
    date_from: str
    date_to: str
    max_pages: int
    delay: float
    timeout: float
    retries: int
    max_file: int
    max_html: int
    retry_failed: bool
    refresh: bool
    api_key: str = field(repr=False)
    api_url: str
    location: int
    isp: int
    proxy_wait: int
    user_agent: str
    vbpl_page_size: int = 20
    congbao_download_all: bool = False
    es_url: str = ""
    es_index: str = "legal-docs"
    max_documents: int = 0

    @classmethod
    def read(cls, env_file):
        load_env(env_file)
        start, end = os.getenv("DATE_FROM", ""), os.getenv("DATE_TO", "")
        for value in (start, end):
            if value and date.fromisoformat(value).isoformat() != value:
                raise ValueError("Dates must use YYYY-MM-DD")
        if start and end and start > end:
            raise ValueError("DATE_FROM must not exceed DATE_TO")
        configs = {}
        for source in os.getenv("SOURCES", "vanban_chinhphu,congbao,vbpl").split(","):
            source = source.strip()
            if not re.fullmatch(r"[a-z][a-z0-9_]*", source):
                raise ValueError("Invalid source name")
            path = Path(os.getenv("CONFIG_DIR", "config")) / source / "source.json"
            config = json.loads(path.read_text(encoding="utf-8"))
            for key in ("seeds", "page_hosts", "file_hosts", "title_selectors"):
                if not isinstance(config.get(key), list) or not config[key]:
                    raise ValueError(f"{path}: {key} must be a nonempty list")
            for key in ("detail_pattern", "listing_pattern", "attachment_pattern", "tab_pattern"):
                re.compile(config[key], re.I)
            configs[source] = config
        api_url = os.getenv("TMPROXY_API_URL", "https://tmproxy.com/api/proxy").rstrip("/")
        if not api_url.startswith("https://"):
            raise ValueError("TMPROXY_API_URL requires HTTPS")
        return cls(
            Path(os.getenv("DATA_DIR", "data")), configs, start, end,
            number("MAX_PAGES", 100), number("REQUEST_DELAY", 2, kind=float),
            number("REQUEST_TIMEOUT", 60, 1, float), number("REQUEST_RETRIES", 3, 1),
            number("MAX_FILE_MB", 100, 1) * 1024**2,
            number("MAX_HTML_MB", 10, 1) * 1024**2,
            boolean("RETRY_FAILED", True), boolean("REFRESH", False),
            os.getenv("TMPROXY_API_KEY", ""), api_url,
            number("TMPROXY_LOCATION", 0), number("TMPROXY_ISP", 0),
            number("TMPROXY_MAX_WAIT", 180),
            os.getenv("USER_AGENT", "LegalDocumentCollector/1.0"),
            number("VBPL_PAGE_SIZE", 20, 1),
            boolean("CONGBAO_DOWNLOAD_ALL", False),
            os.getenv("SOURCE_ES_URL", ""), os.getenv("SOURCE_DOCUMENT_INDEX", "legal-docs"),
            number("MAX_DOCUMENTS", 0),
        )
