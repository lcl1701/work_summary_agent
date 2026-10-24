from datetime import date
from pathlib import Path
import unittest

from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report
from app.storage import Storage
from tests.helpers import temporary_directory


SELF = "ou_1234567890abcdef1234567890abcdef"
OTHER = "ou_abcdef1234567890abcdef1234567890"
DAY = date(2026, 10, 20)


class ReportDisplayTests(unittest.TestCase):
    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store = Storage(Path(self.temp.name) / "display.sqlite3")
        self.store.save_chats("feishu", [{"chat_id": "a", "name": "测试项目"},
                                         {"chat_id": "b", "name": "协调群"}])
        self.store.save_messages("feishu", [self.message("mine", SELF)])

    @staticmethod
    def message(mid, sender, name=None):
        return {"message_id": mid, "chat_id": "a", "sender_id": sender,
                "sender_name": name or sender, "text": "已完成接口测试",
                "created_at": timestamp(DAY) + 10 * 3600}

    def report(self, **extra):
        options = dict(period="daily", anchor_date=str(DAY), source="feishu", chat_ids=["a"],
                       audience="personal", sender_ids=[SELF])
        options.update(extra)
        return build_report(self.store, self.settings, **options)

    def test_local_nickname_readable_body_and_saved_identity(self):
        self.store.save_profile("feishu", SELF, "船长")
        report = self.report()
        body = report["markdown"].split("## 消息来源", 1)[0]
        self.assertIn("汇报人：船长", body)
        self.assertIn("- 已完成接口测试 [M001]", body)
        self.assertNotIn("- 船长：已完成接口测试", body)
        self.assertNotIn(SELF, body)
        self.assertNotIn(SELF.replace("_", "\\_"), body)
        self.assertEqual(report["sender_ids"], [SELF])
        self.assertIn(SELF.replace("_", "\\_"), report["markdown"].split("## 成员标识核对", 1)[1])
        # Raw messages remain useful as evidence and are never renamed in storage.
        self.assertEqual(self.store.messages("feishu")[0]["sender_name"], SELF)

    def test_unresolved_project_members_are_distinct_without_long_ids_in_body(self):
        self.store.save_messages("feishu", [self.message("theirs", OTHER)])
        report = self.report(audience="project")
        body = report["markdown"].split("## 消息来源", 1)[0]
        self.assertRegex(body, r"成员·[A-Za-z0-9]{6}：")
        self.assertEqual(len({m["sender_name"] for m in report["evidence"]}), 2)
        self.assertNotIn(SELF, body)
        self.assertNotIn(OTHER, body)
        self.assertNotIn(SELF.replace("_", "\\_"), body)
        self.assertNotIn(OTHER.replace("_", "\\_"), body)
        self.assertEqual(report["message_count"], 2)

    def test_same_display_name_does_not_merge_two_authors(self):
        self.store.save_profile("feishu", SELF, "船长")
        self.store.save_messages("feishu", [self.message("theirs", OTHER, "船长")])
        report = self.report(audience="project")
        body = report["markdown"].split("## 消息来源", 1)[0]
        self.assertRegex(body, r"船长（[A-Z0-9]{6}）")
        self.assertEqual(len({m["sender_name"] for m in report["evidence"]}), 2)
        self.assertIn("[M001]", body)
        self.assertIn("[M002]", body)

    def test_renaming_only_affects_new_reports_and_survives_reopen(self):
        self.store.save_profile("feishu", SELF, "船长")
        old = self.report()
        self.store.save_profile("feishu", SELF, "项目负责人")
        reopened = Storage(self.store.path)
        self.assertEqual(reopened.profile("feishu")["nickname"], "项目负责人")
        self.assertEqual(reopened.report(old["id"])["markdown"], old["markdown"])
        self.assertIn("项目负责人", self.report()["markdown"])

    def test_untrusted_nickname_is_quoted_not_active_html(self):
        self.store.save_profile("feishu", SELF, "<b>船长</b>")
        markdown = self.report()["markdown"]
        self.assertNotIn("<b>", markdown)
        self.assertIn("&lt;b&gt;船长&lt;/b&gt;", markdown)

    def test_empty_selected_chat_remains_in_scope_and_coverage_snapshot(self):
        report = self.report(chat_ids=["a", "b"])
        self.assertIn("范围：测试项目、协调群", report["markdown"])
        self.assertIn("所选群共 2 个", report["markdown"])
        self.assertEqual(set(report["coverage"]["missing_chat_ids"]), {"a", "b"})
        self.store.save_messages("feishu", [], coverage=(["a", "b"], timestamp(DAY), timestamp(date(2026, 10, 21))))
        self.assertEqual(self.store.report(report["id"])["coverage"], report["coverage"])

    def test_report_leads_with_work_and_moves_detailed_statistics_to_appendix(self):
        self.store.save_profile("feishu", SELF, "船长")
        markdown = self.report()["markdown"]
        headings = [line for line in markdown.splitlines() if line.startswith("## ")]
        self.assertEqual(headings, ["## 本期成果", "## 工作进展", "## 风险与待协调", "## 下一步计划",
                                    "## 补充记录", "## 统计与覆盖说明", "## 消息来源", "## 成员标识核对"])
        lead, notes = markdown.split("## 统计与覆盖说明", 1)
        self.assertIn("个人向上汇报", lead)
        self.assertIn("本地规则草稿", lead)
        self.assertIn("- 已完成接口测试 [M001]", lead)
        self.assertIn("当前为部分消息草稿", lead)
        self.assertNotIn("有效消息", lead)
        self.assertNotIn("消息范围：", lead)
        self.assertIn("有效消息：1 条", notes)
        self.assertIn("不是拆分后的工作事项数", notes)
        self.assertIn("图片、附件", notes)

    def test_project_template_keeps_author_on_every_work_item(self):
        self.store.save_profile("feishu", SELF, "船长")
        self.store.save_messages("feishu", [self.message("theirs", OTHER, "同事")])
        markdown = self.report(audience="project")["markdown"]
        main = markdown.split("## 统计与覆盖说明", 1)[0]
        self.assertIn("项目进展汇报", main)
        self.assertIn("- 船长：已完成接口测试 [M001]", main)
        self.assertIn("- 同事：已完成接口测试 [M002]", main)

    def test_uncertain_meeting_record_is_not_promoted_to_an_achievement(self):
        item = self.message("work-list", SELF)
        item["text"] = ("1、完成三维扫描仪软件测试用例编写\n2、完成测试平台接口自动化代码编写\n"
                        "3、完成测试用例评审\n4、会议纪要记录本次需求评审记录")
        self.store.save_messages("feishu", [item], replace_range=(
            ["a"], timestamp(DAY), timestamp(date(2026, 10, 21))))
        report = self.report()
        markdown = report["markdown"]
        achievements = markdown.split("## 本期成果", 1)[1].split("## 工作进展", 1)[0]
        extra = markdown.split("## 补充记录", 1)[1].split("## 统计与覆盖说明", 1)[0]
        self.assertEqual(achievements.count("[M001]"), 3)
        self.assertNotIn("会议纪要", achievements)
        self.assertIn("会议纪要记录本次需求评审记录 [M001]", extra)
        self.assertEqual(report["message_count"], 1)
        self.assertNotIn("[M002]", markdown)

    def test_long_factual_detail_is_preserved_in_body_and_source(self):
        item = self.message("mine", SELF)
        item["text"] = "已完成接口测试：" + "包含边界条件、" * 100 + "最后核对结论为等待业务复核"
        self.store.save_messages("feishu", [item])
        report = self.report()
        main, source = report["markdown"].split("## 统计与覆盖说明", 1)
        self.assertIn(item["text"], main)
        self.assertIn(item["text"], source)


if __name__ == "__main__":
    unittest.main()
