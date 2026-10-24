from copy import deepcopy
from datetime import date
from pathlib import Path
import unittest
from unittest.mock import patch

from app.composition import catalog, normalize_composition, preference_config, render_composed_report
from app.config import Settings
from app.periods import timestamp
from app.reporting import build_report, extract, readable_members, render_review_report
from app.storage import Storage
from tests.helpers import temporary_directory


class CompositionTests(unittest.TestCase):
    """All messages and manual fields here are fictional, isolated local samples."""

    def setUp(self):
        self.temp = temporary_directory()
        self.addCleanup(self.temp.cleanup)
        self.store = Storage(Path(self.temp.name) / "composition.sqlite3")
        self.settings = Settings(data_dir=Path(self.temp.name))
        self.store.save_chats("demo", [{"chat_id": "a", "name": "【虚构单测】支付项目群"}])
        samples = [
            ("done", "me", "张工", "已完成支付接口开发"),
            ("progress", "me", "张工", "正在整理验收记录"),
            ("risk", "other", "李工", "阻塞：测试环境无法访问"),
            ("plan", "me", "张工", "明天计划发布版本"),
            ("other", "me", "张工", "会议纪要记录本次需求评审记录"),
            ("numbered", "me", "张工", "1、已完成日志补充\n2、正在进行回归测试"),
        ]
        self.store.save_messages("demo", [dict(message_id=identity, chat_id="a", sender_id=sender,
            sender_name=name, text=text, created_at=timestamp(date(2026, 10, 20)) + (9 + index) * 3600)
            for index, (identity, sender, name, text) in enumerate(samples)])
        self.base = self.build()

    def build(self, **extra):
        args = dict(period="daily", anchor_date="2026-10-20", source="demo", chat_ids=["a"], persist=False)
        args.update(extra)
        return build_report(self.store, self.settings, **args)

    def composed(self, config, items=None):
        report = deepcopy(self.base)
        snapshot = normalize_composition(config)
        snapshot["layout"] = deepcopy(report["composition"]["layout"])
        report["composition"] = snapshot
        if items is not None:
            report["items"] = deepcopy(items)
        return report

    @staticmethod
    def main(markdown):
        return markdown.split("## 统计与覆盖说明", 1)[0]

    def test_catalog_declares_versioned_combinations_fields_and_complete_factual_sections(self):
        directory = catalog()
        self.assertEqual({entry["id"] for entry in directory["templates"]}, {"classic", "ppp", "project", "reflection"})
        self.assertEqual({entry["id"] for entry in directory["frameworks"]}, {"none", "bluf", "prep", "scqa", "sbar", "wswnw"})
        field_ids = {field["id"] for field in directory["fields"]}
        for definitions in (directory["templates"], directory["frameworks"], directory["purposes"]):
            for definition in definitions:
                self.assertEqual(definition["version"], 1)
                self.assertTrue(set(definition["field_ids"]) <= field_ids)
                self.assertTrue(set(definition["required_field_ids"]) <= field_ids)
        for template in directory["templates"]:
            covered = {category for section in template["sections"] for category in section["categories"]}
            self.assertEqual(covered, {item["category"] for item in self.base["items"]})
            risk = next(section for section in template["sections"] if section["id"] == "risk")
            self.assertFalse(risk["hideable"])
        directory["templates"][0]["sections"][0]["label"] = "修改了当前返回值"
        directory["frameworks"][2]["steps"][0]["label"] = "也不能污染目录"
        self.assertEqual(catalog()["templates"][0]["sections"][0]["label"], "本期成果")
        self.assertEqual(catalog()["frameworks"][2]["steps"][0]["label"], "观点（P）")

    def test_raw_configuration_rejects_unknown_keys_ids_and_snapshot_injection(self):
        invalid = [[], "classic", {"layout": {}}, {"template_id": "custom"}, {"framework_id": "unknown"},
                   {"purpose": "unknown"}, {"schema_version": True}, {"schema_version": 2},
                   {"template_id": []}, {"fields": {"point": "inactive field"}},
                   {"framework_id": "prep", "purpose": "update"}, normalize_composition(None)]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_composition(config)

    def test_manual_text_limits_types_and_control_characters_are_validated(self):
        for fields in ([], {"recipient": None}, {"recipient": "x" * 2001}, {"recipient": "bad\x00value"}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                normalize_composition({"fields": fields})
        all_fields = {identity: "x" * 2000 for identity in
                      ["recipient", "familiarity", "desired_action", "analysis_condition", "counter_evidence",
                       "status", "milestones", "dependencies", "decisions", "point", "reason", "example"]}
        with self.assertRaisesRegex(ValueError, "合计"):
            normalize_composition({"template_id": "project", "framework_id": "prep", "purpose": "decision", "fields": all_fields})

    def test_order_is_a_full_permutation_only_other_can_hide_and_evidence_ids_are_bounded(self):
        invalid = [{"section_order": ["risk"]}, {"section_order": ["completed", "progress", "risk", "plan", "plan"]},
                   {"hidden_sections": ["risk"]}, {"hidden_sections": ["other", "other"]},
                   {"evidence_item_ids": "one"}, {"evidence_item_ids": ["one", "one"]},
                   {"evidence_item_ids": [""]}, {"evidence_item_ids": list(map(str, range(21)))},
                   {"evidence_item_ids": [None]}]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(ValueError):
                normalize_composition(config)

    def test_preferences_keep_only_reusable_options_without_manual_or_source_material(self):
        snapshot = normalize_composition({"template_id": "project", "framework_id": "bluf", "purpose": "decision",
            "fields": {"recipient": "给项目负责人", "key_message": "人工建议"},
            "evidence_item_ids": [self.base["items"][0]["item_id"]],
            "section_order": ["risk", "completed", "progress", "plan", "other"], "hidden_sections": ["other"]})
        preference = preference_config(snapshot)
        self.assertEqual(set(preference), {"schema_version", "template_id", "framework_id", "purpose", "section_order", "hidden_sections"})
        self.assertEqual(preference["purpose"], "decision")
        self.assertEqual(preference["section_order"][0], "risk")
        self.assertNotIn("人工建议", str(preference))
        preference["hidden_sections"].clear()
        self.assertEqual(snapshot["config"]["hidden_sections"], ["other"])

    def test_preview_returns_full_frozen_report_without_saving(self):
        report = self.build(composition={"template_id": "project"})
        self.assertNotIn("id", report)
        self.assertEqual(self.store.reports(), [])
        self.assertEqual(report["message_count"], 6)
        self.assertEqual(len(report["items"]), 7)
        self.assertEqual(len(report["evidence"]), 6)
        for key in ("header", "appendix", "timeline", "legacy_body"):
            self.assertIsInstance(report["composition"]["layout"][key], str)
        self.assertEqual(set(report["composition"]["layout"]["source_item_baseline"]), {item["item_id"] for item in report["items"]})
        self.assertIn("示例数据（模拟，非真实工作成果）", report["markdown"])
        self.assertIn("同步覆盖提醒", self.main(report["markdown"]))

    def test_resolved_range_is_frozen_instead_of_recomputed_from_anchor(self):
        report = self.build(anchor_date="2030-01-01", resolved_range=(date(2026, 10, 20), date(2026, 10, 21)))
        self.assertEqual((report["start_date"], report["end_date"], report["message_count"]), ("2026-10-20", "2026-10-20", 6))
        for resolved in (("2026-10-20", "2026-10-21"), (date(2026, 10, 21), date(2026, 10, 20)), [date(2026, 10, 20)]):
            with self.subTest(resolved=resolved), self.assertRaises(ValueError):
                self.build(resolved_range=resolved)

    def test_default_classic_uses_the_existing_fact_body_without_additional_analysis(self):
        messages = readable_members(self.store.messages("demo", timestamp(date(2026, 10, 20)), timestamp(date(2026, 10, 21)), ["a"]))
        body = extract(messages)
        self.assertEqual(self.base["composition"]["layout"]["legacy_body"], body)
        self.assertIn(body, self.base["markdown"])
        self.assertNotIn("沟通背景与补充材料", self.base["markdown"])
        self.assertNotIn("沟通用途", self.base["markdown"])
        saved = self.build(persist=True)
        self.assertEqual(saved["composition"], self.base["composition"])
        self.assertEqual(self.store.report(saved["id"])["markdown"], saved["markdown"])

    def test_only_unchanged_default_classic_accepts_ai_and_new_combinations_fail_before_model_call(self):
        self.assertEqual(normalize_composition(None, use_llm=True)["config"]["template_id"], "classic")
        for config in ({"template_id": "ppp"}, {"framework_id": "bluf"}, {"purpose": "update"},
                       {"fields": {"recipient": "负责人"}}, {"hidden_sections": ["other"]}):
            with self.subTest(config=config), patch("app.reporting.generate_with_model") as model, self.assertRaisesRegex(ValueError, "关闭 AI"):
                self.build(composition=config, use_llm=True)
            model.assert_not_called()
        with patch("app.reporting.generate_with_model", return_value="## 模型正文\n独立模型输出"):
            settings = Settings(data_dir=Path(self.temp.name), llm_api_key="fake-local-only", llm_model="fake")
            report = build_report(self.store, settings, period="daily", anchor_date="2026-10-20", source="demo",
                                  chat_ids=["a"], use_llm=True, persist=False)
        self.assertIn("独立模型输出", report["markdown"])
        self.assertEqual(report["mode"], "llm")

    def test_all_catalog_combinations_keep_fact_text_attribution_category_and_every_reference(self):
        original_items = deepcopy(self.base["items"])
        for combination in catalog()["combinations"]:
            for purpose in combination["purposes"]:
                config = {"template_id": combination["template_id"], "framework_id": combination["framework_id"], "purpose": purpose}
                with self.subTest(config=config):
                    report = self.composed(config)
                    main = self.main(render_composed_report(report))
                    for item in original_items:
                        self.assertIn(item["text"], main)
                        self.assertIn(item["sender_name"] + "：", main)
                        for ref in item["refs"]:
                            self.assertIn("[" + ref + "]", main)
                    self.assertEqual(report["items"], original_items)
                    # A numbered original message remains one source message.
                    numbered = [item for item in report["items"] if item["ref"] == "M006"]
                    self.assertEqual(len(numbered), 2)
                    self.assertEqual(report["message_count"], 6)

    def test_custom_order_and_optional_other_hiding_never_hide_risk(self):
        report = self.composed({"section_order": ["risk", "plan", "completed", "progress", "other"], "hidden_sections": ["other"]})
        main = self.main(render_composed_report(report))
        self.assertLess(main.index("## 风险与待协调"), main.index("## 下一步计划"))
        self.assertLess(main.index("## 下一步计划"), main.index("## 本期成果"))
        self.assertIn("阻塞：测试环境无法访问", main)
        self.assertNotIn("## 补充记录", main)
        self.assertNotIn("会议纪要记录本次需求评审记录", main)
        self.assertIn("会议纪要记录本次需求评审记录", render_composed_report(report).split("## 消息来源", 1)[1])
        # Even a stored hidden_sections value cannot override the risk requirement.
        report["composition"]["config"]["hidden_sections"] = ["risk"]
        self.assertIn("阻塞：测试环境无法访问", self.main(render_composed_report(report)))

    def test_required_manual_material_shows_missing_and_never_auto_fills_results_or_owners(self):
        report = self.composed({"template_id": "project", "framework_id": "sbar", "purpose": "escalation"})
        main = self.main(render_composed_report(report))
        self.assertIn("### 项目状态的人工判断\n\n待补充", main)
        self.assertIn("当前判断：待补充", main)
        self.assertIn("希望完成或响应的时间：待补充", main)
        self.assertIn("希望对方采取的行动\n\n待补充", main)
        self.assertNotIn("效率提升", main)
        self.assertNotIn("100%", main)

    def test_manual_fields_are_escaped_and_are_not_automatically_proven_by_selected_quotes(self):
        report = self.composed({"framework_id": "bluf", "purpose": "decision",
            "fields": {"key_message": "<script>收益已翻倍</script> [M001]", "desired_action": "批准此建议"},
            "evidence_item_ids": [self.base["items"][0]["item_id"]]})
        main = self.main(render_composed_report(report))
        analysis = main.split("## 所选核对材料", 1)[0]
        self.assertIn("【人工陈述，需核对】&lt;script&gt;收益已翻倍&lt;/script&gt; \\[M001\\]", analysis)
        self.assertNotIn("<script>", main)
        self.assertNotIn(" [M001]", analysis)
        self.assertLess(main.index("## BLUF"), main.index("## 沟通背景与补充材料"))
        self.assertIn("不表示因果或结论已被证实", main)
        self.assertIn("张工：已完成支付接口开发 [M001]", main)

    def test_prep_has_real_p_r_e_p_order_with_manual_point_repeated(self):
        report = self.composed({"framework_id": "prep", "purpose": "recommendation", "fields": {
            "recipient": "项目负责人", "point": "建议先安排回归", "reason": "先核对关键路径", "example": "以支付回调作为试点"}})
        main = self.main(render_composed_report(report))
        positions = [main.index("### " + heading) for heading in ("观点（P）", "理由（R）", "例证（E）", "重申观点（P）")]
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(main.count("【人工陈述，需核对】建议先安排回归"), 2)
        self.assertLess(main.index("### 重申观点（P）"), main.index("### 沟通对象"))

    def test_scqa_sbar_and_wswnw_follow_their_actual_steps(self):
        cases = [
            ("scqa", "decision", ["共同背景（S）", "变化与困难（C）", "需要回答的问题（Q）", "建议答案（A）"], {}),
            ("sbar", "escalation", ["现状（S）", "必要背景（B）", "当前判断（A）", "建议或请求（R）"], {}),
            ("wswnw", "reflection", ["What：发生了什么", "So What：意义与学习", "Now What：后续改进"],
             {"expectation": "原先目标", "meaning": "人工反思", "next_action": "下次复核", "owner": "人工指定张工", "deadline": "人工约定周五"}),
        ]
        for framework, purpose, headings, fields in cases:
            with self.subTest(framework=framework):
                main = self.main(render_composed_report(self.composed({"framework_id": framework, "purpose": purpose, "fields": fields})))
                positions = [main.index("### " + heading) for heading in headings]
                self.assertEqual(positions, sorted(positions))
                if framework == "wswnw":
                    self.assertIn("实际经过、结果与作者见下方本期事实记录", main[:positions[1]])
                    self.assertLess(positions[-1], main.index("人工指定张工"))
                    self.assertLess(positions[-1], main.index("人工约定周五"))

    def test_evidence_selection_rejects_unknown_and_manual_items_and_personal_scope_remains_filtered(self):
        with self.assertRaisesRegex(ValueError, "不属于本报告"):
            render_composed_report(self.composed({"evidence_item_ids": ["another-report-item"]}))
        manual = dict(self.base["items"][0], item_id="manual-one", origin="manual", refs=[])
        with self.assertRaisesRegex(ValueError, "人工新增"):
            render_composed_report(self.composed({"evidence_item_ids": ["manual-one"]}, self.base["items"] + [manual]))
        other = next(item for item in self.base["items"] if item["sender_id"] == "other")
        personal = self.build(audience="personal", sender_ids=["me"], composition={"template_id": "ppp"})
        self.assertEqual({item["sender_id"] for item in personal["items"]}, {"me"})
        personal["composition"]["config"]["evidence_item_ids"] = [other["item_id"]]
        with self.assertRaisesRegex(ValueError, "不属于本报告"):
            render_composed_report(personal)

    def test_excluded_rewritten_or_reclassified_support_warns_without_reviving_the_old_quote(self):
        original = self.base["items"][0]
        config = {"framework_id": "bluf", "fields": {"key_message": "人工结论仍须重新核对"}, "evidence_item_ids": [original["item_id"]]}
        mutations = [{"included": False}, {"text": "人工改写后的说法", "manual_text": True}, {"category": "后续计划"}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                items = deepcopy(self.base["items"])
                items[0].update(mutation)
                markdown = render_composed_report(self.composed(config, items), reviewed=True)
                main = self.main(markdown)
                support = main.split("## 所选核对材料", 1)[1].split("## 本期成果", 1)[0]
                self.assertIn("请重新核对上方人工陈述", support)
                self.assertNotIn(original["text"], support)
                if not items[0]["included"]:
                    self.assertNotIn(original["text"], main)
                # Original evidence remains available and is explicitly the source range.
                self.assertIn(original["text"], markdown.split("## 消息来源", 1)[1])
                self.assertIn("消息计数 6 条及证据仍为原稿引用范围", markdown)

    def test_returning_to_classic_does_not_resurrect_excluded_or_rewritten_legacy_body(self):
        for mutation in ({"included": False}, {"text": "人工更正后的事实", "manual_text": True}):
            with self.subTest(mutation=mutation):
                items = deepcopy(self.base["items"])
                items[0].update(mutation)
                report = self.composed(None, items)
                main = self.main(render_composed_report(report))
                self.assertNotIn(self.base["items"][0]["text"], main)
                if items[0]["included"]:
                    self.assertIn("人工更正后的事实", main)
                    self.assertIn("人工改写", main)

    def test_all_permitted_combinations_keep_excluded_items_out_of_body(self):
        items = deepcopy(self.base["items"])
        items[0]["included"] = False
        for combination in catalog()["combinations"]:
            config = {"template_id": combination["template_id"], "framework_id": combination["framework_id"], "purpose": combination["purposes"][0]}
            with self.subTest(config=config):
                report = self.composed(config, items)
                main = self.main(render_composed_report(report))
                self.assertNotIn(items[0]["text"], main)
                self.assertIn(items[1]["text"], main)
                self.assertIn("阻塞：测试环境无法访问", main)

    def test_review_uses_saved_structure_without_cutting_custom_titles_or_repeated_appendix(self):
        report = self.composed({"template_id": "ppp", "framework_id": "bluf", "fields": {"key_message": "人工要点"}})
        report["composition"]["layout"]["header"] = "# 已保存的自定义标题\n\n- 模板与方式：旧方式\n"
        report["composition"]["template"]["sections"][0]["label"] = "已保存的自定义进展标题"
        report["markdown"] = "完全无标题的旧展示内容，不应被切割或引用。"
        items = deepcopy(report["items"])
        items[0]["included"] = False
        output = render_review_report(report, items)
        self.assertIn("## 已保存的自定义进展标题", output)
        self.assertIn("# 已保存的自定义标题", output)
        self.assertNotIn(report["markdown"], output)
        self.assertEqual(output.count("## 统计与覆盖说明"), 1)
        self.assertEqual(output.count("## 消息来源"), 1)
        self.assertNotIn(items[0]["text"], self.main(output))
        self.assertIn("人工要点", output)

    def test_render_keeps_saved_definitions_even_if_the_current_catalog_changes(self):
        report = self.composed({"framework_id": "bluf", "fields": {"key_message": "人工观点"}})
        expected = render_composed_report(report)
        with patch("app.composition.catalog", side_effect=AssertionError("historical render must not resolve catalog again")):
            self.assertEqual(render_composed_report(report), expected)
        self.assertEqual(report["composition"]["layout"], self.base["composition"]["layout"])


if __name__ == "__main__":
    unittest.main()
