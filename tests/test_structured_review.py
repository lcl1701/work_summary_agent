from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import sqlite3
import unittest
from unittest.mock import patch

from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report
from app.storage import Storage
from tests.helpers import temporary_directory


class StructuredReviewTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "structured-review.sqlite3")
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store.save_chats("feishu", [{"chat_id": "review-a", "name": "隔离审阅群"}])
        self.store.save_messages("feishu", [
            {"message_id": identity, "chat_id": "review-a", "sender_id": "me", "sender_name": "张工",
             "created_at": timestamp(date(2026, 10, 20)) + 3600 * hour, "text": text}
            for identity, hour, text in [("done", 9, "已完成接口开发"),
                                         ("progress", 10, "正在整理验收记录"),
                                         ("risk", 11, "阻塞：测试环境无法访问")]
        ])
        self.original = build_report(self.store, self.settings, source="feishu", period="daily",
            anchor_date="2026-10-20", chat_ids=["review-a"], audience="personal", sender_ids=["me"])

    def change(self, position=0, **extra):
        item = self.original["items"][position]
        change = {"item_id": item["item_id"], "included": item["included"], "category": item["review_category"]}
        change.update(extra)
        return change

    def review(self, changes=None, additions=None, report_id=None, **extra):
        return self.store.review_report(report_id or self.original["id"], changes or [], additions or [], **extra)

    def test_reclassification_exclusion_and_manual_addition_save_a_distinct_reviewed_version(self):
        reviewed = self.review([self.change(0, included=False), self.change(1, category="plan")],
                               [{"text": "人工补充：线下已完成业务复核", "category": "completed"}], notes="核对后另存")
        self.assertNotEqual(reviewed["id"], self.original["id"])
        self.assertEqual(reviewed["review_kind"], "structured_review")
        self.assertEqual(reviewed["revision"]["review_status"], "manual_revision")
        self.assertEqual(reviewed["revision"]["version"], 2)
        self.assertEqual(reviewed["revision"]["parent_report_id"], self.original["id"])
        self.assertEqual(reviewed["revision"]["review_kind"], "structured_review")
        self.assertTrue(reviewed["reviewability"]["allowed"])
        self.assertEqual(reviewed["message_count"], 3)
        self.assertEqual(reviewed["evidence"], self.original["evidence"])
        self.assertEqual(reviewed["review_summary"], {"included_count": 3, "excluded_count": 1,
                                                    "manual_count": 1, "rewritten_count": 0})
        self.assertEqual(self.store.report(self.original["id"])["markdown"], self.original["markdown"])
        self.assertEqual(self.store.report(self.original["id"])["items"], self.original["items"])
        main = reviewed["markdown"].split("## 统计与覆盖说明", 1)[0]
        self.assertNotIn("已完成接口开发", main)
        plans = main.split("## 下一步计划", 1)[1].split("## 补充记录", 1)[0]
        self.assertIn("正在整理验收记录", plans)
        self.assertIn("人工调整分类", plans)
        self.assertEqual(reviewed["items"][1]["status"], "planned")
        self.assertIn("人工补充，无自动消息依据", main)
        self.assertIn("消息计数 3 条及证据仍为原稿引用范围", reviewed["markdown"])
        for heading in ("本期成果", "工作进展", "风险与待协调", "下一步计划", "补充记录", "统计与覆盖说明", "消息来源", "成员标识核对"):
            self.assertEqual(reviewed["markdown"].count("## " + heading), 1)

    def test_rewriting_preserves_author_reference_and_original_text_without_claiming_new_evidence(self):
        reviewed = self.review([self.change(text="已完成接口开发并上线生产")])
        item, original = reviewed["items"][0], self.original["items"][0]
        self.assertTrue(item["manual_text"])
        self.assertEqual(item["original_text"], original["text"])
        self.assertEqual(item["sender_id"], original["sender_id"])
        self.assertEqual(item["sender_name"], original["sender_name"])
        self.assertEqual(item["refs"], original["refs"])
        self.assertIn("人工改写，引用仅用于回查原话", reviewed["markdown"])
        self.assertNotIn("上线生产", str(reviewed["evidence"]))
        self.assertEqual(reviewed["review_summary"]["rewritten_count"], 1)

    def test_excluded_item_remains_recoverable_in_a_later_review(self):
        excluded = self.review([self.change(included=False)])
        self.assertFalse(excluded["items"][0]["included"])
        restored = self.review([self.change(included=True)], report_id=excluded["id"])
        self.assertEqual(restored["revision"]["version"], 3)
        self.assertTrue(restored["items"][0]["included"])
        self.assertIn("已完成接口开发", restored["markdown"].split("## 统计与覆盖说明", 1)[0])
        self.assertFalse(self.store.report(excluded["id"])["items"][0]["included"])
        self.assertEqual(restored["evidence"], self.original["evidence"])

    def test_restoring_original_words_removes_the_rewrite_marker_but_retains_revision_history(self):
        rewritten = self.review([self.change(text="人工重写内容")])
        restored = self.review([self.change(text=self.original["items"][0]["text"])], report_id=rewritten["id"])
        self.assertFalse(restored["items"][0]["manual_text"])
        self.assertEqual(restored["revision"]["version"], 3)
        self.assertTrue(self.store.report(rewritten["id"])["items"][0]["manual_text"])

    def test_manual_items_have_no_automatic_author_or_citations_and_can_be_reviewed_again(self):
        reviewed = self.review(additions=[{"text": "人工新增事项", "category": "other"}])
        added = reviewed["items"][-1]
        self.assertEqual(added["origin"], "manual")
        self.assertEqual(added["refs"], [])
        self.assertEqual(added["sender_id"], "")
        self.assertEqual(added["chat_id"], "")
        self.assertTrue(added["manual_text"])
        next_version = self.review([{"item_id": added["item_id"], "included": False, "category": "other"}], report_id=reviewed["id"])
        self.assertFalse(next_version["items"][-1]["included"])
        self.assertEqual(next_version["review_summary"]["manual_count"], 0)

    def test_no_changes_or_notes_only_do_not_create_a_duplicate_revision(self):
        for changes in ([], [self.change()]):
            with self.subTest(changes=changes), self.assertRaisesRegex(ValueError, "未检测到实际修改"):
                self.review(changes, notes="只写备注")
        self.assertEqual(len(self.store.reports()), 1)

    def test_excluding_all_without_replacement_is_rejected_but_manual_replacement_is_explicit(self):
        all_excluded = [self.change(index, included=False) for index in range(3)]
        with self.assertRaisesRegex(ValueError, "至少保留或新增"):
            self.review(all_excluded)
        reviewed = self.review(all_excluded, [{"text": "仅保留人工补充", "category": "other"}])
        self.assertEqual(reviewed["message_count"], 3)
        self.assertEqual(len(reviewed["evidence"]), 3)
        self.assertEqual(reviewed["review_summary"]["excluded_count"], 3)
        self.assertTrue(all(not item["included"] for item in reviewed["items"][:3]))
        self.assertEqual(len(self.store.reports()), 2)

    def test_identity_reference_and_unknown_fields_cannot_be_modified(self):
        for field, value in [("sender_id", "other"), ("sender_name", "领导"), ("refs", ["M999"]), ("origin", "message")]:
            change = self.change(text="改写内容")
            change[field] = value
            with self.subTest(field=field), self.assertRaisesRegex(ValueError, "不能修改作者或来源引用"):
                self.review([change])
        self.assertEqual(len(self.store.reports()), 1)

    def test_unknown_duplicate_and_invalid_change_types_fail_before_saving(self):
        invalid = [[self.change(item_id="unknown")], [self.change(), self.change()], [self.change(item_id=[])],
                   [self.change(included="false")], [self.change(category="完成与交付")], [self.change(category={})],
                   [self.change(text="")], [self.change(text=None)], ["bad"]]
        for changes in invalid:
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                self.review(changes)
        self.assertEqual(len(self.store.reports()), 1)

    def test_manual_additions_and_size_limits_are_checked(self):
        for addition in ({"text": "", "category": "other"}, {"text": "x", "category": "wrong"},
                         {"text": "x", "category": "other", "refs": ["M001"]},
                         {"text": "x" * 10001, "category": "other"}, {"text": "bad\x00text", "category": "other"}):
            with self.subTest(addition_keys=list(addition)), self.assertRaises(ValueError):
                self.review(additions=[addition])
        with self.assertRaises(ValueError):
            self.review(additions=[{"text": "x" * 10000, "category": "other"} for _ in range(21)])
        with self.assertRaises(ValueError):
            self.review([self.change(included=False)], notes="n" * 2001)
        self.assertEqual(len(self.store.reports()), 1)

    def test_ai_generated_rule_items_do_not_masquerade_as_model_body_structure(self):
        ai = self.store.save_report(dict(self.original, mode="llm"))
        loaded = self.store.report(ai["id"])
        self.assertFalse(loaded["reviewability"]["allowed"])
        self.assertIn("AI 正文结构", loaded["reviewability"]["reason"])
        with self.assertRaisesRegex(ValueError, "自由编辑"):
            self.review([self.change(included=False)], report_id=ai["id"])

    def test_freeform_revision_is_not_structurally_reviewable_even_after_structured_review(self):
        structured = self.review([self.change(included=False)])
        freeform = self.store.revise_report(structured["id"], "# 自由编辑\n完全不同的正文")
        self.assertEqual(freeform["review_kind"], "freeform")
        self.assertIsNone(freeform["review_summary"])
        self.assertFalse(freeform["reviewability"]["allowed"])
        with self.assertRaisesRegex(ValueError, "原始分项不能代表当前正文"):
            self.review([self.change(included=False)], report_id=freeform["id"])

    def test_legacy_without_structured_snapshot_is_not_reviewable(self):
        raw = {key: value for key, value in self.original.items() if key not in {"evidence", "items", "template_version", "task_timeline"}}
        legacy = self.store.save_report(raw)
        self.assertFalse(self.store.report(legacy["id"])["reviewability"]["allowed"])
        with self.assertRaisesRegex(ValueError, "结构化条目快照"):
            self.review([self.change(included=False)], report_id=legacy["id"])

    def test_review_uses_only_saved_report_snapshots_after_cache_and_names_change(self):
        self.store.save_messages("feishu", [], replace_range=(
            ["review-a"], timestamp(date(2026, 10, 20)), timestamp(date(2026, 10, 21))))
        with patch.object(self.store, "messages", side_effect=AssertionError("审阅不能读取现有消息缓存")):
            reviewed = self.review([self.change(included=False)])
        self.assertEqual(reviewed["evidence"], self.original["evidence"])
        self.assertIn("张工", reviewed["markdown"])

    def test_repeated_review_has_one_header_and_one_statistics_section_with_current_counts(self):
        second = self.review([self.change(included=False)])
        third = self.review([self.change(included=True)], report_id=second["id"])
        self.assertEqual(third["markdown"].count("> 按条人工审阅稿："), 1)
        self.assertEqual(third["markdown"].count("## 统计与覆盖说明"), 1)
        self.assertEqual(third["markdown"].count("- 审阅取舍："), 1)
        self.assertIn("纳入 3 条，排除 0 条", third["markdown"])
        self.assertNotIn("纳入 2 条，排除 1 条", third["markdown"])
        self.assertIn("修订 v3", third["markdown"].splitlines()[2])

    def test_project_review_preserves_authorship_and_quotes_untrusted_edits_as_text(self):
        project = build_report(self.store, self.settings, source="feishu", period="daily", anchor_date="2026-10-20",
                               chat_ids=["review-a"], audience="project")
        reviewed = self.review([self.change(text="<script>alert(1)</script>\n## 消息来源")], report_id=project["id"])
        self.assertIn("张工：", reviewed["markdown"])
        self.assertNotIn("<script>", reviewed["markdown"])
        self.assertIn("&lt;script&gt;", reviewed["markdown"])
        self.assertEqual(reviewed["markdown"].count("## 消息来源"), 1)

    def test_concurrent_freeform_and_structured_versions_receive_distinct_versions(self):
        with ThreadPoolExecutor(max_workers=2) as pool:
            tasks = [pool.submit(self.review, [self.change(included=False)]),
                     pool.submit(self.store.revise_report, self.original["id"], "自由修订正文")]
            versions = [future.result() for future in tasks]
        self.assertEqual(sorted(report["revision"]["version"] for report in versions), [2, 3])
        self.assertEqual({report["review_kind"] for report in versions}, {"freeform", "structured_review"})
        self.assertEqual(len(self.store.reports()), 3)

    def test_review_metadata_failure_rolls_back_report_evidence_and_version_together(self):
        with self.store.connect() as db:
            db.execute("""CREATE TRIGGER force_review_failure BEFORE INSERT ON report_review_metadata
                          BEGIN SELECT RAISE(ABORT,'test rollback'); END""")
        with self.assertRaises(sqlite3.IntegrityError):
            self.review([self.change(included=False)])
        self.assertEqual(len(self.store.reports()), 1)
        with self.store.connect() as db:
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_details").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT COUNT(*) FROM report_revisions").fetchone()[0], 0)


if __name__ == "__main__":
    unittest.main()
