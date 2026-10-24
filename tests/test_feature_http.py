from datetime import date
from http.server import ThreadingHTTPServer
import http.client
import json
from pathlib import Path
from threading import Event, Thread
import time
import unittest

from app.config import Settings
from app.feishu import FeishuError
from app.periods import timestamp
from app.server import Application, handler_for
from tests.helpers import temporary_directory


START = timestamp(date(2026, 10, 20))


def record(mid, text, sender="self", offset=0):
    return {"message_id": mid, "chat_id": "a", "sender_id": sender, "sender_name": sender,
            "text": text, "created_at": START + 3600 + offset}


class FakeClient:
    def __init__(self):
        self.fail = False
        self.gate = None

    def list_members(self, chat):
        return [{"member_id": "self", "name": "张工"}, {"member_id": "other", "name": "李工"}]

    def list_messages(self, chat, begin, end):
        if self.gate:
            self.gate.wait(2)
        if self.fail:
            raise FeishuError("读取测试群失败")
        return [record("new", "已完成新接口测试")]


class FeatureHttpTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.app = Application(Settings(data_dir=Path(self.temp.name)))
        self.app.storage.save_chats("feishu", [{"chat_id": "a", "name": "工作群"}])
        self.app.storage.save_messages("feishu", [record("plan", "明天计划开展接口测试"),
            record("finish", "已完成接口测试", offset=86400), record("other", "正在评审", "other")])
        self.fake = FakeClient()
        self.app.feishu = self.fake
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(self.app))
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.addCleanup(self.cleanup)

    def cleanup(self):
        if self.fake.gate:
            self.fake.gate.set()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        self.temp.cleanup()

    def request(self, path, body=None):
        conn = http.client.HTTPConnection("127.0.0.1", self.server.server_port, timeout=10)
        conn.request("GET" if body is None else "POST", path,
                     None if body is None else json.dumps(body), {"Content-Type": "application/json"})
        response = conn.getresponse()
        result = response.status, response.read()
        conn.close()
        return result

    def json(self, path, body=None, expected=200):
        status, data = self.request(path, body)
        self.assertEqual(status, expected, data)
        return json.loads(data)

    def report(self):
        return self.json("/api/reports", {"source": "feishu", "chat_ids": ["a"], "period": "monthly",
            "anchor_date": "2026-10-20", "audience": "personal", "sender_ids": ["self"]})["report"]

    def wait_job(self, job):
        for _ in range(100):
            result = self.json("/api/jobs/" + job["id"])["job"]
            if result["status"] != "running":
                return result
            time.sleep(.01)
        self.fail("后台任务没有结束")

    def test_member_refresh_and_remark_keep_personal_identity_independent(self):
        self.json("/api/members/refresh", {"source": "feishu", "chat_ids": ["a"]})
        self.json("/api/members/alias", {"source": "feishu", "sender_id": "other", "alias": "项目同事"})
        data = self.json("/api/members?source=feishu&chat_ids=a")["members"]
        self.assertEqual({m["sender_id"]: m["display_name"] for m in data}, {"self": "张工", "other": "项目同事"})
        self.assertEqual(self.app.storage.profile("feishu")["sender_id"], "")

    def test_evidence_revision_download_and_original_are_consistent(self):
        original = self.report()
        self.assertEqual(original["evidence_status"], "snapshot")
        self.app.storage.save_messages("feishu", [], replace_range=(["a"], START, START + 2 * 86400))
        old = self.json("/api/reports/" + original["id"])["report"]
        self.assertEqual(old["evidence"], original["evidence"])
        revised = self.json(f"/api/reports/{old['id']}/revisions", {
            "markdown": "# 人工核对\n\n- 人工补充工作记录。", "notes": "补全工作描述"})["report"]
        self.assertEqual(revised["revision"]["parent_report_id"], old["id"])
        self.assertEqual(revised["revision"]["version"], 2)
        self.assertIn("人工修订稿", revised["markdown"])
        self.assertEqual(self.app.storage.report(old["id"])["markdown"], old["markdown"])
        status, raw = self.request(f"/api/reports/{revised['id']}/download")
        self.assertEqual(status, 200)
        self.assertEqual(raw.decode("utf-8"), revised["markdown"])

    def test_manual_same_author_timeline_and_audit(self):
        group = self.json("/api/tasks", {"source": "feishu", "chat_ids": ["a"], "sender_id": "self",
            "title": "接口测试", "message_ids": ["plan", "finish"]})["group"]
        self.assertEqual(len(group["events"]), 2)
        report = self.report()
        self.assertTrue(report["task_timeline"])
        self.assertIn("事项进展时间线", report["markdown"])
        audit = self.json("/api/audit?source=feishu&chat_ids=a&period=monthly&anchor_date=2026-10-20&sender_ids=self")["audit"]
        self.assertEqual(audit["message_count"], 3)
        self.assertEqual(audit["selected_message_count"], 2)
        self.assertEqual(len(audit["days"]), 31)
        self.json("/api/tasks", {"source": "feishu", "chat_ids": ["a"], "sender_id": "self",
            "title": "不应跨人关联", "message_ids": ["plan", "other"]}, expected=400)
        self.json("/api/tasks/" + group["id"] + "/deactivate", {})
        self.assertEqual(self.app.storage.count("feishu"), 3)

    def test_backup_restore_keeps_safety_copy_and_rejects_arbitrary_path(self):
        backup = self.json("/api/backups", {})["backup"]
        self.app.storage.save_member_alias("feishu", "other", "临时改名")
        self.app.storage.save_messages("feishu", [record("later", "后续记录")])
        restored = self.json("/api/backups/restore", {"backup_id": backup["id"]})
        self.assertTrue(restored["safety_backup"])
        self.assertEqual(self.app.storage.count("feishu"), 3)
        self.assertFalse(next(m for m in self.app.storage.members("feishu") if m["sender_id"] == "other")["alias"])
        self.assertGreaterEqual(len(self.json("/api/backups")["backups"]), 2)
        self.json("/api/backups/restore", {"backup_id": "../work_summary"}, expected=400)

    def test_background_jobs_progress_and_duplicate_are_serialized(self):
        self.fake.gate = Event()
        job = self.json("/api/jobs", {"action": "run", "source": "feishu", "chat_ids": ["a"],
            "period": "daily", "anchor_date": "2026-10-20", "audience": "personal", "sender_ids": ["self"]})["job"]
        duplicate = self.json("/api/jobs", {"action": "sync", "source": "feishu", "chat_ids": ["a"],
            "start_date": "2026-10-20", "end_date": "2026-10-20"}, expected=409)
        self.assertEqual(duplicate["job"]["id"], job["id"])
        self.assertEqual(self.json("/api/jobs/current")["job"]["id"], job["id"])
        self.fake.gate.set()
        result = self.wait_job(job)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["result"]["report"]["message_count"], 1)
        self.assertIsNone(self.json("/api/jobs/current")["job"])

        # Sync-only tasks have no reporting stage to advance the final counter.
        synced = self.json("/api/jobs", {"action": "sync", "source": "feishu", "chat_ids": ["a"],
            "start_date": "2026-10-20", "end_date": "2026-10-20"})["job"]
        finished = self.wait_job(synced)
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual((finished["completed"], finished["total"]), (1, 1))

    def test_background_failure_never_generates_old_cache(self):
        self.fake.fail = True
        job = self.json("/api/jobs", {"action": "run", "source": "feishu", "chat_ids": ["a"],
            "period": "daily", "anchor_date": "2026-10-20"})["job"]
        result = self.wait_job(job)
        self.assertEqual(result["status"], "failed")
        self.assertEqual(self.app.storage.reports(), [])
        self.assertEqual(self.app.storage.count("feishu"), 3)


if __name__ == "__main__":
    unittest.main()
