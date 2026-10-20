"""Save the current VBPL frontend for adapter diagnosis, through TMProxy."""

import hashlib
import json
from pathlib import Path
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

from .http import Client, FetchError
from .settings import Settings


def inspect(settings, client):
    root = settings.data / "diagnostics" / "vbpl"
    root.mkdir(parents=True, exist_ok=True)
    content, final_url, _ = client.fetch("https://vbpl.vn/", ["vbpl.vn"])
    (root / "home.html").write_bytes(content)
    soup = BeautifulSoup(content, "html.parser")
    urls = dict.fromkeys(urljoin(final_url, node["src"]) for node in soup.select("script[src]"))
    manifest = []
    for url in urls:
        parsed = urlsplit(url)
        if parsed.hostname != "vbpl.vn" or not parsed.path.startswith("/_next/static/"):
            continue
        entry = {"url": url}
        try:
            content, _, _ = client.fetch(url, ["vbpl.vn"])
            name = hashlib.sha256(url.encode()).hexdigest()[:24] + ".js"
            (root / name).write_bytes(content)
            entry["path"] = name
            print(f"Saved {name}: {len(content)} bytes", flush=True)
        except FetchError as exc:
            entry["error"] = str(exc)
        manifest.append(entry)
    (root / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    print(f"VBPL diagnostic files: {root}", flush=True)
    return 2 if not manifest or any("error" in entry for entry in manifest) else 0


def main():
    client = None
    try:
        settings = Settings.read(Path(".env"))
        client = Client(settings)
        return inspect(settings, client)
    except (FetchError, ValueError, OSError) as exc:
        print(str(exc))
        return 1
    finally:
        if client:
            client.close()


if __name__ == "__main__":
    raise SystemExit(main())
