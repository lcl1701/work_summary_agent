from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Event
import json
import time
import unittest
from unittest.mock import patch

from app.config import Settings
from app.periods import timestamp
from app.server import Application
from tests.helpers import temporary_directory


DAY = "2026-10-20"


class CompositionWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = temporary_directory()
        self.addCleanup(self.tmp.cleanup)
        self.app = Application(Settings(data_dir=Path(self.tmp.name)))
        self.messages = [{"message_id": "m1", "chat_id": "a", "sender_id": "me", "sender_name": "本人",
                          "created_at": timestamp(date(2026, 10, 20)), "text": "已完成接口测试"},
                         {"message_id": "m2", "chat_id": "a", "sender_id": "other", "sender_name": "同事",
                          "created_at": timestamp(date(2026, 10, 20)) + 1, "text": "等待环境验收"}]
        self.app.storage.save_messages("demo", self.messages, chats=[{"chat_id": "a", "name": "虚构群"}])

    def payload(self, **extra):
        return {"source": "demo", "period": "daily", "anchor_date": DAY,
                "chat_ids": ["a"], "audience": "personal", "sender_ids": ["me"], **extra}

    @staticmethod
    def config(**extra):
        return {"purpose": "update", "template_id": "classic", "framework_id": "bluf", **extra}

    def preview(self, **extra):
        return self.app.post("/api/composition/preview", self.payload(composition=self.config(), **extra))["preview"]

    def test_catalog_and_preview_do_not_write_reports_or_operation_successes(self):
        directory = self.app.get("/api/composition/catalog", {})
        self.assertIn("frameworks", directory)
        before = self.app.operations.list()["total"]
        preview = self.preview()
        self.assertTrue(preview["report"]["is_preview"])
        self.assertFalse(preview["report"]["reviewability"]["allowed"])
        self.assertEqual(self.app.storage.reports(), [])
        self.assertEqual(self.app.operations.list()["total"], before)

    def test_save_commits_exact_preview_after_cache_changes_and_is_idempotent(self):
        preview = self.preview()
        snapshot = deepcopy(preview["report"])
        newer = dict(self.messages[0], text="已完成缓存后来才出现的另一项工作")
        self.app.storage.save_messages("demo", [newer])
        body = {"preview_id": preview["id"], "markdown": "客户端伪造正文", "source": "feishu"}
        saved = self.app.post("/api/composition/save", body)["report"]
        self.assertEqual(saved["markdown"], snapshot["markdown"])
        self.assertEqual(saved["evidence"], snapshot["evidence"])
        self.assertEqual(saved["source"], "demo")
        self.assertNotIn("客户端伪造正文", saved["markdown"])
        self.assertNotIn("后来才出现", saved["markdown"])
        again = self.app.post("/api/composition/save", body)
        self.assertTrue(again["already_saved"])
        self.assertEqual(again["report"]["id"], saved["id"])
        self.assertEqual(len(self.app.storage.reports()), 1)
        self.assertEqual(self.app.operations.list()["total"], 1)
        receipt = self.app.operations.list()["operations"][0]
        self.assertEqual(receipt["framework_id"], "bluf")
        self.assertEqual(receipt["counts"]["message_count"], 1)

    def test_expired_or_restarted_preview_is_explicit_and_cannot_save(self):
        preview = self.preview()
        self.app.previews[preview["id"]]["expires"] = datetime.now(timezone.utc) - timedelta(seconds=1)
        with self.assertRaisesRegex(ValueError, "过期"):
            self.app.post("/api/composition/save", {"preview_id": preview["id"]})
        other = Application(self.app.settings)
        with self.assertRaisesRegex(ValueError, "重启"):
            other.post("/api/composition/save", {"preview_id": preview["id"]})
        self.assertEqual(self.app.storage.reports(), [])

    def test_preferences_are_source_separate_and_never_persist_business_materials(self):
        result = self.app.post("/api/composition/preferences", {"source": "demo", "composition":
            self.config(fields={"key_message": "private-project-summary", "recipient": "private-recipient"})})
        serialized = json.dumps(result, ensure_ascii=False)
        self.assertNotIn("private-", serialized)
        self.assertNotIn("fields", result["composition"])
        saved = self.app.get("/api/composition/preferences", {"source": ["demo"]})
        self.assertEqual(saved["composition"]["framework_id"], "bluf")
        self.assertIsNone(self.app.get("/api/composition/preferences", {"source": ["feishu"]})["composition"])
        self.assertNotIn("private-", json.dumps(self.app.operations.list()))

    def test_invalid_new_configuration_never_starts_remote_sync(self):
        self.app.storage.save_messages("feishu", self.messages, chats=[{"chat_id": "a", "name": "隔离群"}])
        fake = unittest.mock.Mock()
        self.app.feishu = fake
        cases = [self.config(unknown_field="bad"), self.config(template_id="unknown"),
                 self.config(fields={"key_message": {"not": "text"}}),
                 {"schema_version": 1, "config": self.config(), "layout": {"header": "forged"}}]
        for path in ("/api/run", "/api/jobs"):
            for config in cases:
                with self.subTest(path=path, config=config):
                    with self.assertRaises(ValueError):
                        self.app.post(path, self.payload(source="feishu", action="run", composition=config))
        fake.list_messages.assert_not_called()
        self.assertEqual(self.app.storage.reports(), [])

    def test_ai_new_framework_rejected_before_external_model_or_sync(self):
        self.app.storage.save_messages("feishu", self.messages, chats=[{"chat_id": "a", "name": "隔离群"}])
        fake = unittest.mock.Mock()
        self.app.feishu = fake
        with self.assertRaises(ValueError):
            self.app.post("/api/jobs", self.payload(source="feishu", action="run", use_llm=True,
                                                   composition=self.config()))
        fake.list_messages.assert_not_called()

    def test_save_blocked_when_receipt_cannot_start_keeps_preview_retryable(self):
        import sqlite3
        preview = self.preview()
        with patch.object(self.app.operations, "begin", side_effect=sqlite3.OperationalError("private-disk")):
            with self.assertRaisesRegex(ValueError, "未开始"):
                self.app.post("/api/composition/save", {"preview_id": preview["id"]})
        self.assertEqual(self.app.storage.reports(), [])
        self.app.post("/api/composition/save", {"preview_id": preview["id"]})
        self.assertEqual(len(self.app.storage.reports()), 1)

    def test_finish_receipt_failure_does_not_duplicate_committed_preview(self):
        import sqlite3
        preview = self.preview()
        with patch.object(self.app.operations, "finish", side_effect=sqlite3.OperationalError("private-disk")):
            result = self.app.post("/api/composition/save", {"preview_id": preview["id"]})
        self.assertIn("operation_warning", result)
        again = self.app.post("/api/composition/save", {"preview_id": preview["id"]})
        self.assertTrue(again["already_saved"])
        self.assertEqual(len(self.app.storage.reports()), 1)

    def test_background_run_freezes_configuration_and_returns_unsaved_preview(self):
        entered, release = Event(), Event()
        sync = self.app.sync
        def blocked(*args, **kwargs):
            entered.set()
            release.wait(5)
            return sync(*args, **kwargs)
        body = self.payload(action="run", preview_only=True,
                            composition=self.config(fields={"key_message": "已确认的原始说明"}))
        with patch.object(self.app, "sync", side_effect=blocked):
            job = self.app.post("/api/jobs", body)["job"]
            self.assertTrue(entered.wait(5))
            body["composition"]["fields"]["key_message"] = "任务开始后被改掉的说明"
            body["anchor_date"] = "2026-10-21"
            body["sender_ids"][:] = ["other"]
            release.set()
            deadline = time.monotonic() + 5
            while time.monotonic() < deadline:
                result = self.app.jobs.get(job["id"])
                if result["status"] != "running":
                    break
                time.sleep(0.01)
        self.assertEqual(result["status"], "succeeded", result)
        report = result["result"]["preview"]["report"]
        self.assertEqual(report["start_date"], DAY)
        self.assertEqual(report["sender_ids"], ["me"])
        self.assertEqual(report["composition"]["config"]["fields"]["key_message"], "已确认的原始说明")
        self.assertEqual(self.app.storage.reports(), [])
        self.assertEqual(self.app.operations.list()["operations"][0]["action"], "sync_messages")

    def test_preview_count_bounded_and_bad_preview_flags_rejected(self):
        for _ in range(22):
            self.preview()
        self.assertLessEqual(len(self.app.previews), 20)
        with self.assertRaisesRegex(ValueError, "布尔"):
            self.app.post("/api/jobs", self.payload(action="run", preview_only="false", composition=self.config()))

    def test_recomposition_uses_saved_evidence_and_never_overwrites_parent(self):
        original = self.app.post("/api/reports", self.payload())["report"]
        old_markdown = original["markdown"]
        self.app.storage.save_messages("demo", [dict(self.messages[0], text="后来完全不同的内容")])
        preview = self.app.post(f"/api/reports/{original['id']}/composition/preview", {
            "composition": self.config(template_id="ppp")})["preview"]
        saved = self.app.post("/api/composition/save", {"preview_id": preview["id"]})["report"]
        self.assertEqual(saved["evidence"], original["evidence"])
        self.assertEqual(saved["revision"]["parent_report_id"], original["id"])
        self.assertEqual(self.app.storage.report(original["id"])["markdown"], old_markdown)
        self.assertNotIn("后来完全不同", saved["markdown"])


if __name__ == "__main__":
    unittest.main()
