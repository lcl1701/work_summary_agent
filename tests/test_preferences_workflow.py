from datetime import date
from pathlib import Path
import unittest
from unittest.mock import Mock, patch

from app.config import Settings
from app.feishu import FeishuError
from app.periods import timestamp
from app.reporting import ModelError
from app.server import Application
from tests.helpers import temporary_directory as TemporaryDirectory


DAY = "2026-10-19"
BEGIN = timestamp(date(2026, 10, 19))


def message(mid="new", sender="self", chat="a", text="已完成本次同步的新工作"):
    return {"message_id": mid, "chat_id": chat, "sender_id": sender,
            "sender_name": sender, "text": text, "created_at": BEGIN + 9 * 3600}


class PreferencesWorkflowTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.app = Application(self.settings)
        self.fake = Mock()
        self.app.feishu = self.fake

    def run_payload(self, **extra):
        payload = {"source": "feishu", "chat_ids": ["a"], "period": "daily", "anchor_date": DAY,
                   "audience": "project", "sender_ids": [], "use_llm": False}
        payload.update(extra)
        return payload

    def save_preferences(self, **extra):
        payload = {"source": "feishu", "chat_ids": ["a"], "period": "weekly",
                   "audience": "personal", "sender_id": "self"}
        payload.update(extra)
        return self.app.post("/api/preferences", payload)

    def test_preferences_defaults_persist_and_are_source_isolated(self):
        initial = self.app.get("/api/preferences", {"source": ["feishu"]})
        self.assertEqual(initial, {"preferences": {"chat_ids": [], "period": "daily",
            "audience": "project", "sender_id": ""}, "last_source": None, "has_saved": False})
        self.assertIsNone(self.app.get("/api/status", {})["preferred_source"])
        self.app.storage.save_messages("feishu", [message()])
        saved = self.save_preferences(chat_ids=["currently-invisible", "a", "a"])
        self.assertEqual(saved["preferences"]["chat_ids"], ["currently-invisible", "a"])
        self.assertTrue(saved["has_saved"])
        restarted = Application(self.settings)
        self.assertEqual(restarted.get("/api/preferences", {"source": ["feishu"]}), saved)
        self.assertEqual(restarted.get("/api/status", {})["preferred_source"], "feishu")
        demo = restarted.get("/api/preferences", {"source": ["demo"]})
        self.assertFalse(demo["has_saved"])
        self.assertEqual(demo["preferences"]["chat_ids"], [])
        self.assertEqual(demo["last_source"], "feishu")
        restarted.post("/api/preferences", {"source": "demo", "chat_ids": [],
                                            "period": "monthly", "audience": "project", "sender_id": ""})
        self.assertEqual(restarted.storage.last_source(), "demo")
        self.assertEqual(restarted.storage.preferences("feishu"), saved["preferences"])
        self.fake.list_messages.assert_not_called()

    def test_explicit_empty_selection_is_saved_without_dates_or_model_preferences(self):
        saved = self.save_preferences(chat_ids=[], sender_id="", audience="project",
                                      anchor_date="2020-01-01", use_llm=True, credential="ignored")
        self.assertTrue(saved["has_saved"])
        self.assertEqual(saved["preferences"], {"chat_ids": [], "period": "weekly",
                                                "audience": "project", "sender_id": ""})
        with self.app.storage.connect() as db:
            self.assertEqual([r["key"] for r in db.execute("SELECT key FROM local_state")], ["last_source"])
            self.assertEqual(set(db.execute("SELECT * FROM preferences").fetchone().keys()),
                             {"source", "chat_ids", "period", "audience", "sender_id"})

    def test_preferences_validation_does_not_replace_previous_saved_choices(self):
        self.app.storage.save_messages("feishu", [message()])
        self.app.storage.save_messages("demo", [message("demo", sender="only-demo")])
        saved = self.save_preferences()
        invalids = ({"source": []}, {"source": "bad"}, {"chat_ids": "a"}, {"chat_ids": [""]},
            {"chat_ids": [" "]}, {"chat_ids": ["a"] * 21}, {"chat_ids": [1]},
            {"period": "yearly"}, {"period": []}, {"audience": "manager"}, {"audience": []},
            {"sender_id": "unknown"}, {"sender_id": "missing"}, {"sender_id": "only-demo"},
            {"sender_id": None}, {"sender_id": []})
        for invalid in invalids:
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.save_preferences(**invalid)
            self.assertEqual(self.app.get("/api/preferences", {"source": ["feishu"]}), saved)

    def test_run_syncs_new_data_before_generating_instead_of_using_old_cache(self):
        self.app.storage.save_messages("feishu", [message("old", text="旧缓存专有工作")])
        self.fake.list_messages.return_value = [message("fresh")]
        result = self.app.post("/api/run", self.run_payload())
        self.assertEqual(result["synced_count"], 1)
        self.assertEqual(result["total_count"], 1)
        self.assertEqual(result["report"]["message_count"], 1)
        self.assertNotIn("旧缓存专有工作", result["report"]["markdown"])
        self.assertIn("本次同步的新工作", result["report"]["markdown"])
        self.fake.list_messages.assert_called_once_with("a", BEGIN, BEGIN + 86400)
        self.assertEqual(len(self.app.storage.reports()), 1)

    def test_run_multi_chat_failure_keeps_old_cache_and_never_generates(self):
        self.app.storage.save_messages("feishu", [message("old", text="原缓存")])
        self.fake.list_messages.side_effect = [[message("new")], FeishuError("第二群无权限")]
        with patch("app.server.build_report") as build:
            with self.assertRaises(FeishuError):
                self.app.post("/api/run", self.run_payload(chat_ids=["a", "b"]))
            build.assert_not_called()
        self.assertEqual([m["message_id"] for m in self.app.storage.messages("feishu")], ["old"])
        self.assertEqual(self.app.storage.reports(), [])

    def test_invalid_run_parameters_fail_before_any_remote_calls(self):
        self.app.storage.save_messages("feishu", [message()])
        self.app.storage.save_messages("demo", [message("other-source", sender="demo-only")])
        invalids = ({"source": "bad"}, {"chat_ids": []}, {"chat_ids": [""]},
            {"chat_ids": [" "]}, {"chat_ids": ["a"] * 21}, {"period": "yearly"},
            {"anchor_date": "bad"}, {"anchor_date": "2026-02-30"}, {"anchor_date": "9999-12-31"},
            {"use_llm": "false"}, {"use_llm": 1}, {"use_llm": True}, {"audience": "manager"},
            {"sender_ids": "self"}, {"audience": "personal", "sender_ids": []},
            {"audience": "personal", "sender_ids": ["unknown"]},
            {"audience": "personal", "sender_ids": ["missing"]},
            {"audience": "personal", "sender_ids": ["demo-only"]})
        for invalid in invalids:
            with self.subTest(invalid=invalid), self.assertRaises((ValueError, OverflowError)):
                self.app.post("/api/run", self.run_payload(**invalid))
        self.fake.list_messages.assert_not_called()
        self.assertEqual(self.app.storage.reports(), [])

    def test_personal_run_validates_existing_identity_and_filters_fresh_other_authors(self):
        self.app.storage.save_messages("feishu", [message("known", chat="previous-group")])
        self.fake.list_messages.return_value = [message("self-new"), message("other", sender="colleague", text="同事独有工作")]
        result = self.app.post("/api/run", self.run_payload(audience="personal", sender_ids=["self"]))
        self.assertEqual(result["synced_count"], 2)
        self.assertEqual(result["report"]["message_count"], 1)
        self.assertNotIn("同事独有工作", result["report"]["markdown"])
        self.assertEqual(result["report"]["coverage"]["observed_message_count"], 2)

    def test_empty_successful_sync_does_not_generate_using_stale_messages(self):
        self.app.storage.save_messages("feishu", [message("old")])
        self.fake.list_messages.return_value = []
        with self.assertRaisesRegex(ValueError, "消息同步成功，但报告未完成"):
            self.app.post("/api/run", self.run_payload())
        self.assertEqual(self.app.storage.count("feishu"), 0)
        self.assertEqual(self.app.storage.reports(), [])

    def test_model_failure_retains_successful_sync_but_saves_no_report_or_fallback(self):
        self.app.settings = Settings(data_dir=Path(self.tmp.name), llm_api_key="fake-test-key", llm_model="test-model")
        self.fake.list_messages.return_value = [message("new")]
        with patch("app.reporting.generate_with_model", side_effect=ModelError("模拟模型失败")) as generate:
            with self.assertRaisesRegex(ModelError, "消息同步成功，但报告未完成"):
                self.app.post("/api/run", self.run_payload(use_llm=True))
            generate.assert_called_once()
        self.assertEqual(self.app.storage.count("feishu"), 1)
        self.assertEqual(self.app.storage.reports(), [])

    def test_demo_run_marks_coverage_preserves_fictional_label_and_does_not_seed_or_use_remote(self):
        self.app.storage.save_messages("demo", [message("demo", text="已完成虚构示例任务")])
        result = self.app.post("/api/run", self.run_payload(source="demo"))
        self.assertEqual(result["synced_count"], 1)
        self.assertEqual(result["total_count"], 1)
        self.assertTrue(result["report"]["coverage"]["complete"])
        self.assertIn("示例数据（模拟，非真实工作成果）", result["report"]["markdown"])
        self.fake.list_messages.assert_not_called()


if __name__ == "__main__":
    unittest.main()
