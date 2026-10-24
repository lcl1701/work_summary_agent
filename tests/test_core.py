from datetime import date
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from app.config import Settings, load_settings
from app.demo import seed_demo
from app.periods import period_range, timestamp
from app.reporting import ModelError, build_report, classify, generate_with_model, message_items
from app.storage import Storage
from tests.helpers import temporary_directory as TemporaryDirectory


def message(mid="one", sender="self", day=date(2026, 10, 19), chat="group-a", text="已完成接口开发"):
    return {"message_id": mid, "chat_id": chat, "chat_name": chat, "sender_id": sender,
            "sender_name": sender, "text": text, "created_at": timestamp(day) + 9 * 3600}


class PeriodTests(unittest.TestCase):
    def test_shanghai_midnight(self):
        self.assertEqual(timestamp(date(1970, 1, 1)), -8 * 3600)

    def test_week_crosses_year_on_monday(self):
        self.assertEqual(period_range("weekly", "2027-01-01"), (date(2026, 12, 28), date(2027, 1, 4)))

    def test_month_end_and_leap_year(self):
        self.assertEqual(period_range("monthly", "2028-02-29"), (date(2028, 2, 1), date(2028, 3, 1)))
        self.assertEqual(period_range("monthly", "2026-12-31"), (date(2026, 12, 1), date(2027, 1, 1)))

    def test_reject_invalid_calendar_date(self):
        for anchor in ("2026-02-29", "20261019", None, "<script>"):
            with self.subTest(anchor=anchor), self.assertRaises(ValueError):
                period_range("daily", anchor)


class StorageReportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.settings = Settings(data_dir=Path(self.tmp.name))
        self.store = Storage(Path(self.tmp.name) / "test.sqlite3")

    def generate(self, **extra):
        options = dict(period="daily", anchor_date="2026-10-19", source="feishu", chat_ids=["group-a"])
        options.update(extra)
        return build_report(self.store, self.settings, **options)

    def test_dedup_and_source_isolation(self):
        self.store.save_messages("demo", [message()])
        self.store.save_messages("feishu", [message()])
        self.store.save_messages("feishu", [message(text="已修复分页问题")])
        self.assertEqual(self.store.count("demo"), 1)
        self.assertEqual(self.store.count("feishu"), 1)
        self.assertEqual(self.store.messages("feishu")[0]["text"], "已修复分页问题")

    def test_sync_refresh_removes_deleted_only_in_selected_window(self):
        self.store.save_messages("feishu", [message(), message("old", day=date(2026, 10, 18)), message("other", chat="group-b")])
        self.store.save_messages("demo", [message()])
        self.store.save_messages("feishu", [], replace_range=(["group-a"], timestamp(date(2026, 10, 19)), timestamp(date(2026, 10, 20))))
        self.assertEqual({m["message_id"] for m in self.store.messages("feishu")}, {"old", "other"})
        self.assertEqual(self.store.count("demo"), 1)

    def test_storage_refresh_rolls_back_if_insert_fails(self):
        self.store.save_messages("feishu", [message()])
        bad = message("bad")
        del bad["text"]
        with self.assertRaises(KeyError):
            self.store.save_messages("feishu", [bad], replace_range=(["group-a"], 0, 9999999999))
        self.assertEqual(self.store.count("feishu"), 1)

    def test_personal_report_requires_explicit_author_and_filters_colleagues(self):
        self.store.save_messages("feishu", [message(), message("colleague", sender="other", text="已上线同事独有功能")])
        with self.assertRaisesRegex(ValueError, "发言人"):
            self.generate(audience="personal")
        report = self.generate(audience="personal", sender_ids=["self"])
        self.assertEqual(report["message_count"], 1)
        self.assertNotIn("同事独有功能", report["markdown"])
        self.assertIn("仅所选发言人自述", report["markdown"])
        self.assertEqual(self.store.report(report["id"])["sender_ids"], ["self"])

    def test_unknown_sender_cannot_be_attributed_to_person(self):
        with self.assertRaisesRegex(ValueError, "未知发言人"):
            self.generate(audience="personal", sender_ids=["unknown"])

    def test_partial_sync_warning_survives_in_downloaded_report(self):
        self.store.save_messages("feishu", [message()])
        self.assertIn("当前为部分消息草稿", self.generate()["markdown"])
        self.store.save_messages("feishu", [message()], replace_range=(
            ["group-a"], timestamp(date(2026, 10, 19)), timestamp(date(2026, 10, 20))))
        self.assertNotIn("当前为部分消息草稿", self.generate()["markdown"])

    def test_coverage_unions_intervals_and_tracks_empty_success(self):
        self.store.save_messages("feishu", [], coverage=(["a"], 10, 20))
        self.store.save_messages("feishu", [], coverage=(["a"], 20, 30))
        self.assertTrue(self.store.coverage("feishu", ["a"], 10, 30, now=40)["complete"])
        self.assertFalse(self.store.coverage("feishu", ["a", "b"], 10, 30, now=40)["complete"])
        self.assertFalse(self.store.coverage("feishu", ["a"], 9, 30, now=40)["complete"])
        self.assertFalse(self.store.coverage("demo", ["a"], 10, 30, now=40)["complete"])

    def test_project_report_ignores_author_filter(self):
        self.store.save_messages("feishu", [message(), message("other", sender="other")])
        report = self.generate(sender_ids=["self"])
        self.assertEqual(report["message_count"], 2)
        self.assertEqual(report["sender_ids"], [])

    def test_period_excludes_next_midnight(self):
        a = message("first")
        a["created_at"] = timestamp(date(2026, 10, 19))
        b = message("second")
        b["created_at"] = timestamp(date(2026, 10, 20))
        self.store.save_messages("feishu", [a, b])
        self.assertEqual(self.generate()["message_count"], 1)

    def test_no_evidence_does_not_generate_empty_success(self):
        with self.assertRaisesRegex(ValueError, "没有已同步消息"):
            self.generate()
        self.assertEqual(self.store.reports(), [])

    def test_waiting_and_negation_not_completed(self):
        for text in ("尚未完成开发", "已完成实现，等待验收", "计划已完成功能的回归", "未上线"):
            self.assertNotEqual(classify(text), "完成与交付")

    def test_detected_defects_are_risks_without_inventing_findings(self):
        self.assertEqual(classify("发现5个功能效果问题"), "风险与待协调")
        self.assertEqual(classify("本次发现2个缺陷"), "风险与待协调")
        for text in ("未发现功能问题", "没有发现缺陷", "问题记录已经整理完成"):
            self.assertNotEqual(classify(text), "风险与待协调")

    def test_numbered_work_items_keep_one_message_and_source_reference(self):
        self.store.save_messages("feishu", [message(text="1、完成接口测试；\n2、完成用例评审；\n3、会议纪要整理。")])
        report = self.generate()
        completed = report["markdown"].split("## 本期成果\n", 1)[1].split("## 工作进展", 1)[0]
        self.assertIn("完成接口测试", completed)
        self.assertIn("完成用例评审", completed)
        self.assertEqual(report["message_count"], 1)
        self.assertNotIn("[M002]", report["markdown"])
        self.assertEqual(completed.count("[M001]"), 2)

    def test_numbered_items_keep_planning_context_and_negative_status(self):
        plans = message_items("明天计划：\n1、完成接口测试\n2、完成用例评审")
        self.assertEqual(len(plans), 2)
        self.assertTrue(all(classify(item) == "后续计划" for item in plans))
        mixed = message_items("1、未完成测试\n2、完成接口开发")
        self.assertEqual([classify(item) for item in mixed], ["进行中的工作", "完成与交付"])
        for text in ("预计完成测试", "希望已完成的功能能上线", "完成情况如下"):
            self.assertNotEqual(classify(text), "完成与交付")

    def test_version_numbers_are_not_list_markers(self):
        text = "1.0.0版本已发布\n2.0.0版本计划下月发布"
        self.assertEqual(message_items(text), [text])

    def test_demo_idempotent_all_periods_and_no_html(self):
        seed_demo(self.store, date(2026, 10, 19))
        seed_demo(self.store, date(2026, 10, 19))
        self.assertEqual(self.store.count("demo"), 140)
        for period in ("daily", "weekly", "monthly"):
            report = self.generate(source="demo", chat_ids=["demo-product", "demo-project"], period=period)
            self.assertIn("模拟，非真实工作成果", report["markdown"])
            self.assertGreater(report["message_count"], 0)
        self.store.save_messages("feishu", [message(text="<script>alert(1)</script> 已完成")])
        report = self.generate()
        self.assertNotIn("<script>", report["markdown"])
        self.assertIn("[M001]", report["markdown"])

    def test_ai_opt_in_is_required(self):
        self.store.save_messages("feishu", [message()])
        with patch("app.reporting.generate_with_model") as model:
            self.generate()
            model.assert_not_called()
        with self.assertRaisesRegex(ValueError, "尚未配置模型"):
            self.generate(use_llm=True)

    def test_preview_limit_does_not_limit_report(self):
        self.store.save_messages("feishu", [message(str(i)) for i in range(80)])
        self.assertEqual(len(self.store.messages("feishu", limit=5)), 5)
        self.assertEqual(self.generate()["message_count"], 80)


class ConfigModelTests(unittest.TestCase):
    def test_blank_optional_model_base_keeps_offline_startable(self):
        with TemporaryDirectory() as temp:
            path = Path(temp) / ".env"
            path.write_text('LLM_BASE_URL=\nFEISHU_APP_ID="abc"\nFEISHU_APP_SECRET=private\n', encoding="utf-8")
            settings = load_settings(path, environ={})
            self.assertFalse(settings.llm_configured)
            self.assertTrue(settings.feishu_configured)
            self.assertNotIn("private", repr(settings))

    def test_plain_http_remote_model_is_rejected(self):
        with TemporaryDirectory() as temp, self.assertRaises(ValueError):
            load_settings(Path(temp) / "missing", environ={"LLM_BASE_URL": "http://remote.invalid/v1"})

    @staticmethod
    def model_template(fact="- 接口开发 [M001]"):
        return ("## 本期成果\n" + fact + "\n\n## 工作进展\n本期未提及。\n\n"
                "## 风险与待协调\n本期未提及。\n\n## 下一步计划\n本期未提及。\n\n"
                "## 补充记录\n本期未提及。")

    def model(self, output, messages=None, audience="personal"):
        class Response:
            def __enter__(self): return self
            def __exit__(self, *args): pass
            def read(self, length): return json.dumps({"choices": [{"message": {"content": output}}]}).encode()
        class Opener:
            def open(self, request, timeout): return Response()
        return generate_with_model(Settings(llm_api_key="secret", llm_model="test"), messages or [message()],
                                   "日报", "2026-10-19", "2026-10-19", audience, opener=Opener())

    def test_model_valid_evidence_reference(self):
        self.assertIn("[M001]", self.model(self.model_template()))

    def test_model_invalid_or_missing_evidence_is_not_saved(self):
        for text in ("- 已完成全部工作 [M999]", "- 已完成全部工作", "- 功能上线\n- 接口完成 [M001]",
                     "正文 [M001]\n另一段完全无引用内容", "- 接口完成 [M1]", "- 接口完成 [Mabc]"):
            with self.subTest(text=text), self.assertRaises(ModelError):
                self.model(self.model_template(text))

    def test_model_requires_complete_ordered_sections_and_rejects_extra_appendices(self):
        valid = self.model_template()
        for invalid in (valid.replace("## 补充记录\n本期未提及。", ""),
                        valid.replace("## 本期成果", "## 工作进展"),
                        valid.replace("## 本期成果", "### 本期成果"),
                        valid + "\n## 消息来源\n- 编造 [M001]",
                        "已完成全项目 [M001]\n" + valid,
                        valid.replace("## 工作进展\n本期未提及。", "## 工作进展\n")):
            with self.subTest(output=invalid), self.assertRaises(ModelError):
                self.model(invalid)

    def test_model_empty_state_cannot_smuggle_uncited_facts(self):
        for text in ("- 未提及，但已提升效率30%", "本期未提及。已完成全部任务", "- 无明确记录但已经上线"):
            with self.subTest(text=text), self.assertRaises(ModelError):
                self.model(self.model_template().replace("## 工作进展\n本期未提及。", "## 工作进展\n" + text))

    def test_model_project_entries_keep_the_cited_author(self):
        valid = self.model_template("- self：已完成接口开发 [M001]")
        self.assertIn("self：", self.model(valid, audience="project"))
        for fact in ("- 已完成接口开发 [M001]", "- other：已完成接口开发 [M001]",
                     "- self：已完成接口开发 [M001] [M002]"):
            with self.subTest(fact=fact), self.assertRaises(ModelError):
                self.model(self.model_template(fact), [message(), message("two", sender="other")], audience="project")

    def test_model_failure_never_saves_or_silently_falls_back_to_rules(self):
        with TemporaryDirectory() as temp:
            store = Storage(Path(temp) / "model-failure.sqlite3")
            store.save_messages("feishu", [message()])
            settings = Settings(data_dir=Path(temp), llm_api_key="fake-key", llm_model="fake-model")
            with patch("app.reporting.generate_with_model", side_effect=ModelError("模拟模型故障")) as model, \
                    patch("app.reporting.extract") as fallback:
                with self.assertRaisesRegex(ModelError, "模拟模型故障"):
                    build_report(store, settings, period="daily", anchor_date="2026-10-19", source="feishu",
                                 chat_ids=["group-a"], audience="personal", sender_ids=["self"], use_llm=True)
                model.assert_called_once()
                fallback.assert_not_called()
            self.assertEqual(store.reports(), [])

    def test_too_large_model_input_never_sends_partial_period(self):
        with self.assertRaisesRegex(ModelError, "未向模型发送"):
            self.model("", [message(text="a" * 40001)])


if __name__ == "__main__":
    unittest.main()
