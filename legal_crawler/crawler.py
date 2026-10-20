import hashlib
import json
import logging

from .es import ESDedupe
from .http import FetchError
from .parser import canonical, document_id, parse_page
from .storage import now
from .vbpl import VBPL


log = logging.getLogger(__name__)


class Crawler:
    def __init__(self, settings, store, client):
        self.settings, self.store, self.client = settings, store, client
        # A different date interval must revisit documents previously filtered out.
        self.scope = settings.date_from + ":" + settings.date_to
        self.es = ESDedupe(settings) if settings.es_url else None
        self.documents = 0  # in-range documents handled this run, for MAX_DOCUMENTS
        self.vbpl = VBPL(settings, client, store) if settings.configs.get("vbpl", {}).get("adapter") == "vbpl_actions" else None

    def page(self, source, payload):
        if source == "vbpl" and self.vbpl:
            return self.vbpl.page(payload)
        config = self.settings.configs[source]
        content, url, mime = self.client.fetch(payload["url"], config["page_hosts"],
            method=payload.get("method", "GET"), data=payload.get("body"))
        if "html" not in mime.lower() and not content.lstrip().startswith(b"<"):
            raise FetchError("Expected an HTML page")
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        raw = self.settings.data / "raw" / source / (key + ".html")
        raw.parent.mkdir(parents=True, exist_ok=True)
        temp = raw.with_suffix(".tmp")
        temp.write_bytes(content)
        temp.replace(raw)
        parsed = parse_page(content, url, config)
        parsed["raw_path"] = str(raw.relative_to(self.settings.data))
        return parsed

    def listing(self, source, payload):
        page = self.page(source, payload)
        ids = []
        for url, label in page["details"].items():
            identifier = document_id(source, url)
            ids.append(identifier)
            self.store.enqueue(self.scope, source, "detail", {"url": url, "label": label}, identifier)
        if not ids and not page["listings"] and not page["postbacks"]:
            if page.get("empty_listing"):
                return
            if source == "vbpl" and page.get("requires_javascript"):
                raise FetchError("VBPL returned its new JavaScript frontend without document links; run python -m legal_crawler.inspect_vbpl to capture the current frontend for adapter diagnosis")
            raise FetchError("No document/listing links found; inspect saved HTML and source config")
        if ids and self.store.seen_listing(self.scope, source, ids):
            return
        for url in page["listings"]:
            self.store.enqueue(self.scope, source, "listing", {"url": url})
        for request in page["postbacks"]:
            # ViewState changes every response; identify the page by the postback action.
            body = request["body"]
            identity = [request["url"], body["__EVENTTARGET"], body["__EVENTARGUMENT"]]
            if not body["__EVENTARGUMENT"] or body["__EVENTARGUMENT"].lower() in {"page$next", "page$prev"}:
                identity.append(sorted(ids))
            self.store.enqueue(self.scope, source, "listing", request, identity)
        if ids:
            self.store.seen_listing(self.scope, source, ids, remember=True)

    def detail(self, source, payload):
        config = self.settings.configs[source]
        identifier = document_id(source, payload["url"])
        page = self.page(source, payload)
        metadata = page["metadata"]
        files, raw_pages = set(page["files"]), [page["raw_path"], *page.get("extra_raw_pages", [])]
        tab_errors = []
        for url in dict.fromkeys(page["tabs"]):
            if canonical(url) == canonical(payload["url"]) or document_id(source, url) != identifier:
                continue
            try:
                tab = self.page(source, {"url": url})
                files.update(tab["files"])
                raw_pages.append(tab["raw_path"])
                for key, value in tab["metadata"].items():
                    if value and not metadata.get(key):
                        metadata[key] = value
            except FetchError as exc:
                tab_errors.append({"url": url, "error": str(exc)})
        record = {"schema_version": 1, "id": identifier, "source": source,
                  "source_url": payload["url"], **metadata, "fetched_at": now(),
                  "raw_pages": raw_pages, "attachment_urls": sorted(files),
                  "files": [], "errors": list(tab_errors), "status": "downloaded"}
        record["title"] = record["title"] or payload.get("label") or None
        issued, effective = record["issued_date"], record.get("effective_date")
        if (self.settings.date_from or self.settings.date_to) and not issued:
            record["status"] = "missing_date"
        elif issued and ((self.settings.date_from and issued < self.settings.date_from)
                         or (self.settings.date_to and issued > self.settings.date_to)):
            record["status"] = "outside_date_range"
        elif effective and self.settings.date_from and effective < self.settings.date_from:
            record["status"] = "outside_date_range"
        elif self.es and (match := self.es.find(record.get("number"), issued)):
            # Same number + same issue date = same document: keep metadata, skip the download.
            record["status"], record["es_match"] = "duplicate_es", match
        elif not files:
            record["status"] = "no_attachments"
        else:
            first_file_enough = source == "congbao" and not self.settings.congbao_download_all
            for url in sorted(files):
                try:
                    record["files"].append(self.vbpl.download(url) if source == "vbpl" and self.vbpl
                                           else self.store.download(self.client, source, url, config["file_hosts"]))
                    if first_file_enough:
                        break
                except FetchError as exc:
                    record["errors"].append({"url": url, "error": str(exc)})
            if record["errors"] and not (first_file_enough and record["files"] and not tab_errors):
                record["status"] = "partial" if record["files"] else "failed"
        self.store.save_document(record)
        if record["status"] not in {"outside_date_range", "missing_date"}:
            self.documents += 1
        log.info("%s %s files=%d", identifier, record["status"], len(record["files"]))
        if record["status"] in {"missing_date", "no_attachments", "partial", "failed"} or tab_errors:
            reason = "; ".join(dict.fromkeys(item["error"] for item in record["errors"]))
            raise FetchError("Document requires retry/review: " + record["status"] + (": " + reason if reason else ""))

    def run(self):
        self.store.prepare(self.scope, self.settings.configs, self.settings.retry_failed, self.settings.refresh)
        for source, config in self.settings.configs.items():
            for url in config["seeds"]:
                self.store.enqueue(self.scope, source, "listing", {"url": canonical(url)})
        counts = dict.fromkeys(self.settings.configs, 0)
        while True:
            processed = False
            if self.settings.max_documents and self.documents >= self.settings.max_documents:
                log.info("Reached MAX_DOCUMENTS=%d", self.settings.max_documents)
                break
            for source in self.settings.configs:
                if self.settings.max_pages and counts[source] >= self.settings.max_pages:
                    continue
                job = self.store.next_job(self.scope, source)
                if not job:
                    continue
                try:
                    payload = json.loads(job["payload"])
                    if job["kind"] == "listing":
                        self.listing(source, payload)
                    else:
                        self.detail(source, payload)
                except FetchError as exc:
                    self.store.finish(job, str(exc))
                    log.warning("%s %s: %s", source, job["kind"], exc)
                else:
                    self.store.finish(job)
                counts[source] += 1
                processed = True
            if not processed:
                break
        for source, count in counts.items():
            log.info("%s: processed %d jobs", source, count)
        return self.store.db.execute("SELECT count(*) FROM jobs WHERE scope=? AND state='failed' AND source IN (" + ",".join("?" for _ in self.settings.configs) + ")", (self.scope, *self.settings.configs)).fetchone()[0]
