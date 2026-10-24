from datetime import date
from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
from threading import Thread
import unittest

from app.config import Settings
from app.feishu import FeishuError
from app.periods import timestamp
from app.server import Application, handler_for
from tests.helpers import temporary_directory


DAY = "2026-10-20"
START = timestamp(date(2026, 10, 20))


def record(mid, sender, text, hour=9):
    return {"message_id": mid, "chat_id": "test-group", "sender_id": sender,
            "sender_name": sender, "text": text, "created_at": START + hour * 3600}


class FakeFeishu:
    def __init__(self):
        self.calls = []
        self.fail = False

    def list_messages(self, chat_id, begin, end):
        self.calls.append((chat_id, begin, end))
        if self.fail:
            raise FeishuError("测试群暂时无法读取")
        return [record("fresh-self", "me", "已完成接口测试"),
                record("fresh-other", "other", "已完成同事独有工作"),
                record("fresh-plan", "me", "明天计划整理评审记录", 10)]


class WorkflowHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.app = Application(Settings(data_dir=Path(self.temp.name)))
        self.app.storage.save_chats("feishu", [{"chat_id": "test-group", "name": "隔离测试群"}])
        self.app.storage.save_messages("feishu", [record("stale", "me", "已完成上一版缓存事项")])
        self.app.storage.save_profile("feishu", "me", "本人")
        self.fake = FakeFeishu()
        self.app.feishu = self.fake
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None, headers=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=5)
        request_headers = {"Content-Type": "application/json", **(headers or {})}
        connection.request("GET" if body is None else "POST", path,
                           None if body is None else json.dumps(body), request_headers)
        response = connection.getresponse()
        status, content = response.status, response.read()
        connection.close()
        return status, content

    def payload(self, **extra):
        return {"source": "feishu", "period": "daily", "anchor_date": DAY,
                "chat_ids": ["test-group"], "audience": "personal", "sender_ids": ["me"],
                "use_llm": False, **extra}

    def test_one_click_syncs_fresh_data_filters_personal_and_downloads_same_report(self):
        status, raw = self.request("/api/run", self.payload())
        self.assertEqual(status, 200, raw)
        result = json.loads(raw)
        self.assertEqual(result["synced_count"], 3)
        self.assertEqual(result["report"]["message_count"], 2)
        self.assertEqual(self.fake.calls, [("test-group", START, START + 86400)])
        markdown = result["report"]["markdown"]
        self.assertNotIn("上一版缓存事项", markdown)
        self.assertNotIn("同事独有工作", markdown)
        self.assertIn("已完成接口测试", markdown)
        self.assertIn("明天计划整理评审记录", markdown)
        status, download = self.request(f"/api/reports/{result['report']['id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(download.decode("utf-8"), markdown)

    def test_sync_failure_cannot_generate_from_previous_cached_messages(self):
        self.fake.fail = True
        status, raw = self.request("/api/run", self.payload())
        self.assertEqual(status, 502, raw)
        self.assertEqual(self.app.storage.reports(), [])
        self.assertEqual(self.app.storage.count("feishu"), 1)

    def test_preferences_restore_source_without_saving_date_or_ai_switch(self):
        status, raw = self.request("/api/preferences", {
            "source": "feishu", "chat_ids": ["test-group"], "period": "weekly",
            "audience": "personal", "sender_id": "me", "use_llm": True,
            "anchor_date": "2020-01-01"})
        self.assertEqual(status, 200, raw)
        _, raw = self.request("/api/preferences?source=feishu")
        saved = json.loads(raw)
        self.assertTrue(saved["has_saved"])
        self.assertEqual(saved["last_source"], "feishu")
        self.assertEqual(set(saved["preferences"]), {"chat_ids", "period", "audience", "sender_id"})
        _, raw = self.request("/api/status")
        self.assertEqual(json.loads(raw)["preferred_source"], "feishu")
        _, raw = self.request("/api/preferences?source=demo")
        self.assertFalse(json.loads(raw)["has_saved"])
        self.assertEqual(self.fake.calls, [])

    def test_invalid_or_cross_site_run_never_reads_remote_messages(self):
        for payload in (self.payload(use_llm=True), self.payload(use_llm="false"),
                        self.payload(sender_ids=["unknown"]), self.payload(anchor_date="bad")):
            status, raw = self.request("/api/run", payload)
            self.assertEqual(status, 400, raw)
        status, _ = self.request("/api/run", self.payload(), {"Origin": "https://untrusted.invalid"})
        self.assertEqual(status, 403)
        self.assertEqual(self.fake.calls, [])
        self.assertEqual(self.app.storage.reports(), [])


if __name__ == "__main__":
    unittest.main()
