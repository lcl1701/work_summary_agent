from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app.config import Settings
from app.periods import timestamp
from app.server import Application
from app.storage import Storage
from tests.helpers import temporary_directory as TemporaryDirectory


def message(mid, sender, created_at, name=None, chat="a"):
    return {"message_id": mid, "chat_id": chat, "sender_id": sender,
            "sender_name": name or sender, "text": "已完成测试", "created_at": created_at}


class IdentityAndCoverageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.app = Application(self.settings)
        self.store = self.app.storage

    def test_profile_persists_per_source_and_original_names_are_preserved(self):
        self.store.save_messages("feishu", [message("f", "same", 10)])
        self.store.save_messages("demo", [message("d", "same", 10, "示例成员")])
        self.assertEqual(self.app.get("/api/profile", {"source": ["feishu"]}),
                         {"profile": {"sender_id": "", "nickname": ""}})
        saved = self.app.post("/api/profile", {"source": "feishu", "sender_id": "same", "nickname": "  示例昵称  "})
        self.assertEqual(saved["profile"], {"sender_id": "same", "nickname": "示例昵称"})
        restarted = Application(self.settings)
        self.assertEqual(restarted.get("/api/profile", {"source": ["feishu"]}), saved)
        self.assertEqual(restarted.get("/api/profile", {"source": ["demo"]})["profile"]["nickname"], "")
        live = self.store.messages("feishu")[0]
        self.assertEqual(live["sender_name"], "same")
        self.assertEqual(live["display_name"], "示例昵称")
        self.assertEqual(self.store.senders("feishu")[0]["display_name"], "示例昵称")
        self.assertEqual(self.store.messages("demo")[0]["display_name"], "示例成员")

    def test_profile_rejects_unknown_unsynced_and_other_source_senders(self):
        self.store.save_messages("demo", [message("one", "only-demo", 10)])
        self.store.save_messages("feishu", [message("unknown", "unknown", 10)])
        for sender_id in ("unknown", "missing", "only-demo", None, [], ""):
            with self.subTest(sender_id=sender_id), self.assertRaisesRegex(ValueError, "已同步"):
                self.app.post("/api/profile", {"source": "feishu", "sender_id": sender_id, "nickname": "本人"})

    def test_sender_list_prefers_known_name_over_fallback_identifier(self):
        self.store.save_messages("feishu", [message("first", "ou_self", 10, "Alice"),
                                             message("second", "ou_self", 20)])
        sender = self.store.senders("feishu")[0]
        self.assertEqual(sender["sender_name"], "Alice")
        self.assertEqual(sender["display_name"], "Alice")

    def test_profile_validation_and_explicit_clear(self):
        self.store.save_messages("feishu", [message("f", "self", 10)])
        for nickname in ("x" * 31, "示例\n昵称", "示例\x00昵称", "\u202e示例昵称", None, []):
            with self.subTest(nickname=nickname), self.assertRaises(ValueError):
                self.app.post("/api/profile", {"source": "feishu", "sender_id": "self", "nickname": nickname})
        self.app.post("/api/profile", {"source": "feishu", "sender_id": "self", "nickname": "本人"})
        cleared = self.app.post("/api/profile", {"source": "feishu", "sender_id": "stale", "nickname": "   "})
        self.assertEqual(cleared["profile"], {"sender_id": "", "nickname": ""})
        cleared_name = self.store.messages("feishu")[0]["display_name"]
        self.assertRegex(cleared_name, r"^未命名成员·[a-f0-9]{6}$")
        self.assertEqual(self.store.senders("feishu")[0]["display_name"], cleared_name)
        with self.assertRaises(ValueError):
            self.app.post("/api/profile", {"source": "invalid", "sender_id": "self", "nickname": "本人"})

    def test_coverage_does_not_claim_future_and_survives_elapsed_seconds(self):
        with patch("app.storage.time.time", return_value=150):
            self.store.save_messages("feishu", [], coverage=(["a"], 100, 200))
        result = self.store.coverage("feishu", ["a"], 100, 200, now=175)
        self.assertTrue(result["complete"])
        self.assertEqual(result["checked_through_ts"], 150)
        self.assertTrue(result["period_has_future"])
        self.assertFalse(result["period_not_started"])
        self.assertEqual(result["last_synced_at"], 150)
        future = self.store.coverage("feishu", ["a"], 180, 200, now=175)
        self.assertTrue(future["period_not_started"])
        self.assertFalse(future["complete"])
        self.assertIsNone(future["checked_through_ts"])
        # Once that day has ended, the pre-end query is not full-day coverage.
        past = self.store.coverage("feishu", ["a"], 100, 200, now=250)
        self.assertFalse(past["complete"])
        self.assertEqual(past["checked_through_ts"], 150)

    def test_coverage_uses_common_actual_cursor_and_detects_real_gaps(self):
        with patch("app.storage.time.time", return_value=150):
            self.store.save_messages("feishu", [], coverage=(["a"], 100, 200))
        with patch("app.storage.time.time", return_value=160):
            self.store.save_messages("feishu", [], coverage=(["b"], 100, 200))
        result = self.store.coverage("feishu", ["a", "b"], 100, 200, now=175)
        self.assertTrue(result["complete"])
        self.assertEqual(result["checked_through_ts"], 150)
        with patch("app.storage.time.time", return_value=120):
            self.store.save_messages("feishu", [], coverage=(["gap"], 100, 150))
        with patch("app.storage.time.time", return_value=170):
            self.store.save_messages("feishu", [], coverage=(["gap"], 150, 200))
        gap = self.store.coverage("feishu", ["gap"], 100, 200, now=175)
        self.assertFalse(gap["complete"])
        self.assertEqual(gap["checked_through_ts"], 120)
        missing = self.store.coverage("feishu", ["a", "never"], 100, 200, now=175)
        self.assertEqual(missing["missing_chat_ids"], ["never"])
        self.assertIsNone(missing["checked_through_ts"])

    def test_coverage_counts_only_selected_period_source_and_shanghai_days(self):
        start = timestamp(date(2026, 10, 19))
        end = start + 2 * 86400
        self.store.save_messages("feishu", [message("one", "self", start),
            message("two", "other", start + 86400), message("excluded-end", "other", end),
            message("other-chat", "self", start, chat="b")])
        self.store.save_messages("demo", [message("demo", "self", start)])
        with patch("app.storage.time.time", return_value=end + 1):
            self.store.save_messages("feishu", [], coverage=(["a"], start, end))
        result = self.store.coverage("feishu", ["a"], start, end, now=end + 5)
        self.assertTrue(result["complete"])
        self.assertEqual(result["observed_message_count"], 2)
        self.assertEqual(result["active_day_count"], 2)
        self.assertEqual((result["observed_first_at"], result["observed_last_at"]), (start, start + 86400))
        self.assertEqual((result["period_start_ts"], result["period_end_ts"]), (start, end))
        self.assertFalse(result["period_has_future"])
        empty = self.store.coverage("feishu", ["empty"], start, end, now=end + 5)
        self.assertEqual(empty["observed_message_count"], 0)
        self.assertEqual(empty["active_day_count"], 0)
        self.assertIsNone(empty["observed_first_at"])

    def test_coverage_api_requires_groups_and_valid_period(self):
        result = self.app.get("/api/coverage", {"source": ["feishu"], "chat_ids": ["a"],
                                                "period": ["daily"], "anchor_date": ["2026-10-19"]})
        self.assertIn("checked_through_ts", result["coverage"])
        self.assertEqual(result["coverage"]["chat_count"], 1)
        for query in ({"chat_ids": []}, {"chat_ids": ["a"] * 21},
                      {"chat_ids": ["a"], "period": ["yearly"], "anchor_date": ["2026-10-19"]},
                      {"chat_ids": ["a"], "period": ["daily"], "anchor_date": ["bad"]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                self.app.get("/api/coverage", query)

    def test_existing_database_migrates_report_snapshot_without_losing_rows(self):
        path = Path(self.tmp.name) / "old.sqlite3"
        with closing(sqlite3.connect(path)) as db:
            db.execute("""CREATE TABLE reports (id TEXT PRIMARY KEY,title TEXT,period TEXT,start_date TEXT,
                end_date TEXT,source TEXT,mode TEXT,audience TEXT,chat_ids TEXT,sender_ids TEXT,
                message_count INTEGER,markdown TEXT,created_at TEXT)""")
            db.execute("INSERT INTO reports VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (
                "old", "旧报告", "daily", "2026-10-19", "2026-10-19", "feishu", "extractive",
                "personal", '["a"]', '["self"]', 1, "旧正文", "2026-10-19"))
            db.commit()
        migrated = Storage(path)
        self.assertEqual(migrated.report("old")["markdown"], "旧正文")
        self.assertEqual(migrated.report("old")["coverage"], {})
        self.assertEqual(migrated.reports()[0]["coverage"], {})


if __name__ == "__main__":
    unittest.main()
