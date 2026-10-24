from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report, classify, event_status, message_items, readable_members, render_task_timeline
from app.storage import Storage
from tests.helpers import temporary_directory


SELF = "ou_features000000000000000000000001"
OTHER = "ou_features000000000000000000000002"


def message(mid="one", text="已完成接口测试", day=20, sender=SELF, chat="a"):
    return {"message_id": mid, "chat_id": chat, "sender_id": sender, "sender_name": sender,
            "text": text, "created_at": timestamp(date(2026, 10, day)) + 10 * 3600}


class ReportFeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "report-features.sqlite3")
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store.save_chats("feishu", [{"chat_id": "a", "name": "甲项目"}, {"chat_id": "b", "name": "乙项目"}])
        self.store.save_messages("feishu", [message()])

    def report(self, **extra):
        args = dict(period="daily", anchor_date="2026-10-20", source="feishu", chat_ids=["a"],
                    audience="personal", sender_ids=[SELF])
        args.update(extra)
        return build_report(self.store, self.settings, **args)

    def link(self, ids, **extra):
        args = dict(source="feishu", chat_ids=["a"], sender_id=SELF, title="接口验收", message_ids=ids)
        args.update(extra)
        return self.store.create_task_group(**args)

    def test_generated_report_contains_structured_source_and_item_snapshots(self):
        report = self.report()
        self.assertEqual(report["evidence_status"], "snapshot")
        self.assertEqual(report["evidence"][0]["ref"], "M001")
        self.assertEqual(report["evidence"][0]["message_id"], "one")
        self.assertEqual(report["evidence"][0]["chat_name"], "甲项目")
        self.assertEqual(report["items"][0]["refs"], ["M001"])
        self.assertEqual(report["items"][0]["category_label"], "本期成果")
        self.assertEqual(self.store.report(report["id"])["evidence"], report["evidence"])

    def test_history_metadata_avoids_reading_or_parsing_large_evidence_json(self):
        original = self.report()
        with patch("app.report_features.json.loads", side_effect=AssertionError("列表不应解析快照")):
            metadata = self.store.enrich_report({"id": original["id"]}, include_snapshot=False)
        self.assertEqual(metadata["evidence_status"], "snapshot")
        self.assertEqual(metadata["template_version"], "3")
        self.assertEqual(metadata["evidence"], [])
        self.assertEqual(metadata["items"], [])
        self.assertEqual(metadata["task_timeline"], [])
        self.assertEqual(metadata["revision"]["version"], 1)

    def test_resync_delete_and_rename_cannot_rewrite_report_evidence(self):
        self.store.save_profile("feishu", SELF, "阿舟")
        old = self.report()
        self.store.save_messages("feishu", [message(text="尚未完成，之前消息有误")])
        self.store.save_profile("feishu", SELF, "新昵称")
        self.store.save_messages("feishu", [], replace_range=(
            ["a"], timestamp(date(2026, 10, 20)), timestamp(date(2026, 10, 21))))
        snapshot = self.store.report(old["id"])
        self.assertEqual(snapshot["evidence"], old["evidence"])
        self.assertEqual(snapshot["items"], old["items"])
        self.assertEqual(snapshot["markdown"], old["markdown"])
        self.assertEqual(snapshot["evidence"][0]["sender_name"], "阿舟")
        self.store.attach_report_details(old["id"], [], [])
        self.assertEqual(self.store.report(old["id"])["evidence"], old["evidence"])

    def test_legacy_report_does_not_borrow_current_cached_messages(self):
        original = self.report()
        old_fields = {key: value for key, value in original.items() if key not in {
            "evidence", "items", "task_timeline", "template_version", "evidence_status", "revision"}}
        legacy = self.store.save_report(dict(old_fields, markdown="# 历史报告\n旧版本正文"))
        result = self.store.report(legacy["id"])
        self.assertEqual(result["evidence_status"], "legacy_unavailable")
        self.assertEqual(result["evidence"], [])
        self.assertEqual(result["items"], [])
        self.assertIn("没有结构化来源快照", result["evidence_notice"])

    def test_manual_revision_preserves_original_and_marks_new_facts_as_manual(self):
        original = self.report()
        revised = self.store.revise_report(original["id"], "# 我的修订\n- 人工补充：补做了复核", "补充业务复核")
        self.assertEqual(self.store.report(original["id"])["markdown"], original["markdown"])
        self.assertNotEqual(revised["id"], original["id"])
        self.assertEqual(revised["revision"]["parent_report_id"], original["id"])
        self.assertEqual(revised["revision"]["root_report_id"], original["id"])
        self.assertEqual(revised["revision"]["version"], 2)
        self.assertEqual(revised["revision"]["review_status"], "manual_revision")
        self.assertIn("人工修订稿", revised["markdown"])
        self.assertIn("不代表新增文字已获自动证实", revised["markdown"])
        self.assertEqual(revised["evidence"], original["evidence"])
        self.assertEqual(revised["items"], original["items"])
        self.assertNotIn("补做了复核", str(revised["items"]))
        self.assertEqual(self.store.report(revised["id"])["markdown"], revised["markdown"])

    def test_revision_chain_and_branches_receive_unique_monotonic_versions(self):
        original = self.report()
        second = self.store.revise_report(original["id"], "第二版")
        third = self.store.revise_report(second["id"], "第三版")
        fourth = self.store.revise_report(original["id"], "从原稿重新修订")
        self.assertEqual([r["revision"]["version"] for r in (second, third, fourth)], [2, 3, 4])
        self.assertEqual(third["revision"]["parent_report_id"], second["id"])
        self.assertEqual(fourth["revision"]["parent_report_id"], original["id"])
        self.assertEqual(third["revision"]["root_report_id"], original["id"])
        self.assertNotIn("修订 v2", third["title"])

    def test_concurrent_revisions_commit_distinct_versions_and_evidence(self):
        original = self.report()
        with ThreadPoolExecutor(max_workers=2) as pool:
            revisions = list(pool.map(lambda text: self.store.revise_report(original["id"], text), ["人工甲版", "人工乙版"]))
        self.assertEqual(sorted(r["revision"]["version"] for r in revisions), [2, 3])
        self.assertTrue(all(r["evidence"] == original["evidence"] for r in revisions))
        self.assertEqual(len(self.store.reports()), 3)

    def test_invalid_revision_is_rejected_without_creating_a_report(self):
        original = self.report()
        for content, notes in [("", ""), (None, ""), ("a" * 200001, ""), ("正文", "a" * 2001)]:
            with self.subTest(content_type=type(content)), self.assertRaises(ValueError):
                self.store.revise_report(original["id"], content, notes)
        self.assertEqual(len(self.store.reports()), 1)

    def test_revising_legacy_report_preserves_legacy_evidence_status(self):
        old_fields = {key: value for key, value in self.report().items() if key not in {
            "evidence", "items", "task_timeline", "template_version", "evidence_status", "revision"}}
        raw = self.store.save_report(dict(old_fields, markdown="# 原历史报告"))
        revised = self.store.revise_report(raw["id"], "人工核对版")
        self.assertEqual(revised["evidence_status"], "legacy_unavailable")
        self.assertEqual(revised["evidence"], [])

    def test_task_merge_is_manual_and_does_not_change_messages_or_report_counts(self):
        self.store.save_messages("feishu", [message("plan", "明天计划测试接口", 19), message("done", "已完成接口测试", 21)])
        self.assertEqual(self.store.task_groups("feishu")["groups"], [])
        count = self.store.count("feishu")
        group = self.link(["plan", "done"])
        self.assertEqual([event["status"] for event in group["events"]], ["planned", "completed"])
        report = self.report(period="weekly")
        self.assertEqual(report["message_count"], count)
        self.assertEqual(self.store.count("feishu"), count)
        self.assertEqual(report["task_timeline"][0]["latest_status"], "completed")
        section = report["markdown"].split("## 事项进展时间线", 1)[1].split("## 统计与覆盖说明", 1)[0]
        self.assertIn("[M001]", section)
        self.assertIn("[M003]", section)
        self.assertNotIn(SELF, section)

    def test_task_merge_rejects_cross_author_source_group_and_duplicate_only_selection(self):
        self.store.save_messages("feishu", [message("other", sender=OTHER), message("other-group", chat="b")])
        self.store.save_messages("demo", [message("demo-record")])
        for ids in (["one", "other"], ["one", "other-group"], ["one", "demo-record"], ["one", "one"]):
            with self.subTest(ids=ids), self.assertRaises(ValueError):
                self.link(ids)
        self.assertEqual(self.store.task_groups("feishu")["groups"], [])

    def test_missing_followup_never_closes_a_risk_and_other_authors_are_not_in_personal_timeline(self):
        self.store.save_messages("feishu", [message("blocked", "阻塞：接口无法访问", 19),
            message("still-blocked", "风险尚未解除", 20), message("other1", sender=OTHER), message("other2", day=21, sender=OTHER)])
        self.link(["blocked", "still-blocked"])
        self.link(["other1", "other2"], sender_id=OTHER, title="同事工作")
        personal = self.report(period="weekly")
        self.assertEqual(len(personal["task_timeline"]), 1)
        self.assertEqual(personal["task_timeline"][0]["latest_status"], "blocked")
        self.assertNotIn("同事工作", personal["markdown"])

    def test_explicit_risk_resolution_and_period_filtered_timeline_keep_their_own_references(self):
        self.store.save_messages("feishu", [message("blocked", "阻塞：接口无法访问", 18),
            message("resolved", "阻塞已解除，接口恢复访问", 20)])
        self.link(["blocked", "resolved"])
        report = self.report(period="weekly")
        events = report["task_timeline"][0]["events"]
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["status"], "resolved")
        self.assertEqual(events[0]["ref"], "M002")
        self.assertNotIn("接口无法访问", report["markdown"])
        self.assertEqual(self.report()["task_timeline"], [])

    def test_report_timeline_uses_current_report_snapshot_after_message_was_edited(self):
        self.store.save_messages("feishu", [message("followup", "已完成接口验收", 21)])
        self.link(["one", "followup"])
        old = self.report(period="weekly")
        self.store.save_messages("feishu", [message("followup", "尚未完成接口验收", 21)])
        new = self.report(period="weekly")
        self.assertEqual(old["task_timeline"][0]["latest_status"], "completed")
        self.assertEqual(new["task_timeline"][0]["latest_status"], "in_progress")
        self.assertEqual(self.store.report(old["id"])["task_timeline"], old["task_timeline"])

    def test_latest_multi_item_message_cannot_close_an_entire_task_from_its_last_subitem(self):
        self.store.save_messages("feishu", [message("mixed", "1、阻塞：测试账号无法登录\n2、已完成另一个模块测试", 21)])
        self.link(["one", "mixed"])
        report = self.report(period="weekly")
        timeline = report["task_timeline"][0]
        self.assertEqual(timeline["latest_status"], "unspecified")
        self.assertTrue(timeline["review_warning"])
        self.assertEqual([e["status"] for e in timeline["events"][-2:]], ["blocked", "completed"])

    def test_rendered_timeline_exposes_final_record_status_and_does_not_promote_ambiguous_subitems(self):
        def event(status, ref, day, text):
            return {"status": status, "ref": ref, "created_at": timestamp(date(2026, 10, day)) + 10 * 3600,
                    "sender_name": "测试成员", "text": text}
        timeline = [
            {"title": "回调修复", "latest_status": "resolved", "review_warning": "", "events": [
                event("blocked", "M001", 19, "支付回调被阻塞"),
                event("resolved", "M002", 21, "支付回调问题已解决")]},
            {"title": "混合记录", "latest_status": "unspecified", "review_warning": "最新消息包含不同状态，请核对。", "events": [
                event("blocked", "M003", 21, "仍有账号阻塞"),
                event("completed", "M003", 21, "已完成另一个模块测试")]},
        ]
        markdown = render_task_timeline(timeline)
        resolved, ambiguous = markdown.split("### 回调修复", 1)[1].split("### 混合记录", 1)
        self.assertIn("本期最后记录状态：明确解除/修复 · 10-21 10:00 [M002]", resolved)
        self.assertIn("支付回调被阻塞", resolved)  # Preserve historical risk evidence.
        self.assertIn("本期最后记录状态：待核对 · 10-21 10:00 [M003]", ambiguous)
        self.assertNotIn("本期最后记录状态：明确完成", ambiguous)
        self.assertIn("最新消息包含不同状态，请核对", ambiguous)
        self.assertIn("不推断当前实时状态", markdown)

    def test_deactivating_task_group_preserves_history_and_existing_report_snapshot(self):
        self.store.save_messages("feishu", [message("followup", "已完成接口验收", 21)])
        group = self.link(["one", "followup"])
        report = self.report(period="weekly")
        self.store.deactivate_task_group(group["id"])
        self.assertFalse(self.store.task_groups("feishu")["groups"][0]["active"])
        self.assertEqual(self.report(period="weekly")["task_timeline"], [])
        self.assertEqual(self.store.report(report["id"])["task_timeline"], report["task_timeline"])

    def test_daily_routine_on_different_days_is_not_automatically_one_business_task(self):
        self.store.save_messages("feishu", [message("routine1", "已完成每日巡检", 19), message("routine2", "已完成每日巡检", 20)])
        report = self.report(period="weekly")
        achievements = report["markdown"].split("## 本期成果", 1)[1].split("## 工作进展", 1)[0]
        self.assertEqual(achievements.count("已完成每日巡检"), 2)
        self.assertEqual(report["task_timeline"], [])

    def test_team_statement_keeps_original_words_and_prompts_attribution_review(self):
        self.store.save_messages("feishu", [message(text="团队已完成系统上线，我负责联调")])
        report = self.report()
        self.assertIn("团队已完成系统上线，我负责联调", report["markdown"])
        self.assertIn("归属待核对", report["markdown"])
        self.assertTrue(report["items"][0]["attribution_warning"])
        self.assertNotIn("我独立完成", report["markdown"])

    def test_stored_display_name_is_stable_in_personal_project_and_reordered_periods(self):
        personal = self.report()
        self.store.save_messages("feishu", [message("earlier-colleague", day=19, sender=OTHER)])
        project = self.report(period="weekly", audience="project")
        current = next(e for e in project["evidence"] if e["sender_id"] == SELF)
        self.assertEqual(personal["evidence"][0]["sender_name"], current["sender_name"])
        self.assertNotEqual(current["sender_name"], next(e for e in project["evidence"] if e["sender_id"] == OTHER)["sender_name"])


class ClassificationCorrectionTests(unittest.TestCase):
    def test_negative_risk_and_completed_risk_check_do_not_become_open_risks(self):
        for text in ("没有风险", "未发现风险", "没有发现新的风险", "未出现报错", "已完成风险排查，未发现风险"):
            with self.subTest(text=text):
                self.assertNotEqual(classify(text), "风险与待协调")
        self.assertEqual(classify("已完成风险排查，未发现风险"), "完成与交付")

    def test_questions_and_future_plans_never_become_completed_results_or_current_risks(self):
        for text in ("是否已完成接口测试", "接口已完成了吗", "接口已完成?", "明天计划修复报错"):
            with self.subTest(text=text):
                self.assertNotEqual(classify(text), "完成与交付")
        self.assertEqual(classify("明天计划修复报错"), "后续计划")

    def test_resolution_requires_explicit_non_negated_statement(self):
        self.assertEqual(event_status("阻塞已解除，接口恢复访问"), "resolved")
        self.assertEqual(event_status("风险已经解决"), "resolved")
        for text in ("风险尚未解除", "没有解除风险", "阻塞已解除，但仍有报错", "风险已解除但仍有缺陷", "是否已解除风险"):
            with self.subTest(text=text):
                self.assertNotIn(event_status(text), {"completed", "resolved"})
        self.assertEqual(classify("已修复报错，等待验收"), "进行中的工作")

    def test_explicit_time_clauses_keep_both_history_and_current_status(self):
        items = message_items("昨天计划上线，今天已完成发布")
        self.assertEqual(items, ["昨天计划上线", "今天已完成发布"])
        self.assertEqual([classify(item) for item in items], ["后续计划", "完成与交付"])
        plans = message_items("明天计划：完成接口测试；完成订单测试")
        self.assertTrue(all(classify(item) == "后续计划" for item in plans))
        self.assertEqual(classify("今天已完成昨天计划的接口测试"), "完成与交付")
        self.assertEqual(classify("已完成计划中的接口回归"), "完成与交付")
        self.assertEqual(classify("计划已完成功能的回归"), "后续计划")

    def test_unresolved_names_are_stable_when_other_authors_or_sort_order_change(self):
        records = [message(sender=SELF), message("other", sender=OTHER)]
        first = {m["sender_id"]: m["sender_name"] for m in readable_members(records)}
        reversed_names = {m["sender_id"]: m["sender_name"] for m in readable_members(records[::-1])}
        self.assertEqual(first, reversed_names)
        self.assertEqual(first[SELF], readable_members(records[:1])[0]["sender_name"])
        self.assertNotIn(SELF, first[SELF])


if __name__ == "__main__":
    unittest.main()
