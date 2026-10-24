from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
from threading import Thread
import unittest
from unittest.mock import Mock

from app.config import Settings
from app.feishu import FeishuError
from app.server import Application, handler_for
from tests.helpers import temporary_directory as TemporaryDirectory


class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = TemporaryDirectory()
        cls.app = Application(Settings(data_dir=Path(cls.tmp.name)))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(cls.app))
        cls.thread = Thread(target=cls.server.serve_forever, daemon=True)
        cls.thread.start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.thread.join()
        cls.tmp.cleanup()

    def request(self, path, body=None, headers=None, method=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json"}
        request_headers.update(headers or {})
        raw = json.dumps(body) if isinstance(body, dict) else body
        conn.request(method or ("POST" if body is not None else "GET"), path, raw, request_headers)
        response = conn.getresponse()
        data = response.read()
        result = (response.status, dict(response.getheaders()), data)
        conn.close()
        return result

    def test_status_no_credential_exposure(self):
        status, headers, data = self.request("/api/status")
        self.assertEqual(status, 200)
        self.assertNotIn(b"secret", data)
        self.assertFalse(json.loads(data)["feishu_configured"])
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_end_to_end_all_periods_and_download(self):
        status, _, raw = self.request("/api/demo/seed", {})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(raw)["message_count"], 140)
        _, _, raw = self.request("/api/status")
        today = json.loads(raw)["today"]
        for period in ("daily", "weekly", "monthly"):
            status, _, raw = self.request("/api/reports", {"period": period, "anchor_date": today,
                "source": "demo", "chat_ids": ["demo-product", "demo-project"], "audience": "personal", "sender_ids": ["demo-me"]})
            self.assertEqual(status, 200, raw)
            report = json.loads(raw)["report"]
            self.assertEqual(report["audience"], "personal")
            status, headers, data = self.request(f"/api/reports/{report['id']}/download")
            self.assertEqual(status, 200)
            self.assertIn("filename*=UTF-8", headers["Content-Disposition"])
            self.assertEqual(data.decode("utf-8"), report["markdown"])
            self.assertNotIn("示例同事", report["markdown"])

    def test_real_source_requires_local_configuration(self):
        status, _, raw = self.request("/api/chats?source=feishu")
        self.assertEqual(status, 400)
        self.assertIn("尚未配置飞书", raw.decode())

    def test_input_validation(self):
        for data in ("{", "[]", '{"source":"feishu","chat_ids":[]}'):
            status, _, _ = self.request("/api/reports", data)
            self.assertEqual(status, 400)
        status, _, _ = self.request("/api/reports", {"source": [], "chat_ids": ["one"]})
        self.assertEqual(status, 400)
        status, _, _ = self.request("/api/reports", {}, {"Content-Type": "text/plain"})
        self.assertEqual(status, 415)

    def test_cross_site_and_dns_rebinding_rejected(self):
        status, _, _ = self.request("/api/demo/seed", {}, {"Origin": "https://evil.invalid"})
        self.assertEqual(status, 403)
        status, _, _ = self.request("/api/status", headers={"Host": "evil.invalid"})
        self.assertEqual(status, 403)

    def test_failed_multi_group_sync_does_not_partially_replace(self):
        # Isolated app invocation: no live HTTP class state or real Feishu request.
        with TemporaryDirectory() as temp:
            app = Application(Settings(data_dir=Path(temp)))
            fake = Mock()
            fake.list_messages.side_effect = [[], FeishuError("模拟无权限")]
            app.feishu = fake
            with self.assertRaises(FeishuError):
                app.post("/api/sync", {"source": "feishu", "chat_ids": ["a", "b"],
                                      "start_date": "2026-10-19", "end_date": "2026-10-19"})
            self.assertEqual(app.storage.count("feishu"), 0)


if __name__ == "__main__":
    unittest.main()
