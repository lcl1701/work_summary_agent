from contextlib import closing
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import Mock, patch

from app.feishu import FeishuError
from app.periods import timestamp
from app.storage import Storage
from app.workspace_features import refresh_members
from tests.helpers import temporary_directory


START = timestamp(date(2026, 10, 19))


def message(mid="one", sender="ou_self", chat="a", moment=START + 3600, name=None):
    return {"message_id": mid, "chat_id": chat, "sender_id": sender, "sender_name": name or sender,
            "text": "已完成验证任务", "created_at": moment}


class WorkspaceFeaturesTests(unittest.TestCase):
    def setUp(self):
        self.temporary = temporary_directory()
        self.addCleanup(self.temporary.cleanup)
        self.store = Storage(Path(self.temporary.name) / "test.sqlite3")

    def test_name_precedence_persists_and_does_not_rewrite_original_names(self):
        self.store.save_messages("feishu", [message(name="原消息名称")])
        self.assertEqual(self.store.resolve_display_name("feishu", "ou_self", "原消息名称"), "原消息名称")
        self.store.save_member_names("feishu", "a", [{"sender_id": "ou_self", "name": "官方名称"}])
        self.assertEqual(self.store.resolve_display_name("feishu", "ou_self", "原消息名称"), "官方名称")
        self.store.save_member_alias("feishu", "ou_self", "  本地备注  ")
        self.assertEqual(self.store.resolve_display_name("feishu", "ou_self", "原消息名称"), "本地备注")
        self.store.save_profile("feishu", "ou_self", "本人昵称")
        restarted = Storage(self.store.path)
        self.assertEqual(restarted.resolve_display_name("feishu", "ou_self", "原消息名称"), "本人昵称")
        self.assertEqual(restarted.messages("feishu")[0]["sender_name"], "原消息名称")
        restarted.save_profile("feishu", "", "")
        self.assertEqual(restarted.resolve_display_name("feishu", "ou_self", "原消息名称"), "本地备注")
        restarted.save_member_alias("feishu", "ou_self", "")
        self.assertEqual(restarted.resolve_display_name("feishu", "ou_self", "原消息名称"), "官方名称")

    def test_alias_source_isolation_validation_and_stable_unknown_names(self):
        self.store.save_messages("feishu", [message()])
        self.store.save_messages("demo", [message()])
        self.store.save_member_alias("feishu", "ou_self", "飞书成员备注")
        self.assertNotEqual(self.store.resolve_display_name("demo", "ou_self", "ou_self"), "飞书成员备注")
        first = self.store.resolve_display_name("feishu", "ou_new", "ou_new")
        second = self.store.resolve_display_name("feishu", "ou_other", "ou_other")
        self.assertRegex(first, r"^未命名成员·[0-9a-f]{6}$")
        self.assertNotEqual(first, second)
        self.assertEqual(Storage(self.store.path).resolve_display_name("feishu", "ou_new", "ou_new"), first)
        for alias in ("x" * 31, "有\n换行", "\u202e方向控制", None, []):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                self.store.save_member_alias("feishu", "ou_self", alias)
        for sender in ("unknown", "not-synced", None):
            with self.subTest(sender=sender), self.assertRaises(ValueError):
                self.store.save_member_alias("feishu", sender, "备注")
        self.assertEqual(self.store.members("feishu")[0]["alias"], "飞书成员备注")

    def test_members_filter_selected_groups_and_cache_only_members(self):
        self.store.save_messages("feishu", [message(), message("two", chat="b", sender="ou_other")])
        self.store.save_member_names("feishu", "a", [{"member_id": "ou_cached", "name": "未发言成员"}])
        members = {member["sender_id"]: member for member in self.store.members("feishu", ["a"])}
        self.assertEqual(set(members), {"ou_self", "ou_cached"})
        self.assertEqual(members["ou_cached"]["message_count"], 0)
        self.assertEqual(members["ou_self"]["message_count"], 1)
        self.store.save_member_alias("feishu", "ou_cached", "缓存成员备注")
        self.assertEqual(self.store.members("feishu", ["a"])[0]["display_name"], "缓存成员备注")

    def test_member_names_validation_is_atomic_and_refresh_is_independent(self):
        with self.assertRaises(ValueError):
            self.store.save_member_names("feishu", "a", [{"sender_id": "valid", "name": "有效"},
                                                          {"sender_id": "", "name": "无效"}])
        self.assertEqual(self.store.members("feishu"), [])
        client = Mock()
        client.list_members.side_effect = [FeishuError("需要 im:chat.members:read 权限", 99991672),
                                            [{"sender_id": "second", "name": "第二群成员"}]]
        result = refresh_members(self.store, client, "feishu", ["a", "b"])
        self.assertEqual(result["members_refreshed"], 1)
        self.assertEqual(result["warnings"][0]["code"], 99991672)
        self.assertIn("im:chat.members:read", result["warnings"][0]["message"])
        self.assertEqual([call.args[0] for call in client.list_members.call_args_list], ["a", "b"])
        client.reset_mock()
        self.assertEqual(refresh_members(self.store, client, "demo", ["a"])["members_refreshed"], 0)
        client.list_members.assert_not_called()

    def test_backup_restores_full_data_and_keeps_safety_backup(self):
        self.store.save_messages("feishu", [message()])
        self.store.save_profile("feishu", "ou_self", "本人")
        self.store.save_member_alias("feishu", "ou_self", "备注")
        self.store.save_preferences("feishu", ["a"], "weekly", "personal", "ou_self")
        report = self.store.save_report({"title": "测试报告", "period": "daily", "start_date": "2026-10-19",
            "end_date": "2026-10-19", "source": "feishu", "mode": "extractive", "audience": "personal",
            "chat_ids": ["a"], "sender_ids": ["ou_self"], "message_count": 1, "markdown": "原报告正文"})
        (self.store.path.parent / ".env").write_text("TEST_SECRET=not-in-backup", encoding="utf-8")
        snapshot = self.store.create_backup()
        self.assertEqual(snapshot["counts"]["messages"], 1)
        backup_file = self.store.path.parent / "backups" / (snapshot["id"] + ".sqlite3")
        self.assertNotIn(b"not-in-backup", backup_file.read_bytes())
        self.store.save_messages("feishu", [message("later")])
        self.store.save_profile("feishu", "ou_self", "后来昵称")
        restored = self.store.restore_backup(snapshot["id"])
        self.assertEqual(self.store.count("feishu"), 1)
        self.assertEqual(self.store.profile("feishu")["nickname"], "本人")
        self.assertEqual(self.store.members("feishu")[0]["alias"], "备注")
        self.assertEqual(self.store.preferences("feishu")["period"], "weekly")
        self.assertEqual(self.store.report(report["id"])["markdown"], "原报告正文")
        self.assertEqual(restored["safety_backup"]["counts"]["messages"], 2)
        ids = {item["id"] for item in self.store.backups()}
        self.assertIn(snapshot["id"], ids)
        self.assertIn(restored["safety_backup"]["id"], ids)
        self.store.restore_backup(restored["safety_backup"]["id"])
        self.assertEqual(self.store.count("feishu"), 2)

    def test_corrupt_backup_and_arbitrary_path_never_overwrite_current_data(self):
        self.store.save_messages("feishu", [message()])
        snapshot = self.store.create_backup()
        backup_file = self.store.path.parent / "backups" / (snapshot["id"] + ".sqlite3")
        backup_file.write_bytes(b"not-a-database")
        for identity in (snapshot["id"], "../test.sqlite3", str(self.store.path), "a" * 32, None):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.store.restore_backup(identity)
            self.assertEqual(self.store.count("feishu"), 1)
        self.assertEqual(self.store.backups(), [])

    def resign_backup(self, snapshot):
        directory = self.store.path.parent / "backups"
        target = directory / (snapshot["id"] + ".sqlite3")
        manifest = directory / (snapshot["id"] + ".json")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["size"] = target.stat().st_size
        metadata["sha256"] = hashlib.sha256(target.read_bytes()).hexdigest()
        manifest.write_text(json.dumps(metadata), encoding="utf-8")

    def test_valid_sqlite_with_missing_schema_or_trigger_is_not_restorable(self):
        self.store.save_messages("feishu", [message()])
        for mutation in ("DROP TABLE messages", "CREATE TRIGGER unsafe AFTER INSERT ON messages BEGIN DELETE FROM messages; END"):
            snapshot = self.store.create_backup()
            target = self.store.path.parent / "backups" / (snapshot["id"] + ".sqlite3")
            with closing(sqlite3.connect(target)) as db:
                db.execute(mutation)
                db.commit()
            self.resign_backup(snapshot)
            with self.subTest(mutation=mutation), self.assertRaises(ValueError):
                self.store.restore_backup(snapshot["id"])
            self.assertEqual(self.store.count("feishu"), 1)

    def test_old_backup_report_schema_is_migrated_after_restore(self):
        snapshot = self.store.create_backup()
        target = self.store.path.parent / "backups" / (snapshot["id"] + ".sqlite3")
        with closing(sqlite3.connect(target)) as db:
            db.execute("ALTER TABLE reports DROP COLUMN coverage")
            db.execute("PRAGMA user_version=1")
            db.commit()
        self.resign_backup(snapshot)
        self.store.restore_backup(snapshot["id"])
        with self.store.connect() as db:
            self.assertIn("coverage", {row["name"] for row in db.execute("PRAGMA table_info(reports)")})

    def test_invalid_manifest_and_failed_migration_keep_current_data(self):
        self.store.save_messages("feishu", [message()])
        snapshot = self.store.create_backup()
        self.store.save_messages("feishu", [message("later")])
        with patch.object(Storage, "setup_workspace_schema", side_effect=RuntimeError("模拟迁移失败")):
            with self.assertRaisesRegex(ValueError, "已保留恢复前的数据"):
                self.store.restore_backup(snapshot["id"])
        self.assertEqual(self.store.count("feishu"), 2)
        manifest = self.store.path.parent / "backups" / (snapshot["id"] + ".json")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata["created_at"] = {"invalid": True}
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaises(ValueError):
            self.store.restore_backup(snapshot["id"])
        self.assertEqual(self.store.count("feishu"), 2)

    def test_audit_matches_personal_project_counts_and_shanghai_boundaries(self):
        self.store.save_messages("feishu", [message(moment=START),
            message("other", sender="ou_other", moment=START + 86399),
            message("next", moment=START + 86400), message("excluded", moment=START + 2 * 86400),
            message("other-group", chat="b")], chats=[{"chat_id": "a", "name": "核对群"}])
        self.store.save_messages("demo", [message("demo")])
        self.store.save_messages("feishu", [], coverage=(["a"], START, START + 3 * 86400))
        audit = self.store.audit("feishu", ["a"], START, START + 2 * 86400, ["ou_self"])
        self.assertEqual(audit["message_count"], 3)
        self.assertEqual(audit["selected_message_count"], 2)
        self.assertEqual(audit["active_days"], 2)
        self.assertEqual([day["message_count"] for day in audit["days"]], [2, 1])
        self.assertEqual([day["selected_sender_count"] for day in audit["days"]], [1, 1])
        self.assertEqual(audit["days"][0]["chat_name"], "核对群")
        self.assertEqual(sum(sender["message_count"] for sender in audit["senders"]), 3)
        project = self.store.audit("feishu", ["a"], START, START + 2 * 86400)
        self.assertEqual(project["selected_message_count"], 3)
        zero = self.store.audit("feishu", ["missing"], START, START + 86400)
        self.assertEqual(zero["days"][0]["message_count"], 0)
        self.assertFalse(zero["days"][0]["coverage"]["complete"])
        self.assertIn("零消息不代表", zero["note"])

    def test_audit_rejects_unbounded_or_empty_ranges(self):
        for chats, start, end in (([], START, START + 86400), (["a"], START, START),
                                   (["a"], START, START + 367 * 86400), (["a"], "bad", START)):
            with self.subTest(chats=chats, start=start, end=end), self.assertRaises(ValueError):
                self.store.audit("feishu", chats, start, end)


if __name__ == "__main__":
    unittest.main()
