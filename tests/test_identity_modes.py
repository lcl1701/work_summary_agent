from contextlib import closing
from datetime import date
from pathlib import Path
import sqlite3
import unittest

from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report
from app.storage import Storage
from tests.helpers import temporary_directory


SELF, OTHER = "ou_self", "ou_other"
DAY = date(2026, 10, 20)


def message(sender=SELF, name=None):
    return {"message_id": sender, "chat_id": "a", "sender_id": sender, "sender_name": name or sender,
            "text": "已完成接口测试", "created_at": timestamp(DAY) + 3600}


class IdentityModeTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.settings = Settings(data_dir=Path(self.temporary.name))
        self.store = Storage(Path(self.temporary.name) / "identity.sqlite3")
        self.store.save_messages("feishu", [message(), message(OTHER)])
        self.store.save_member_names("feishu", "a", [{"sender_id": SELF, "name": "示例成员甲"},
                                                    {"sender_id": OTHER, "name": "示例成员乙"}])

    def report(self):
        return build_report(self.store, self.settings, source="feishu", period="daily", anchor_date=str(DAY),
                            audience="personal", chat_ids=["a"], sender_ids=[SELF], use_llm=False)

    def test_upgrade_defaults_to_official_keeps_legacy_profile_preferences_and_reports(self):
        self.store.save_profile("feishu", SELF, "示例昵称")
        self.store.save_preferences("feishu", ["a"], "weekly", "personal", SELF)
        old_report = self.report()
        profile = self.store.profile("feishu")
        preferences = self.store.preferences("feishu")
        with self.store.connect() as db:
            db.execute("DROP TABLE identity_settings")
            db.execute("PRAGMA user_version=2")
        upgraded = Storage(self.store.path)
        self.assertEqual(upgraded.identity("feishu"), {"sender_id": SELF, "nickname": "示例昵称", "mode": "official",
            "official_name": "示例成员甲", "display_name": "示例成员甲", "name_source": "official", "notice": ""})
        self.assertEqual(upgraded.profile("feishu"), profile)
        self.assertEqual(upgraded.preferences("feishu"), preferences)
        self.assertEqual(upgraded.report(old_report["id"]), old_report)
        original_names = {row["sender_id"]: row["sender_name"] for row in upgraded.messages("feishu")}
        self.assertEqual(original_names[SELF], SELF)
        backups = upgraded.backups()
        self.assertEqual(len(backups), 1)
        backup = upgraded.path.parent / "backups" / (backups[0]["id"] + ".sqlite3")
        with closing(sqlite3.connect(backup)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT 1 FROM sqlite_master WHERE name='identity_settings'").fetchone())
            self.assertEqual(db.execute("SELECT nickname FROM profiles WHERE source='feishu'").fetchone()[0], "示例昵称")
        with upgraded.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_mode_switch_preserves_nickname_identity_and_preferences_across_restart(self):
        self.store.save_identity("feishu", SELF, "nickname", "示例昵称")
        self.store.save_preferences("feishu", ["a"], "daily", "personal", SELF)
        expected_preferences = self.store.preferences("feishu")
        selected = self.store.save_identity("feishu", SELF, "official")
        self.assertEqual((selected["nickname"], selected["display_name"], selected["name_source"]), ("示例昵称", "示例成员甲", "official"))
        self.assertEqual(self.store.profile("feishu"), {"sender_id": SELF, "nickname": "示例昵称"})
        self.assertEqual(self.store.preferences("feishu"), expected_preferences)
        reopened = Storage(self.store.path)
        self.assertEqual(reopened.identity("feishu", SELF), selected)
        nick = reopened.save_identity("feishu", SELF, "nickname")
        self.assertEqual((nick["display_name"], nick["name_source"]), ("示例昵称", "nickname"))
        self.assertEqual(Storage(self.store.path).identity("feishu", SELF)["mode"], "nickname")

    def test_modes_and_nickname_text_are_isolated_by_sender_and_source(self):
        self.store.save_messages("demo", [message()])
        self.store.save_identity("feishu", SELF, "nickname", "示例昵称")
        other = self.store.identity("feishu", OTHER)
        self.assertEqual(other["nickname"], "")
        self.assertEqual(other["display_name"], "示例成员乙")
        self.assertEqual(self.store.identity("demo", SELF)["nickname"], "")
        self.store.save_identity("feishu", OTHER, "nickname", "同事昵称")
        self.assertEqual(self.store.identity("feishu", SELF)["nickname"], "示例昵称")
        self.store.save_identity("feishu", SELF, "official")
        self.assertEqual(self.store.identity("feishu")["sender_id"], SELF)
        self.assertEqual(self.store.identity("feishu", OTHER)["nickname"], "同事昵称")

    def test_official_mode_is_explicit_and_other_member_alias_still_has_priority(self):
        self.store.save_member_alias("feishu", SELF, "本人备注")
        self.store.save_member_alias("feishu", OTHER, "同事备注")
        self.assertEqual(self.store.resolve_display_name("feishu", OTHER, OTHER), "同事备注")
        saved = self.store.save_identity("feishu", SELF, "official", "保存但不启用")
        self.assertEqual(saved["nickname"], "保存但不启用")
        self.assertEqual(saved["display_name"], "示例成员甲")
        self.assertEqual(self.store.resolve_display_name("feishu", OTHER, OTHER), "同事备注")
        self.assertEqual(self.store.members("feishu", ["a"])[0]["name_source"], "alias")

    def test_fallback_sources_are_truthful_when_official_name_is_not_cached(self):
        self.store.save_messages("feishu", [message("ou_original", "原消息姓名"), message("ou_unnamed")])
        original = self.store.save_identity("feishu", "ou_original", "official")
        self.assertEqual((original["display_name"], original["official_name"], original["name_source"]), ("原消息姓名", "", "original"))
        self.assertIn("尚未读取飞书官方名称", original["notice"])
        self.store.save_member_alias("feishu", "ou_unnamed", "成员备注")
        alias = self.store.save_identity("feishu", "ou_unnamed", "official")
        self.assertEqual(alias["name_source"], "alias")
        self.store.save_member_alias("feishu", "ou_unnamed", "")
        unnamed = self.store.identity("feishu", "ou_unnamed")
        self.assertEqual(unnamed["name_source"], "unnamed")
        self.assertRegex(unnamed["display_name"], r"^未命名成员·[a-f0-9]{6}$")

    def test_identity_validation_is_atomic_and_unknown_sender_cannot_be_selected(self):
        baseline = self.store.save_identity("feishu", SELF, "official", "旧昵称")
        preferences = self.store.preferences("feishu")
        cases = [("feishu", SELF, "other", None), ("feishu", SELF, [], None),
            ("feishu", SELF, "nickname", ""), ("feishu", SELF, "nickname", "x" * 31),
            ("feishu", SELF, "official", "含\n换行"), ("feishu", SELF, "official", []),
            ("feishu", "unknown", "official", None), ("demo", SELF, "official", None),
            ("feishu", None, "official", None), ("invalid", SELF, "official", None)]
        for args in cases:
            with self.subTest(args=args), self.assertRaises(ValueError):
                self.store.save_identity(*args)
        self.assertEqual(self.store.identity("feishu", SELF), baseline)
        self.assertEqual(self.store.preferences("feishu"), preferences)
        self.assertEqual(self.store.identity("demo")["sender_id"], "")

    def test_legacy_profile_explicit_save_still_enables_nickname_and_clear_resets(self):
        self.assertEqual(self.store.save_profile("feishu", SELF, "示例昵称"), {"sender_id": SELF, "nickname": "示例昵称"})
        self.assertEqual(self.store.identity("feishu", SELF)["mode"], "nickname")
        self.assertEqual(self.store.resolve_display_name("feishu", SELF, SELF), "示例昵称")
        self.assertEqual(self.store.save_profile("feishu", "", ""), {"sender_id": "", "nickname": ""})
        self.assertEqual(self.store.identity("feishu", SELF)["mode"], "official")
        self.assertEqual(self.store.resolve_display_name("feishu", SELF, SELF), "示例成员甲")

    def test_messages_senders_and_new_reports_follow_modes_without_mutating_history(self):
        self.store.save_identity("feishu", SELF, "nickname", "示例昵称")
        old = self.report()
        self.store.save_identity("feishu", SELF, "official")
        messages = {row["sender_id"]: row for row in self.store.messages("feishu")}
        senders = {row["sender_id"]: row for row in self.store.senders("feishu")}
        self.assertEqual((messages[SELF]["display_name"], senders[SELF]["display_name"]), ("示例成员甲", "示例成员甲"))
        self.assertEqual(messages[SELF]["name_source"], "official")
        fresh = self.report()
        self.assertIn("汇报人：示例成员甲", fresh["markdown"])
        self.assertEqual(fresh["evidence"][0]["sender_name"], "示例成员甲")
        self.assertEqual(self.store.report(old["id"]), old)
        self.assertEqual(self.store.report(old["id"])["evidence"][0]["sender_name"], "示例昵称")


if __name__ == "__main__":
    unittest.main()
