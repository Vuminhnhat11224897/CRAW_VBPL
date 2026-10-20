import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch
import zipfile

import requests

from legal_crawler.crawler import Crawler
from legal_crawler.http import Client, FetchError, TMProxy, allowed
from legal_crawler.inspect_vbpl import inspect as inspect_vbpl
from legal_crawler.parser import canonical, document_id, parse_date, parse_page
from legal_crawler.settings import Settings, load_env
from legal_crawler.storage import Store, file_type


ROOT = Path(__file__).resolve().parents[1]
PDF = b"%PDF-1.7\n1 0 obj << /Type /Catalog >> endobj\n%%EOF\n"


def settings(root, **env):
    defaults = {"CONFIG_DIR": str(ROOT / "config"), "DATA_DIR": str(root),
                "TMPROXY_API_KEY": "test-secret", "REQUEST_DELAY": "0", "DATE_FROM": "2026-08-01"}
    defaults.update(env)
    with patch.dict(os.environ, defaults, clear=True):
        return Settings.read(root / "nonexistent.env")


def detail_html(date="01/08/2026", attachment="https://datafiles.chinhphu.vn/a.pdf"):
    return f'''<html><h4>Văn bản thử nghiệm</h4><table>
        <tr><td>Số ký hiệu</td><td>1/2026/NĐ-CP</td></tr>
        <tr><td>Ngày ban hành</td><td>{date}</td></tr>
        <tr><td>Ngày có hiệu lực</td><td>02/08/2026</td></tr>
        <tr><td>Cơ quan ban hành</td><td>Chính phủ</td></tr>
        <tr><td>Trích yếu</td><td>Nội dung thử nghiệm</td></tr>
        <tr><td>Tài liệu đính kèm</td><td><a href="{attachment}">file.pdf</a></td></tr>
        </table></html>'''.encode()


class FakeClient:
    def __init__(self, resources):
        self.resources = resources
        self.calls = []

    def fetch(self, url, hosts, **kwargs):
        assert allowed(url, hosts), url
        self.calls.append((url, kwargs.get("method", "GET")))
        value = self.resources[url]
        if isinstance(value, Exception):
            raise value
        if kwargs.get("destination"):
            kwargs["destination"].write_bytes(value)
            return b"", url, "application/octet-stream"
        return value, url, "text/html; charset=utf-8"


class CrawlerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.settings = settings(self.root, SOURCES="vanban_chinhphu")
        self.config = self.settings.configs["vanban_chinhphu"]

    def store(self):
        store = Store(self.root)
        self.addCleanup(store.db.close)
        return store

    def test_env_is_literal_and_existing_environment_wins(self):
        path = self.root / ".env"
        path.write_text("# comment\nTMPROXY_API_KEY='$(do-not-run)'\nDATE_FROM=2026-08-01\n")
        with patch.dict(os.environ, {"DATE_FROM": "2026-09-01"}, clear=True):
            load_env(path)
            self.assertEqual(os.environ["TMPROXY_API_KEY"], "$(do-not-run)")
            self.assertEqual(os.environ["DATE_FROM"], "2026-09-01")
        self.assertNotIn("test-secret", repr(self.settings))

    def test_config_rejects_invalid_dates_numbers_booleans(self):
        for values in ({"DATE_FROM": "2026-02-31"}, {"DATE_TO": "2026-07-31"},
                       {"REQUEST_RETRIES": "0"}, {"REQUEST_DELAY": "nan"}, {"REFRESH": "yes"}):
            with self.subTest(values=values), self.assertRaises(ValueError):
                settings(self.root, **values)

    def test_source_configs_load(self):
        self.assertEqual(len(settings(self.root).configs), 3)

    def test_parse_government_metadata(self):
        page = parse_page(detail_html(), "https://vanban.chinhphu.vn/?docid=1", self.config)
        self.assertEqual(page["metadata"]["issued_date"], "2026-08-01")
        self.assertEqual(page["metadata"]["number"], "1/2026/NĐ-CP")
        self.assertEqual(page["metadata"]["issuer"], "Chính phủ")
        self.assertEqual(page["metadata"]["title"], "Nội dung thử nghiệm")
        self.assertEqual(len(page["files"]), 1)
        self.assertIsNone(parse_date("31/02/2026"))

    def test_congbao_date_and_cdn_files(self):
        config = settings(self.root).configs["congbao"]
        html = '''<p>Ban hành: 01/08/2026 - Hiệu lực: 02/08/2026</p><h1>Tiêu đề</h1>
        <a href="https://g7.cdnchinhphu.vn/a.docx">Tải về</a>
        <a href="/van-ban/a-123.htm">Chi tiết</a>
        <a href="/van-ban-dang-cong-bao/trang-2.htm">2</a>'''
        page = parse_page(html, "https://congbao.chinhphu.vn/", config)
        self.assertEqual(page["metadata"]["issued_date"], "2026-08-01")
        self.assertEqual(len(page["files"]), 1)
        self.assertEqual(len(page["details"]), 1)
        self.assertEqual(len(page["listings"]), 1)

    def test_congbao_actual_stream_downloads_and_metadata_scope(self):
        self.settings = settings(self.root, SOURCES="congbao", CONGBAO_DOWNLOAD_ALL="true")
        config = self.settings.configs["congbao"]
        url = "https://congbao.chinhphu.vn/van-ban/van-ban-hop-nhat-so-26-2026-vbhn-nd-bkhcn-470788.htm"
        content = (ROOT / "tests/fixtures/congbao_detail.html").read_bytes()
        page = parse_page(content, url, config)
        self.assertEqual(len(page["files"]), 2)
        self.assertTrue(all("/api/download/stream?" in u for u in page["files"]))
        self.assertEqual(page["metadata"]["issuer"], "BỘ KHOA HỌC VÀ CÔNG NGHỆ")
        self.assertEqual(page["metadata"]["issued_date"], "2026-10-08")
        store = self.store()
        client = FakeClient({url: content, **{u: PDF for u in page["files"]}})
        Crawler(self.settings, store, client).detail("congbao", {"url": url})
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(len(record["files"]), 2)

    def test_congbao_finishes_with_original_pdf_and_can_opt_in_to_all_files(self):
        self.settings = settings(self.root, SOURCES="congbao")
        store = self.store()
        url = "https://congbao.chinhphu.vn/van-ban/example-123.htm"
        original = "https://congbaocdn.chinhphu.vn/original.pdf"
        stream = "https://g7.cdnchinhphu.vn/api/download/stream?file_name=example.docx"
        content = detail_html(attachment=original) + f'<a href="{stream}">DOCX</a>'.encode()
        client = FakeClient({url: content, original: PDF, stream: FetchError("SSLError")})
        crawler = Crawler(self.settings, store, client)
        crawler.detail("congbao", {"url": url})
        self.assertNotIn(stream, [call[0] for call in client.calls])
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(len(record["files"]), 1)
        self.settings.congbao_download_all = True
        with self.assertRaisesRegex(FetchError, "partial"):
            crawler.detail("congbao", {"url": url})
        self.assertIn(stream, [call[0] for call in client.calls])

    def test_congbao_can_finish_with_alternative_when_original_fails(self):
        self.settings = settings(self.root, SOURCES="congbao")
        store = self.store()
        url = "https://congbao.chinhphu.vn/van-ban/example-123.htm"
        original = "https://congbaocdn.chinhphu.vn/original.pdf"
        stream = "https://g7.cdnchinhphu.vn/api/download/stream?file_name=example.pdf"
        content = detail_html(attachment=original) + f'<a href="{stream}">PDF</a>'.encode()
        client = FakeClient({url: content, original: FetchError("HTTP 404"), stream: PDF})
        Crawler(self.settings, store, client).detail("congbao", {"url": url})
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(record["files"][0]["url"], stream)
        self.assertEqual(record["errors"][0]["error"], "HTTP 404")

    @patch("legal_crawler.http.time.sleep")
    def test_http_error_body_is_bounded_and_kept_after_retries(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        response = MagicMock(status_code=500)
        response.headers = {}
        response.iter_content.return_value = [b"X" * 70000]
        with patch.object(client.proxy, "get", return_value="http://proxy:80"), patch.object(client.session, "request", return_value=response):
            with self.assertRaises(FetchError) as caught:
                client.fetch("https://vbpl.vn/", ["vbpl.vn"])
        self.assertEqual(str(caught.exception), "HTTP 500")
        self.assertEqual(len(caught.exception.response_body), 65536)

    def test_vbpl_javascript_shell_reports_specific_error(self):
        self.settings = settings(self.root, SOURCES="vbpl")
        self.settings.configs["vbpl"].pop("adapter")
        content = (ROOT / "tests/fixtures/vbpl_loading.html").read_bytes()
        client = FakeClient({"https://vbpl.vn/": content})
        crawler = Crawler(self.settings, self.store(), client)
        with self.assertRaisesRegex(FetchError, "new JavaScript frontend"):
            crawler.listing("vbpl", {"url": "https://vbpl.vn/"})

    def test_vbpl_inspection_saves_only_first_party_next_scripts(self):
        home = b'''<script src="/_next/static/chunks/app.js"></script>
            <script src="/_next/static/chunks/app.js"></script>
            <script src="https://other.test/_next/static/chunks/a.js"></script>
            <script src="/_fec_sbu/fec_wrapper.js"></script>'''
        bundle = "https://vbpl.vn/_next/static/chunks/app.js"
        client = FakeClient({"https://vbpl.vn/": home, bundle: b"app javascript"})
        with patch("builtins.print"):
            self.assertEqual(inspect_vbpl(self.settings, client), 0)
        root = self.root / "diagnostics/vbpl"
        manifest = json.loads((root / "manifest.json").read_text())
        self.assertEqual(len(manifest), 1)
        self.assertEqual(manifest[0]["url"], bundle)
        self.assertEqual((root / manifest[0]["path"]).read_bytes(), b"app javascript")
        self.assertEqual((root / "home.html").read_bytes(), home)

    def test_vbpl_tabs_and_viewer(self):
        config = settings(self.root).configs["vbpl"]
        url = "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=123"
        html = '''<a href="vbpq-vanbangoc.aspx?ItemID=123">Bản gốc</a>
        <iframe src="/viewer?file=%2FFileData%2FTW%2Fa.pdf"></iframe>'''
        page = parse_page(html, url, config)
        self.assertIn("https://vbpl.vn/FileData/TW/a.pdf", page["files"])
        self.assertEqual(len(page["files"]), 1)
        self.assertEqual(len(page["tabs"]), 1)
        self.assertEqual(document_id("vbpl", url), document_id("vbpl", page["tabs"][0]))
        self.assertNotEqual(document_id("vbpl", url), document_id("vbpl", url.replace("/TW/", "/hanoi/")))

    def test_congbao_original_pdf_in_data_href(self):
        config = settings(self.root).configs["congbao"]
        html = '<a href="/van-ban/title/123.htm" data-href="https://congbaocdn.chinhphu.vn/document-signed.pdf">PDF gốc</a>'
        page = parse_page(html, "https://congbao.chinhphu.vn/", config)
        self.assertEqual(page["files"], ["https://congbaocdn.chinhphu.vn/document-signed.pdf"])

    @patch("legal_crawler.http.time.sleep")
    def test_ssl_diagnostic_keeps_reason_without_credentials_or_rotation(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        error = requests.exceptions.SSLError('https://user:test-secret@proxy [SSL: CERTIFICATE_VERIFY_FAILED]')
        with patch.object(client, "response", side_effect=error) as response:
            with self.assertRaises(FetchError) as caught:
                client.fetch("https://g7.cdnchinhphu.vn/", ["cdnchinhphu.vn"])
        self.assertEqual(str(caught.exception), "SSLError: CERTIFICATE_VERIFY_FAILED")
        self.assertTrue(all(not call.kwargs["rotate"] for call in response.call_args_list))

    def test_webforms_keeps_viewstate(self):
        html = '''<form action="/he-thong-van-ban?classid=1">
        <input type="hidden" name="__VIEWSTATE" value="abc">
        <a href="javascript:__doPostBack('pager','Page$2')">2</a></form>'''
        page = parse_page(html, self.config["seeds"][0], self.config)
        self.assertEqual(page["postbacks"][0]["body"]["__VIEWSTATE"], "abc")
        self.assertEqual(page["postbacks"][0]["body"]["__EVENTARGUMENT"], "Page$2")

    def test_host_validation_and_canonical(self):
        self.assertFalse(allowed("https://vbpl.vn.evil.test/a", ["vbpl.vn"]))
        self.assertFalse(allowed("http://localhost/a", ["vbpl.vn"]))
        self.assertFalse(allowed("https://u:p@vbpl.vn/a", ["vbpl.vn"]))
        self.assertFalse(allowed("file:///tmp/a", ["vbpl.vn"]))
        self.assertEqual(canonical("https://vbpl.vn/a?b=2&a=1#tab"), "https://vbpl.vn/a?a=1&b=2")

    def test_file_signatures_and_html_rejection(self):
        path = self.root / "document"
        path.write_bytes(PDF)
        self.assertEqual(file_type(path), "pdf")
        for content in (b"<html>Access denied</html>", b"%PDF-1.7 truncated"):
            path.write_bytes(content)
            with self.assertRaises(FetchError):
                file_type(path)
        with zipfile.ZipFile(path, "w") as archive:
            archive.writestr("[Content_Types].xml", "<Types/>")
            archive.writestr("word/document.xml", "<document/>")
        self.assertEqual(file_type(path), "docx")

    def test_download_dedup_repairs_corrupt_files(self):
        store = self.store()
        first, second = "https://datafiles.chinhphu.vn/a.pdf", "https://datafiles.chinhphu.vn/b.pdf"
        client = FakeClient({first: PDF, second: PDF})
        one = store.download(client, "vanban_chinhphu", first, self.config["file_hosts"])
        two = store.download(client, "vanban_chinhphu", second, self.config["file_hosts"])
        self.assertEqual(one["path"], two["path"])
        store.download(client, "vanban_chinhphu", first, self.config["file_hosts"])
        self.assertEqual(len(client.calls), 2)
        (self.root / one["path"]).write_bytes(b"corrupted")
        store.download(client, "vanban_chinhphu", first, self.config["file_hosts"])
        self.assertEqual(len(client.calls), 3)
        self.assertEqual((self.root / one["path"]).read_bytes(), PDF)

    def test_invalid_download_not_committed(self):
        store = self.store()
        url = "https://datafiles.chinhphu.vn/a.pdf"
        with self.assertRaises(FetchError):
            store.download(FakeClient({url: b"<html>blocked</html>"}), "vanban_chinhphu", url, self.config["file_hosts"])
        self.assertEqual(store.db.execute("SELECT count(*) FROM files").fetchone()[0], 0)
        self.assertFalse(list((self.root / "files").rglob("*.part")))

    def test_end_to_end_date_filter_resume_export(self):
        store = self.store()
        start = self.config["seeds"][0]
        new, old = "https://vanban.chinhphu.vn/?docid=1", "https://vanban.chinhphu.vn/?docid=2"
        file_url = "https://datafiles.chinhphu.vn/a.pdf"
        client = FakeClient({start: f'<a href="{new}">Mới</a><a href="{old}">Cũ</a>'.encode(),
                             new: detail_html(), old: detail_html("31/07/2026"), file_url: PDF})
        crawler = Crawler(self.settings, store, client)
        self.assertEqual(crawler.run(), 0)
        records = [json.loads(row[0]) for row in store.db.execute("SELECT record FROM documents")]
        self.assertEqual({r["status"] for r in records}, {"downloaded", "outside_date_range"})
        self.assertEqual(sum(len(r["files"]) for r in records), 1)
        count = len(client.calls)
        self.assertEqual(crawler.run(), 0)
        self.assertEqual(len(client.calls), count)
        self.assertEqual(len(store.export().read_text().splitlines()), 2)

    def test_retry_partial_preserves_successful_download(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        a, b = "https://datafiles.chinhphu.vn/a.pdf", "https://datafiles.chinhphu.vn/b.pdf"
        page = detail_html() + f'<a href="{b}">b</a>'.encode()
        client = FakeClient({url: page, a: PDF, b: FetchError("HTTP 503")})
        crawler = Crawler(self.settings, store, client)
        with self.assertRaises(FetchError):
            crawler.detail("vanban_chinhphu", {"url": url})
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "partial")
        client.resources[b] = PDF
        crawler.detail("vanban_chinhphu", {"url": url})
        self.assertEqual(sum(call[0] == a for call in client.calls), 1)

    def test_es_duplicate_keeps_metadata_but_skips_download(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        client = FakeClient({url: detail_html("01/08/2026")})
        self.settings.es_url = "http://es.test:9200"
        crawler = Crawler(self.settings, store, client)
        crawler.es.find = MagicMock(return_value="42")
        crawler.detail("vanban_chinhphu", {"url": url})
        crawler.es.find.assert_called_once_with("1/2026/NĐ-CP", "2026-08-01")
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual((record["status"], record["es_match"], record["files"]), ("duplicate_es", "42", []))
        self.assertEqual(len(client.calls), 1)

    def test_es_unavailable_fails_job_instead_of_downloading(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        client = FakeClient({url: detail_html("01/08/2026")})
        self.settings.es_url = "http://127.0.0.1:9"
        self.settings.timeout = 1
        with self.assertRaisesRegex(FetchError, "Elasticsearch"):
            Crawler(self.settings, store, client).detail("vanban_chinhphu", {"url": url})
        self.assertEqual(len(client.calls), 1)

    def test_effective_date_before_start_is_excluded(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        client = FakeClient({url: detail_html("01/08/2026")})
        self.settings.date_from = "2026-08-03"
        self.settings.date_to = ""
        Crawler(self.settings, store, client).detail("vanban_chinhphu", {"url": url})
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "outside_date_range")

    def test_es_number_folds_cyrillic_lookalikes(self):
        from legal_crawler.es import normalize
        self.assertEqual(normalize("379/2026/NĐ-C\u0420"), normalize("379/2026/NĐ-CP"))
        self.assertEqual(normalize(" 1/2026 / NĐ-CP"), "1/2026/NĐ-CP")

    def test_missing_date_does_not_download(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        client = FakeClient({url: detail_html("Không rõ")})
        with self.assertRaisesRegex(FetchError, "missing_date"):
            Crawler(self.settings, store, client).detail("vanban_chinhphu", {"url": url})
        self.assertEqual(len(client.calls), 1)

    def test_date_change_revisits_previously_excluded_documents(self):
        store = self.store()
        url = "https://vanban.chinhphu.vn/?docid=1"
        start = self.config["seeds"][0]
        file_url = "https://datafiles.chinhphu.vn/a.pdf"
        client = FakeClient({start: f'<a href="{url}">doc</a>'.encode(),
                             url: detail_html("31/07/2026"), file_url: PDF})
        Crawler(self.settings, store, client).run()
        self.assertNotIn(file_url, [c[0] for c in client.calls])
        self.settings.date_from = "2026-07-01"
        Crawler(self.settings, store, client).run()
        self.assertIn(file_url, [c[0] for c in client.calls])
        self.assertEqual(store.db.execute("SELECT count(*) FROM documents").fetchone()[0], 1)

    def test_page_budget_and_pending_resume(self):
        store = self.store()
        self.settings.max_pages = 1
        start = self.config["seeds"][0]
        url = "https://vanban.chinhphu.vn/?docid=1"
        client = FakeClient({start: f'<a href="{url}">doc</a>'.encode(), url: detail_html(),
                             "https://datafiles.chinhphu.vn/a.pdf": PDF})
        crawler = Crawler(self.settings, store, client)
        self.assertEqual(crawler.run(), 0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0], 1)
        self.assertEqual(crawler.run(), 0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM files").fetchone()[0], 1)

    def test_empty_listing_fails_and_is_retried_next_run(self):
        store = self.store()
        start = self.config["seeds"][0]
        client = FakeClient({start: b"<html>JavaScript required</html>"})
        crawler = Crawler(self.settings, store, client)
        self.assertEqual(crawler.run(), 1)
        self.assertEqual(crawler.run(), 1)
        self.assertEqual(len(client.calls), 2)

    def test_round_robin_discovery_empty_sources_and_failure(self):
        self.settings = settings(self.root, MAX_PAGES="0")
        sources = list(self.settings.configs)
        for config in self.settings.configs.values():
            config["seeds"] = config["seeds"][:1]
        store = self.store()
        crawler = Crawler(self.settings, store, FakeClient({}))
        calls = []

        def discover(source, payload):
            calls.append((source, "listing"))
            for number in range({sources[0]: 3, sources[1]: 1, sources[2]: 2}[source]):
                store.enqueue(crawler.scope, source, "detail", {"number": number})

        def process(source, payload):
            calls.append((source, payload["number"]))
            if source == sources[1]:
                raise FetchError("HTTP 503")

        with patch.object(crawler, "listing", side_effect=discover), patch.object(crawler, "detail", side_effect=process):
            self.assertEqual(crawler.run(), 1)
        self.assertEqual(calls, [
            *[(source, "listing") for source in sources],
            *[(source, 0) for source in sources],
            (sources[0], 1), (sources[2], 1), (sources[0], 2),
        ])
        self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0], 0)
        self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE state='failed'").fetchone()[0], 1)

    def test_round_robin_budget_per_source_and_resume_existing_queue(self):
        self.settings = settings(self.root, MAX_PAGES="2")
        for config in self.settings.configs.values():
            config["seeds"] = []
        store = self.store()
        crawler = Crawler(self.settings, store, FakeClient({}))
        sources = list(self.settings.configs)
        for source in sources:
            for number in range(4):
                store.enqueue(crawler.scope, source, "detail", {"number": number})
        calls = []
        with patch.object(crawler, "detail", side_effect=lambda source, payload: calls.append((source, payload["number"]))):
            self.assertEqual(crawler.run(), 0)
            self.assertEqual(calls, [(source, number) for number in range(2) for source in sources])
            self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0], 6)
            self.assertEqual(crawler.run(), 0)
            self.assertEqual(calls, [(source, number) for number in range(4) for source in sources])
            self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE state='pending'").fetchone()[0], 0)

    def test_vbpl_merges_metadata_and_files_from_same_document_tabs(self):
        self.settings = settings(self.root, SOURCES="vbpl")
        self.settings.configs["vbpl"].pop("adapter")
        store = self.store()
        page = "https://vbpl.vn/TW/Pages/vbpq-toanvan.aspx?ItemID=1"
        original = "https://vbpl.vn/TW/Pages/vbpq-vanbangoc.aspx?ItemID=1"
        other = "https://vbpl.vn/TW/Pages/vbpq-thuoctinh.aspx?ItemID=2"
        file_url = "https://vbpl.vn/FileData/TW/a.pdf"
        client = FakeClient({page: f'<h1>Title</h1><a href="{original}">Bản gốc</a><a href="{other}">Related</a>'.encode(),
            original: detail_html(attachment=file_url), file_url: PDF})
        Crawler(self.settings, store, client).detail("vbpl", {"url": page})
        record = json.loads(store.db.execute("SELECT record FROM documents").fetchone()[0])
        self.assertEqual(record["status"], "downloaded")
        self.assertEqual(record["issued_date"], "2026-08-01")
        self.assertEqual(len(record["raw_pages"]), 2)
        self.assertNotIn(other, [c[0] for c in client.calls])

    def test_relative_postbacks_do_not_collapse_different_pages(self):
        store = self.store()
        crawler = Crawler(self.settings, store, FakeClient({}))
        url = self.config["seeds"][0]
        request = {"url": url, "method": "POST", "body": {"__EVENTTARGET": "pager", "__EVENTARGUMENT": "Page$Next"}}
        for number in (1, 2):
            parsed = {"details": {f"https://vanban.chinhphu.vn/?docid={number}": "doc"},
                      "listings": [], "postbacks": [request]}
            with patch.object(crawler, "page", return_value=parsed):
                crawler.listing("vanban_chinhphu", {"url": url})
        self.assertEqual(store.db.execute("SELECT count(*) FROM jobs WHERE kind='listing'").fetchone()[0], 2)

    def test_date_parser_does_not_take_date_inside_document_title(self):
        html = '<h1>Ban hành quy định áp dụng từ 01/01/2020</h1>' + detail_html().decode()
        page = parse_page(html, "https://vanban.chinhphu.vn/?docid=1", self.config)
        self.assertEqual(page["metadata"]["issued_date"], "2026-08-01")

    @patch("legal_crawler.http.time.sleep")
    def test_oversized_stream_is_removed(self, sleep):
        self.settings.max_file = 4
        client = Client(self.settings)
        self.addCleanup(client.close)
        response = MagicMock()
        response.__enter__.return_value = response
        response.headers = {}
        response.iter_content.side_effect = lambda _: iter([b"123", b"456"])
        path = self.root / "file.part"
        with patch.object(client, "response", return_value=response):
            with self.assertRaisesRegex(FetchError, "size limit"):
                client.fetch("https://vbpl.vn/a.pdf", ["vbpl.vn"], destination=path)
        self.assertFalse(path.exists())

    def test_tmproxy_credentials_and_cooldown(self):
        proxy = TMProxy(self.settings)
        self.addCleanup(proxy.session.close)
        self.assertTrue(proxy.use({"code": 0, "data": {"https": "1.2.3.4:1234", "username": "a@b", "password": "p:/", "next_request": 999, "timeout": 0}}))
        self.assertEqual(proxy.get(), "http://a%40b:p%3A%2F@1.2.3.4:1234")
        with patch.object(proxy, "call") as call, patch("legal_crawler.http.time.sleep") as sleep:
            self.assertEqual(proxy.get(rotate=True), proxy.url)
            call.assert_not_called()
            sleep.assert_not_called()
        proxy.expires = 0
        with self.assertRaisesRegex(FetchError, "cooldown"):
            proxy.get(rotate=True)

    @patch("legal_crawler.http.time.sleep")
    def test_http_error_keeps_original_reason_and_reuses_proxy(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        with patch.object(client, "response", side_effect=FetchError("HTTP 400")) as response:
            with self.assertRaisesRegex(FetchError, "^HTTP 400$"):
                client.fetch("https://g7.cdnchinhphu.vn/api/download/stream", ["cdnchinhphu.vn"])
            self.assertTrue(all(not call.kwargs["rotate"] for call in response.call_args_list))

    @patch("legal_crawler.http.time.sleep")
    def test_rotation_error_does_not_hide_initial_http_error(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        with patch.object(client, "response", side_effect=[FetchError("HTTP 403", rotate=True), FetchError("TMProxy API request failed"), FetchError("HTTP 503")]):
            with self.assertRaises(FetchError) as caught:
                client.fetch("https://vbpl.vn/", ["vbpl.vn"])
        self.assertIn("HTTP 403", str(caught.exception))
        self.assertIn("TMProxy API request failed", str(caught.exception))

    @patch("legal_crawler.http.time.sleep")
    def test_retry_with_proxy_in_cooldown_recovers_download(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        client.proxy.use({"code": 0, "data": {"https": "1.2.3.4:1234", "next_request": 999, "timeout": 0}})
        blocked = MagicMock(status_code=403)
        blocked.headers = {}
        complete = MagicMock(status_code=200)
        complete.headers = {"Content-Type": "application/pdf"}
        complete.url = "https://vbpl.vn/a.pdf"
        complete.__enter__.return_value = complete
        complete.iter_content.return_value = [PDF]
        path = self.root / "file.part"
        with patch.object(client.proxy, "call") as call, patch.object(client.session, "request", side_effect=[blocked, complete]):
            client.fetch(complete.url, ["vbpl.vn"], destination=path)
            call.assert_not_called()
        self.assertEqual(path.read_bytes(), PDF)

    def test_tmproxy_missing_key_fails_closed(self):
        self.settings.api_key = ""
        with self.assertRaisesRegex(ValueError, "TMPROXY_API_KEY"):
            Client(self.settings)

    def test_tmproxy_api_payload_and_error_does_not_leak_key(self):
        proxy = TMProxy(self.settings)
        self.addCleanup(proxy.session.close)
        response = MagicMock()
        response.__enter__.return_value = response
        response.json.return_value = {"code": 0, "data": {"https": "1.2.3.4:80"}}
        with patch.object(proxy.session, "post", return_value=response) as post:
            proxy.call("get-new-proxy")
            self.assertEqual(post.call_args.kwargs["json"], {"api_key": "test-secret", "id_location": 0, "id_isp": 0})
        with patch.object(proxy.session, "post", side_effect=requests.ConnectionError("test-secret")):
            with self.assertRaises(FetchError) as caught:
                proxy.call("get-current-proxy")
            self.assertNotIn("test-secret", str(caught.exception))

    @patch("legal_crawler.http.time.sleep")
    def test_http_uses_proxy_on_both_protocols_and_checks_redirect(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        response = MagicMock(status_code=302)
        response.headers = {"Location": "http://127.0.0.1/private"}
        with patch.object(client.proxy, "get", return_value="http://proxy:80"), patch.object(client.session, "request", return_value=response) as request:
            with self.assertRaisesRegex(FetchError, "outside"):
                client.response("GET", "https://vbpl.vn/", ["vbpl.vn"])
            self.assertEqual(request.call_count, 1)
            self.assertEqual(request.call_args.kwargs["proxies"], {"http": "http://proxy:80", "https": "http://proxy:80"})
            self.assertFalse(client.session.trust_env)

    @patch("legal_crawler.http.time.sleep")
    def test_stream_failure_retries_without_partial_bytes(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        broken, complete = MagicMock(), MagicMock()
        for response in (broken, complete):
            response.__enter__.return_value = response
            response.headers = {"Content-Type": "application/pdf"}
            response.url = "https://vbpl.vn/a.pdf"
        def interrupted(_):
            yield b"partial bytes"
            raise requests.ConnectionError("proxy secret")
        broken.iter_content.side_effect = interrupted
        complete.iter_content.return_value = [PDF]
        path = self.root / "file.part"
        with patch.object(client, "response", side_effect=[broken, complete]) as fetch:
            client.fetch("https://vbpl.vn/a.pdf", ["vbpl.vn"], destination=path)
            self.assertEqual(path.read_bytes(), PDF)
            self.assertTrue(fetch.call_args.kwargs["rotate"])


if __name__ == "__main__":
    unittest.main()
