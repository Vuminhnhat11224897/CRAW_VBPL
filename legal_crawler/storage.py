import hashlib
import json
import os
import re
import shutil
import sqlite3
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from .http import FetchError


def now():
    return datetime.now(timezone.utc).isoformat()


def digest_file(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def file_type(path):
    with path.open("rb") as stream:
        header = stream.read(1024)
        stream.seek(max(0, path.stat().st_size - 4096))
        tail = stream.read()
    if header.startswith(b"%PDF-") and b"%%EOF" in tail:
        return "pdf"
    if header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # shortcut: OLE header validation only, add a document parser when extracting text.
        return "doc"
    if header.lstrip().startswith(b"{\\rtf"):
        return "rtf"
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as archive:
            names = set(archive.namelist())
            if "[Content_Types].xml" in names and "word/document.xml" in names:
                return "docx"
        return "zip"
    raise FetchError("Unsupported or incomplete document (possibly HTML/error page)")


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    with temp.open("w", encoding="utf-8") as output:
        json.dump(value, output, ensure_ascii=False, indent=2)
        output.write("\n")
        output.flush()
        os.fsync(output.fileno())
    temp.replace(path)


class Store:
    def __init__(self, root):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "state.sqlite3")
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, scope TEXT, source TEXT, kind TEXT,
                payload TEXT, state TEXT DEFAULT 'pending', error TEXT, attempts INTEGER DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS jobs_pending ON jobs(scope, source, state, kind);
            CREATE TABLE IF NOT EXISTS snapshots (
                scope TEXT, source TEXT, fingerprint TEXT,
                PRIMARY KEY(scope, source, fingerprint)
            );
            CREATE TABLE IF NOT EXISTS documents (id TEXT PRIMARY KEY, source TEXT, record TEXT);
            CREATE TABLE IF NOT EXISTS files (
                source TEXT NOT NULL, url TEXT NOT NULL, record TEXT NOT NULL,
                PRIMARY KEY(source, url)
            );
        """)

    def enqueue(self, scope, source, kind, payload, identity=None):
        key = json.dumps([scope, source, kind, identity or payload], sort_keys=True)
        identifier = hashlib.sha256(key.encode()).hexdigest()
        with self.db:
            self.db.execute("INSERT OR IGNORE INTO jobs(id,scope,source,kind,payload) VALUES(?,?,?,?,?)",
                (identifier, scope, source, kind, json.dumps(payload)))

    def prepare(self, scope, sources, retry, refresh):
        for source in sources:
            with self.db:
                if retry:
                    self.db.execute("UPDATE jobs SET state='pending' WHERE scope=? AND source=? AND state='failed'", (scope, source))
                if refresh:
                    self.db.execute("UPDATE jobs SET state='pending' WHERE scope=? AND source=? AND state='done'", (scope, source))
                    self.db.execute("DELETE FROM snapshots WHERE scope=? AND source=?", (scope, source))

    def next_job(self, scope, source):
        row = self.db.execute("SELECT * FROM jobs WHERE scope=? AND source=? AND state='pending' ORDER BY kind, rowid LIMIT 1", (scope, source)).fetchone()
        return dict(row) if row else None

    def finish(self, job, error=None):
        with self.db:
            self.db.execute("UPDATE jobs SET state=?,error=?,attempts=attempts+1 WHERE id=?",
                ("failed" if error else "done", error, job["id"]))

    def seen_listing(self, scope, source, detail_ids, remember=False):
        fingerprint = hashlib.sha256("\n".join(sorted(set(detail_ids))).encode()).hexdigest()
        if remember:
            with self.db:
                self.db.execute("INSERT OR IGNORE INTO snapshots VALUES(?,?,?)", (scope, source, fingerprint))
        return self.db.execute("SELECT 1 FROM snapshots WHERE scope=? AND source=? AND fingerprint=?", (scope, source, fingerprint)).fetchone() is not None

    def save_document(self, record):
        existing = self.db.execute("SELECT record FROM documents WHERE id=?", (record["id"],)).fetchone()
        if existing and record["status"] == "outside_date_range":
            return
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO documents VALUES(?,?,?)",
                (record["id"], record["source"], json.dumps(record, ensure_ascii=False)))
        atomic_json(self.root / "metadata" / record["source"] / (record["id"].split(":", 1)[1] + ".json"), record)

    def file_path(self, source, digest, extension):
        if not re.fullmatch(r"[a-z][a-z0-9_]*", source) or not re.fullmatch(r"[0-9a-f]{64}", digest) or extension not in {"pdf", "doc", "docx", "rtf", "zip"}:
            raise ValueError("Invalid file source, digest or format")
        path = self.root / "files" / source / (digest + "." + extension)
        if not path.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Source folder is outside DATA_DIR")
        return path

    def relocate_file(self, source, record):
        path = self.file_path(source, record["sha256"], record["format"])
        original = self.root / record["path"]
        if not original.resolve().is_relative_to(self.root.resolve()):
            raise ValueError("Cached file path is outside DATA_DIR")
        if not path.is_file() or digest_file(path) != record["sha256"]:
            if not original.is_file() or digest_file(original) != record["sha256"]:
                raise ValueError("Cannot migrate missing or corrupt file: " + record["path"])
            path.parent.mkdir(parents=True, exist_ok=True)
            temp = path.with_suffix(path.suffix + ".part")
            try:
                with original.open("rb") as input_file, temp.open("wb") as output:
                    shutil.copyfileobj(input_file, output)
                    output.flush()
                    os.fsync(output.fileno())
                if digest_file(temp) != record["sha256"]:
                    raise ValueError("Migrated file checksum mismatch")
                temp.replace(path)
            finally:
                temp.unlink(missing_ok=True)
        return dict(record, source=source, path=str(path.relative_to(self.root)))

    def migrate_files(self):
        if self.db.execute("PRAGMA user_version").fetchone()[0] >= 1:
            return 0
        legacy = "source" not in {row["name"] for row in self.db.execute("PRAGMA table_info(files)")}
        backup = self.root / "state.before-source-folders.sqlite3"
        if legacy and not backup.exists():
            with sqlite3.connect(backup) as copy:
                self.db.backup(copy)
        documents = [json.loads(row["record"]) for row in self.db.execute("SELECT record FROM documents")]
        owners = {}
        for document in documents:
            for record in document.get("files", []):
                owners.setdefault(record["url"], set()).add(document["source"])
        files = [json.loads(row["record"]) for row in self.db.execute("SELECT record FROM files")]
        relocated = {}
        for record in files:
            sources = owners.get(record["url"], {record.get("source", "unassigned")})
            for source in sources:
                relocated[source, record["url"]] = self.relocate_file(source, record)
        for document in documents:
            document["files"] = [self.relocate_file(document["source"], record) for record in document.get("files", [])]
            for record in document["files"]:
                relocated[document["source"], record["url"]] = record
        with self.db:
            if legacy:
                self.db.execute("ALTER TABLE files RENAME TO files_legacy")
                self.db.execute("CREATE TABLE files(source TEXT NOT NULL,url TEXT NOT NULL,record TEXT NOT NULL,PRIMARY KEY(source,url))")
                self.db.execute("DROP TABLE files_legacy")
            for (source, url), record in relocated.items():
                self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (source, url, json.dumps(record)))
            for document in documents:
                self.db.execute("UPDATE documents SET record=? WHERE id=?", (json.dumps(document, ensure_ascii=False), document["id"]))
        # Old files remain until all persisted references point at verified copies.
        for document in documents:
            atomic_json(self.root / "metadata" / document["source"] / (document["id"].split(":", 1)[1] + ".json"), document)
        self.export()
        for record in relocated.values():
            old = self.root / "files" / record["sha256"][:2] / (record["sha256"] + "." + record["format"])
            if old.is_file() and old != self.root / record["path"]:
                if digest_file(old) != record["sha256"]:
                    raise ValueError("Old file checksum mismatch; preserved original: " + str(old))
                old.unlink()
                try:
                    old.parent.rmdir()
                except OSError:
                    pass
        with self.db:
            self.db.execute("PRAGMA user_version=1")
        return len(relocated)

    def cached_file(self, source, url):
        row = self.db.execute("SELECT record FROM files WHERE url=? ORDER BY (source=?) DESC LIMIT 1", (url, source)).fetchone()
        if row:
            record = json.loads(row["record"])
            path = self.root / record["path"]
            if path.is_file() and digest_file(path) == record["sha256"]:
                if record.get("source") != source:
                    record = self.relocate_file(source, record)
                    with self.db:
                        self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (source, url, json.dumps(record)))
                return record
        return None

    def save_download(self, source, temp, url, final_url, mime, request=None):
        extension = file_type(temp)
        digest = digest_file(temp)
        path = self.file_path(source, digest, extension)
        path.parent.mkdir(parents=True, exist_ok=True)
        with temp.open("rb") as stream:
            os.fsync(stream.fileno())
        temp.replace(path)
        record = {"source": source, "url": url, "final_url": final_url, "path": str(path.relative_to(self.root)),
                  "format": extension, "content_type": mime, "size_bytes": path.stat().st_size,
                  "sha256": digest, "downloaded_at": now()}
        if request:
            record["request"] = request
        with self.db:
            self.db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)", (source, url, json.dumps(record)))
        return record

    def download(self, client, source, url, hosts):
        record = self.cached_file(source, url)
        if record:
            return record
        directory = self.file_path(source, "0" * 64, "pdf").parent
        directory.mkdir(parents=True, exist_ok=True)
        temp = directory / (hashlib.sha256(url.encode()).hexdigest() + ".part")
        try:
            _, final_url, mime = client.fetch(url, hosts, destination=temp)
            return self.save_download(source, temp, url, final_url, mime)
        finally:
            temp.unlink(missing_ok=True)

    def export(self):
        path = self.root / "documents.jsonl"
        temp = path.with_suffix(".jsonl.tmp")
        with temp.open("w", encoding="utf-8") as output:
            for row in self.db.execute("SELECT record FROM documents ORDER BY id"):
                output.write(row[0] + "\n")
            output.flush()
            os.fsync(output.fileno())
        temp.replace(path)
        return path

    def stats(self):
        return [dict(row) for row in self.db.execute("SELECT source,state,count(*) AS count FROM jobs GROUP BY source,state ORDER BY source,state")]
