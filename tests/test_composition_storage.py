from contextlib import closing
from copy import deepcopy
from datetime import date
import hashlib
import json
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app.composition import normalize_composition
from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report
from app.storage import Storage
from tests.helpers import temporary_directory


class CompositionStorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "composition.sqlite3")
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store.save_messages("feishu", [
            {"message_id": key, "chat_id": "a", "sender_id": "me", "sender_name": "张工",
             "created_at": timestamp(date(2026, 10, 20)) + index * 3600, "text": text}
            for index, (key, text) in enumerate((("done", "已完成接口开发"),
                                               ("risk", "阻塞：测试环境无法访问")), 9)
        ], chats=[{"chat_id": "a", "name": "隔离测试群"}])

    @staticmethod
    def config(**extra):
        return {"template_id": "ppp", "framework_id": "bluf", "purpose": "update", **extra}

    def report(self):
        return build_report(self.store, self.settings, source="feishu", period="daily",
                            anchor_date="2026-10-20", audience="personal", chat_ids=["a"], sender_ids=["me"])

    def legacy(self):
        report = self.report()
        self.store.save_report(dict(report, composition=None))
        with self.store.connect() as db:
            identity = db.execute("SELECT id FROM reports ORDER BY rowid DESC LIMIT 1").fetchone()[0]
        return self.store.report(identity)

    def counts(self):
        with self.store.connect() as db:
            return {table: db.execute("SELECT COUNT(*) FROM " + table).fetchone()[0]
                    for table in ("reports", "report_details", "report_revisions",
                                  "report_review_metadata", "report_compositions")}

    def downgrade(self):
        with self.store.connect() as db:
            db.execute("DROP TABLE report_compositions")
            db.execute("DROP TABLE composition_preferences")
            db.execute("PRAGMA user_version=3")

    def test_schema4_upgrade_has_consistent_preupgrade_backup_and_preserves_history(self):
        report = self.report()
        self.store.save_profile("feishu", "me", "旧昵称")
        self.store.save_preferences("feishu", ["a"], "weekly", "personal", "me")
        self.downgrade()
        upgraded = Storage(self.store.path)
        self.assertEqual(upgraded.report(report["id"])["markdown"], report["markdown"])
        self.assertEqual(upgraded.report(report["id"])["evidence"], report["evidence"])
        self.assertIsNone(upgraded.report(report["id"])["composition"])
        self.assertEqual(upgraded.profile("feishu")["nickname"], "旧昵称")
        self.assertEqual(upgraded.preferences("feishu")["sender_id"], "me")
        backups = upgraded.backups()
        self.assertEqual(len(backups), 1)
        with closing(sqlite3.connect(upgraded.path.parent / "backups" / (backups[0]["id"] + ".sqlite3"))) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM messages").fetchone()[0], 2)
            self.assertIsNone(db.execute("SELECT name FROM sqlite_master WHERE name='report_compositions'").fetchone())
        with upgraded.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_failed_upgrade_does_not_advance_version_or_change_reports(self):
        report = self.report()
        self.downgrade()
        with patch.object(Storage, "setup_report_schema", side_effect=RuntimeError("isolated migration failure")):
            with self.assertRaises(RuntimeError):
                Storage(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(db.execute("SELECT markdown FROM reports WHERE id=?", (report["id"],)).fetchone()[0], report["markdown"])
        Storage(self.store.path)

    def test_future_schema_is_rejected_without_downgrade(self):
        with self.store.connect() as db:
            db.execute("PRAGMA user_version=5")
        with self.assertRaisesRegex(ValueError, "更新的工程版本"):
            Storage(self.store.path)
        with closing(sqlite3.connect(self.store.path)) as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 5)

    def test_preferences_are_source_isolated_and_never_reuse_report_material(self):
        self.assertIsNone(self.store.composition_preferences("demo"))
        raw = self.config(fields={"key_message": "本次私有材料"}, evidence_item_ids=["one-item"])
        saved = self.store.save_composition_preferences("feishu", raw)
        self.assertNotIn("fields", saved)
        self.assertNotIn("evidence_item_ids", saved)
        reopened = Storage(self.store.path)
        self.assertEqual(reopened.composition_preferences("feishu"), saved)
        self.assertIsNone(reopened.composition_preferences("demo"))
        with reopened.connect() as db:
            self.assertNotIn("本次私有材料", db.execute("SELECT config FROM composition_preferences").fetchone()[0])
        with self.assertRaises(ValueError):
            reopened.save_composition_preferences("feishu", self.config(untrusted="ignored"))
        with self.assertRaises(ValueError):
            reopened.composition_preferences("other")
        self.assertEqual(reopened.composition_preferences("feishu"), saved)

    def test_report_snapshot_is_a_copy_and_insert_failure_rolls_back_evidence(self):
        report = self.report()
        snapshot = normalize_composition(self.config(fields={"key_message": "保存时的材料"}))
        saved = self.store.save_report(dict(report, composition=snapshot))
        snapshot["config"]["fields"]["key_message"] = "后续修改"
        self.assertEqual(self.store.report(saved["id"])["composition"]["config"]["fields"]["key_message"], "保存时的材料")
        before = self.counts()
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER stop_composition BEFORE INSERT ON report_compositions
                          BEGIN SELECT RAISE(ABORT,'isolated atomicity test'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_report(dict(report, composition=normalize_composition(None)))
        self.assertEqual(self.counts(), before)

    def test_history_summary_never_loads_layout_or_private_material(self):
        original = self.report()
        candidate = self.store.recompose_report(original["id"], self.config(fields={"key_message": "本次私有说明"}))
        saved = self.store.save_composition_revision(original["id"], candidate)
        with patch("app.report_features.json.loads", side_effect=AssertionError("history must not decode snapshots")):
            summary = self.store.enrich_report({"id": saved["id"]}, include_snapshot=False)
        self.assertEqual(summary["composition"]["framework"]["id"], "bluf")
        self.assertNotIn("layout", summary["composition"])
        self.assertNotIn("fields", summary["composition"]["config"])
        self.assertNotIn("本次私有说明", str(summary))
        self.assertEqual(self.store.report(saved["id"])["composition"], candidate["composition"])

    def test_legacy_reports_remain_unconfigured_but_structured_rules_can_recompose(self):
        original = self.legacy()
        self.assertIsNone(original["composition"])
        self.assertEqual(original["composition_status"], "legacy")
        self.assertTrue(original["recomposeability"]["allowed"])
        before = self.counts()
        with patch.object(self.store, "messages", side_effect=AssertionError("no mutable cache read")):
            candidate = self.store.recompose_report(original["id"], self.config())
        self.assertEqual(self.counts(), before)
        self.assertEqual(candidate["evidence"], original["evidence"])
        self.assertEqual(candidate["items"], original["items"])
        self.assertIsNone(self.store.report(original["id"])["composition"])

    def test_recompose_rejects_ai_freeform_and_missing_evidence(self):
        original = self.report()
        ai = self.store.save_report(dict(original, mode="llm"))
        freeform = self.store.revise_report(original["id"], "自由修订正文")
        no_evidence = self.store.save_report({key: value for key, value in original.items()
                                             if key not in {"evidence", "items", "template_version", "task_timeline", "composition"}})
        for identity in (ai["id"], freeform["id"], no_evidence["id"]):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                self.store.recompose_report(identity, self.config())

    def test_preview_save_preserves_exact_text_and_frozen_sources_despite_cache_changes(self):
        original = self.legacy()
        candidate = self.store.recompose_report(original["id"], self.config(fields={"key_message": "待业务确认"}))
        self.store.save_messages("feishu", [], replace_range=(["a"], timestamp(date(2026, 10, 20)), timestamp(date(2026, 10, 21))))
        with patch.object(self.store, "messages", side_effect=AssertionError("no resync or cache refresh")):
            saved = self.store.save_composition_revision(original["id"], candidate)
        self.assertEqual(saved["id"], candidate["id"])
        self.assertEqual(saved["title"], candidate["title"])
        self.assertEqual(saved["markdown"], candidate["markdown"])
        self.assertEqual(saved["composition"], candidate["composition"])
        self.assertEqual(saved["evidence"], original["evidence"])
        self.assertEqual(saved["review_kind"], "composition_revision")
        self.assertTrue(saved["reviewability"]["allowed"])
        self.assertEqual(self.store.report(original["id"])["markdown"], original["markdown"])
        self.assertEqual(self.store.save_composition_revision(original["id"], candidate)["id"], saved["id"])

    def test_concurrent_other_revision_does_not_change_frozen_composition_preview(self):
        original = self.report()
        candidate = self.store.recompose_report(original["id"], self.config())
        self.store.revise_report(original["id"], "另一个已保存的自由修订")
        saved = self.store.save_composition_revision(original["id"], candidate)
        self.assertEqual(saved["revision"]["version"], 3)
        self.assertEqual(saved["markdown"], candidate["markdown"])
        self.assertEqual(saved["title"], candidate["title"])

    def test_candidate_tampering_and_invalid_support_cannot_write_revision(self):
        original = self.report()
        with self.assertRaises(ValueError):
            self.store.recompose_report(original["id"], self.config(evidence_item_ids=["not-in-this-report"]))
        candidate = self.store.recompose_report(original["id"], self.config())
        tampered = deepcopy(candidate)
        tampered["items"][0]["sender_id"] = "other"
        before = self.counts()
        with self.assertRaises(ValueError):
            self.store.save_composition_revision(original["id"], tampered)
        self.assertEqual(self.counts(), before)

    def test_revision_snapshot_failure_rolls_back_version_and_body(self):
        original = self.report()
        candidate = self.store.recompose_report(original["id"], self.config())
        before = self.counts()
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER stop_revision_composition BEFORE INSERT ON report_compositions
                          BEGIN SELECT RAISE(ABORT,'isolated revision rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_composition_revision(original["id"], candidate)
        self.assertEqual(self.counts(), before)
        self.assertIsNone(self.store.report(candidate["id"]))

    def test_review_inherits_composition_and_excluded_support_does_not_reappear(self):
        original = self.report()
        target = original["items"][0]
        candidate = self.store.recompose_report(original["id"], self.config(
            fields={"key_message": "请确认后续安排"}, evidence_item_ids=[target["item_id"]]))
        configured = self.store.save_composition_revision(original["id"], candidate)
        reviewed = self.store.review_report(configured["id"], [{"item_id": target["item_id"],
            "included": False, "category": target["review_category"]}], [])
        self.assertEqual(reviewed["composition"], configured["composition"])
        self.assertEqual(reviewed["evidence"], original["evidence"])
        main = reviewed["markdown"].split("## 统计与覆盖说明", 1)[0]
        self.assertNotIn(target["text"], main)
        self.assertIn("请重新核对上方人工陈述", main)
        again = self.store.recompose_report(reviewed["id"], self.config())
        self.assertNotIn(target["text"], again["markdown"].split("## 统计与覆盖说明", 1)[0])
        freeform = self.store.revise_report(reviewed["id"], "人工自由正文")
        self.assertEqual(freeform["composition"], configured["composition"])
        self.assertFalse(freeform["recomposeability"]["allowed"])

    def test_recompose_keeps_weekly_timeline_until_item_review_excludes_sources(self):
        self.store.create_task_group("feishu", ["a"], "me", "隔离关联事项", ["done", "risk"])
        original = build_report(self.store, self.settings, source="feishu", period="weekly",
            anchor_date="2026-10-20", audience="project", chat_ids=["a"])
        preview = self.store.recompose_report(original["id"], self.config())
        self.assertIn("## 事项进展时间线", preview["markdown"])
        saved = self.store.save_composition_revision(original["id"], preview)
        first = saved["items"][0]
        reviewed = self.store.review_report(saved["id"], [{"item_id": first["item_id"],
            "included": False, "category": first["review_category"]}], [])
        again = self.store.recompose_report(reviewed["id"], self.config())
        self.assertNotIn("## 事项进展时间线", again["markdown"])
        self.assertEqual(again["task_timeline"], original["task_timeline"])

    def test_postcommit_refresh_failure_still_returns_saved_preview_id(self):
        original = self.report()
        candidate = self.store.recompose_report(original["id"], self.config())
        real_enrich = self.store.enrich_report
        def fail_saved(report, *args, **kwargs):
            if report and report.get("id") == candidate["id"]:
                raise sqlite3.OperationalError("isolated detail refresh failure")
            return real_enrich(report, *args, **kwargs)
        with patch.object(self.store, "enrich_report", side_effect=fail_saved):
            saved = self.store.save_composition_revision(original["id"], candidate)
        self.assertEqual(saved["id"], candidate["id"])
        self.assertIn("operation_warning", saved)
        self.assertEqual(self.store.report(candidate["id"])["markdown"], candidate["markdown"])

    def test_composition_snapshot_roundtrips_backup_and_old_backup_migrates(self):
        original = self.report()
        candidate = self.store.recompose_report(original["id"], self.config())
        saved = self.store.save_composition_revision(original["id"], candidate)
        self.store.save_composition_preferences("feishu", self.config())
        backup = self.store.create_backup()
        self.assertGreater(backup["counts"]["report_compositions"], 0)
        self.assertEqual(backup["counts"]["composition_preferences"], 1)
        self.store.save_composition_preferences("feishu", None)
        self.store.restore_backup(backup["id"])
        self.assertEqual(self.store.report(saved["id"])["composition"], candidate["composition"])
        self.assertEqual(self.store.composition_preferences("feishu")["framework_id"], "bluf")
        self.downgrade()
        old = self.store.create_backup()
        Storage(self.store.path)
        self.store.restore_backup(old["id"])
        self.assertIsNone(self.store.report(saved["id"])["composition"])
        self.assertIsNone(self.store.composition_preferences("feishu"))
        with self.store.connect() as db:
            self.assertEqual(db.execute("PRAGMA user_version").fetchone()[0], 4)

    def test_future_backup_is_rejected_before_current_data_changes(self):
        report = self.report()
        backup = self.store.create_backup()
        directory = self.store.path.parent / "backups"
        target = directory / (backup["id"] + ".sqlite3")
        with closing(sqlite3.connect(target)) as db:
            db.execute("PRAGMA user_version=5")
            db.commit()
        manifest = directory / (backup["id"] + ".json")
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        metadata.update(size=target.stat().st_size, sha256=hashlib.sha256(target.read_bytes()).hexdigest())
        manifest.write_text(json.dumps(metadata), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "更新的工程版本"):
            self.store.restore_backup(backup["id"])
        self.assertEqual(self.store.report(report["id"])["markdown"], report["markdown"])


if __name__ == "__main__":
    unittest.main()
