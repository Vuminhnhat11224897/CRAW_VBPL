import hashlib
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from legal_crawler.storage import Store
from test_crawler import FakeClient, PDF, settings


class SourceFoldersTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def store(self):
        store = Store(self.root)
        self.addCleanup(store.db.close)
        return store

    def legacy(self, sources=("vanban_chinhphu",)):
        digest = hashlib.sha256(PDF).hexdigest()
        path = Path("files") / digest[:2] / (digest + ".pdf")
        (self.root / path).parent.mkdir(parents=True)
        (self.root / path).write_bytes(PDF)
        record = {"url": "https://datafiles.chinhphu.vn/a.pdf", "final_url": "https://datafiles.chinhphu.vn/a.pdf",
                  "path": str(path), "sha256": digest, "format": "pdf", "size_bytes": len(PDF)}
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            db.execute("CREATE TABLE files(url TEXT PRIMARY KEY, record TEXT)")
            db.execute("INSERT INTO files VALUES(?,?)", (record["url"], json.dumps(record)))
            db.execute("CREATE TABLE documents(id TEXT PRIMARY KEY,source TEXT,record TEXT)")
            for source in sources:
                document = {"id": source + ":abc", "source": source, "status": "downloaded", "files": [record]}
                db.execute("INSERT INTO documents VALUES(?,?,?)", (document["id"], source, json.dumps(document)))
        return record

    def test_legacy_files_metadata_export_and_progress_are_migrated_together(self):
        old = self.legacy()
        store = self.store()
        store.enqueue("scope", "vanban_chinhphu", "listing", {"url": "https://vanban.chinhphu.vn/"})
        jobs = store.stats()
        self.assertEqual(store.migrate_files(), 1)
        file = store.cached_file("vanban_chinhphu", old["url"])
        self.assertEqual(file["path"], "files/vanban_chinhphu/" + old["sha256"] + ".pdf")
        self.assertEqual((self.root / file["path"]).read_bytes(), PDF)
        self.assertFalse((self.root / old["path"]).exists())
        metadata = json.loads((self.root / "metadata/vanban_chinhphu/abc.json").read_text())
        exported = json.loads((self.root / "documents.jsonl").read_text())
        self.assertEqual(metadata["files"][0]["path"], file["path"])
        self.assertEqual(exported, metadata)
        self.assertEqual(store.stats(), jobs)
        self.assertTrue((self.root / "state.before-source-folders.sqlite3").is_file())
        self.assertEqual(store.migrate_files(), 0)
        client = FakeClient({})
        store.download(client, "vanban_chinhphu", old["url"], ["chinhphu.vn"])
        self.assertEqual(client.calls, [])

    def test_shared_url_has_separate_source_paths_and_cache_entries(self):
        old = self.legacy(("vanban_chinhphu", "congbao"))
        store = self.store()
        self.assertEqual(store.migrate_files(), 2)
        one = store.cached_file("vanban_chinhphu", old["url"])
        two = store.cached_file("congbao", old["url"])
        self.assertNotEqual(one["path"], two["path"])
        self.assertEqual(store.db.execute("SELECT count(*) FROM files").fetchone()[0], 2)
        (self.root / one["path"]).write_bytes(b"edited")
        self.assertEqual((self.root / two["path"]).read_bytes(), PDF)

    def test_cross_source_cache_reuse_does_not_download_again(self):
        store = self.store()
        url = "https://datafiles.chinhphu.vn/a.pdf"
        client = FakeClient({url: PDF})
        one = store.download(client, "vanban_chinhphu", url, ["chinhphu.vn"])
        two = store.download(client, "congbao", url, ["chinhphu.vn"])
        self.assertEqual(len(client.calls), 1)
        self.assertNotEqual(one["path"], two["path"])
        self.assertEqual((self.root / two["path"]).read_bytes(), PDF)

    def test_bad_source_and_symlink_cannot_write_outside_data(self):
        store = self.store()
        for source in ("../outside", "/tmp/outside", "SOURCE"):
            with self.assertRaises(ValueError):
                store.file_path(source, "a" * 64, "pdf")
        (self.root / "files").mkdir()
        (self.root / "files/congbao").symlink_to(self.root.parent, target_is_directory=True)
        with self.assertRaises(ValueError):
            store.file_path("congbao", "a" * 64, "pdf")

    def test_corrupt_original_is_preserved_and_db_is_not_converted(self):
        old = self.legacy()
        (self.root / old["path"]).write_bytes(b"broken")
        store = self.store()
        with self.assertRaisesRegex(ValueError, "corrupt"):
            store.migrate_files()
        self.assertTrue((self.root / old["path"]).exists())
        self.assertNotIn("source", {row["name"] for row in store.db.execute("PRAGMA table_info(files)")})
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 0)

    def test_interrupted_metadata_write_keeps_old_files_and_resumes(self):
        old = self.legacy()
        store = self.store()
        with patch("legal_crawler.storage.atomic_json", side_effect=OSError("interrupted")):
            with self.assertRaises(OSError):
                store.migrate_files()
        self.assertTrue((self.root / old["path"]).exists())
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 0)
        self.assertEqual(store.migrate_files(), 1)
        self.assertFalse((self.root / old["path"]).exists())
        self.assertEqual(store.db.execute("PRAGMA user_version").fetchone()[0], 1)

    def test_unreferenced_legacy_file_is_preserved_and_can_be_assigned_later(self):
        old = self.legacy(())
        store = self.store()
        self.assertEqual(store.migrate_files(), 1)
        cached = store.cached_file("congbao", old["url"])
        self.assertEqual((self.root / cached["path"]).read_bytes(), PDF)
        self.assertTrue(cached["path"].startswith("files/congbao/"))

    def test_two_letter_source_does_not_delete_its_own_file(self):
        digest = hashlib.sha256(PDF).hexdigest()
        old = self.legacy((digest[:2],))
        store = self.store()
        store.migrate_files()
        self.assertEqual((self.root / old["path"]).read_bytes(), PDF)

    def test_migration_cli_acquires_lock_and_completes_without_proxy(self):
        from legal_crawler.__main__ import main
        self.legacy()
        config = settings(self.root)
        with patch("sys.argv", ["legal_crawler", "migrate-files"]), patch("legal_crawler.__main__.Settings.read", return_value=config), patch("legal_crawler.__main__.Client") as client, patch("builtins.print"), patch("logging.basicConfig"):
            self.assertEqual(main(), 0)
            client.assert_not_called()
        with sqlite3.connect(self.root / "state.sqlite3") as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 1)
        self.assertTrue((self.root / "documents.jsonl").exists())

    def test_migration_waits_for_current_crawler_before_changing_data(self):
        from legal_crawler.__main__ import main
        import fcntl
        self.legacy()
        config = settings(self.root)
        with patch("sys.argv", ["legal_crawler", "migrate-files"]), patch("legal_crawler.__main__.Settings.read", return_value=config), patch("legal_crawler.__main__.Client") as client, patch("builtins.print"), patch("logging.basicConfig"), patch("fcntl.flock", side_effect=[BlockingIOError(), None]) as lock:
            self.assertEqual(main(), 0)
            client.assert_not_called()
            self.assertEqual([call.args[1] for call in lock.call_args_list], [fcntl.LOCK_EX | fcntl.LOCK_NB, fcntl.LOCK_EX])


if __name__ == "__main__":
    unittest.main()
