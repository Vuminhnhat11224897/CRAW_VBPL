import base64
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from urllib.parse import unquote

from legal_crawler.crawler import Crawler
from legal_crawler.http import Client, FetchError, allowed
from legal_crawler.storage import Store
from legal_crawler.vbpl import action_result
from test_crawler import PDF, settings


def flight(value):
    return ('0:["$@1",["",[]]]\n1:' + json.dumps(value, ensure_ascii=False) + '\n').encode()


class ActionClient:
    def __init__(self, config, handler):
        self.config, self.handler, self.calls = config, handler, []

    def fetch(self, url, hosts, **kwargs):
        assert url == self.config["action_url"] and allowed(url, hosts)
        assert kwargs["method"] == "POST"
        headers = kwargs["headers"]
        assert headers["Accept"] == "text/x-component"
        assert json.loads(unquote(headers["Next-Router-State-Tree"])) == self.config["router_state"]
        name = next(name for name, value in self.config["actions"].items() if value == headers["Next-Action"])
        args = json.loads(kwargs["data"])
        self.calls.append((name, args))
        data = flight(self.handler(name, args))
        if kwargs.get("destination"):
            kwargs["destination"].write_bytes(data)
            return b"", url, "text/x-component"
        return data, url, "text/x-component"


class VBPLTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.settings = settings(self.root, SOURCES="vbpl", MAX_PAGES="0", VBPL_PAGE_SIZE="1")
        self.store = Store(self.root)
        self.addCleanup(self.store.db.close)

    def test_flight_references_dates_and_multibyte_text(self):
        text = 'Nội dung\ncó xuống dòng'.encode()
        content = (b'0:{"a":"$@1","f":[]}\n1:{"title":"$2","date":"$D2026-08-01T00:00:00Z","missing":"$undefined"}\n'
                   + f'2:T{len(text):x},'.encode() + text + b'\n')
        self.assertEqual(action_result(content), {"title": text.decode(), "date": "2026-08-01T00:00:00Z", "missing": None})

    def test_flight_invalid_html_and_error_rows_fail_closed(self):
        for value in (b'<html>Error</html>', b'0:{"a":"$@1"}\n1:E{"digest":"123"}\n',
                      b'0:{"a":"$@1"}\n1:{"data":"$1"}\n', b'0:{"a":"$@2"}\n', b'2:Tffff,short'):
            with self.subTest(value=value), self.assertRaises(FetchError):
                action_result(value)

    def test_next14_action_result_ignores_unrelated_router_component_references(self):
        response = b'0:["$@1",["",["$L99","$L100"]]]\n1:{"items":[],"total":0}\n'
        self.assertEqual(action_result(response), {"items": [], "total": 0})

    def test_failed_action_saves_response_for_diagnosis(self):
        body = b'0:["$@1",["",[]]]\n1:E{"digest":"failure"}\n'
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: None)
        crawler = Crawler(self.settings, self.store, client)
        with patch.object(client, "fetch", side_effect=FetchError("HTTP 500", response_body=body)):
            with self.assertRaisesRegex(FetchError, "response saved"):
                crawler.vbpl.action("listing", [{"pageNumber": 1}])
        captures = list((self.root / "raw/vbpl").glob("*.error"))
        self.assertEqual(len(captures), 1)
        self.assertEqual(captures[0].read_bytes(), body)

    def test_actions_pagination_filter_download_and_resume(self):
        def handler(name, args):
            if name == "listing":
                page = args[0]["pageNumber"]
                return {"items": [{"id": f"doc-{page}", "title": "Văn bản"}], "total": 2}
            if name == "detail":
                return {"id": args[0], "title": "Văn bản", "docNum": "1/2026",
                        "issueDate": "2026-08-01T00:00:00Z", "effFrom": "02/08/2026"}
            if name == "files":
                return [{"fileName": "Văn bản gốc.pdf", "relatedType": "1"},
                        {"fileName": "skip.docx", "relatedType": "3"}]
            return {"data": base64.b64encode(PDF).decode(), "mimeType": "application/pdf"}
        client = ActionClient(self.settings.configs["vbpl"], handler)
        crawler = Crawler(self.settings, self.store, client)
        self.assertEqual(crawler.run(), 0)
        listing = [args[0] for name, args in client.calls if name == "listing"]
        self.assertEqual([args["pageNumber"] for args in listing], [1, 2])
        self.assertTrue(all(args["issueDateFrom"] == "2026-08-01T00:00:00" for args in listing))
        records = [json.loads(row[0]) for row in self.store.db.execute("SELECT record FROM documents")]
        self.assertEqual(len(records), 2)
        for record in records:
            self.assertEqual(record["status"], "downloaded")
            self.assertEqual(record["issued_date"], "2026-08-01")
            self.assertEqual(len(record["files"]), 1)
            saved = record["files"][0]
            self.assertEqual((self.root / saved["path"]).read_bytes(), PDF)
            self.assertEqual(saved["request"]["arguments"][0]["objectName"], "Văn bản gốc.pdf")
        count = len(client.calls)
        self.assertEqual(crawler.run(), 0)
        self.assertEqual(len(client.calls), count)

    def test_empty_date_range_finishes_without_false_failure(self):
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: {"items": [], "total": 0})
        self.assertEqual(Crawler(self.settings, self.store, client).run(), 0)
        self.assertEqual(len(client.calls), 1)

    def test_filter_dates_follow_frontend_api_time_format_and_include_end_day(self):
        from datetime import datetime
        self.settings.date_to = "2026-10-14"

        def handler(name, args):
            filters = args[0]
            for key in ("issueDateFrom", "issueDateTo"):
                datetime.strptime(filters[key], "%Y-%m-%dT%H:%M:%S")
            self.assertEqual(filters["issueDateFrom"], "2026-08-01T00:00:00")
            self.assertEqual(filters["issueDateTo"], "2026-10-14T23:59:59")
            return {"items": [], "total": 0}

        client = ActionClient(self.settings.configs["vbpl"], handler)
        self.assertEqual(Crawler(self.settings, self.store, client).run(), 0)
        self.assertEqual(self.settings.date_from, "2026-08-01")
        self.assertEqual(self.settings.date_to, "2026-10-14")

    def test_missing_date_bounds_are_omitted_from_request(self):
        self.settings.date_from = self.settings.date_to = ""
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: {"items": [], "total": 0})
        self.assertEqual(Crawler(self.settings, self.store, client).run(), 0)
        filters = client.calls[0][1][0]
        self.assertNotIn("issueDateFrom", filters)
        self.assertNotIn("issueDateTo", filters)

    def test_actual_server_error_keeps_digest_in_saved_failure(self):
        body = (Path(__file__).parent / "fixtures/vbpl_error.flight").read_bytes()
        with self.assertRaisesRegex(FetchError, "digest 748076738"):
            action_result(body)
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: None)
        crawler = Crawler(self.settings, self.store, client)
        with patch.object(client, "fetch", side_effect=FetchError("HTTP 500", response_body=body)):
            with self.assertRaisesRegex(FetchError, "HTTP 500; VBPL server action failed \\(digest 748076738\\)"):
                crawler.vbpl.action("listing", [{"pageNumber": 1}])

    def test_source_schema_change_fails_instead_of_silently_finishing(self):
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: {"items": [], "total": 100})
        self.assertEqual(Crawler(self.settings, self.store, client).run(), 1)

    def test_invalid_file_payload_is_not_saved(self):
        client = ActionClient(self.settings.configs["vbpl"], lambda name, args: {"data": "%%%", "mimeType": "application/pdf"})
        crawler = Crawler(self.settings, self.store, client)
        with self.assertRaisesRegex(FetchError, "base64"):
            crawler.vbpl.download('https://vbpl.vn/#documentId=doc-1&fileName=a.pdf')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM files").fetchone()[0], 0)
        self.assertFalse(list((self.root / "files").rglob("*.part")))

    def test_decoded_html_and_oversized_file_are_rejected(self):
        for data in (b'<html>error</html>', PDF):
            client = ActionClient(self.settings.configs["vbpl"], lambda name, args: {"data": base64.b64encode(data).decode()})
            self.settings.max_file = 8 if data == PDF else 1024
            crawler = Crawler(self.settings, self.store, client)
            with self.assertRaises(FetchError):
                crawler.vbpl.download('https://vbpl.vn/#documentId=doc-1&fileName=a.pdf')
        self.assertEqual(self.store.db.execute("SELECT count(*) FROM files").fetchone()[0], 0)

    @patch("legal_crawler.http.time.sleep")
    def test_action_headers_and_arguments_reach_http_client(self, sleep):
        client = Client(self.settings)
        self.addCleanup(client.close)
        from unittest.mock import MagicMock
        response = MagicMock(status_code=200)
        with patch.object(client.proxy, "get", return_value="http://proxy:80"), patch.object(client.session, "request", return_value=response) as request:
            client.response("POST", "https://vbpl.vn/", ["vbpl.vn"], data='[{"pageNumber":1}]', headers={"Next-Action": "action-id"})
        self.assertEqual(request.call_args.kwargs["headers"], {"Next-Action": "action-id"})
        self.assertEqual(request.call_args.kwargs["data"], '[{"pageNumber":1}]')
        self.assertEqual(request.call_args.kwargs["proxies"]["https"], "http://proxy:80")


if __name__ == "__main__":
    unittest.main()
